"""Hunter-specific verification, discovery, quota handling, and caching."""

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from .settings import parse_api_keys
from .storage import load_json_cache, save_json_cache


HUNTER_API_ROOT = "https://api.hunter.io/v2"
HUNTER_VERIFY_URL = f"{HUNTER_API_ROOT}/email-verifier"
HUNTER_FINDER_URL = f"{HUNTER_API_ROOT}/email-finder"
HUNTER_DOMAIN_SEARCH_URL = f"{HUNTER_API_ROOT}/domain-search"
HUNTER_ACCOUNT_URL = f"{HUNTER_API_ROOT}/account"
HUNTER_CACHE_VERSION = "verified-selection-v2"
HUNTER_CACHE_STABLE_DAYS = 30
HUNTER_CACHE_UNCERTAIN_DAYS = 7

_OPERATION_COST = {"verification": 0.5, "search": 1.0}


class HunterError(RuntimeError):
    """Raised when Hunter cannot safely complete an operation."""


class HunterRowError(HunterError):
    """A request-specific problem that must not stop the remaining leads."""

    def __init__(self, message, status="error"):
        super().__init__(message)
        self.status = status


def _domain(row):
    """Return a normalized company domain from a lead source URL."""
    source_url = (row.get("source_url") or "").strip()
    parsed = urlparse(source_url if "://" in source_url else f"https://{source_url}")
    return (parsed.hostname or "").lower().removeprefix("www.")


def _cache_key(row):
    """Build a versioned key so unsafe results from older logic are not reused."""
    identity = "|".join(
        (
            HUNTER_CACHE_VERSION,
            (row.get("email") or "").strip().lower(),
            (row.get("person_name") or "").strip().lower(),
            (row.get("role_target") or "").strip().lower(),
            _domain(row),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _cache_entry_is_fresh(value):
    """Reject malformed, stale, and indefinitely cached uncertain results."""
    if not isinstance(value, dict) or "hunter_status" not in value:
        return False
    try:
        checked = datetime.strptime(value.get("hunter_checked_at", ""), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False
    stable_statuses = {"valid", "invalid", "webmail", "disposable", "blocked"}
    max_age = (
        HUNTER_CACHE_STABLE_DAYS
        if str(value.get("hunter_status") or "").lower() in stable_statuses
        else HUNTER_CACHE_UNCERTAIN_DAYS
    )
    age = (datetime.now(timezone.utc).date() - checked).days
    return 0 <= age <= max_age


def _response_error(response):
    """Extract Hunter's safe error details without exposing credentials."""
    try:
        payload = response.json()
    except (AttributeError, TypeError, ValueError):
        return ""
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if isinstance(errors, dict):
        errors = [errors]
    if not isinstance(errors, list):
        return ""
    details = []
    for error in errors:
        if not isinstance(error, dict):
            continue
        detail = error.get("details") or error.get("id")
        if detail:
            details.append(str(detail))
    return "; ".join(details)


class HunterClient:
    """Small Hunter V2 client with ordered key fallback and usage awareness."""

    def __init__(self, api_key, timeout=20, max_retries=3, session=None):
        self.api_keys = parse_api_keys(api_key)
        self.active_key_index = 0
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.session = session or requests.Session()
        self.invalid_keys = set()
        self.rate_limited_operations = set()
        self.exhausted_operations = set()
        self.key_profiles = {}
        self.team_profiles = {}
        self.http_attempts = 0
        self._preflight_done = False

    @property
    def current_api_key(self):
        if not self.api_keys:
            return ""
        return self.api_keys[self.active_key_index]

    def _request_json(self, url, params, api_key):
        self.http_attempts += 1
        return self.session.get(
            url,
            params=params,
            headers={"X-API-KEY": api_key},
            timeout=self.timeout,
        )

    @staticmethod
    def _usage_profile(account_data):
        requests_data = account_data.get("requests") or {}
        profile = {"credits": None, "searches": None, "verifications": None}
        for name in profile:
            bucket = requests_data.get(name) or {}
            remaining = bucket.get("remaining")
            if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
                profile[name] = float(remaining)
        return profile

    def preflight(self):
        """Check each key and its free account-usage data before spending credits."""
        if self._preflight_done:
            return
        if not self.api_keys:
            raise HunterError("No Hunter API keys are configured")

        for index, api_key in enumerate(self.api_keys):
            try:
                response = self._request_json(HUNTER_ACCOUNT_URL, {}, api_key)
            except requests.RequestException:
                continue

            if response.status_code == 401:
                self.invalid_keys.add(index)
                continue
            if response.status_code != 200:
                continue
            try:
                payload = response.json()
            except (TypeError, ValueError):
                continue
            account_data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(account_data, dict):
                continue

            team_id = account_data.get("team_id")
            fallback = account_data.get("email") or index
            scope = f"team:{team_id}" if team_id is not None else f"account:{fallback}"
            profile = self.team_profiles.setdefault(scope, self._usage_profile(account_data))
            self.key_profiles[index] = profile

        self._preflight_done = True
        usable = len(self.api_keys) - len(self.invalid_keys)
        if usable == 0:
            raise HunterError("Hunter rejected all configured API keys")

        teams = len({id(profile) for profile in self.key_profiles.values()})
        if (
            len(self.api_keys) > 1
            and len(self.key_profiles) == usable
            and teams
            and teams < usable
        ):
            print(
                f"[Hunter] {usable} usable key(s) map to {teams} account/team quota(s); "
                "keys on the same account share credits."
            )
        else:
            print(f"[Hunter] Preflight found {usable} usable configured key(s).")

        unique_profiles = list(
            {id(profile): profile for profile in self.key_profiles.values()}.values()
        )
        unified = [profile["credits"] for profile in unique_profiles if profile["credits"] is not None]
        split_searches = [
            profile["searches"]
            for profile in unique_profiles
            if profile["credits"] is None and profile["searches"] is not None
        ]
        split_verifications = [
            profile["verifications"]
            for profile in unique_profiles
            if profile["credits"] is None and profile["verifications"] is not None
        ]
        quota_parts = []
        if unified:
            quota_parts.append(f"{sum(unified):g} unified credit(s)")
        if split_searches:
            quota_parts.append(f"{sum(split_searches):g} search credit(s)")
        if split_verifications:
            quota_parts.append(f"{sum(split_verifications):g} verification credit(s)")
        if quota_parts:
            print(f"[Hunter] Reported remaining: {', '.join(quota_parts)}.")

    def _remaining(self, index, operation):
        profile = self.key_profiles.get(index)
        if not profile:
            return None
        if profile.get("credits") is not None:
            return profile["credits"]
        bucket = "verifications" if operation == "verification" else "searches"
        return profile.get(bucket)

    def _has_quota(self, index, operation):
        remaining = self._remaining(index, operation)
        return remaining is None or remaining >= _OPERATION_COST[operation]

    @staticmethod
    def _actual_cost(operation, product, data):
        if operation == "verification":
            return _OPERATION_COST[operation]
        if product == "finder":
            return 1.0 if data.get("email") else 0.0
        if product == "domain_search":
            return float((len(data.get("emails") or []) + 9) // 10)
        return _OPERATION_COST[operation]

    def _debit_quota(self, index, operation, cost):
        profile = self.key_profiles.get(index)
        if not profile or cost <= 0:
            return
        bucket = "credits" if profile.get("credits") is not None else (
            "verifications" if operation == "verification" else "searches"
        )
        if profile.get(bucket) is not None:
            profile[bucket] = max(0.0, profile[bucket] - cost)

    def _exhaust_quota(self, index, operation):
        profile = self.key_profiles.get(index)
        if not profile:
            self.exhausted_operations.add((index, operation))
            return
        bucket = "credits" if profile.get("credits") is not None else (
            "verifications" if operation == "verification" else "searches"
        )
        profile[bucket] = 0.0

    def _candidate_indexes(self, operation):
        if not self.api_keys:
            return []
        # Priority is per operation. A key with no verification credits may
        # still have search credits on older split-quota Hunter plans.
        indexes = list(range(len(self.api_keys)))
        return [
            index
            for index in indexes
            if index not in self.invalid_keys
            and (index, operation) not in self.rate_limited_operations
            and (index, operation) not in self.exhausted_operations
            and self._has_quota(index, operation)
        ]

    def _select_key(self, operation):
        candidates = self._candidate_indexes(operation)
        if not candidates:
            label = "verification" if operation == "verification" else "search"
            if len(self.invalid_keys) == len(self.api_keys):
                raise HunterError("Hunter rejected all configured API keys")
            if self.rate_limited_operations and all(
                index in self.invalid_keys
                or (index, operation) in self.rate_limited_operations
                for index in range(len(self.api_keys))
            ):
                raise HunterError("All Hunter API keys are temporarily rate-limited")
            raise HunterError(
                f"All Hunter API keys/accounts have exhausted their {label} credits"
            )
        selected = candidates[0]
        if selected != self.active_key_index:
            self.active_key_index = selected
            print(f"[Hunter] Switching to API key #{selected + 1} of {len(self.api_keys)}")
        return selected

    def _get(self, url, params, operation, product):
        if not self.api_keys:
            raise HunterError("No Hunter API keys are configured")

        attempted_keys = set()
        while True:
            index = self._select_key(operation)
            if index in attempted_keys:
                raise HunterError("No unused Hunter API key can complete this request")
            attempted_keys.add(index)
            req_params = dict(params)

            for attempt in range(self.max_retries):
                try:
                    response = self._request_json(url, req_params, self.api_keys[index])
                except requests.RequestException as err:
                    if attempt + 1 == self.max_retries:
                        raise HunterError(f"Hunter network error: {err}") from err
                    time.sleep(min(4, 2**attempt))
                    continue

                status_code = response.status_code
                detail = _response_error(response)
                if status_code == 401:
                    self.invalid_keys.add(index)
                    break
                if status_code == 429:
                    self._exhaust_quota(index, operation)
                    break
                if status_code == 403:
                    if attempt + 1 < self.max_retries:
                        retry_after = getattr(response, "headers", {}).get("Retry-After")
                        try:
                            delay = float(retry_after) if retry_after is not None else 2**attempt
                        except (TypeError, ValueError):
                            delay = 2**attempt
                        time.sleep(max(0.0, min(delay, 10.0)))
                        continue
                    self.rate_limited_operations.add((index, operation))
                    break
                if status_code >= 500:
                    if attempt + 1 == self.max_retries:
                        suffix = f": {detail}" if detail else ""
                        raise HunterError(f"Hunter server error ({status_code}){suffix}")
                    time.sleep(min(4, 2**attempt))
                    continue
                if status_code == 202 and operation == "verification":
                    # Hunter asks clients to poll the same verification. Its
                    # backend counts these polls as one paid verification.
                    if attempt + 1 < self.max_retries:
                        time.sleep(min(4, 2**attempt))
                        continue
                if status_code >= 400:
                    suffix = f": {detail}" if detail else ""
                    if status_code == 451:
                        raise HunterRowError(
                            f"Hunter cannot process this personal data (451){suffix}",
                            status="blocked",
                        )
                    if status_code in (400, 422):
                        raise HunterRowError(
                            f"Hunter rejected this lead ({status_code}){suffix}"
                        )
                    raise HunterError(f"Hunter request failed ({status_code}){suffix}")

                try:
                    payload = response.json()
                except (TypeError, ValueError) as exc:
                    raise HunterError("Hunter returned invalid JSON") from exc
                if not isinstance(payload, dict):
                    raise HunterError("Hunter returned an invalid response object")
                data = payload.get("data")
                if data is not None and not isinstance(data, dict):
                    raise HunterError("Hunter returned an invalid data object")
                result = data or {}
                self._debit_quota(
                    index,
                    operation,
                    self._actual_cost(operation, product, result),
                )
                return result

            continue

    def verify(self, email):
        return self._get(
            HUNTER_VERIFY_URL,
            {"email": email},
            "verification",
            "verifier",
        )

    def find(self, full_name, domain):
        return self._get(
            HUNTER_FINDER_URL,
            {"full_name": full_name, "domain": domain},
            "search",
            "finder",
        )

    def domain_search(self, domain, type="personal", decision_maker=True):
        params = {"domain": domain, "type": type, "limit": 10}
        if decision_maker is not None:
            params["decision_maker"] = str(bool(decision_maker)).lower()
        return self._get(HUNTER_DOMAIN_SEARCH_URL, params, "search", "domain_search")


ROLE_ALIASES = {
    "ceo": (
        "ceo",
        "chief executive officer",
        "plan president",
        "president and chief executive officer",
        "president & ceo",
    ),
    "founder": ("founder", "co-founder", "cofounder", "owner"),
    "clinic director": (
        "clinic director",
        "clinical director",
        "medical director",
        "director of clinical services",
        "director of clinic operations",
    ),
    "director": ("director", "executive director", "managing director", "senior director"),
    "decision maker": (
        "chief executive officer",
        "ceo",
        "president",
        "founder",
        "owner",
        "managing director",
        "executive director",
    ),
}


def _normalize_role(value):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def _role_terms(target):
    target = _normalize_role(target or "")
    if not target:
        return ()
    terms = {target}
    for key, aliases in ROLE_ALIASES.items():
        normalized_aliases = {_normalize_role(alias) for alias in aliases}
        if key == "decision maker":
            matches = key == target
        else:
            matches = key == target or target in normalized_aliases or (
                key != "director" and re.search(rf"\b{re.escape(key)}\b", target)
            )
        if matches:
            terms.update(normalized_aliases)
    return tuple(terms)


def role_match_score(position, target):
    """Return 0-100 role similarity without accepting unrelated directors."""
    position = _normalize_role(position or "")
    terms = _role_terms(target)
    if not position or not terms:
        return 0
    if position in terms:
        return 100
    if any(term in position or position in term for term in terms):
        return 85
    target_words = {word for word in _normalize_role(target or "").split() if len(word) > 3}
    overlap = target_words & set(position.split())
    return min(70, 25 * len(overlap)) if overlap else 0


def _candidate_status(item):
    verification = item.get("verification") or {}
    return str(verification.get("status") or item.get("status") or "").lower()


def choose_domain_candidate(items, target_role):
    """Choose a role-relevant person; never silently use an unrelated result."""
    candidates = [item for item in items if isinstance(item, dict) and item.get("value")]
    ranked = sorted(
        candidates,
        key=lambda item: (
            role_match_score(item.get("position"), target_role),
            _candidate_status(item) == "valid",
            bool(item.get("decision_maker")),
            item.get("confidence") or 0,
        ),
        reverse=True,
    )
    if not ranked:
        return None
    best = ranked[0]
    if target_role and role_match_score(best.get("position"), target_role) < 50:
        return None
    return best


def _result(email="", status="", score="", sources=None, action=""):
    return {
        "hunter_email": (email or "").strip().lower(),
        "hunter_status": str(status or "").strip().lower(),
        "hunter_score": score if score is not None else "",
        "hunter_sources": json.dumps(sources or [], ensure_ascii=False),
        "hunter_checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "hunter_action": action,
    }


def choose_selected_email(row):
    """Choose only an address explicitly marked valid by Hunter."""
    if str(row.get("hunter_status") or "").lower() == "valid" and row.get("hunter_email"):
        row.update(
            {
                "selected_email": row["hunter_email"],
                "selected_email_status": "valid",
                "selected_email_provider": "hunter",
            }
        )
    else:
        row.update(
            {
                "selected_email": "",
                "selected_email_status": row.get("hunter_status", ""),
                "selected_email_provider": "",
            }
        )


def _apply_person_context(row, result):
    if not row.get("person_name") and result.get("hunter_person_name"):
        row["person_name"] = result["hunter_person_name"]
    if not row.get("role_title") and result.get("hunter_role_title"):
        row["role_title"] = result["hunter_role_title"]


def enrich_rows(rows, api_key, cache_path="output/hunter_cache.json", max_requests=50, client=None):
    """Verify and enrich candidate emails while enforcing verification invariants."""
    cache = load_json_cache(cache_path)
    client = client or HunterClient(api_key)
    stats = {
        "attempted": 0,
        "requested": 0,
        "http_attempts": 0,
        "cached": 0,
        "valid": 0,
        "skipped": 0,
        "failed": 0,
        "stopped": "",
    }
    row_errors = []

    uncached = [
        row for row in rows if not _cache_entry_is_fresh(cache.get(_cache_key(row)))
    ]
    preflight = getattr(client, "preflight", None)
    if uncached and max_requests > 0 and callable(preflight):
        try:
            preflight()
        except RuntimeError as exc:
            stats["stopped"] = str(exc)
            stats["skipped"] = len(uncached)
            for row in rows:
                choose_selected_email(row)
            stats["http_attempts"] = getattr(client, "http_attempts", 0)
            return stats

    def call(method, *args):
        if stats["attempted"] >= max_requests:
            raise HunterError("Hunter per-run request budget reached")
        stats["attempted"] += 1
        try:
            value = method(*args)
        except HunterRowError as exc:
            stats["failed"] += 1
            row_errors.append(exc)
            return {}
        except RuntimeError:
            stats["failed"] += 1
            raise
        stats["requested"] += 1
        return value

    for index, row in enumerate(rows):
        key = _cache_key(row)
        if _cache_entry_is_fresh(cache.get(key)):
            result = cache[key]
            row.update(result)
            _apply_person_context(row, result)
            stats["cached"] += 1
            if str(row.get("hunter_status") or "").lower() == "valid":
                stats["valid"] += 1
            choose_selected_email(row)
            continue

        cache.pop(key, None)

        if stats["attempted"] >= max_requests:
            stats["stopped"] = "Hunter per-run request budget reached"
            stats["skipped"] += len(rows) - index
            break

        candidate_email = (row.get("email") or "").strip().lower()
        email_type = (row.get("email_type") or "").lower()
        is_personal = bool(candidate_email) and email_type not in ("generic", "domain_only")
        domain = _domain(row)
        result = _result(action="domain_search" if not is_personal else "verify")
        row_errors.clear()

        try:
            if is_personal:
                verified = call(client.verify, candidate_email)
                result = _result(
                    email=verified.get("email") or candidate_email,
                    status=verified.get("status"),
                    score=verified.get("score"),
                    sources=verified.get("sources"),
                    action="verify",
                )

            can_find = (
                is_personal
                and result["hunter_status"] != "valid"
                and not any(error.status == "blocked" for error in row_errors)
                and len((row.get("person_name") or "").split()) >= 2
                and bool(domain)
                and stats["attempted"] < max_requests
            )
            if can_find:
                found = call(client.find, row["person_name"], domain)
                found_email = (found.get("email") or "").strip().lower()
                if found_email:
                    verification = found.get("verification") or {}
                    status = verification.get("status") or found.get("status") or ""
                    score = verification.get("score") or found.get("score")
                    if not status and stats["attempted"] < max_requests:
                        checked = call(client.verify, found_email)
                        status = checked.get("status") or ""
                        score = checked.get("score") or score
                    result = _result(
                        email=found_email,
                        status=status,
                        score=score,
                        sources=found.get("sources"),
                        action="find",
                    )

            if (
                result["hunter_status"] != "valid"
                and not any(error.status == "blocked" for error in row_errors)
                and domain
                and stats["attempted"] < max_requests
            ):
                domain_result = call(client.domain_search, domain)
                target_role = row.get("role_target") or row.get("role_title") or ""
                matched = choose_domain_candidate(domain_result.get("emails") or [], target_role)
                if matched:
                    person_name = (
                        f"{matched.get('first_name', '')} {matched.get('last_name', '')}".strip()
                    )
                    verification = matched.get("verification") or {}
                    status = verification.get("status") or matched.get("status") or ""
                    score = verification.get("score") or matched.get("confidence")
                    if not status and stats["attempted"] < max_requests:
                        checked = call(client.verify, matched["value"])
                        status = checked.get("status") or ""
                        score = checked.get("score") or score
                    result = _result(
                        email=matched["value"],
                        status=status,
                        score=score,
                        sources=matched.get("sources"),
                        action="domain_search",
                    )
                    result["hunter_person_name"] = person_name
                    result["hunter_role_title"] = matched.get("position") or ""
        except RuntimeError as exc:
            stats["stopped"] = str(exc)
            stats["skipped"] += len(rows) - index - 1
            break

        if any(error.status == "blocked" for error in row_errors):
            result["hunter_status"] = "blocked"
        elif row_errors and not result["hunter_status"]:
            result["hunter_status"] = "error"

        cache[key] = result
        row.update(result)
        _apply_person_context(row, result)
        choose_selected_email(row)
        if result["hunter_status"] == "valid":
            stats["valid"] += 1
        save_json_cache(cache_path, cache)

    for row in rows:
        if "selected_email" not in row:
            choose_selected_email(row)
    save_json_cache(cache_path, cache)
    stats["http_attempts"] = getattr(client, "http_attempts", stats["attempted"])
    return stats

"""Phase 1: bounded query construction and explicit-backend DDGS discovery."""

import random
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from rapidfuzz import fuzz

from .settings import LOCATION_POOLS, SKIP_DOMAINS


NON_HTML_EXTENSIONS = {
    ".7z", ".avi", ".css", ".csv", ".doc", ".docx", ".gif", ".gz",
    ".ico", ".jpeg", ".jpg", ".js", ".json", ".m4a", ".mov", ".mp3",
    ".mp4", ".ods", ".odt", ".pdf", ".png", ".ppt", ".pptx", ".rar",
    ".rss", ".svg", ".tar", ".tgz", ".tif", ".tiff", ".txt", ".wav",
    ".webm", ".webp", ".xls", ".xlsx", ".xml", ".zip",
}

@dataclass(slots=True)
class SearchOutcome:
    """One search result with failure kept distinct from a valid empty result."""

    urls: list[str]
    status: str
    backend: str = ""
    attempts: int = 0
    error: str = ""
    attempted_backends: tuple[str, ...] = ()


class DDGSSearchClient:
    """Reuse one DDGS instance and contact at most two explicit backends."""

    def __init__(
        self,
        *,
        backends=("duckduckgo", "brave"),
        timeout=5,
        retry_delay=1.0,
        ddgs_factory=None,
    ):
        cleaned = [str(item).strip() for item in backends if str(item).strip()]
        self.backends = list(dict.fromkeys(cleaned))[:2]
        self.timeout = timeout
        self.retry_delay = retry_delay
        self.ddgs_factory = ddgs_factory
        self._context = None
        self._ddgs = None
        self._preferred_backend = self.backends[0] if self.backends else ""

    def __enter__(self):
        factory = self.ddgs_factory or _import_ddgs()
        self._context = factory(timeout=self.timeout)
        self._ddgs = self._context.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._context is not None:
            return self._context.__exit__(exc_type, exc_value, traceback)
        return False

    def search(self, query, max_results=30):
        """Search at most two backends, falling back on errors or empty results."""
        if self._ddgs is None:
            raise RuntimeError("DDGSSearchClient must be used as a context manager")
        if not self.backends:
            return SearchOutcome([], "failed", error="No DDGS backend configured")

        ordered_backends = [self._preferred_backend]
        ordered_backends.extend(
            backend for backend in self.backends if backend != self._preferred_backend
        )
        last_error = ""
        saw_empty = False
        attempted_backends = []
        for attempt, backend in enumerate(ordered_backends, 1):
            attempted_backends.append(backend)
            try:
                results = self._ddgs.text(
                    query,
                    max_results=max_results,
                    backend=backend,
                )
            except Exception as exc:
                last_error = str(exc) or exc.__class__.__name__
                if "no results found" in last_error.lower():
                    saw_empty = True
                elif attempt < len(ordered_backends):
                    wait = self.retry_delay + random.uniform(0, 0.5)
                    next_backend = ordered_backends[attempt]
                    print(
                        f"  [!] {backend} search failed ({last_error}). "
                        f"Trying {next_backend} in {wait:.1f}s ...",
                        file=sys.stderr,
                    )
                    time.sleep(wait)
                    continue
                else:
                    print(
                        f"  [!] Search failed after {attempt} attempt(s): {last_error}",
                        file=sys.stderr,
                    )
            else:
                urls = []
                for result in results:
                    href = result.get("href") or result.get("link")
                    if href and is_search_result_usable(href):
                        urls.append(href)
                urls = list(dict.fromkeys(urls))
                if urls:
                    if backend != self._preferred_backend:
                        print(
                            f"  [search] {backend} produced usable results; "
                            "using it first for later searches."
                        )
                    self._preferred_backend = backend
                    return SearchOutcome(
                        urls,
                        "success",
                        backend=backend,
                        attempts=attempt,
                        attempted_backends=tuple(attempted_backends),
                    )
                saw_empty = True

            if attempt < len(ordered_backends):
                next_backend = ordered_backends[attempt]
                print(
                    f"  [search] {backend} returned no usable results. "
                    f"Trying {next_backend} ..."
                )

        status = "empty" if saw_empty else "failed"
        return SearchOutcome(
            [],
            status,
            backend=attempted_backends[-1],
            attempts=len(attempted_backends),
            error=last_error,
            attempted_backends=tuple(attempted_backends),
        )


def _import_ddgs():
    """Import DDGS from the maintained package, with legacy compatibility."""
    try:
        from ddgs import DDGS
        return DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
            return DDGS
        except ImportError:
            print(
                "ERROR: The 'ddgs' package is required for search-based discovery.\n"
                "Install all dependencies with: pip install -r requirements.txt",
                file=sys.stderr,
            )
            sys.exit(1)


def duckduckgo_search(query, max_results=30, client=None, **client_options):
    """Run one bounded DDGS search and preserve success/empty/failure status."""
    if client is not None:
        return client.search(query, max_results=max_results)
    with DDGSSearchClient(**client_options) as owned_client:
        return owned_client.search(query, max_results=max_results)


def is_domain_blocked(url):
    """Check if a URL's domain (or any parent domain) is in SKIP_DOMAINS."""
    domain = urlparse(url).netloc.lower().removeprefix("www.")
    # Check the domain itself
    if domain in SKIP_DOMAINS:
        return True
    # Check parent domains (e.g. 'news4jax.com' matches 'sports.news4jax.com')
    parts = domain.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[i:])
        if parent in SKIP_DOMAINS:
            return True
    return False


def normalized_domain(url):
    """Normalize equivalent www/non-www website hosts for global deduplication."""
    value = str(url or "").strip()
    parsed = urlparse(value if "://" in value else f"//{value}")
    host = (parsed.hostname or "").lower().rstrip(".")
    return host.removeprefix("www.")


def is_search_result_usable(url):
    """Keep crawlable web pages and reject blocked domains or obvious files."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not normalized_domain(url):
        return False
    if is_domain_blocked(url):
        return False
    path = parsed.path.lower().rstrip("/")
    return not any(path.endswith(extension) for extension in NON_HTML_EXTENSIONS)


def rank_candidate_websites(urls, role="", industry="", country=""):
    """Rank candidate URLs semantically while preserving every result."""
    industry_text = (
        " ".join(str(item) for item in industry)
        if isinstance(industry, (list, tuple))
        else str(industry or "")
    )
    target = " ".join(part for part in (role, industry_text, country) if part).lower()

    def score(item):
        index, url = item
        parsed = urlparse(url)
        url_text = f"{parsed.netloc} {parsed.path}".replace("-", " ").replace("_", " ")
        relevance = fuzz.token_set_ratio(target, url_text.lower()) if target else 0
        page_bonus = max(
            (
                fuzz.partial_ratio(term, url_text.lower())
                for term in ("team", "leadership", "staff", "providers", "contact")
            ),
            default=0,
        )
        return relevance + page_bonus, -index

    return [url for _, url in sorted(enumerate(urls), key=score, reverse=True)]


def get_daily_locations(country, locations_per_day=3):
    """
    Pick a rotating subset of sub-locations for a country based on today's date.

    Uses the day-of-year to deterministically rotate through the pool,
    ensuring different locations each day. Cycles back after exhausting all
    locations (e.g. 50 US states ÷ 3 per day ≈ 17 days before repeating).

    Parameters
    ----------
    country : str
        The country name (must match a key in LOCATION_POOLS).
    locations_per_day : int
        How many sub-locations to use per country per day.

    Returns
    -------
    list[str]
        The selected locations for today, or [country] if no pool exists.
    """
    pool = LOCATION_POOLS.get(country, [])
    if not pool:
        return [country]  # No sub-locations available, use the country itself

    day_of_year = datetime.now(timezone.utc).timetuple().tm_yday
    total = len(pool)

    # Calculate the starting index for today
    start_idx = (day_of_year * locations_per_day) % total

    # Pick locations_per_day locations, wrapping around if needed
    selected = []
    for i in range(min(locations_per_day, total)):
        idx = (start_idx + i) % total
        selected.append(pool[idx])

    return selected


def build_queries(roles, industries, countries, locations_per_day=3):
    """Build one simple website-discovery query per role and location."""
    queries = []
    rotation_info = {}
    industry_clause = " OR ".join(str(industry).strip() for industry in industries)

    for role in roles:
        for country in countries:
            locations = get_daily_locations(country, locations_per_day)
            rotation_info[country] = locations
            for location in locations:
                query = f"{role} ({industry_clause}) {location}"
                queries.append((query, role, tuple(industries), country, location))

    return queries, rotation_info


def build_adaptive_queries(underperforming, limit):
    """Create balanced, narrower industry searches for weak base queries."""
    if limit <= 0:
        return []
    ordered = sorted(underperforming, key=lambda item: item[0])
    candidates = []
    max_industries = max((len(item[1][2]) for item in ordered), default=0)

    for round_index in range(max_industries):
        for base_index, (_, base) in enumerate(ordered):
            _, role, industries, country, location = base
            if not industries:
                continue
            industry = industries[(base_index + round_index) % len(industries)]
            query = f"{role} {industry} {location}"
            candidates.append((query, role, (industry,), country, location))
            if len(candidates) >= limit:
                return candidates
    return candidates

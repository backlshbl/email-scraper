"""Phase 3: email extraction, context detection, classification, and filtering."""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import tldextract
from bs4 import BeautifulSoup
from email_validator import EmailNotValidError, caching_resolver, validate_email
from rapidfuzz import fuzz

from .settings import (
    COUNTRY_SUFFIXES,
    EMAIL_BLOCKLIST_PATTERNS,
    EMAIL_DOMAIN_BLOCKLIST,
    EMAIL_REGEX,
    GENERIC_EMAIL_PREFIXES,
    INDUSTRY_TERMS,
    KNOWN_COUNTRY_SUFFIXES,
    OBFUSCATED_EMAIL_REGEXES,
    _NAME_PATTERN,
    _NON_PERSON_NAME_WORDS,
    _ROLE_PHRASE_PATTERN,
    _ROLE_TITLE_PATTERNS,
)


_TLD_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())


def role_match_score(text, role_filters):
    """Return a 0-100 fuzzy match score against the requested roles."""
    text = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    if not text or not role_filters:
        return 0
    scores = []
    for role in role_filters:
        normalized_role = re.sub(r"[^a-z0-9]+", " ", role.lower()).strip()
        if not normalized_role:
            continue
        if normalized_role in text:
            return 100
        scores.append(fuzz.token_set_ratio(normalized_role, text))
    return round(max(scores, default=0))


def validate_lead_rows(rows, check_mx=True, timeout=3.0, max_workers=8):
    """Normalize emails and remove syntactically or DNS-invalid addresses.

    Empty placeholder rows are preserved for provider-based domain enrichment.
    One representative address per domain is checked, and unrelated domains
    are resolved concurrently.
    """
    accepted = []
    prepared = []
    stats = {"normalized": 0, "invalid_syntax": 0, "invalid_domain": 0}

    for row in rows:
        email = (row.get("email") or "").strip()
        if not email:
            prepared.append((row, "", ""))
            continue
        try:
            syntax_result = validate_email(email, check_deliverability=False)
        except EmailNotValidError:
            stats["invalid_syntax"] += 1
            continue

        normalized = syntax_result.normalized
        prepared.append((row, normalized, normalized.rsplit("@", 1)[-1].lower()))

    invalid_domains = set()
    if check_mx:
        representatives = {}
        for _row, normalized, domain in prepared:
            if normalized:
                representatives.setdefault(domain, normalized)

        def domain_is_deliverable(email):
            try:
                validate_email(
                    email,
                    check_deliverability=True,
                    dns_resolver=caching_resolver(timeout=timeout),
                )
            except EmailNotValidError:
                return False
            return True

        if representatives:
            workers = max(1, min(int(max_workers), len(representatives)))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(domain_is_deliverable, email): domain
                    for domain, email in representatives.items()
                }
                for future in as_completed(futures):
                    if not future.result():
                        invalid_domains.add(futures[future])

    for row, normalized, domain in prepared:
        if not normalized:
            accepted.append(row)
            continue
        if domain in invalid_domains:
            stats["invalid_domain"] += 1
            continue
        email = (row.get("email") or "").strip()
        if normalized != email:
            stats["normalized"] += 1
        row["email"] = normalized
        accepted.append(row)

    return accepted, stats


def registrable_domain(value):
    """Return the Public-Suffix-List-aware registrable domain for a URL/domain."""
    host = urlparse(value).hostname if "://" in value else value
    host = (host or "").strip().lower().rstrip(".")
    extracted = _TLD_EXTRACT(host)
    return extracted.top_domain_under_public_suffix or host


def decode_cloudflare_email(cf_hex):
    """Decode Cloudflare XOR-encrypted email protection hex strings."""
    try:
        r = int(cf_hex[:2], 16)
        email = "".join([chr(int(cf_hex[i:i+2], 16) ^ r) for i in range(2, len(cf_hex), 2)])
        return email.strip()
    except Exception:
        return ""


def filter_emails_by_site_domain(emails, site_url):
    """
    Keep only emails whose domain is related to the site being crawled.

    For example, when crawling 'example-clinic.com', keep
    'info@example-clinic.com' but discard 'reporter@nytimes.com'.

    Matching logic:
    - Email domain matches site domain exactly (ignoring www.)
    - Email domain shares the same root (e.g. site=childrens.com matches email=x@Childrens.com)
    - Emails from generic providers (gmail, yahoo) are dropped
    """
    site_root = registrable_domain(site_url)

    filtered = set()
    for email in emails:
        email_domain = email.split("@")[-1].lower()

        # Block generic email providers
        if email_domain in EMAIL_DOMAIN_BLOCKLIST:
            continue

        email_root = registrable_domain(email_domain)

        # Keep if email domain is related to the crawled site
        if email_root == site_root:
            filtered.add(email)

    return filtered


def classify_email(email):
    """
    Classify an email as 'personal' or 'generic'.

    Returns 'personal' for emails that look like a person's name
    (e.g. jane.smith@..., jsmith@...), 'generic' for role-based
    addresses (e.g. info@..., contact@..., support@...).
    """
    prefix = email.split("@")[0].lower().strip()
    # Remove common separators to get the base prefix
    clean_prefix = prefix.replace(".", "").replace("-", "").replace("_", "")

    # Check against known generic prefixes
    if prefix in GENERIC_EMAIL_PREFIXES or clean_prefix in GENERIC_EMAIL_PREFIXES:
        return "generic"

    # Prefixes with dots/hyphens/underscores are usually personal, after the
    # normalized generic-prefix check above (e.g. physician.relations).
    # (e.g. "jane.smith", "j-doe", "john_doe")
    if any(sep in prefix for sep in [".", "-", "_"]):
        return "personal"

    # Unknown single words are conservatively generic. They can be promoted
    # later only when reliable nearby person context exists.
    return "generic"


def is_site_relevant(url, html, industry="", country=""):
    """Conservative post-search country/industry validation."""
    host = (urlparse(url).hostname or "").lower()
    suffix = host.rsplit(".", 1)[-1] if "." in host else ""
    allowed_suffixes = COUNTRY_SUFFIXES.get(country, set())
    if suffix in KNOWN_COUNTRY_SUFFIXES and allowed_suffixes and suffix not in allowed_suffixes:
        return False, f"country-domain mismatch (.{suffix} vs {country})"

    targets = industry if isinstance(industry, (list, tuple)) else [industry]
    known_targets = [
        (str(target).strip(), INDUSTRY_TERMS.get(str(target).strip().lower()))
        for target in targets
        if str(target).strip()
    ]
    known_targets = [(target, terms) for target, terms in known_targets if terms]
    if known_targets:
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True).lower()
        strong = {"hospital", "clinic", "medical center", "health system",
                  "medical practice", "healthcare", "health care"}
        matched_industry = False
        for _target, terms in known_targets:
            matched = {term for term in terms if term in text}
            if (matched & strong) or len(matched) >= 2:
                matched_industry = True
                break
        if not matched_industry:
            label = " | ".join(target for target, _terms in known_targets)
            return False, f"industry mismatch ({label})"
    return True, ""


def _extract_context_from_container(email, soup):
    """
    Look at the HTML container (div, li, section, tr, article) around an
    email to find the person's name and role title.

    Returns (person_name, role_title) or ("", "").
    """
    # Find the element containing the email text
    email_element = None

    # First try mailto links
    for a in soup.find_all("a", href=True):
        if email.lower() in a["href"].lower():
            email_element = a
            break

    # Fall back to finding the email in text nodes
    if email_element is None:
        for elem in soup.find_all(string=re.compile(re.escape(email), re.IGNORECASE)):
            email_element = elem.parent
            break

    if email_element is None:
        return "", ""

    # Walk up the DOM to find a meaningful container
    container_tags = {"div", "li", "section", "tr", "article", "td",
                      "aside", "figure", "blockquote", "card"}
    container = email_element
    for _ in range(8):  # walk up max 8 levels
        parent = container.parent
        if parent is None:
            break
        if parent.name in container_tags:
            container = parent
            break
        container = parent

    # Get text from the container
    container_text = container.get_text(" ", strip=True)

    # Extract role title
    role_title = ""
    role_match = _ROLE_PHRASE_PATTERN.search(container_text)
    if role_match:
        # Return only a recognized title phrase. Nearby prose previously
        # produced fragments such as "with ... executive leadership".
        role_title = re.sub(r"\s+", " ", role_match.group()).strip()

    # Extract person name
    person_name = ""
    # Honorific prefixes that should not disqualify a name candidate
    _honorifics_re = re.compile(r"^(?:Dr\.?\s*|Prof\.?\s*|Mr\.?\s*|Mrs\.?\s*|Ms\.?\s*)",
                                re.IGNORECASE)

    def _is_valid_name(candidate):
        """Check if a candidate string is a person name (not a role/company)."""
        # Strip honorific prefix before checking for role keywords
        stripped = _honorifics_re.sub("", candidate).strip()
        if _ROLE_TITLE_PATTERNS.search(stripped):
            return False
        if re.search(r"\b(?:Inc|LLC|Ltd|Corp|Hospital|Clinic|Center|Health)\b",
                     candidate, re.IGNORECASE):
            return False
        words = {word.lower().strip(".,") for word in stripped.split()}
        if words & _NON_PERSON_NAME_WORDS:
            return False
        return True

    # Look in headings (h2, h3, h4) and strong/b tags within the container
    for tag in container.find_all(["h2", "h3", "h4", "h5", "strong", "b", "span"]):
        tag_text = tag.get_text(" ", strip=True)
        if tag_text and len(tag_text) < 60:
            name_match = _NAME_PATTERN.search(tag_text)
            if name_match:
                candidate = name_match.group().strip()
                if _is_valid_name(candidate):
                    person_name = candidate
                    break

    # If no name found in structured tags, try the full container text
    if not person_name:
        for name_match in _NAME_PATTERN.finditer(container_text):
            candidate = name_match.group().strip()
            if _is_valid_name(candidate):
                person_name = candidate
                break

    return person_name, role_title


def extract_emails_with_context(html, role_filters=None):
    """
    Extract emails from HTML with person name, role, and email type.

    Returns a list of dicts:
        [{"email": ..., "person_name": ..., "role_title": ..., "email_type": ...}, ...]

    If role_filters is provided, only return emails whose surrounding
    context or prefix matches one of the specified roles (but always
    include personal emails found on team/leadership pages).
    """
    soup = BeautifulSoup(html, "html.parser")

    # --- Collect all candidate emails ---
    candidates = set()

    # mailto: links
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().startswith("mailto:"):
            addr = href.split("mailto:")[1].split("?")[0].strip()
            if addr:
                candidates.add(addr)
        elif "email-protection#" in href.lower():
            hex_part = href.rsplit("email-protection#", 1)[-1].split("?")[0].strip()
            decoded = decode_cloudflare_email(hex_part)
            if decoded and EMAIL_REGEX.match(decoded):
                candidates.add(decoded)

    # Cloudflare data-cfemail attributes
    for elem in soup.find_all(attrs={"data-cfemail": True}):
        decoded = decode_cloudflare_email(elem["data-cfemail"])
        if decoded and EMAIL_REGEX.match(decoded):
            candidates.add(decoded)

    # plain text regex match
    page_text = soup.get_text(" ")
    for match in EMAIL_REGEX.findall(page_text):
        candidates.add(match)

    # obfuscated text regex match (e.g. name [at] domain [dot] com)
    for pattern in OBFUSCATED_EMAIL_REGEXES:
        for match in pattern.findall(page_text):
            candidates.add(f"{match[0]}@{match[1]}.{match[2]}")

    # filter junk
    clean = set()
    for e in candidates:
        e_lower = e.lower()
        if any(bad in e_lower for bad in EMAIL_BLOCKLIST_PATTERNS):
            continue
        clean.add(e)

    # --- Build enriched results ---
    results = []

    # Prepare role-filter sets if provided
    role_phrases = set()
    role_words = set()
    if role_filters:
        role_phrases = {r.lower() for r in role_filters}
        for role in role_filters:
            for word in role.lower().split():
                if len(word) >= 4:
                    role_words.add(word)

    page_text_lower = page_text.lower()

    for email in clean:
        email_type = classify_email(email)
        person_name, role_title = _extract_context_from_container(email, soup)
        prefix = email.split("@", 1)[0].lower()
        clean_prefix = re.sub(r"[._-]", "", prefix)
        if (email_type == "generic" and person_name
                and prefix not in GENERIC_EMAIL_PREFIXES
                and clean_prefix not in GENERIC_EMAIL_PREFIXES):
            email_type = "personal"

        # If role_filters are set, decide whether to keep this email
        if role_filters:
            # A published, same-domain personal address is still a useful lead
            # even when the page layout prevents reliable role proximity
            # matching. Exact role matches remain useful metadata/ranking
            # signals, but should not turn this extraction filter into an
            # all-or-nothing gate.
            keep = email_type == "personal"

            # Check email prefix for role keywords
            if any(kw in prefix for kw in role_words):
                keep = True

            # Check extracted role title
            if role_title:
                role_title_lower = role_title.lower()
                if any(phrase in role_title_lower for phrase in role_phrases):
                    keep = True
                elif any(word in role_title_lower for word in role_words):
                    keep = True
                elif role_match_score(role_title, role_filters) >= 82:
                    keep = True

            # Check surrounding page context (±300 chars)
            if not keep:
                email_pos = page_text_lower.find(email.lower())
                if email_pos != -1:
                    ctx_start = max(0, email_pos - 300)
                    ctx_end = min(len(page_text_lower), email_pos + len(email) + 300)
                    context = page_text_lower[ctx_start:ctx_end]
                    # Generic mailboxes need the complete requested role in
                    # nearby prose. Matching a single word is unsafe here:
                    # industry words such as "clinic" occur across the whole
                    # site and previously admitted info@/hello@/support@.
                    if any(phrase in context for phrase in role_phrases):
                        keep = True
                    elif (email_type == "personal"
                          and any(word in context for word in role_words)):
                        keep = True
                    elif role_match_score(context, role_filters) >= 90:
                        keep = True

            if not keep:
                continue

        results.append({
            "email": email,
            "person_name": person_name,
            "role_title": role_title,
            "email_type": email_type,
        })

    # Sort: personal emails first, then generic
    results.sort(key=lambda r: (0 if r["email_type"] == "personal" else 1, r["email"]))

    return results


def extract_emails(html, role_filters=None):
    """
    Extract emails from HTML page content (backward-compatible wrapper).

    Returns a set of email strings (no context). Used by deep_crawl_site.
    """
    results = extract_emails_with_context(html, role_filters=role_filters)
    return {r["email"] for r in results}

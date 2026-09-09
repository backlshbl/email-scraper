"""Phase 2: robots-aware page fetching and website crawling."""

import asyncio
import hashlib
import re
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from .extraction import (
    extract_emails,
    extract_emails_with_context,
    filter_emails_by_site_domain,
    is_site_relevant,
)
from .settings import CONTACT_PAGE_HINTS, HEADERS, TEAM_PAGE_PROBES
from .storage import CrawlCache


def can_fetch(url, user_agent=HEADERS["User-Agent"]):
    """Check robots.txt before crawling a URL."""
    try:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(robots_url)
        rp.read()
        return rp.can_fetch(user_agent, url)
    except Exception:
        # If robots.txt can't be read, default to allowing (most sites have none)
        return True


def fetch_page(url, timeout=10):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException:
        return None


def find_contact_pages(base_url, html):
    """Look for links that look like contact/about/team pages."""
    soup = BeautifulSoup(html, "html.parser")
    candidates = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = (a.get_text() or "").lower()
        combined = f"{href.lower()} {text}"
        if any(hint in combined for hint in CONTACT_PAGE_HINTS):
            full_url = urljoin(base_url, href)
            if urlparse(full_url).netloc == urlparse(base_url).netloc:
                candidates.add(full_url)
    return list(candidates)[:15]  # increased cap for better coverage


def probe_team_pages(base_url, crawl_delay=1.5, respect_robots=True):
    """
    Brute-force probe common team/leadership page URLs.

    Tries paths like /team, /our-team, /leadership, /providers etc.
    even if they aren't linked from the homepage. Returns a list of
    (url, html) tuples for pages that returned a 200 OK.
    """
    parsed = urlparse(base_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    found_pages = []
    probed = set()

    for path in TEAM_PAGE_PROBES:
        probe_url = base + path
        if probe_url in probed:
            continue
        probed.add(probe_url)

        if respect_robots and not can_fetch(probe_url):
            continue

        time.sleep(crawl_delay * 0.5)  # faster probing (just checking if page exists)
        try:
            resp = requests.get(probe_url, headers=HEADERS, timeout=8,
                                allow_redirects=True)
            # Only count as found if it's a 200 and has substantial content
            # (not a redirect to homepage or a 404 page)
            if resp.status_code == 200 and len(resp.text) > 1000:
                # Check it didn't just redirect back to homepage
                final_url = resp.url.rstrip("/")
                base_clean = base.rstrip("/")
                if final_url != base_clean and final_url != base_clean + "/":
                    found_pages.append((probe_url, resp.text))
                    print(f"    [probe] Found team page: {path}")
        except requests.RequestException:
            continue

    return found_pages


def crawl_site(base_url, crawl_delay=1.5, respect_robots=True, role_filters=None,
               filter_by_domain=True, industry_target="", country_target=""):
    """
    Crawl homepage + contact/team pages + brute-force probes.

    Returns a list of dicts with email, person_name, role_title, email_type.
    """
    all_results = []
    seen_emails = set()

    if respect_robots and not can_fetch(base_url):
        print(f"  [robots.txt] Skipping {base_url} (disallowed)")
        return all_results

    html = fetch_page(base_url)
    if not html:
        return all_results

    relevant, reason = is_site_relevant(
        base_url, html, industry=industry_target, country=country_target
    )
    if not relevant:
        print(f"    [relevance] Skipping {base_url}: {reason}")
        return None

    def collect(page_html):
        """Extract emails with context and deduplicate."""
        results = extract_emails_with_context(page_html, role_filters=role_filters)
        for r in results:
            if r["email"].lower() not in seen_emails:
                seen_emails.add(r["email"].lower())
                all_results.append(r)

    collect(html)

    # Crawl linked contact/team pages
    visited_urls = {base_url}
    for sub_url in find_contact_pages(base_url, html):
        if sub_url in visited_urls:
            continue
        visited_urls.add(sub_url)
        if respect_robots and not can_fetch(sub_url):
            continue
        time.sleep(crawl_delay)
        sub_html = fetch_page(sub_url)
        if sub_html:
            collect(sub_html)

    # Brute-force probe team pages not found via links
    for probe_url, probe_html in probe_team_pages(base_url, crawl_delay, respect_robots):
        if probe_url not in visited_urls:
            visited_urls.add(probe_url)
            collect(probe_html)

    # Filter: only keep emails whose domain matches the site being crawled
    if filter_by_domain and all_results:
        before = len(all_results)
        site_emails = filter_emails_by_site_domain(
            {r["email"] for r in all_results}, base_url
        )
        all_results = [r for r in all_results if r["email"] in site_emails]
        dropped = before - len(all_results)
        if dropped:
            print(f"    [filter] Dropped {dropped} off-domain email(s)")

    return all_results


def _discover_internal_links(base_url, html):
    """Extract all same-domain internal links from a page."""
    soup = BeautifulSoup(html, "html.parser")
    base_domain = urlparse(base_url).netloc.lower()
    links = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        # Skip fragments, javascript, mailto, tel links
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full_url = urljoin(base_url, href)
        parsed = urlparse(full_url)
        # Only keep same-domain, http(s) links
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc.lower() != base_domain:
            continue
        # Strip fragment, normalize
        clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        # Optionally keep query string (some sites use it for real pages)
        if parsed.query:
            clean_url += f"?{parsed.query}"
        # Skip common non-page resources
        skip_extensions = (
            ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp",
            ".css", ".js", ".zip", ".tar", ".gz", ".mp4", ".mp3",
            ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        )
        if any(parsed.path.lower().endswith(ext) for ext in skip_extensions):
            continue
        links.add(clean_url)
    return links


def deep_crawl_site(base_url, crawl_delay=1.5, respect_robots=True,
                    role_filters=None, max_pages=50, filter_by_domain=True):
    """
    Deep-crawl an entire website (BFS) following all internal links.

    Starts from base_url, discovers every same-domain page, and extracts
    emails from each one. Stops after visiting max_pages pages to avoid
    runaway crawling on huge sites.

    Parameters
    ----------
    base_url : str
        The starting URL.
    crawl_delay : float
        Seconds to wait between page requests.
    respect_robots : bool
        Whether to check robots.txt before fetching.
    role_filters : list[str] or None
        Only keep emails near these role keywords in page context.
    max_pages : int
        Maximum number of pages to visit on this site (default 50).
    filter_by_domain : bool
        If True, only keep emails whose domain matches the crawled site.

    Returns
    -------
    set[str]
        All emails found across the crawled pages.
    """
    found_emails = set()
    visited = set()
    queue = [base_url]

    # Normalize the starting URL's domain for same-domain checks
    base_domain = urlparse(base_url).netloc.lower()

    while queue and len(visited) < max_pages:
        url = queue.pop(0)

        # Normalize to avoid revisiting the same page
        parsed = urlparse(url)
        normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if parsed.query:
            normalized += f"?{parsed.query}"

        if normalized in visited:
            continue
        visited.add(normalized)

        # Check robots.txt
        if respect_robots and not can_fetch(url):
            print(f"    [robots.txt] Skipping {url}")
            continue

        # Fetch the page
        if len(visited) > 1:  # Don't delay before the first page
            time.sleep(crawl_delay)

        html = fetch_page(url)
        if not html:
            continue

        # Extract emails
        page_emails = extract_emails(html, role_filters=role_filters)
        if page_emails:
            print(f"    [found] {len(page_emails)} email(s) on {url}")
        found_emails |= page_emails

        # Discover internal links and add to queue
        new_links = _discover_internal_links(url, html)
        for link in new_links:
            link_parsed = urlparse(link)
            link_normalized = f"{link_parsed.scheme}://{link_parsed.netloc}{link_parsed.path}"
            if link_parsed.query:
                link_normalized += f"?{link_parsed.query}"
            if link_normalized not in visited:
                queue.append(link)

    print(f"    Crawled {len(visited)} page(s) on {base_domain}")

    # Filter: only keep emails whose domain matches the site being crawled
    if filter_by_domain and found_emails:
        before = len(found_emails)
        found_emails = filter_emails_by_site_domain(found_emails, base_url)
        dropped = before - len(found_emails)
        if dropped:
            print(f"    [filter] Dropped {dropped} off-domain email(s)")

    return found_emails


@dataclass(slots=True)
class FetchedPage:
    """Minimal HTTP result used by the async crawler and its cache."""

    status_code: int
    final_url: str
    body: str
    from_cache: bool = False


def _canonical_url(url):
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    normalized = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"
    if parsed.query:
        normalized += f"?{parsed.query}"
    return normalized


def _soft_404_signature(body):
    """Return stable visible text for comparing custom not-found templates."""
    soup = BeautifulSoup(body or "", "html.parser")
    for element in soup(["script", "style", "noscript", "svg"]):
        element.decompose()
    text = " ".join(soup.stripped_strings).lower()
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\b[0-9a-f]{8,}\b", " ", text)
    text = re.sub(r"\d+", "#", text)
    return re.sub(r"\s+", " ", text).strip()[:8000]


def _looks_like_soft_404(page, baseline):
    """Detect a successful response that actually repeats the site's 404 page."""
    if baseline is None or baseline.status_code != 200 or page.status_code != 200:
        return False
    if _canonical_url(page.final_url) == _canonical_url(baseline.final_url):
        return True
    baseline_text = _soft_404_signature(baseline.body)
    page_text = _soft_404_signature(page.body)
    if not baseline_text or not page_text:
        return False
    length_ratio = min(len(page_text), len(baseline_text)) / max(
        len(page_text), len(baseline_text)
    )
    return length_ratio >= 0.95 and fuzz.ratio(page_text, baseline_text) >= 98.5


def _page_priority(url, role_filters=None, linked=False):
    """Score promising staff/contact URLs without excluding low-ranked paths."""
    parsed = urlparse(url)
    text = f"{parsed.path} {parsed.query}".replace("-", " ").replace("_", " ")
    score = 100 if linked else 0
    high_value = (
        "leadership", "our team", "team", "staff", "providers", "physicians",
        "executives", "management", "directory", "contact",
    )
    score += max((fuzz.partial_ratio(term, text) for term in high_value), default=0)
    if role_filters:
        score += max(
            (fuzz.token_set_ratio(role.lower(), text.lower()) for role in role_filters),
            default=0,
        )
    return score


def prioritized_complete_pages(base_url, html, role_filters=None):
    """Return linked candidates plus every configured probe, ranked and deduplicated."""
    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    candidates = []
    seen = {_canonical_url(base_url)}

    for url in find_contact_pages(base_url, html):
        key = _canonical_url(url)
        if key not in seen:
            seen.add(key)
            candidates.append((url, False))

    for path in TEAM_PAGE_PROBES:
        url = root + path
        key = _canonical_url(url)
        if key not in seen:
            seen.add(key)
            candidates.append((url, True))

    return sorted(
        candidates,
        key=lambda item: _page_priority(
            item[0],
            role_filters=role_filters,
            linked=not item[1],
        ),
        reverse=True,
    )


class AsyncCrawler:
    """Shared HTTPX crawler with caching and polite cross-domain concurrency."""

    def __init__(
        self,
        *,
        crawl_delay=1.5,
        respect_robots=True,
        site_concurrency=5,
        cache_path="output/crawl_cache.sqlite",
        cache_ttl_hours=24.0,
        negative_cache_ttl_hours=168.0,
        request_timeout=8.0,
        max_robots_crawl_delay=10.0,
        client=None,
        cache=None,
    ):
        self.crawl_delay = crawl_delay
        self.respect_robots = respect_robots
        self.site_concurrency = site_concurrency
        self.cache_ttl_seconds = cache_ttl_hours * 3600
        self.negative_cache_ttl_seconds = negative_cache_ttl_hours * 3600
        self.request_timeout = request_timeout
        self.max_robots_crawl_delay = max_robots_crawl_delay
        self.client = client
        self.cache = cache if cache is not None else (
            CrawlCache(cache_path) if cache_path else None
        )
        self._owns_client = client is None
        self._owns_cache = cache is None and self.cache is not None
        self._domain_locks = {}
        self._last_request_finished = {}
        self._robots = {}
        self._robots_delay_exceeded = {}
        self._request_semaphore = None
        self._domain_delays = {}

    async def __aenter__(self):
        self._request_semaphore = asyncio.Semaphore(self.site_concurrency)
        if self.client is None:
            limit = max(10, self.site_concurrency * 2)
            self.client = httpx.AsyncClient(
                headers=HEADERS,
                follow_redirects=True,
                limits=httpx.Limits(
                    max_connections=limit,
                    max_keepalive_connections=limit,
                ),
            )
        return self

    async def __aexit__(self, *_args):
        if self._owns_client and self.client is not None:
            await self.client.aclose()
        if self._owns_cache and self.cache is not None:
            self.cache.close()

    def _domain_lock(self, url):
        domain = (urlparse(url).netloc or "").lower()
        if domain not in self._domain_locks:
            self._domain_locks[domain] = asyncio.Lock()
        return domain, self._domain_locks[domain]

    def _increase_domain_delay(self, domain, response=None):
        current = self._domain_delays.get(domain, self.crawl_delay)
        retry_after = 0.0
        status = None
        if response is not None:
            status = response.status_code
            try:
                retry_after = float(response.headers.get("Retry-After", 0))
            except (TypeError, ValueError):
                retry_after = 0.0
        factor = 2.0 if status == 429 else 1.25
        maximum = max(5.0, self.crawl_delay * 4) if status == 429 else max(
            2.0, self.crawl_delay * 2.5
        )
        self._domain_delays[domain] = min(max(current * factor, retry_after), maximum)

    def _relax_domain_delay(self, domain):
        current = self._domain_delays.get(domain, self.crawl_delay)
        self._domain_delays[domain] = max(self.crawl_delay, current * 0.8)

    async def fetch(self, url, *, timeout=None, success_ttl_seconds=None):
        """Fetch one URL, using cache and one polite request stream per domain."""
        if self.cache is not None:
            cached = self.cache.get(url)
            if cached is not None:
                return FetchedPage(
                    cached["status_code"],
                    cached["final_url"],
                    cached["body"],
                    from_cache=True,
                )

        domain, lock = self._domain_lock(url)
        async with lock:
            if self.cache is not None:
                cached = self.cache.get(url)
                if cached is not None:
                    return FetchedPage(
                        cached["status_code"],
                        cached["final_url"],
                        cached["body"],
                        from_cache=True,
                    )

            domain_delay = self._domain_delays.get(domain, self.crawl_delay)
            elapsed = time.monotonic() - self._last_request_finished.get(domain, 0)
            if elapsed < domain_delay:
                remaining = domain_delay - elapsed
                if remaining >= 2:
                    print(f"    [wait] {domain}: waiting {remaining:.1f}s before next request")
                await asyncio.sleep(remaining)
            try:
                if self._request_semaphore is None:
                    self._request_semaphore = asyncio.Semaphore(self.site_concurrency)
                async with self._request_semaphore:
                    response = await self.client.get(
                        url,
                        timeout=self.request_timeout if timeout is None else timeout,
                    )
            except httpx.HTTPError:
                self._last_request_finished[domain] = time.monotonic()
                self._increase_domain_delay(domain)
                return None
            self._last_request_finished[domain] = time.monotonic()

            status = response.status_code
            if status in {403, 429} or status >= 500:
                self._increase_domain_delay(domain, response)
            else:
                self._relax_domain_delay(domain)
            body = response.text if status == 200 else ""
            result = FetchedPage(status, str(response.url), body)
            if self.cache is not None and status in {200, 404, 410}:
                ttl = (
                    success_ttl_seconds
                    if status == 200 and success_ttl_seconds is not None
                    else self.cache_ttl_seconds
                    if status == 200
                    else self.negative_cache_ttl_seconds
                )
                self.cache.set(url, status, result.final_url, body, ttl)
            return result

    async def can_fetch(self, url):
        """Return whether cached robots rules permit fetching a URL."""
        if not self.respect_robots:
            return True
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain in self._robots_delay_exceeded:
            return False
        if domain not in self._robots:
            robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
            response = await self.fetch(
                robots_url,
                timeout=min(self.request_timeout, 6),
                success_ttl_seconds=self.negative_cache_ttl_seconds,
            )
            if response is None or response.status_code != 200:
                self._robots[domain] = None
            else:
                parser = urllib.robotparser.RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(response.body.splitlines())
                self._robots[domain] = parser
                requested_delay = (
                    parser.crawl_delay(HEADERS["User-Agent"])
                    or parser.crawl_delay("*")
                )
                if requested_delay is not None:
                    requested_delay = float(requested_delay)
                    if requested_delay > self.max_robots_crawl_delay:
                        self._robots_delay_exceeded[domain] = requested_delay
                        print(
                            f"    [robots.txt] Skipping {domain}: requested "
                            f"Crawl-delay {requested_delay:g}s exceeds the "
                            f"{self.max_robots_crawl_delay:g}s safety limit"
                        )
                        return False
                    self._domain_delays[domain] = max(
                        self._domain_delays.get(domain, self.crawl_delay),
                        requested_delay,
                    )
        parser = self._robots[domain]
        return True if parser is None else parser.can_fetch(HEADERS["User-Agent"], url)

    async def crawl_site(
        self,
        base_url,
        *,
        role_filters=None,
        filter_by_domain=True,
        industry_target="",
        country_target="",
    ):
        """Crawl a search result and every one of the configured probe paths."""
        if not await self.can_fetch(base_url):
            print(f"  [robots.txt] Skipping {base_url} (disallowed)")
            return []
        homepage = await self.fetch(base_url)
        if homepage is None or homepage.status_code != 200:
            return []

        relevant, reason = is_site_relevant(
            base_url,
            homepage.body,
            industry=industry_target,
            country=country_target,
        )
        if not relevant:
            print(f"    [relevance] Skipping {base_url}: {reason}")
            return None

        results = []
        seen_emails = set()

        def collect(html):
            for row in extract_emails_with_context(html, role_filters=role_filters):
                key = row["email"].lower()
                if key not in seen_emails:
                    seen_emails.add(key)
                    results.append(row)

        collect(homepage.body)
        root = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}".rstrip("/")
        domain = (urlparse(base_url).netloc or "").lower()
        missing_token = hashlib.sha256(domain.encode("utf-8")).hexdigest()[:12]
        soft_404_baseline = await self.fetch(
            f"{root}/__email-lead-scraper-missing-{missing_token}",
            timeout=min(self.request_timeout, 5),
        )
        for page_url, is_probe in prioritized_complete_pages(
            base_url,
            homepage.body,
            role_filters=role_filters,
        ):
            if not await self.can_fetch(page_url):
                continue
            page = await self.fetch(
                page_url,
                timeout=min(self.request_timeout, 5) if is_probe else self.request_timeout,
            )
            if page is None or page.status_code != 200:
                continue
            if is_probe:
                final_url = page.final_url.rstrip("/")
                if (
                    len(page.body) <= 1000
                    or final_url == root
                    or _looks_like_soft_404(page, soft_404_baseline)
                ):
                    continue
                print(f"    [probe] Found candidate page: {urlparse(page_url).path}")
            collect(page.body)

        if filter_by_domain and results:
            allowed = filter_emails_by_site_domain(
                {row["email"] for row in results},
                base_url,
            )
            before = len(results)
            results = [row for row in results if row["email"] in allowed]
            if before != len(results):
                print(f"    [filter] Dropped {before - len(results)} off-domain email(s)")
        return results

    async def crawl_many(self, targets):
        """Crawl domains together; the shared request semaphore controls load."""

        async def crawl_one(target):
            try:
                result = await self.crawl_site(
                    target[0],
                    role_filters=target[1],
                    industry_target=target[2],
                    country_target=target[3],
                )
            except Exception as exc:
                print(f"    [!] Crawl failed for {target[0]}: {exc}")
                result = []
            return target, result

        return await asyncio.gather(*(crawl_one(target) for target in targets))

    async def deep_crawl_site(
        self,
        base_url,
        *,
        role_filters=None,
        max_pages=50,
        filter_by_domain=True,
    ):
        """Deep-crawl one direct website using the shared async HTTP client."""
        found_emails = set()
        visited = set()
        queue = [base_url]
        base_domain = urlparse(base_url).netloc.lower()

        while queue and len(visited) < max_pages:
            url = queue.pop(0)
            normalized = _canonical_url(url)
            if normalized in visited:
                continue
            visited.add(normalized)
            if not await self.can_fetch(url):
                print(f"    [robots.txt] Skipping {url}")
                continue
            page = await self.fetch(url)
            if page is None or page.status_code != 200:
                continue
            page_emails = extract_emails(page.body, role_filters=role_filters)
            if page_emails:
                print(f"    [found] {len(page_emails)} email(s) on {url}")
            found_emails |= page_emails
            for link in _discover_internal_links(url, page.body):
                if _canonical_url(link) not in visited:
                    queue.append(link)

        print(f"    Crawled {len(visited)} page(s) on {base_domain}")
        if filter_by_domain and found_emails:
            before = len(found_emails)
            found_emails = filter_emails_by_site_domain(found_emails, base_url)
            if before != len(found_emails):
                print(f"    [filter] Dropped {before - len(found_emails)} off-domain email(s)")
        return found_emails

    async def deep_crawl_many(self, targets, *, role_filters=None, max_pages=50):
        """Deep-crawl direct sites with shared request-level concurrency."""

        async def crawl_one(target):
            try:
                emails = await self.deep_crawl_site(
                    target,
                    role_filters=role_filters,
                    max_pages=max_pages,
                )
            except Exception as exc:
                print(f"    [!] Deep crawl failed for {target}: {exc}")
                emails = set()
            return target, emails

        return await asyncio.gather(*(crawl_one(target) for target in targets))

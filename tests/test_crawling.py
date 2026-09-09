import asyncio
from collections import Counter

import httpx

from email_lead_scraper import crawling
from email_lead_scraper.settings import TEAM_PAGE_PROBES


def test_find_contact_pages_keeps_same_domain_links():
    html = """
    <a href="/team">Our Team</a>
    <a href="https://example.com/contact">Contact</a>
    <a href="https://other.example/team">Other Team</a>
    """
    assert set(crawling.find_contact_pages("https://example.com", html)) == {
        "https://example.com/team",
        "https://example.com/contact",
    }


def test_discover_internal_links_skips_assets_and_external_urls():
    html = """
    <a href="/team#people">Team</a>
    <a href="/brochure.pdf">PDF</a>
    <a href="mailto:info@example.com">Email</a>
    <a href="https://other.example/contact">External</a>
    """
    assert crawling._discover_internal_links("https://example.com", html) == {
        "https://example.com/team"
    }


def test_crawl_site_collects_contextual_results(monkeypatch):
    expected = [
        {
            "email": "jane.smith@example.com",
            "person_name": "Jane Smith",
            "role_title": "Clinic Director",
            "email_type": "personal",
        }
    ]
    monkeypatch.setattr(crawling, "can_fetch", lambda _url: True)
    monkeypatch.setattr(crawling, "fetch_page", lambda _url: "<html>Clinic</html>")
    monkeypatch.setattr(crawling, "is_site_relevant", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(crawling, "extract_emails_with_context", lambda *_args, **_kwargs: expected)
    monkeypatch.setattr(crawling, "find_contact_pages", lambda *_args: [])
    monkeypatch.setattr(crawling, "probe_team_pages", lambda *_args: [])
    monkeypatch.setattr(
        crawling,
        "filter_emails_by_site_domain",
        lambda emails, _url: emails,
    )
    assert crawling.crawl_site("https://example.com") == expected


def test_crawl_site_returns_none_for_irrelevant_site(monkeypatch):
    monkeypatch.setattr(crawling, "can_fetch", lambda _url: True)
    monkeypatch.setattr(crawling, "fetch_page", lambda _url: "<html>Technology</html>")
    monkeypatch.setattr(
        crawling,
        "is_site_relevant",
        lambda *_args, **_kwargs: (False, "industry mismatch"),
    )
    assert crawling.crawl_site("https://example.com", industry_target="Clinic") is None


def test_deep_crawl_console_output_supports_windows_cp1252(monkeypatch, capsys):
    monkeypatch.setattr(crawling, "can_fetch", lambda _url: True)
    monkeypatch.setattr(crawling, "fetch_page", lambda _url: "<html>info@example.com</html>")
    monkeypatch.setattr(crawling, "extract_emails", lambda *_args, **_kwargs: {"info@example.com"})
    monkeypatch.setattr(crawling, "_discover_internal_links", lambda *_args: set())
    monkeypatch.setattr(
        crawling,
        "filter_emails_by_site_domain",
        lambda emails, _url: emails,
    )

    crawling.deep_crawl_site("https://example.com", crawl_delay=0, max_pages=1)

    capsys.readouterr().out.encode("cp1252")


def test_prioritized_pages_keep_every_probe_and_linked_candidate():
    html = '<a href="/custom-leadership">Leadership team</a><a href="/team">Team</a>'
    pages = crawling.prioritized_complete_pages(
        "https://example.com/",
        html,
        role_filters=["Clinic Director"],
    )
    urls = [url for url, _is_probe in pages]

    for path in TEAM_PAGE_PROBES:
        assert f"https://example.com{path}" in urls
    assert "https://example.com/custom-leadership" in urls
    assert len(urls) == len(set(urls))


def test_async_fetch_uses_sqlite_cache(tmp_path):
    request_count = 0

    async def handler(request):
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, text="<html>Clinic</html>", request=request)

    async def exercise():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            async with crawling.AsyncCrawler(
                crawl_delay=0,
                respect_robots=False,
                cache_path=tmp_path / "crawl.sqlite",
                client=client,
            ) as crawler:
                first = await crawler.fetch("https://example.com/")
                second = await crawler.fetch("https://example.com/")
        return first, second

    first, second = asyncio.run(exercise())
    assert request_count == 1
    assert first.from_cache is False
    assert second.from_cache is True


def test_async_crawler_probes_all_paths_and_parallelizes_only_across_domains():
    active_total = 0
    max_active_total = 0
    active_by_domain = Counter()
    max_active_by_domain = Counter()
    requested_paths = {}

    async def handler(request):
        nonlocal active_total, max_active_total
        domain = request.url.host
        requested_paths.setdefault(domain, set()).add(request.url.path)
        active_total += 1
        active_by_domain[domain] += 1
        max_active_total = max(max_active_total, active_total)
        max_active_by_domain[domain] = max(
            max_active_by_domain[domain],
            active_by_domain[domain],
        )
        await asyncio.sleep(0.002)
        active_by_domain[domain] -= 1
        active_total -= 1
        if request.url.path == "/":
            body = "<html><body>Clinic patient services" + ("x" * 1100) + "</body></html>"
            return httpx.Response(200, text=body, request=request)
        return httpx.Response(404, request=request)

    async def exercise():
        transport = httpx.MockTransport(handler)
        targets = [
            ("https://one.example/", [], "Clinic", ""),
            ("https://two.example/", [], "Clinic", ""),
        ]
        async with httpx.AsyncClient(transport=transport) as client:
            async with crawling.AsyncCrawler(
                crawl_delay=0,
                respect_robots=False,
                site_concurrency=2,
                cache_path=None,
                client=client,
            ) as crawler:
                return await crawler.crawl_many(targets)

    results = asyncio.run(exercise())
    assert len(results) == 2
    assert max_active_total == 2
    assert all(count == 1 for count in max_active_by_domain.values())
    expected_paths = {path.rstrip("/") or "/" for path in TEAM_PAGE_PROBES}
    for domain in ("one.example", "two.example"):
        normalized_requested = {path.rstrip("/") or "/" for path in requested_paths[domain]}
        assert expected_paths <= normalized_requested


def test_soft_404_detection_rejects_repeated_success_template():
    baseline = crawling.FetchedPage(
        200,
        "https://example.com/missing-one",
        "<html><h1>Page not found</h1><p>Return to our clinic homepage.</p></html>",
    )
    repeated = crawling.FetchedPage(
        200,
        "https://example.com/team",
        "<html><h1>Page not found</h1><p>Return to our clinic homepage.</p></html>",
    )
    real_page = crawling.FetchedPage(
        200,
        "https://example.com/team",
        "<html><h1>Leadership</h1><p>Jane Smith, Clinic Director</p></html>",
    )

    assert crawling._looks_like_soft_404(repeated, baseline)
    assert not crawling._looks_like_soft_404(real_page, baseline)


def test_request_slots_move_to_fast_domains_while_a_slow_domain_waits():
    completion_order = []

    async def handler(request):
        if request.url.host == "slow.example":
            await asyncio.sleep(0.05)
        else:
            await asyncio.sleep(0.005)
        completion_order.append(request.url.host)
        return httpx.Response(200, text="ok", request=request)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async with crawling.AsyncCrawler(
                crawl_delay=0,
                respect_robots=False,
                site_concurrency=2,
                cache_path=None,
                client=client,
            ) as crawler:
                return await asyncio.gather(
                    crawler.fetch("https://slow.example/"),
                    crawler.fetch("https://fast-one.example/"),
                    crawler.fetch("https://fast-two.example/"),
                )

    asyncio.run(exercise())
    assert completion_order[-1] == "slow.example"
    assert set(completion_order[:2]) == {"fast-one.example", "fast-two.example"}


def test_adaptive_delay_backs_off_and_then_recovers():
    crawler = crawling.AsyncCrawler(crawl_delay=1, cache_path=None)
    throttled = httpx.Response(429, headers={"Retry-After": "3"})

    crawler._increase_domain_delay("example.com", throttled)
    assert crawler._domain_delays["example.com"] == 3

    crawler._relax_domain_delay("example.com")
    assert round(crawler._domain_delays["example.com"], 2) == 2.4


def test_async_robots_crawl_delay_overrides_faster_default():
    async def handler(request):
        return httpx.Response(
            200,
            text="User-agent: *\nCrawl-delay: 2\nAllow: /",
            request=request,
        )

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async with crawling.AsyncCrawler(
                crawl_delay=0.1,
                respect_robots=True,
                cache_path=None,
                client=client,
            ) as crawler:
                assert await crawler.can_fetch("https://example.com/team")
                return crawler._domain_delays["example.com"]

    assert asyncio.run(exercise()) == 2


def test_async_robots_crawl_delay_above_limit_skips_site(capsys):
    async def handler(request):
        return httpx.Response(
            200,
            text="User-agent: *\nCrawl-delay: 120\nAllow: /",
            request=request,
        )

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async with crawling.AsyncCrawler(
                crawl_delay=0.1,
                respect_robots=True,
                max_robots_crawl_delay=10,
                cache_path=None,
                client=client,
            ) as crawler:
                assert not await crawler.can_fetch("https://slow.example/team")
                assert not await crawler.can_fetch("https://slow.example/contact")
                return crawler._robots_delay_exceeded["slow.example"]

    assert asyncio.run(exercise()) == 120
    output = capsys.readouterr().out
    assert output.count("requested Crawl-delay 120s") == 1

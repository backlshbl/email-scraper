import csv
import asyncio

import pytest

from email_lead_scraper import application
from email_lead_scraper.settings import Settings, SettingsError


def test_direct_site_pipeline_writes_results(tmp_path, monkeypatch):
    class FakeAsyncCrawler:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def deep_crawl_many(self, targets, **_kwargs):
            return [(target, {"jane.smith@example.com"}) for target in targets]

    output = tmp_path / "leads.csv"
    settings = Settings(
        direct_sites=["example.com"],
        output_path=str(output),
        fresh_output=True,
        crawl_delay=0,
        crawl_cache_enabled=False,
        check_mx=False,
    )
    monkeypatch.setattr(application.crawling, "AsyncCrawler", FakeAsyncCrawler)
    monkeypatch.setattr(application, "enrich_leads", lambda rows, _settings: rows)

    assert application.run(settings) == 0

    with output.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["email"] == "jane.smith@example.com"
    assert rows[0]["source_url"] == "https://example.com/"


def test_pipeline_validates_programmatic_settings_before_work_starts(tmp_path):
    settings = Settings(
        direct_sites=["example.com"],
        output_path=str(tmp_path / "leads.csv"),
        max_pages=0,
    )

    with pytest.raises(SettingsError, match="max_pages"):
        application.run(settings)


def test_searches_continue_while_previous_websites_are_crawling(tmp_path, monkeypatch):
    state = {"crawl_started": False, "crawl_finished": False}

    class FakeCrawler:
        cache = None

        async def crawl_site(self, *_args, **_kwargs):
            state["crawl_started"] = True
            await asyncio.sleep(0.05)
            state["crawl_finished"] = True
            return []

    monkeypatch.setattr(
        application.search,
        "build_queries",
        lambda *_args, **_kwargs: (
                [
                    ("query-one", "CEO", ("Clinic",), "US", "Test City"),
                    ("query-two", "CEO", ("Clinic",), "US", "Test City"),
                ],
            {"US": ["Test City"]},
        ),
    )

    def fake_search(query, **_kwargs):
        if query == "query-two":
            assert state["crawl_started"] is True
            assert state["crawl_finished"] is False
        return application.search.SearchOutcome(
            [f"https://{query}.example/team"],
            "success",
            backend="fake",
        )

    class FakeSearchContext:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(application.search, "duckduckgo_search", fake_search)
    monkeypatch.setattr(application.search, "DDGSSearchClient", FakeSearchContext)
    monkeypatch.setattr(application.random, "uniform", lambda *_args: 0)
    settings = Settings(
        roles=["CEO"],
        industries=["Clinic"],
        countries=["US"],
        tracker_path=str(tmp_path / "tracker.json"),
        search_delay=0,
        search_max_queries=2,
        search_min_results=0,
        check_mx=False,
    )

    asyncio.run(
        application._run_search(
            settings,
            application.storage.LeadCollector(),
            FakeCrawler(),
        )
    )

    assert state["crawl_finished"] is True


def test_site_crawl_timeout_finishes_queued_progress(tmp_path, monkeypatch, capsys):
    class FakeCrawler:
        cache = None

        async def crawl_site(self, *_args, **_kwargs):
            await asyncio.sleep(0.05)
            return []

    class FakeSearchContext:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        application.search,
        "build_queries",
        lambda *_args, **_kwargs: (
            [("query-one", "CEO", ("Clinic",), "US", "Test City")],
            {"US": ["Test City"]},
        ),
    )
    monkeypatch.setattr(
        application.search,
        "duckduckgo_search",
        lambda *_args, **_kwargs: application.search.SearchOutcome(
            ["https://slow.example/team"], "success"
        ),
    )
    monkeypatch.setattr(application.search, "DDGSSearchClient", FakeSearchContext)
    monkeypatch.setattr(application.random, "uniform", lambda *_args: 0)
    settings = Settings(
        roles=["CEO"],
        industries=["Clinic"],
        countries=["US"],
        tracker_path=str(tmp_path / "tracker.json"),
        search_delay=0,
        search_max_queries=1,
        search_min_results=0,
        site_crawl_timeout=0.001,
        check_mx=False,
    )

    asyncio.run(
        application._run_search(
            settings,
            application.storage.LeadCollector(),
            FakeCrawler(),
        )
    )

    output = capsys.readouterr().out
    assert "[timeout] Stopped slow.example after 0.001s" in output
    assert "[crawl progress] 1 completed / 1 queued so far" in output


def test_search_circuit_breaker_finishes_active_crawls_and_keeps_partial_rows(
    tmp_path,
    monkeypatch,
):
    queries = [
        (f"query-{index}", "CEO", ("Clinic",), "US", "Test City")
        for index in range(1, 6)
    ]
    requested = []

    class FakeCrawler:
        cache = None

        async def crawl_site(self, *_args, **_kwargs):
            await asyncio.sleep(0)
            return [
                {
                    "email": "jane@example.com",
                    "person_name": "Jane Doe",
                    "role_title": "CEO",
                    "email_type": "personal",
                }
            ]

    class FakeSearchContext:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_search(query, **_kwargs):
        requested.append(query)
        if query == "query-1":
            return application.search.SearchOutcome(
                ["https://example.com/team"],
                "success",
            )
        return application.search.SearchOutcome([], "failed", error="timed out")

    monkeypatch.setattr(
        application.search,
        "build_queries",
        lambda *_args, **_kwargs: (queries, {"US": ["Test City"]}),
    )
    monkeypatch.setattr(application.search, "duckduckgo_search", fake_search)
    monkeypatch.setattr(application.search, "DDGSSearchClient", FakeSearchContext)
    monkeypatch.setattr(application.random, "uniform", lambda *_args: 0)
    settings = Settings(
        roles=["CEO"],
        industries=["Clinic"],
        countries=["US"],
        tracker_path=str(tmp_path / "tracker.json"),
        search_delay=0,
        search_max_queries=5,
        search_min_results=0,
        search_failure_threshold=3,
        check_mx=False,
    )
    collector = application.storage.LeadCollector()

    asyncio.run(application._run_search(settings, collector, FakeCrawler()))

    assert requested == ["query-1", "query-2", "query-3", "query-4"]
    assert collector.rows()[0]["email"] == "jane@example.com"
    assert (tmp_path / "tracker.json").exists()


def test_empty_searches_open_circuit_and_are_not_cached(tmp_path, monkeypatch):
    queries = [
        (f"query-{index}", "CEO", ("Clinic",), "US", "Test City")
        for index in range(1, 6)
    ]
    requested = []

    class FakeCache:
        def __init__(self):
            self.saved = []

        def get_search_results(self, _key):
            return None

        def set_search_results(self, *args):
            self.saved.append(args)

    class FakeCrawler:
        cache = FakeCache()

        async def crawl_site(self, *_args, **_kwargs):
            raise AssertionError("An empty search must not queue a crawl")

    class FakeSearchContext:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_search(query, **_kwargs):
        requested.append(query)
        return application.search.SearchOutcome(
            [],
            "empty",
            backend="brave",
            attempts=2,
            attempted_backends=("duckduckgo", "brave"),
        )

    monkeypatch.setattr(
        application.search,
        "build_queries",
        lambda *_args, **_kwargs: (queries, {"US": ["Test City"]}),
    )
    monkeypatch.setattr(application.search, "duckduckgo_search", fake_search)
    monkeypatch.setattr(application.search, "DDGSSearchClient", FakeSearchContext)
    monkeypatch.setattr(application.random, "uniform", lambda *_args: 0)
    settings = Settings(
        roles=["CEO"],
        industries=["Clinic"],
        countries=["US"],
        tracker_path=str(tmp_path / "tracker.json"),
        search_delay=0,
        search_max_queries=5,
        search_min_results=0,
        search_failure_threshold=3,
        check_mx=False,
    )
    crawler = FakeCrawler()

    asyncio.run(
        application._run_search(
            settings,
            application.storage.LeadCollector(),
            crawler,
        )
    )

    assert requested == ["query-1", "query-2", "query-3"]
    assert crawler.cache.saved == []

from email_lead_scraper import cli


def test_parser_accepts_direct_crawl_and_enrichment_options():
    args = cli.build_parser().parse_args(
        [
            "--websites",
            "https://example.com",
            "--max-pages",
            "12",
            "--hunter-enrich",
            "--site-concurrency",
            "7",
            "--request-timeout",
            "6",
            "--crawl-cache-ttl-hours",
            "12",
            "--search-cache-ttl-hours",
            "4",
            "--search-backends",
            "duckduckgo",
            "brave",
            "--search-timeout",
            "5",
            "--search-retry-delay",
            "1",
            "--search-failure-threshold",
            "3",
            "--search-max-queries",
            "30",
            "--search-min-results",
            "8",
            "--skip-mx-check",
            "--mx-concurrency",
            "3",
            "--output",
            "output/test.csv",
        ]
    )

    assert args.websites == ["https://example.com"]
    assert args.max_pages == 12
    assert args.hunter_enrich is True
    assert args.site_concurrency == 7
    assert args.request_timeout == 6
    assert args.crawl_cache_ttl_hours == 12
    assert args.search_cache_ttl_hours == 4
    assert args.search_backends == ["duckduckgo", "brave"]
    assert args.search_timeout == 5
    assert args.search_retry_delay == 1
    assert args.search_failure_threshold == 3
    assert args.search_max_queries == 30
    assert args.search_min_results == 8
    assert args.skip_mx_check is True
    assert args.mx_concurrency == 3
    assert args.output == "output/test.csv"


def test_main_dispatches_parsed_namespace(monkeypatch):
    captured = {}

    def fake_run(args):
        captured["websites"] = args.websites
        return 7

    monkeypatch.setattr(cli, "run_from_args", fake_run)

    assert cli.main(["--websites", "example.com"]) == 7
    assert captured["websites"] == ["example.com"]

from email_lead_scraper import search


def test_build_queries_use_simple_unquoted_discovery_terms():
    queries, rotation = search.build_queries(
        roles=["Medical Director"],
        industries=["Urgent Care"],
        countries=["United States"],
        locations_per_day=1,
    )
    assert len(queries) == 1
    assert queries[0][0].startswith("Medical Director (Urgent Care) ")
    assert '"' not in queries[0][0]
    assert "email" not in queries[0][0].lower()
    assert "our team" not in queries[0][0].lower()
    assert queries[0][2] == ("Urgent Care",)
    assert len(rotation["United States"]) == 1


def test_build_queries_combines_industries_into_one_query_per_role_and_location():
    queries, _rotation = search.build_queries(
        roles=["Founder", "CEO"],
        industries=["Hospital", "Clinic", "Healthcare"],
        countries=["United States"],
        locations_per_day=2,
    )

    assert len(queries) == 4
    assert "(Hospital OR Clinic OR Healthcare)" in queries[0][0]
    assert all(query[2] == ("Hospital", "Clinic", "Healthcare") for query in queries)


def test_unknown_country_uses_country_as_location():
    assert search.get_daily_locations("Singapore", locations_per_day=3) == ["Singapore"]


def test_parent_domain_is_blocked():
    assert search.is_domain_blocked("https://jobs.linkedin.com/company/example")
    assert search.is_domain_blocked("https://www.zippia.com/example/executives")
    assert not search.is_domain_blocked("https://example-clinic.com/team")


def test_duckduckgo_search_filters_blocked_domains(monkeypatch):
    class FakeDDGS:
        def __init__(self, timeout):
            assert timeout == 5

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def text(self, _query, max_results, backend):
            assert max_results == 5
            assert backend == "duckduckgo"
            return [
                {"href": "https://example-clinic.com/team"},
                {"href": "https://linkedin.com/company/example"},
                {"href": "https://example-clinic.com/provider-list.pdf"},
                {"link": "https://second-clinic.org/contact"},
            ]

    monkeypatch.setattr(search, "_import_ddgs", lambda: FakeDDGS)
    outcome = search.duckduckgo_search("clinic", max_results=5)
    assert outcome.status == "success"
    assert outcome.urls == [
        "https://example-clinic.com/team",
        "https://second-clinic.org/contact",
    ]


def test_ddgs_client_reuses_instance_and_uses_one_backend_at_a_time(monkeypatch):
    events = {"instances": 0, "backends": []}

    class FakeDDGS:
        def __init__(self, timeout):
            events["instances"] += 1
            assert timeout == 4

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def text(self, query, max_results, backend):
            events["backends"].append((query, backend))
            return [{"href": f"https://{query}.example/team"}]

    with search.DDGSSearchClient(
        backends=["duckduckgo", "brave"],
        timeout=4,
        ddgs_factory=FakeDDGS,
    ) as client:
        first = client.search("first")
        second = client.search("second")

    assert first.status == second.status == "success"
    assert events["instances"] == 1
    assert events["backends"] == [
        ("first", "duckduckgo"),
        ("second", "duckduckgo"),
    ]


def test_ddgs_client_allows_only_one_fallback_attempt(monkeypatch):
    calls = []

    class FakeDDGS:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def text(self, _query, max_results, backend):
            calls.append(backend)
            if backend == "duckduckgo":
                raise RuntimeError("timed out")
            return [{"href": "https://example.com/team"}]

    monkeypatch.setattr(search.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(search.random, "uniform", lambda *_args: 0)
    with search.DDGSSearchClient(
        backends=["duckduckgo", "brave", "bing"],
        ddgs_factory=FakeDDGS,
    ) as client:
        outcome = client.search("clinic")

    assert outcome.status == "success"
    assert outcome.attempts == 2
    assert calls == ["duckduckgo", "brave"]


def test_ddgs_client_tries_fallback_after_no_results():
    calls = []

    class FakeDDGS:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def text(self, _query, max_results, backend):
            calls.append(backend)
            raise RuntimeError("No results found.")

    with search.DDGSSearchClient(
        backends=["duckduckgo", "brave"],
        ddgs_factory=FakeDDGS,
    ) as client:
        outcome = client.search("clinic")

    assert outcome.status == "empty"
    assert outcome.attempts == 2
    assert outcome.attempted_backends == ("duckduckgo", "brave")
    assert calls == ["duckduckgo", "brave"]


def test_ddgs_client_promotes_a_successful_fallback_backend():
    calls = []

    class FakeDDGS:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def text(self, query, max_results, backend):
            calls.append((query, backend))
            if backend == "duckduckgo":
                return []
            return [{"href": "https://example-clinic.com"}]

    with search.DDGSSearchClient(
        backends=["duckduckgo", "brave"],
        ddgs_factory=FakeDDGS,
    ) as client:
        first = client.search("first")
        second = client.search("second")

    assert first.status == second.status == "success"
    assert first.attempted_backends == ("duckduckgo", "brave")
    assert second.attempted_backends == ("brave",)
    assert calls == [
        ("first", "duckduckgo"),
        ("first", "brave"),
        ("second", "brave"),
    ]


def test_adaptive_queries_respect_limit_and_narrow_industries():
    base, _rotation = search.build_queries(
        roles=["Founder", "CEO"],
        industries=["Hospital", "Clinic", "Healthcare"],
        countries=["United States"],
        locations_per_day=2,
    )
    adaptive = search.build_adaptive_queries(
        [(2, query) for query in base],
        limit=3,
    )

    assert len(adaptive) == 3
    assert all(len(query[2]) == 1 for query in adaptive)
    assert len({query[2][0] for query in adaptive}) == 3
    assert all('"' not in query[0] for query in adaptive)
    assert all("email" not in query[0].lower() for query in adaptive)


def test_domain_normalization_collapses_www_and_protocol_variants():
    assert search.normalized_domain("https://www.Example.com/team") == "example.com"
    assert search.normalized_domain("http://example.com") == "example.com"
    assert search.normalized_domain("www.example.com") == "example.com"


def test_non_html_search_results_are_rejected():
    assert not search.is_search_result_usable("https://example.com/team.pdf")
    assert not search.is_search_result_usable("https://example.com/list.DOCX?download=1")
    assert search.is_search_result_usable("https://example.com/leadership")


def test_candidate_ranking_preserves_all_urls_and_prioritizes_useful_pages():
    urls = [
        "https://example.com/news",
        "https://clinic.example/leadership",
        "https://hospital.example/about",
    ]
    ranked = search.rank_candidate_websites(
        urls,
        role="Clinic Director",
        industry="Clinic",
        country="United States",
    )

    assert set(ranked) == set(urls)
    assert len(ranked) == len(urls)
    assert ranked[0] == "https://clinic.example/leadership"

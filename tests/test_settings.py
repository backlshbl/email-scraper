import json

import pytest

from email_lead_scraper.cli import build_parser
from email_lead_scraper.settings import (
    Settings,
    SettingsError,
    from_namespace,
    parse_api_keys,
)


def test_config_values_are_resolved_and_roles_become_default_filters(tmp_path):
    config_path = tmp_path / "scraper.json"
    config_path.write_text(
        json.dumps(
            {
                "roles": ["Clinic Director"],
                "industries": ["Clinic"],
                "countries": ["United States"],
                "max_sites": 8,
                "crawl_delay": 0.25,
                "search_backends": ["duckduckgo", "brave"],
                "search_timeout": 4,
                "search_retry_delay": 0.5,
                "search_failure_threshold": 2,
                "search_max_queries": 24,
                "search_min_results": 6,
                "site_concurrency": 3,
                "request_timeout": 7,
                "site_crawl_timeout": 80,
                "max_robots_crawl_delay": 6,
                "crawl_cache_ttl_hours": 12,
                "negative_cache_ttl_hours": 72,
                "search_cache_ttl_hours": 6,
                "check_mx": False,
                "mx_concurrency": 4,
            }
        ),
        encoding="utf-8",
    )

    args = build_parser().parse_args(["--config", str(config_path)])
    settings = from_namespace(args)

    assert settings.roles == ["Clinic Director"]
    assert settings.filter_roles == ["Clinic Director"]
    assert settings.max_sites == 8
    assert settings.crawl_delay == 0.25
    assert settings.search_backends == ["duckduckgo", "brave"]
    assert settings.search_timeout == 4
    assert settings.search_retry_delay == 0.5
    assert settings.search_failure_threshold == 2
    assert settings.search_max_queries == 24
    assert settings.search_min_results == 6
    assert settings.site_concurrency == 3
    assert settings.request_timeout == 7
    assert settings.site_crawl_timeout == 80
    assert settings.max_robots_crawl_delay == 6
    assert settings.crawl_cache_ttl_hours == 12
    assert settings.negative_cache_ttl_hours == 72
    assert settings.search_cache_ttl_hours == 6
    assert settings.check_mx is False
    assert settings.mx_concurrency == 4


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_sites", 0),
        ("max_pages", -1),
        ("locations_per_day", 0),
        ("search_failure_threshold", 0),
        ("search_max_queries", 0),
        ("search_min_results", -1),
        ("crawl_delay", -0.1),
        ("search_retry_delay", -0.1),
        ("site_concurrency", 0),
        ("mx_concurrency", 0),
        ("request_timeout", 0),
        ("site_crawl_timeout", 0),
        ("max_robots_crawl_delay", -0.1),
        ("search_timeout", 0),
        ("crawl_cache_ttl_hours", -0.1),
        ("negative_cache_ttl_hours", -0.1),
        ("search_cache_ttl_hours", -0.1),
        ("mx_timeout", 0),
        ("hunter_max_requests", -1),
    ],
)
def test_validation_rejects_invalid_limits(field, value):
    settings = Settings(direct_sites=["example.com"])
    setattr(settings, field, value)

    with pytest.raises(SettingsError, match=field):
        settings.validate()


def test_validation_rejects_more_than_thirty_searches():
    settings = Settings(direct_sites=["example.com"], search_max_queries=31)

    with pytest.raises(SettingsError, match="cannot exceed 30"):
        settings.validate()


@pytest.mark.parametrize("backends", [[], ["duckduckgo", "brave", "bing"], "duckduckgo"])
def test_validation_requires_one_or_two_explicit_search_backends(backends):
    settings = Settings(direct_sites=["example.com"], search_backends=backends)

    with pytest.raises(SettingsError, match="search_backends"):
        settings.validate()


def test_config_requires_lists_for_search_dimensions(tmp_path):
    config_path = tmp_path / "scraper.json"
    config_path.write_text(
        json.dumps(
            {
                "roles": "CEO",
                "industries": ["Clinic"],
                "countries": ["United States"],
            }
        ),
        encoding="utf-8",
    )

    args = build_parser().parse_args(["--config", str(config_path)])
    with pytest.raises(SettingsError, match="roles"):
        from_namespace(args)


def test_api_key_json_arrays_preserve_priority_and_remove_duplicates():
    assert parse_api_keys('["first", "second", "first", "third"]') == [
        "first",
        "second",
        "third",
    ]


def test_hunter_plural_environment_key_array_takes_priority(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEYS", '["hunter-first", "hunter-second"]')
    monkeypatch.setenv("HUNTER_API_KEY", "legacy-hunter")

    args = build_parser().parse_args(["--websites", "https://example.com"])
    settings = from_namespace(args)

    assert settings.hunter_api_keys == ["hunter-first", "hunter-second"]


def test_empty_hunter_plural_array_falls_back_to_legacy_single_key(monkeypatch):
    monkeypatch.setenv("HUNTER_API_KEYS", "[]")
    monkeypatch.setenv("HUNTER_API_KEY", "legacy-hunter")

    args = build_parser().parse_args(["--websites", "https://example.com"])
    settings = from_namespace(args)

    assert settings.hunter_api_keys == ["legacy-hunter"]

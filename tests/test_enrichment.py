import pytest

from email_lead_scraper import enrichment
from email_lead_scraper.settings import Settings


def test_hunter_enrichment_processes_ranked_rows(monkeypatch):
    rows = [
        {"email": "info@example.com", "email_type": "generic"},
        {
            "email": "jane.smith@example.com",
            "email_type": "personal",
            "person_name": "Jane Smith",
        },
    ]
    received = []

    def fake_hunter(input_rows, *_args, **_kwargs):
        received.extend(input_rows)
        return {"requested": 2, "cached": 0, "valid": 0, "skipped": 0, "stopped": ""}

    monkeypatch.setattr(enrichment, "enrich_rows_with_hunter", fake_hunter)
    settings = Settings(
        hunter_enabled=True,
        hunter_api_keys=["hunter-key"],
    )

    assert enrichment.enrich_leads(rows, settings) is rows
    assert received == [rows[1], rows[0]]


def test_enabled_provider_requires_key():
    with pytest.raises(enrichment.EnrichmentError):
        enrichment.enrich_leads([{"email": "a@example.com"}], Settings(hunter_enabled=True))


def test_candidates_with_matching_name_and_role_are_prioritized():
    weak = {
        "email": "info@example.com",
        "email_type": "generic",
        "source_url": "https://example.com/contact",
        "role_target": "Clinic Director",
    }
    strong = {
        "email": "jane.smith@example.com",
        "email_type": "personal",
        "person_name": "Jane Smith",
        "role_title": "Senior Clinic Director",
        "role_target": "Clinic Director",
        "source_url": "https://example.com/team",
    }

    assert enrichment.rank_candidates([weak, strong]) == [strong, weak]
    assert enrichment.candidate_quality_score(strong) > 80

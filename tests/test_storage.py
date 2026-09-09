import csv

from email_lead_scraper import storage


def test_json_cache_and_tracker_round_trip(tmp_path):
    cache_path = tmp_path / "nested" / "cache.json"
    storage.save_json_cache(cache_path, {"key": {"status": "valid"}})
    assert storage.load_json_cache(cache_path) == {"key": {"status": "valid"}}

    tracker_path = tmp_path / "tracker.json"
    tracker = {}
    storage.record_crawled_domain(tracker, "example.com")
    storage.save_tracker(tracker_path, tracker)
    assert storage.load_tracker(tracker_path)["example.com"]["crawl_count"] == 1


def test_crawl_cache_round_trip_and_expiration(tmp_path):
    cache_path = tmp_path / "crawl.sqlite"
    with storage.CrawlCache(cache_path) as cache:
        cache.set("https://example.com/", 200, "https://example.com/", "body", 60)
        assert cache.get("https://example.com/") == {
            "status_code": 200,
            "final_url": "https://example.com/",
            "body": "body",
        }
        cache.set("https://example.com/missing", 404, "https://example.com/missing", "", 0)
        assert cache.get("https://example.com/missing") is None

        cache.set_search_results("query", ["https://example.com/team"], 60)
        assert cache.get_search_results("query") == ["https://example.com/team"]
        cache.set_search_results("expired-query", ["https://example.com"], 0)
        assert cache.get_search_results("expired-query") is None


def test_lead_collector_deduplicates_and_preserves_targets():
    collector = storage.LeadCollector()
    result = {
        "email": "Jane.Smith@example.com",
        "person_name": "Jane Smith",
        "role_title": "Clinic Director",
        "email_type": "personal",
    }
    collector.record([result], "https://example.com/team", "Clinic Director", "Clinic", "US")
    collector.record([result], "https://example.com/contact", "CEO", "Clinic", "US")

    rows = collector.rows()
    assert len(rows) == 1
    assert rows[0]["role_target"] == "Clinic Director"
    assert collector.site_count == 1


def test_write_and_load_existing_results(tmp_path):
    output = tmp_path / "nested" / "leads.csv"
    rows = [{"email": "a@example.com", "email_type": "personal"}]
    assert storage.write_results(rows, output, fresh_output=True) == 1
    emails, count = storage.load_existing_emails(output)
    assert emails == {"a@example.com"}
    assert count == 1


def test_merge_results_expands_legacy_rows_and_deduplicates(tmp_path):
    today = tmp_path / "today.csv"
    cumulative = tmp_path / "cumulative.csv"
    with today.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["email", "source_url"])
        writer.writeheader()
        writer.writerow(
            {
                "email": "info@example.com; jane.smith@example.com",
                "source_url": "https://example.com",
            }
        )

    stats = storage.merge_results(today, cumulative, run_date="2026-08-29")
    rows = storage.load_csv(cumulative)
    assert stats["new"] == 2
    assert len(rows) == 2
    assert {row["date_found"] for row in rows} == {"2026-08-29"}


def test_empty_daily_merge_reports_existing_totals(tmp_path):
    today = tmp_path / "today.csv"
    cumulative = tmp_path / "cumulative.csv"
    today.write_text("email,source_url\n", encoding="utf-8")
    with cumulative.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["email", "email_type", "source_url"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "email": "jane@example.com",
                "email_type": "personal",
                "source_url": "https://example.com",
            }
        )

    assert storage.merge_results(today, cumulative) == {
        "new": 0,
        "total": 1,
        "personal": 1,
        "sites": 1,
    }

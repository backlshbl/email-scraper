import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from email_lead_scraper.hunter import (
    choose_domain_candidate,
    choose_selected_email,
    enrich_rows,
)


class FakeHunterClient:
    def __init__(self, verify_status="valid"):
        self.verify_status = verify_status
        self.verifications = 0
        self.finds = 0
        self.domain_searches = 0

    def verify(self, email):
        self.verifications += 1
        return {"email": email, "status": self.verify_status, "score": 95}

    def find(self, full_name, domain):
        self.finds += 1
        return {"email": "jane@example.com", "score": 98,
                "verification": {"status": "valid"}}

    def domain_search(self, domain, type="personal", decision_maker=True):
        self.domain_searches += 1
        return {
            "emails": [
                {
                    "value": "director@example.com",
                    "first_name": "Clinic",
                    "last_name": "Director",
                    "position": "Clinic Director",
                    "confidence": 92,
                    "verification": {"status": "valid"},
                }
            ]
        }


class HunterEnrichmentTests(unittest.TestCase):
    def test_domain_candidate_rejects_unrelated_employee(self):
        candidates = [{"value": "engineer@example.com",
                       "position": "Software Engineer", "confidence": 99}]
        self.assertIsNone(choose_domain_candidate(candidates, "CEO"))

    def test_domain_candidate_understands_ceo_alias(self):
        candidates = [
            {"value": "director@example.com", "position": "Marketing Director",
             "confidence": 99},
            {"value": "ceo@example.com", "position": "Plan President",
             "confidence": 80},
        ]
        self.assertEqual(
            choose_domain_candidate(candidates, "CEO")["value"], "ceo@example.com"
        )

    def test_founder_target_does_not_accept_a_ceo_without_founder_context(self):
        candidates = [
            {
                "value": "ceo@example.com",
                "position": "CEO",
                "decision_maker": True,
                "confidence": 99,
            }
        ]
        self.assertIsNone(choose_domain_candidate(candidates, "Founder"))

    def test_clinic_director_does_not_match_unrelated_director(self):
        candidates = [
            {
                "value": "marketing@example.com",
                "position": "Marketing Director",
                "confidence": 99,
            }
        ]
        self.assertIsNone(choose_domain_candidate(candidates, "Clinic Director"))

    def test_hunter_verifies_scraped_email(self):
        row = {"email": "jane@example.com", "source_url": "https://example.com"}
        client = FakeHunterClient("valid")
        with tempfile.TemporaryDirectory() as directory:
            stats = enrich_rows([row], "unused", str(Path(directory) / "cache.json"),
                                client=client)
        self.assertEqual(stats["valid"], 1)
        self.assertEqual(row["selected_email"], "jane@example.com")
        self.assertEqual(row["selected_email_provider"], "hunter")

    def test_finder_runs_after_invalid_email_for_named_person(self):
        row = {"email": "old@example.com", "person_name": "Jane Doe",
               "source_url": "https://example.com/team"}
        client = FakeHunterClient("invalid")
        with tempfile.TemporaryDirectory() as directory:
            enrich_rows([row], "unused", str(Path(directory) / "cache.json"),
                        client=client)
        self.assertEqual(client.finds, 1)
        self.assertEqual(row["hunter_action"], "find")
        self.assertEqual(row["selected_email"], "jane@example.com")

    def test_domain_search_fallback_for_domain_only_row(self):
        row = {"email": "", "email_type": "domain_only", "role_target": "Clinic Director",
               "source_url": "https://example.com"}
        client = FakeHunterClient("invalid")
        with tempfile.TemporaryDirectory() as directory:
            enrich_rows([row], "unused", str(Path(directory) / "cache.json"),
                        client=client)
        self.assertEqual(client.domain_searches, 1)
        self.assertEqual(row["hunter_action"], "domain_search")
        self.assertEqual(row["hunter_email"], "director@example.com")
        self.assertEqual(row["selected_email"], "director@example.com")

    def test_valid_generic_email_still_uses_personal_domain_search(self):
        row = {"email": "info@example.com", "email_type": "generic",
               "role_target": "Clinic Director", "source_url": "https://example.com"}
        client = FakeHunterClient("valid")
        with tempfile.TemporaryDirectory() as directory:
            enrich_rows([row], "unused", str(Path(directory) / "cache.json"), client=client)
        self.assertEqual(client.domain_searches, 1)
        self.assertEqual(client.verifications, 0)
        self.assertEqual(row["selected_email"], "director@example.com")

    def test_finder_confidence_is_not_treated_as_verification(self):
        class UnverifiedFinderClient(FakeHunterClient):
            def find(self, full_name, domain):
                self.finds += 1
                return {"email": "jane@example.com", "score": 99}

        row = {
            "email": "old@example.com",
            "person_name": "Jane Doe",
            "source_url": "https://example.com/team",
        }
        client = UnverifiedFinderClient("invalid")
        with tempfile.TemporaryDirectory() as directory:
            enrich_rows(
                [row],
                "unused",
                str(Path(directory) / "cache.json"),
                max_requests=2,
                client=client,
            )
        self.assertEqual(client.verifications, 1)
        self.assertEqual(row["hunter_status"], "")
        self.assertEqual(row["selected_email"], "")

    def test_finder_without_a_result_continues_to_domain_search(self):
        class EmptyFinderClient(FakeHunterClient):
            def find(self, full_name, domain):
                self.finds += 1
                return {}

        row = {
            "email": "old@example.com",
            "person_name": "Jane Doe",
            "role_target": "Clinic Director",
            "source_url": "https://example.com/team",
        }
        client = EmptyFinderClient("invalid")
        with tempfile.TemporaryDirectory() as directory:
            enrich_rows(
                [row], "unused", str(Path(directory) / "cache.json"), client=client
            )
        self.assertEqual(client.finds, 1)
        self.assertEqual(client.domain_searches, 1)
        self.assertEqual(row["selected_email"], "director@example.com")

    def test_generic_email_is_not_selected_when_target_role_is_not_found(self):
        class UnrelatedClient(FakeHunterClient):
            def domain_search(self, domain, type="personal", decision_maker=True):
                self.domain_searches += 1
                return {"emails": [{"value": "engineer@example.com",
                                    "position": "Software Engineer",
                                    "confidence": 99,
                                    "verification": {"status": "valid"}}]}

        row = {"email": "info@example.com", "email_type": "generic",
               "role_target": "CEO", "source_url": "https://example.com"}
        with tempfile.TemporaryDirectory() as directory:
            enrich_rows([row], "unused", str(Path(directory) / "cache.json"),
                        client=UnrelatedClient("valid"))
        self.assertEqual(row["selected_email"], "")

    def test_accept_all_is_not_selected(self):
        row = {"hunter_email": "maybe@example.com", "hunter_status": "accept_all"}
        choose_selected_email(row)
        self.assertEqual(row["selected_email"], "")

    def test_budget_exhausted_stops_processing(self):
        rows = [
            {"email": f"user{i}@example.com", "source_url": "https://example.com"}
            for i in range(5)
        ]
        client = FakeHunterClient("valid")
        with tempfile.TemporaryDirectory() as directory:
            stats = enrich_rows(rows, "unused", str(Path(directory) / "cache.json"),
                                max_requests=2, client=client)
        self.assertEqual(stats["requested"], 2)
        self.assertEqual(stats["stopped"], "Hunter per-run request budget reached")

    def test_150_email_run_honors_50_operation_budget(self):
        rows = [
            {
                "email": f"person{i}@example.com",
                "email_type": "personal",
                "source_url": "https://example.com/team",
            }
            for i in range(150)
        ]
        client = FakeHunterClient("valid")
        with tempfile.TemporaryDirectory() as directory:
            stats = enrich_rows(
                rows,
                "unused",
                str(Path(directory) / "cache.json"),
                max_requests=50,
                client=client,
            )

        self.assertEqual(client.verifications, 50)
        self.assertEqual(stats["attempted"], 50)
        self.assertEqual(stats["requested"], 50)
        self.assertEqual(stats["valid"], 50)
        self.assertEqual(stats["skipped"], 100)
        self.assertEqual(stats["stopped"], "Hunter per-run request budget reached")
        self.assertEqual(sum(bool(row["selected_email"]) for row in rows), 50)

    def test_hunter_cache_is_reused_but_stale_entries_are_refreshed(self):
        row = {"email": "jane@example.com", "source_url": "https://example.com"}
        client = FakeHunterClient("valid")
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "cache.json"
            first = enrich_rows([row.copy()], "unused", str(cache_path), client=client)
            second = enrich_rows([row.copy()], "unused", str(cache_path), client=client)
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            for result in cache.values():
                result["hunter_checked_at"] = "2000-01-01"
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
            third = enrich_rows([row.copy()], "unused", str(cache_path), client=client)

        self.assertEqual(first["requested"], 1)
        self.assertEqual(second["cached"], 1)
        self.assertEqual(third["requested"], 1)
        self.assertEqual(client.verifications, 2)

    def test_parse_api_keys_handles_strings_and_lists(self):
        from email_lead_scraper.hunter import parse_api_keys
        self.assertEqual(parse_api_keys("key1, key2 , key3"), ["key1", "key2", "key3"])
        self.assertEqual(parse_api_keys(["key1", "key2,key3"]), ["key1", "key2", "key3"])
        self.assertEqual(
            parse_api_keys('["key1", "key2", "key1"]'),
            ["key1", "key2"],
        )
        self.assertEqual(parse_api_keys(""), [])

    def test_parse_api_keys_rejects_malformed_json_array(self):
        from email_lead_scraper.hunter import parse_api_keys

        with pytest.raises(ValueError, match="valid JSON"):
            parse_api_keys('["key1",]')

    def test_hunter_client_rotates_keys_on_limit(self):
        from email_lead_scraper.hunter import HunterClient
        class DummyResponse:
            def __init__(self, status_code):
                self.status_code = status_code
            def json(self):
                return {"data": {"email": "test@example.com", "status": "valid"}}

        class RotatingSession:
            def __init__(self):
                self.calls = []
            def get(self, url, params=None, headers=None, timeout=None):
                self.calls.append(headers.get("X-API-KEY"))
                if headers.get("X-API-KEY") == "key1":
                    return DummyResponse(403)
                return DummyResponse(200)

        session = RotatingSession()
        client = HunterClient("key1, key2", max_retries=1, session=session)
        result = client.verify("test@example.com")
        self.assertEqual(result.get("status"), "valid")
        self.assertEqual(session.calls, ["key1", "key2"])

    def test_hunter_client_retries_rate_limit_before_rotating(self):
        from email_lead_scraper.hunter import HunterClient

        class Response:
            headers = {"Retry-After": "0"}

            def __init__(self, status_code):
                self.status_code = status_code

            def json(self):
                return {"data": {"email": "test@example.com", "status": "valid"}}

        class Session:
            def __init__(self):
                self.calls = []

            def get(self, _url, params=None, headers=None, timeout=None):
                self.calls.append(headers["X-API-KEY"])
                return Response(403 if headers["X-API-KEY"] == "key1" else 200)

        session = Session()
        client = HunterClient("key1,key2", max_retries=2, session=session)
        with patch("email_lead_scraper.hunter.time.sleep", return_value=None):
            self.assertEqual(client.verify("test@example.com")["status"], "valid")
        self.assertEqual(session.calls, ["key1", "key1", "key2"])

    def test_hunter_client_uses_each_key_once_when_all_are_exhausted(self):
        from email_lead_scraper.hunter import HunterClient

        class LimitedResponse:
            status_code = 429

        class LimitedSession:
            def __init__(self):
                self.calls = []

            def get(self, _url, params=None, headers=None, timeout=None):
                self.calls.append(headers["X-API-KEY"])
                return LimitedResponse()

        session = LimitedSession()
        client = HunterClient('["key1", "key2", "key3"]', session=session)
        with self.assertRaisesRegex(RuntimeError, "All Hunter API keys"):
            client.verify("test@example.com")
        self.assertEqual(session.calls, ["key1", "key2", "key3"])

    def test_preflight_understands_shared_account_credits(self):
        from email_lead_scraper.hunter import HUNTER_ACCOUNT_URL, HunterClient

        class Response:
            status_code = 200

            def json(self):
                return {
                    "data": {
                        "email": "owner@example.com",
                        "team_id": 42,
                        "requests": {"credits": {"remaining": 0}},
                    }
                }

        class Session:
            def __init__(self):
                self.urls = []

            def get(self, url, params=None, headers=None, timeout=None):
                self.urls.append(url)
                return Response()

        session = Session()
        client = HunterClient("key1,key2", session=session)
        client.preflight()
        with self.assertRaisesRegex(RuntimeError, "exhausted their verification credits"):
            client.verify("test@example.com")
        self.assertEqual(session.urls, [HUNTER_ACCOUNT_URL, HUNTER_ACCOUNT_URL])

    def test_verifier_polls_a_pending_response_without_exposing_key(self):
        from email_lead_scraper.hunter import HunterClient

        class Response:
            def __init__(self, status_code, data):
                self.status_code = status_code
                self.data = data

            def json(self):
                return {"data": self.data}

        class Session:
            def __init__(self):
                self.calls = []

            def get(self, url, params=None, headers=None, timeout=None):
                self.calls.append((params, headers))
                if len(self.calls) == 1:
                    return Response(202, {})
                return Response(200, {"email": "test@example.com", "status": "valid"})

        session = Session()
        client = HunterClient("secret-key", max_retries=2, session=session)
        with patch("email_lead_scraper.hunter.time.sleep", return_value=None):
            result = client.verify("test@example.com")
        self.assertEqual(result["status"], "valid")
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(all("api_key" not in params for params, _headers in session.calls))
        self.assertTrue(
            all(headers == {"X-API-KEY": "secret-key"} for _params, headers in session.calls)
        )

    def test_empty_finder_result_does_not_debit_search_credit(self):
        from email_lead_scraper.hunter import (
            HUNTER_ACCOUNT_URL,
            HUNTER_DOMAIN_SEARCH_URL,
            HunterClient,
        )

        class Response:
            status_code = 200

            def __init__(self, data):
                self.data = data

            def json(self):
                return {"data": self.data}

        class Session:
            def get(self, url, params=None, headers=None, timeout=None):
                if url == HUNTER_ACCOUNT_URL:
                    return Response(
                        {
                            "team_id": 1,
                            "requests": {
                                "searches": {"remaining": 1},
                                "verifications": {"remaining": 0},
                            },
                        }
                    )
                if url == HUNTER_DOMAIN_SEARCH_URL:
                    return Response({"emails": [{"value": "ceo@example.com"}]})
                return Response({})

        client = HunterClient("key", session=Session())
        client.preflight()
        self.assertEqual(client.find("Nobody Here", "example.com"), {})
        result = client.domain_search("example.com")
        self.assertEqual(result["emails"][0]["value"], "ceo@example.com")

    def test_row_specific_hunter_error_does_not_stop_later_rows(self):
        from email_lead_scraper.hunter import HunterRowError

        class RowErrorClient(FakeHunterClient):
            def verify(self, email):
                self.verifications += 1
                if email.startswith("bad"):
                    raise HunterRowError("Hunter rejected this lead (400)")
                return {"email": email, "status": "valid", "score": 95}

        rows = [
            {"email": "bad@example.com", "source_url": ""},
            {"email": "good@example.com", "source_url": "https://example.com"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            stats = enrich_rows(
                rows,
                "unused",
                str(Path(directory) / "cache.json"),
                client=RowErrorClient(),
            )
        self.assertEqual(stats["attempted"], 2)
        self.assertEqual(stats["requested"], 1)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["stopped"], "")
        self.assertEqual(rows[0]["hunter_status"], "error")
        self.assertEqual(rows[1]["selected_email"], "good@example.com")


if __name__ == "__main__":
    unittest.main()

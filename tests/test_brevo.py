import csv

from email_lead_scraper.brevo import (
    load_contacts,
    normalize_email,
    parse_list_id,
    send_contact,
    send_contacts_import,
)


class FakeResponse:
    def __init__(self, status_code, headers=None, body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.body = body or {}
        self.text = ""

    def json(self):
        return self.body


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.payloads = []

    def post(self, _url, **kwargs):
        self.payloads.append(kwargs["json"])
        return next(self.responses)


def test_load_contacts_includes_raw_and_selected_and_deduplicates(tmp_path):
    path = tmp_path / "leads.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["email", "selected_email"])
        writer.writeheader()
        writer.writerows([
            {"email": "info@example.com", "selected_email": "CEO@Example.com"},
            {"email": "ceo@example.com", "selected_email": ""},
            {"email": "not-an-email", "selected_email": ""},
        ])
    assert load_contacts(path) == [
        {"email": "info@example.com"},
        {"email": "ceo@example.com"},
    ]


def test_send_contact_updates_and_assigns_list():
    session = FakeSession([FakeResponse(204)])
    send_contact(session, "secret", {"email": "a@example.com"}, 42)
    assert session.payloads == [{
        "email": "a@example.com", "updateEnabled": True, "listIds": [42]
    }]


def test_parse_list_id():
    assert parse_list_id("") is None
    assert parse_list_id("12") == 12


def test_normalize_email_removes_encoded_tab_artifact():
    assert normalize_email("%09Alexa.Dangelo@Phoenix.gov") == "alexa.dangelo@phoenix.gov"
    assert normalize_email("not-an-email") is None


def test_bulk_import_uses_one_request():
    session = FakeSession([FakeResponse(202, body={"processId": 78})])
    contacts = [{"email": "a@example.com"}, {"email": "b@example.com"}]
    assert send_contacts_import(session, "secret", contacts, 32) == 78
    assert session.payloads == [{
        "jsonBody": contacts,
        "listIds": [32],
        "updateExistingContacts": True,
        "disableNotification": False,
    }]

"""Brevo-specific contact synchronization.

Authentication and destination settings are intentionally environment-only so
API credentials are never committed to the repository.
"""

import argparse
import csv
import os
import random
import re
import sys
import time
from urllib.parse import unquote

import requests


BREVO_CONTACTS_URL = "https://api.brevo.com/v3/contacts"
BREVO_IMPORT_URL = "https://api.brevo.com/v3/contacts/import"
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def normalize_email(value):
    """Clean URL-encoded extraction artifacts and validate an email address."""
    email = unquote(value or "").strip().lower()
    if any(ord(char) < 32 or ord(char) == 127 for char in email):
        return None
    return email if EMAIL_RE.fullmatch(email) else None


def parse_list_id(value):
    if not value:
        return None
    try:
        list_id = int(value)
    except ValueError as exc:
        raise ValueError("BREVO_LIST_ID must be a whole number") from exc
    if list_id <= 0:
        raise ValueError("BREVO_LIST_ID must be greater than zero")
    return list_id


def load_contacts(csv_path):
    """Return every unique valid raw or enriched email in the CSV."""
    contacts = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            for field in ("email", "selected_email"):
                email = normalize_email(row.get(field))
                if email:
                    contacts.setdefault(email, {"email": email})
    return list(contacts.values())


def send_contact(session, api_key, contact, list_id=None, max_retries=4):
    payload = {**contact, "updateEnabled": True}
    if list_id is not None:
        payload["listIds"] = [list_id]

    for attempt in range(max_retries + 1):
        response = session.post(
            BREVO_CONTACTS_URL,
            headers={
                "accept": "application/json",
                "api-key": api_key,
                "content-type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        if response.status_code in (201, 204):
            return
        if response.status_code == 429 and attempt < max_retries:
            reset = response.headers.get("x-sib-ratelimit-reset", "1")
            try:
                delay = max(1.0, float(reset))
            except ValueError:
                delay = 2**attempt
            time.sleep(delay + random.random())
            continue

        try:
            detail = response.json().get("message", response.text)
        except ValueError:
            detail = response.text
        if response.status_code == 401 and "unrecognised IP address" in detail:
            raise RuntimeError(
                "Brevo blocked this runner's IP address. In Brevo, open Settings > "
                "Security > Authorized IPs and deactivate unknown-IP blocking for API "
                "keys, or run this workflow from a runner with a fixed authorized IP."
            )
        raise RuntimeError(f"Brevo rejected {contact['email']} ({response.status_code}): {detail}")

    raise RuntimeError(f"Brevo rate limit retries exhausted for {contact['email']}")


def send_contacts_import(session, api_key, contacts, list_id):
    """Submit one asynchronous Brevo import job for all contacts."""
    response = session.post(
        BREVO_IMPORT_URL,
        headers={
            "accept": "application/json",
            "api-key": api_key,
            "content-type": "application/json",
        },
        json={
            "jsonBody": contacts,
            "listIds": [list_id],
            "updateExistingContacts": True,
            "disableNotification": False,
        },
        timeout=30,
    )
    if response.status_code == 202:
        try:
            return response.json().get("processId")
        except ValueError:
            return None

    try:
        detail = response.json().get("message", response.text)
    except ValueError:
        detail = response.text
    if response.status_code == 401 and "unrecognised IP address" in detail:
        raise RuntimeError(
            "Brevo blocked this runner's IP address. In Brevo, open Settings > "
            "Security > Authorized IPs and deactivate unknown-IP blocking for API "
            "keys, or run this workflow from a runner with a fixed authorized IP."
        )
    raise RuntimeError(f"Brevo import rejected ({response.status_code}): {detail}")


def main():
    parser = argparse.ArgumentParser(description="Sync CSV email leads to Brevo Contacts")
    parser.add_argument("csv_path", nargs="?", default="output/leads_today.csv")
    args = parser.parse_args()

    api_key = os.environ.get("BREVO_API_KEY", "").strip()
    if not api_key:
        print("BREVO_API_KEY is not configured; skipping Brevo sync.")
        return 0

    try:
        list_id = parse_list_id(os.environ.get("BREVO_LIST_ID", "").strip())
        contacts = load_contacts(args.csv_path)
    except (OSError, ValueError) as exc:
        print(f"Brevo sync configuration error: {exc}", file=sys.stderr)
        return 2

    if not contacts:
        print("Brevo sync: no valid email addresses found.")
        return 0

    uploaded = 0
    with requests.Session() as session:
        if list_id is not None:
            try:
                process_id = send_contacts_import(session, api_key, contacts, list_id)
            except (requests.RequestException, RuntimeError) as exc:
                print(f"Brevo sync failed: {exc}", file=sys.stderr)
                return 1
            process = f" (process {process_id})" if process_id is not None else ""
            print(
                f"Brevo accepted {len(contacts)} unique contact(s) for list {list_id}{process}."
            )
            return 0

        for contact in contacts:
            try:
                send_contact(session, api_key, contact)
                uploaded += 1
            except (requests.RequestException, RuntimeError) as exc:
                print(f"Brevo sync failed after {uploaded}/{len(contacts)} contacts: {exc}", file=sys.stderr)
                return 1

    destination = f" list {list_id}" if list_id is not None else " Contacts"
    print(f"Brevo sync complete: {uploaded} unique contact(s) sent to{destination}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

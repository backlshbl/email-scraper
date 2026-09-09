#!/usr/bin/env python3
"""Phase 5 storage, tracker, cache, output, and cumulative merge operations.

Merge today's scrape results into a cumulative CSV, deduplicating by email + source_url.

Handles both legacy (grouped ; separated) and new (one row per email) formats.
The output uses the new format: one row per email with person_name, role_title,
and email_type columns.

Usage:
    email-lead-merge output/leads_today.csv output/leads.csv
"""

import csv
import json
import os
import sqlite3
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone

from .extraction import classify_email
from .settings import OUTPUT_FIELDNAMES


# Known generic email prefixes (duplicated from main scraper for standalone use)
_GENERIC_PREFIXES = {
    "info", "contact", "support", "admin", "sales", "hr", "hello",
    "office", "privacy", "legal", "noreply", "no-reply", "billing",
    "enquiries", "inquiries", "reception", "general", "mail",
    "webmaster", "help", "team", "press", "media", "marketing",
    "feedback", "service", "customerservice", "careers", "jobs",
    "accounts", "orders", "bookings", "reservations", "appointments",
}

# Hunter enrichment and selected-email columns carried through the merge.
_ENRICHMENT_FIELDS = (
    "hunter_email", "hunter_status", "hunter_score", "hunter_sources",
    "hunter_checked_at", "hunter_action",
    "selected_email", "selected_email_status", "selected_email_provider",
)


class CrawlCache:
    """Small SQLite cache for fetched pages, redirects, and negative responses."""

    def __init__(self, path):
        self.path = os.fspath(path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS responses (
                url TEXT PRIMARY KEY,
                status_code INTEGER NOT NULL,
                final_url TEXT NOT NULL,
                body TEXT NOT NULL,
                expires_at REAL NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS search_results (
                cache_key TEXT PRIMARY KEY,
                urls_json TEXT NOT NULL,
                expires_at REAL NOT NULL
            )
            """
        )
        self.connection.commit()

    def get(self, url):
        """Return an unexpired cached response dictionary, or ``None``."""
        row = self.connection.execute(
            "SELECT status_code, final_url, body, expires_at FROM responses WHERE url = ?",
            (url,),
        ).fetchone()
        if row is None:
            return None
        status_code, final_url, body, expires_at = row
        if expires_at <= time.time():
            self.connection.execute("DELETE FROM responses WHERE url = ?", (url,))
            self.connection.commit()
            return None
        return {
            "status_code": status_code,
            "final_url": final_url,
            "body": body,
        }

    def set(self, url, status_code, final_url, body, ttl_seconds):
        """Store a cacheable response until its TTL expires."""
        self.connection.execute(
            """
            INSERT INTO responses (url, status_code, final_url, body, expires_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                status_code = excluded.status_code,
                final_url = excluded.final_url,
                body = excluded.body,
                expires_at = excluded.expires_at
            """,
            (
                url,
                int(status_code),
                final_url,
                body,
                time.time() + max(0, float(ttl_seconds)),
            ),
        )
        self.connection.commit()

    def get_search_results(self, cache_key):
        """Return cached search URLs, or ``None`` when absent or expired."""
        row = self.connection.execute(
            "SELECT urls_json, expires_at FROM search_results WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
        if row is None:
            return None
        urls_json, expires_at = row
        if expires_at <= time.time():
            self.connection.execute(
                "DELETE FROM search_results WHERE cache_key = ?",
                (cache_key,),
            )
            self.connection.commit()
            return None
        try:
            urls = json.loads(urls_json)
        except json.JSONDecodeError:
            return None
        return urls if isinstance(urls, list) else None

    def set_search_results(self, cache_key, urls, ttl_seconds):
        """Cache a list of search-result URLs for later runs."""
        self.connection.execute(
            """
            INSERT INTO search_results (cache_key, urls_json, expires_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                urls_json = excluded.urls_json,
                expires_at = excluded.expires_at
            """,
            (
                cache_key,
                json.dumps(list(urls), ensure_ascii=False),
                time.time() + max(0, float(ttl_seconds)),
            ),
        )
        self.connection.commit()

    def close(self):
        """Close the underlying SQLite connection."""
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _classify_email(email):
    """Classify an email as 'personal' or 'generic'."""
    prefix = email.split("@")[0].lower().strip()
    clean = prefix.replace(".", "").replace("-", "").replace("_", "")
    if prefix in _GENERIC_PREFIXES or clean in _GENERIC_PREFIXES:
        return "generic"
    return "personal"


def load_csv(path):
    """Load a CSV file and return list of dicts. Returns [] if file doesn't exist."""
    if not os.path.exists(path):
        return []
    with open(path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_json_cache(path):
    """Load a dictionary-backed JSON cache, returning an empty cache on failure."""
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_json_cache(path, cache):
    """Persist a dictionary-backed JSON cache."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(cache, handle, indent=2, ensure_ascii=False)


def load_tracker(tracker_path):
    """Load previously crawled domains from the tracker JSON file."""
    if not os.path.isfile(tracker_path):
        return {}
    try:
        with open(tracker_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        print(
            f"Loaded tracker: {len(data)} previously crawled domain(s) "
            f"from {tracker_path}"
        )
        return data
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  [!] Could not read tracker ({exc}), starting fresh.", file=sys.stderr)
        return {}


def save_tracker(tracker_path, tracker_data):
    """Save the crawled-sites tracker to disk."""
    os.makedirs(os.path.dirname(tracker_path) or ".", exist_ok=True)
    with open(tracker_path, "w", encoding="utf-8") as handle:
        json.dump(tracker_data, handle, indent=2, ensure_ascii=False)
    print(f"Tracker saved: {len(tracker_data)} domain(s) in {tracker_path}")


def record_crawled_domain(tracker_data, domain):
    """Mark a domain as crawled in the tracker."""
    now = datetime.now(timezone.utc).isoformat()
    if domain in tracker_data:
        tracker_data[domain]["last_crawled"] = now
        tracker_data[domain]["crawl_count"] = tracker_data[domain].get("crawl_count", 1) + 1
    else:
        tracker_data[domain] = {
            "first_crawled": now,
            "last_crawled": now,
            "crawl_count": 1,
        }


def load_existing_emails(output_path, fresh_output=False):
    """Load existing output emails for cross-run deduplication."""
    seen_emails = set()
    existing_rows = 0
    if os.path.isfile(output_path) and not fresh_output:
        try:
            with open(output_path, "r", newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    email_field = row.get("email", "").strip()
                    for email in email_field.split(";"):
                        email = email.strip().lower()
                        if email:
                            seen_emails.add(email)
                    existing_rows += 1
            if existing_rows:
                print(
                    f"Loaded {existing_rows} existing row(s) from {output_path} "
                    "(will skip duplicate emails).\n"
                )
        except Exception as exc:
            print(
                f"  [!] Could not read existing CSV ({exc}), starting fresh.",
                file=sys.stderr,
            )
    return seen_emails, existing_rows


class LeadCollector:
    """Collect and deduplicate extracted lead entries by raw email."""

    def __init__(self, seen_emails=None):
        self.seen_emails = seen_emails if seen_emails is not None else set()
        self.grouped = {}

    def record(self, email_results, source_url, role="", industry="", country=""):
        """Record contextual results or a set returned by deep crawling."""
        if isinstance(email_results, set):
            email_results = [
                {
                    "email": email,
                    "person_name": "",
                    "role_title": "",
                    "email_type": classify_email(email),
                }
                for email in email_results
            ]

        if not email_results and source_url not in self.grouped:
            self.grouped[source_url] = {
                "email_entries": [
                    {
                        "email": "",
                        "person_name": "",
                        "role_title": "",
                        "email_type": "domain_only",
                    }
                ],
                "role_target": role,
                "industry_target": industry,
                "country_target": country,
            }

        for entry in email_results:
            email = entry["email"] if isinstance(entry, dict) else entry
            key = email.lower()
            if key in self.seen_emails:
                continue
            self.seen_emails.add(key)
            if source_url not in self.grouped:
                self.grouped[source_url] = {
                    "email_entries": [],
                    "role_target": role,
                    "industry_target": industry,
                    "country_target": country,
                }
            if isinstance(entry, dict):
                self.grouped[source_url]["email_entries"].append(entry)
            else:
                self.grouped[source_url]["email_entries"].append(
                    {
                        "email": email,
                        "person_name": "",
                        "role_title": "",
                        "email_type": classify_email(email),
                    }
                )

    def rows(self):
        """Return one normalized output row per collected email entry."""
        rows = []
        for source_url, data in self.grouped.items():
            for entry in data["email_entries"]:
                rows.append(
                    {
                        "email": entry["email"],
                        "person_name": entry.get("person_name", ""),
                        "role_title": entry.get("role_title", ""),
                        "email_type": entry.get("email_type", ""),
                        "source_url": source_url,
                        "role_target": data["role_target"],
                        "industry_target": data["industry_target"],
                        "country_target": data["country_target"],
                    }
                )
        rows.sort(
            key=lambda row: (
                0 if row.get("email_type") == "personal" else 1,
                row.get("email", ""),
            )
        )
        return rows

    @property
    def site_count(self):
        return len(self.grouped)


def write_results(rows, output_path, fresh_output=False, existing_rows=0):
    """Write per-run rows, preserving the existing append/fresh behavior."""
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    file_is_new = fresh_output or not os.path.isfile(output_path) or existing_rows == 0
    with open(
        output_path,
        "w" if file_is_new else "a",
        newline="",
        encoding="utf-8",
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDNAMES, extrasaction="ignore")
        if file_is_new:
            writer.writeheader()
        writer.writerows(rows)
    return existing_rows + len(rows)


def merge_results(today_path, cumulative_path, run_date=None):
    """Merge per-run results into the cumulative CSV and return merge statistics."""
    # Ensure output directory exists
    os.makedirs(os.path.dirname(cumulative_path) or ".", exist_ok=True)

    # Load today's results
    today_rows = load_csv(today_path)
    if not today_rows:
        print("No new results to merge.")
        existing_rows = load_csv(cumulative_path)
        return {
            "new": 0,
            "total": len(existing_rows),
            "personal": sum(
                1 for row in existing_rows if row.get("email_type") == "personal"
            ),
            "sites": len(
                {row.get("source_url", "") for row in existing_rows if row.get("source_url")}
            ),
        }

    # Load existing cumulative results
    existing_rows = load_csv(cumulative_path)

    # Track unique emails: (email_lower, source_url) -> row dict
    seen = OrderedDict()

    def add_row(row, date_found=None):
        """Add a single row or expand ;-separated emails into individual rows."""
        source_url = row.get("source_url", "")
        email_field = row.get("email", "").strip()
        role = row.get("role_target", "")
        industry = row.get("industry_target", "")
        country = row.get("country_target", "")
        date_val = date_found or row.get("date_found", "")

        # Handle new-format columns (may be missing in old data)
        person_name = row.get("person_name", "")
        role_title = row.get("role_title", "")
        email_type = row.get("email_type", "")
        enrichment_fields = {
            field: row.get(field, "") for field in _ENRICHMENT_FIELDS
        }

        # Expand ;-separated emails (legacy format) into individual entries
        emails = [e.strip() for e in email_field.split(";") if e.strip()]

        for email in emails:
            key = (email.lower(), source_url)
            if key not in seen:
                # Auto-classify if email_type is missing (old data)
                et = email_type if email_type else _classify_email(email)
                seen[key] = {
                    "email": email,
                    "person_name": person_name if len(emails) == 1 else "",
                    "role_title": role_title if len(emails) == 1 else "",
                    "email_type": et,
                    "source_url": source_url,
                    "role_target": role,
                    "industry_target": industry,
                    "country_target": country,
                    "date_found": date_val,
                    **enrichment_fields,
                }
            else:
                # Update date if newer
                existing = seen[key]
                if date_val and (not existing["date_found"] or date_val > existing["date_found"]):
                    existing["date_found"] = date_val
                # Update person_name/role_title if we now have richer data
                if person_name and not existing["person_name"]:
                    existing["person_name"] = person_name
                if role_title and not existing["role_title"]:
                    existing["role_title"] = role_title
                for field, value in enrichment_fields.items():
                    if value and not existing.get(field):
                        existing[field] = value

    # Process existing cumulative rows first
    for row in existing_rows:
        add_row(row)

    existing_count = len(seen)

    # Process today's rows
    run_date = run_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for row in today_rows:
        add_row(row, date_found=run_date)

    new_count = len(seen) - existing_count

    # Write cumulative CSV — one row per email
    fieldnames = ["email", "person_name", "role_title", "email_type",
                  "source_url", "role_target", "industry_target",
                  "country_target", "date_found",
                  "hunter_email", "hunter_status", "hunter_score",
                  "hunter_sources", "hunter_checked_at", "hunter_action",
                  "selected_email",
                  "selected_email_status", "selected_email_provider"]

    # Sort: personal emails first, then by email
    sorted_rows = sorted(
        seen.values(),
        key=lambda r: (0 if r.get("email_type") == "personal" else 1,
                       r.get("email", "").lower()),
    )

    with open(cumulative_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(sorted_rows)

    total_emails = len(seen)
    personal_count = sum(1 for r in seen.values()
                         if r.get("email_type") == "personal")
    unique_sites = len({r["source_url"] for r in seen.values()})

    print(f"Merged: {new_count} new email(s) added. "
          f"Cumulative: {total_emails} emails ({personal_count} personal) "
          f"across {unique_sites} sites")
    return {
        "new": new_count,
        "total": total_emails,
        "personal": personal_count,
        "sites": unique_sites,
    }


def merge_main(argv=None):
    """Console entry point for cumulative CSV merging."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        print("Usage: email-lead-merge <today.csv> <cumulative.csv>", file=sys.stderr)
        return 1
    merge_results(argv[0], argv[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(merge_main())

"""Phase 4 orchestration for Hunter enrichment."""

import re
import sys

from rapidfuzz import fuzz

from .hunter import enrich_rows as enrich_rows_with_hunter


class EnrichmentError(RuntimeError):
    """Raised when enabled enrichment is missing required configuration."""


def candidate_quality_score(row):
    """Score context quality so limited provider credits reach the best leads first."""
    score = 0
    email = (row.get("email") or "").strip().lower()
    person_name = (row.get("person_name") or "").strip().lower()
    role_title = (row.get("role_title") or "").strip().lower()
    role_target = (row.get("role_target") or "").strip().lower()

    if row.get("email_type") == "personal":
        score += 25
    if person_name:
        score += 15
        if email:
            local_part = re.sub(r"[^a-z0-9]+", " ", email.split("@", 1)[0]).strip()
            score += round(fuzz.token_set_ratio(person_name, local_part) * 0.2)
    if role_title:
        score += 10
    if role_title and role_target:
        score += round(fuzz.token_set_ratio(role_title, role_target) * 0.3)
    elif role_target:
        score += 10
    if row.get("source_url"):
        score += 5
    return min(score, 100)


def rank_candidates(rows):
    """Return the same row objects ordered from richest to weakest context."""
    return sorted(rows, key=candidate_quality_score, reverse=True)


def enrich_leads(rows, settings):
    """Enrich rows in place with Hunter when it is enabled."""
    ranked_rows = rank_candidates(rows)
    if settings.hunter_enabled and rows:
        if not settings.hunter_api_keys:
            raise EnrichmentError(
                "--hunter-enrich requires HUNTER_API_KEY or HUNTER_API_KEYS "
                "in the environment, CLI, or config."
            )
        hunter_stats = enrich_rows_with_hunter(
            ranked_rows,
            settings.hunter_api_keys,
            cache_path=settings.hunter_cache,
            max_requests=settings.hunter_max_requests,
        )
        attempted = hunter_stats.get("attempted", hunter_stats["requested"])
        failed = hunter_stats.get("failed", max(0, attempted - hunter_stats["requested"]))
        http_attempts = hunter_stats.get("http_attempts", attempted)
        print(
            "Hunter enrichment: "
            f"{attempted} attempted, {hunter_stats['requested']} completed, "
            f"{failed} failed, {http_attempts} HTTP call(s), "
            f"{hunter_stats['cached']} cached, "
            f"{hunter_stats['valid']} valid, {hunter_stats['skipped']} skipped."
        )
        if hunter_stats["stopped"]:
            print(
                f"  [!] Hunter stopped safely: {hunter_stats['stopped']}",
                file=sys.stderr,
            )

    return rows

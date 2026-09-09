# Architecture

## Runtime flow

1. `email_lead_scraper.cli` parses command-line arguments.
2. `email_lead_scraper.application` coordinates the complete scraper pipeline.
3. `email_lead_scraper.search` builds 18 simple unquoted rotating queries, reuses one DDGS client with a single fallback on errors or empty results, promotes a successful fallback, and creates bounded adaptive expansions for weak results.
4. `email_lead_scraper.application` continuously queues discovered domains, caches only non-empty searches, stops discovery after three consecutive failed or empty queries, and still finishes active crawls before the normal save path.
5. `email_lead_scraper.crawling` uses one async HTTPX client and a global request semaphore. Each domain remains sequential, adaptively rate-limited, and robots-aware; all 35 configured probe paths remain eligible. A per-domain baseline detects soft-404 templates.
6. `email_lead_scraper.extraction` extracts, classifies, contextualizes, fuzzy-matches roles, normalizes addresses, and validates unrelated mail domains concurrently.
7. `email_lead_scraper.enrichment` ranks candidates by name/role evidence, then runs Hunter within its request budget.
8. `email_lead_scraper.storage` persists CSV output, SQLite page/search caches, provider caches, and tracker state.
9. `email_lead_scraper.brevo` synchronizes CSV contacts to Brevo.

## Repository layout

```text
config/                    Runtime configuration and direct-crawl inputs
docs/                      Architecture and maintenance documentation
output/                    Generated CSV and persistent JSON state
scripts/                   PowerShell developer and automation helpers
src/email_lead_scraper/    Installable application package
  application.py           Pipeline orchestration
  settings.py              Constants and resolved runtime settings
  search.py                Phase 1 search discovery
  crawling.py              Phase 2 page fetching and traversal
  extraction.py            Phase 3 lead extraction and filtering
  enrichment.py            Phase 4 provider orchestration
  hunter.py                Hunter API provider
  storage.py               Phase 5 CSV, cache, and tracker persistence
  brevo.py                 Brevo contact integration
tests/                     Automated tests mirroring package behavior
.github/workflows/         CI and scheduled/manual automation
```

## State files

The application writes cumulative and per-run CSV files to `output/`. Provider caches and the crawled-site tracker are JSON files in the same directory. HTTP pages and DDGS result lists share `output/crawl_cache.sqlite`; it is ignored by Git and restored between GitHub Actions runs through `actions/cache`.

## Concurrency and coverage

`application.py` creates one shared `AsyncCrawler` and schedules crawl tasks as
each search completes. A semaphore caps actual in-flight HTTP requests at ten
by default; sleeping or slowly queued websites do not reserve a whole crawler
slot. A separate lock and adaptive delay timestamp serialize requests to each
domain. Candidate scoring only changes fetch order: linked pages and all 35
configured probe paths are deduplicated and kept.

Search is intentionally sequential. A reusable DDGS client contacts only one
configured backend at a time and permits one fallback attempt. Valid empty
results are cached without retry. A three-failure circuit breaker prevents a
shared GitHub runner from spending the rest of its time budget in a retry storm.

Behavioral limitations intentionally preserved are listed in
[`DEFERRED_REPAIRS.md`](DEFERRED_REPAIRS.md).

# Email Lead Scraper

Finds publicly-listed business contact emails from company/clinic websites based on target role, industry, and country. It uses bounded **DDGS metasearch** with explicit free backends, so no search API key is needed.

## Setup

```powershell
.\scripts\bootstrap.ps1
.\.venv\Scripts\Activate.ps1
```

The bootstrap script creates an ignored local `.venv`, installs the project in
editable mode, and installs the test/lint tools. For a runtime-only install,
use `python -m pip install -r requirements.txt`.

### Project layout

Application code lives in `src/email_lead_scraper/`, automated tests live in
`tests/`, runtime inputs live in `config/`, and generated state remains under
`output/`. See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the component
map.

The package uses one flat module per phase: `search.py`, `crawling.py`,
`extraction.py`, `enrichment.py`, and `storage.py`. `application.py` coordinates
the phases, while `hunter.py` and `brevo.py` isolate external APIs.

The behavior changes intentionally postponed during the organization refactor
are recorded in [`docs/DEFERRED_REPAIRS.md`](docs/DEFERRED_REPAIRS.md).

## Usage

### Mode A — Search by role / industry / country (free, no API key)

```bash
email-lead-scraper \
    --roles "Founder" "Clinic Director" \
    --industries "Hospital" "Clinic" \
    --countries "United States" "United Kingdom" \
    --max-sites 30 \
    --output leads.csv
```

### Mode B — Crawl specific websites directly

```bash
email-lead-scraper \
    --websites "https://example-clinic.com" "https://another-hospital.co.uk" \
    --output leads.csv
```

### Mode C — Use a config file (recommended for automation)

Edit `config/scraper.json` with your search parameters:

```json
{
    "roles": ["Founder", "Clinic Director", "CEO"],
    "industries": ["Hospital", "Clinic", "Healthcare"],
    "countries": ["United States", "United Kingdom"],
    "max_sites": 30,
    "crawl_delay": 0.75,
    "search_delay": 3.0,
    "search_backends": ["duckduckgo", "brave"],
    "search_timeout": 5.0,
    "search_retry_delay": 1.0,
    "search_failure_threshold": 3,
    "search_max_queries": 30,
    "search_min_results": 8,
    "site_concurrency": 10,
    "request_timeout": 8.0,
    "site_crawl_timeout": 90.0,
    "max_robots_crawl_delay": 10.0,
    "crawl_cache_enabled": true,
    "crawl_cache": "output/crawl_cache.sqlite",
    "search_cache_ttl_hours": 24,
    "check_mx": true,
    "mx_concurrency": 8
}
```

Then run:

```bash
email-lead-scraper --config config/scraper.json --output leads.csv
```

CLI arguments override config values, so you can use the config as defaults.

## Optional Hunter Email Enrichment

Hunter checks scraped addresses and can use Email Finder or Domain Search when
enough person/company context is available. It records the provider response,
score, sources, action, and any selected address in the output CSV. Only an
address explicitly returned with Hunter status `valid` can become the selected
email. Finder and Domain Search confidence scores are never treated as proof of
deliverability.

```bash
export HUNTER_API_KEYS='["hunter-key-1","hunter-key-2"]'
email-lead-scraper --config config/scraper.json --hunter-enrich \
  --hunter-max-requests 50 --fresh-output --output output/leads_today.csv
```

On PowerShell, use
`$env:HUNTER_API_KEYS = '["hunter-key-1","hunter-key-2"]'`. For GitHub
Actions, create a repository secret named `HUNTER_API_KEYS` containing the
same JSON array; the daily workflow enables enrichment automatically when it
exists.

Hunter keys are used in array order. Hunter first makes a free
Account Information request for each key, rejects invalid keys, reads remaining
usage, and recognizes keys sharing the same account quota. A `403` rate
limit is retried with bounded backoff; a `429` usage limit moves to the next
key or account with suitable credits. Hunter keys are sent in the supported
`X-API-KEY` header instead of the URL. Temporary network and server errors retry
the current key, and processing stops safely when every key is unavailable.
Duplicate keys are removed while preserving their first position. Legacy
`HUNTER_API_KEY` value remains supported as a fallback. The Hunter command-line
budget stays a total per-run limit across all keys.

Hunter does not spend a verification credit on a scraped generic mailbox such
as `info@...`, because generic mailboxes cannot become personal selected leads.
A missing Finder result can continue to Domain Search, pending `202`
verifications are polled a bounded number of times, and an error concerning one
lead no longer stops every remaining lead. Stable cache results expire after 30
days; uncertain or empty results expire after 7 days so temporary uncertainty is
not reused forever.

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--roles` | — | Target job roles (e.g. "Founder", "Director") |
| `--industries` | — | Target industries (e.g. "Hospital", "Clinic") |
| `--countries` | — | Target countries (e.g. "United States") |
| `--websites` | — | Direct list of URLs to crawl |
| `--websites-file` | — | Text file with one URL per line |
| `--config` | — | JSON config file with search parameters |
| `--max-sites` | 30 | Max websites per search query |
| `--output` | leads.csv | Output CSV file path |
| `--fresh-output` | off | Replace the per-run output instead of appending |
| `--site-concurrency` | 10 | Maximum simultaneous requests across unrelated domains |
| `--request-timeout` | 8.0s | Maximum wait for one website response |
| `--site-crawl-timeout` | 90.0s | Maximum total time allowed for one search-result website |
| `--max-robots-crawl-delay` | 10.0s | Skip a website rather than wait longer than this robots.txt crawl delay |
| `--crawl-cache` | output/crawl_cache.sqlite | Reusable SQLite page cache |
| `--no-crawl-cache` | off | Disable the page cache |
| `--crawl-cache-ttl-hours` | 24 | Cache lifetime for successful pages |
| `--negative-cache-ttl-hours` | 168 | Cache lifetime for missing pages such as 404s |
| `--search-cache-ttl-hours` | 24 | Cache lifetime for successful, non-empty DDGS results |
| `--search-backends` | duckduckgo brave | Primary DDGS backend and optional fallback for errors or empty results |
| `--search-timeout` | 5.0s | Timeout for one DDGS backend request |
| `--search-retry-delay` | 1.0s | Delay before the single fallback-backend attempt |
| `--search-failure-threshold` | 3 | Consecutive failed or empty queries before discovery stops safely |
| `--search-max-queries` | 30 | Maximum combined plus adaptive searches per run |
| `--search-min-results` | 8 | Weak base-query threshold for adaptive expansion |
| `--skip-mx-check` | off | Skip email-domain deliverability checks |
| `--mx-timeout` | 3.0s | Timeout for DNS/MX lookups |
| `--mx-concurrency` | 8 | Number of unrelated email domains checked together |
| `--hunter-enrich` | off | Verify and enrich candidates using ordered `HUNTER_API_KEYS` |
| `--hunter-max-requests` | 50 | Maximum Hunter Finder/Search/Verifier operations attempted per run; free preflight calls and verifier polling are excluded |
| `--crawl-delay` | 0.75s | Base delay between page requests on the same site |
| `--search-delay` | 3.0s | Base delay between live DDGS searches |
| `--ignore-robots` | off | Skip robots.txt checking |

## GitHub Actions — Daily Automation

This repo includes a GitHub Actions workflow (`.github/workflows/scrape_emails.yml`) that:

1. **Runs daily** at 06:00 UTC (11:30 AM IST)
2. **Searches** explicit DDGS backends using the bounded policy in `config/scraper.json`
3. **Enriches** with Hunter when API keys are configured
4. **Merges** new results into `output/leads.csv` (cumulative, deduplicated)
5. **Syncs** contacts from the daily CSV to Brevo using the current sync rules
6. **Commits** the updated CSV back to the repo automatically

### Setup for GitHub Actions

1. Push this repo to GitHub
2. Go to **Settings → Actions → General → Workflow permissions**
3. Select **"Read and write permissions"**
4. (Optional) Add `HUNTER_API_KEYS` as a repository secret containing a JSON array in priority order, such as `["key-1","key-2"]`. The old singular `HUNTER_API_KEY` secret remains supported as a fallback.
5. Add your Brevo API key as a repository secret named `BREVO_API_KEY`
6. (Optional) Add a repository variable named `BREVO_LIST_ID` with the numeric ID of the Brevo list that should receive the contacts. Without it, contacts are added to the main Contacts database.
7. That's it — the workflow runs automatically every day, or you can trigger it manually from the **Actions** tab

The Brevo sync is idempotent: existing contacts are updated and added to the
configured list, so rerunning a workflow does not create duplicates. API keys
remain in GitHub Actions secrets and are never written to the CSV or repository.

To test the uploader locally in PowerShell:

```powershell
$env:BREVO_API_KEY = "your-key"
$env:BREVO_LIST_ID = "123"
email-lead-brevo-sync output/leads_today.csv
```

### Brevo-only manual sync

To sync all saved leads without running the scraper, open **GitHub → Actions →
Sync Leads to Brevo → Run workflow**. This separate workflow reads the
cumulative `output/leads.csv` file and safely updates existing Brevo contacts.

### Customizing the Schedule

Edit the cron expression in `.github/workflows/scrape_emails.yml`:

```yaml
schedule:
  - cron: '0 6 * * *'  # 06:00 UTC daily
```

### Output Files

| File | Description |
|------|-------------|
| `output/leads.csv` | Cumulative results (all emails ever found, deduplicated) |
| `output/leads_today.csv` | Results from the most recent run only |

The cumulative CSV includes a `date_found` column so you can track when each email was first discovered.

## How It Works

1. Builds one simple, unquoted industry query per role and daily location (18 with the default config)
2. Reuses one DDGS client, tries DuckDuckGo first, and allows at most one Brave fallback when a result fails or is empty
3. Adds balanced, narrower industry searches only when base queries return fewer than eight results, with a hard cap of 30 total searches
4. Promotes a successful fallback backend for later searches and stops discovery after three consecutive failed or empty queries
5. Finishes active crawls and follows the normal save path when the search circuit breaker opens
6. Caches only successful non-empty searches and rejects social networks, directories, and obvious non-HTML files
7. Normalizes `www`/non-`www` hosts so the same domain is queued once
8. Ranks promising website URLs without discarding crawlable search results
9. Shares up to ten HTTP request slots across unrelated domains
10. Keeps requests to each individual domain sequential, rate-limited, and adaptively slowed after throttling
11. Checks the homepage, linked contact/team pages, and all 35 configured probe paths
12. Detects soft-404 templates so fake successful pages are not processed as team pages
13. Reuses successful, missing-page, and search responses from a SQLite cache
14. Extracts emails from normal, obfuscated, and Cloudflare-protected forms
15. Uses fuzzy role context to retain relevant contacts despite small wording differences
16. Normalizes addresses and validates unrelated mail domains concurrently
17. Ranks richer name/role candidates before spending limited Hunter requests
18. Deduplicates and writes results to CSV, then optionally syncs through Brevo

The crawler deliberately preserves complete 35-path coverage. Ranking changes
the order in which pages are checked; it does not reduce the list to 5–8 pages.
The default campaign now starts with 18 queries such as
`Founder (Hospital OR Clinic OR Healthcare) New Jersey`. Search discovery does
not require email or team-page terms because the crawler probes those pages on
each discovered domain. Weak queries expand adaptively, never beyond 30 total
searches. Website crawling still overlaps with search.
Slow websites do not reserve a crawler slot while merely waiting, and repeated
runs can reuse search and page responses. Each individual website still
receives only one sequential request stream.

## Legal / Ethical Notes

- Only collects emails already published publicly on business websites
- Respects `robots.txt` by default
- Rate-limited to avoid hammering servers
- Comply with CAN-SPAM (US) / GDPR (EU) before emailing contacts

## Development checks

Run the same lint and test checks used by CI:

```powershell
.\scripts\check.ps1
```

CI runs the package on Python 3.10 and 3.12 on both Ubuntu and Windows.

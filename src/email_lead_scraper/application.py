"""Application orchestration for the search-to-storage lead pipeline."""

import asyncio
import random
import sys
from urllib.parse import urlparse

from . import crawling, search, storage
from .enrichment import EnrichmentError, enrich_leads
from .extraction import validate_lead_rows
from .settings import SettingsError, from_namespace


async def _run_direct_sites(settings, collector, crawler):
    if not settings.has_direct_sites:
        return
    print(
        f"Deep-crawling {len(settings.direct_sites)} website(s) "
        f"(up to {settings.max_pages} pages each)...\n"
    )
    targets = []
    seen_domains = set()
    for site_url in settings.direct_sites:
        if not site_url.startswith("http"):
            site_url = "https://" + site_url
        parsed = urlparse(site_url)
        root_url = f"{parsed.scheme}://{parsed.netloc}/"
        domain = search.normalized_domain(root_url)
        if domain in seen_domains:
            print(f"  [skip] Duplicate direct website: {domain}")
            continue
        seen_domains.add(domain)
        print(f"  Deep-crawling {parsed.netloc} (starting from {root_url})")
        targets.append(root_url)

    for root_url, emails in await crawler.deep_crawl_many(
        targets,
        role_filters=settings.filter_roles,
        max_pages=settings.max_pages,
    ):
        collector.record(emails, root_url)
    print()


def _new_crawl_stats():
    return {
        "successful": 0,
        "empty_retryable": 0,
        "irrelevant": 0,
        "skipped_previous": 0,
        "skipped_this_run": 0,
    }


async def _run_search(settings, collector, crawler):
    if not settings.has_search_criteria:
        return

    base_queries, rotation_info = search.build_queries(
        settings.roles,
        settings.industries,
        settings.countries,
        locations_per_day=settings.locations_per_day,
    )
    base_queries = base_queries[:settings.search_max_queries]
    print(
        f"Built {len(base_queries)} combined base search queries "
        f"(adaptive cap: {settings.search_max_queries}, DDGS backends: "
        f"{' -> '.join(settings.search_backends)})."
    )
    print("Today's location rotation:")
    for country, locations in rotation_info.items():
        print(f"  {country}: {', '.join(locations)}")
    print()

    if settings.reset_tracker:
        tracker_data = {}
        print("Tracker reset - all sites will be re-crawled.\n")
    else:
        tracker_data = storage.load_tracker(settings.tracker_path)

    tracker_keys = {
        search.normalized_domain(domain): domain
        for domain in tracker_data
        if search.normalized_domain(domain)
    }
    seen_sites = set()
    skipped_previously = 0
    crawl_stats = _new_crawl_stats()
    crawl_tasks = []
    progress = {"queued": 0, "completed": 0}
    search_stats = {
        "attempted": 0,
        "success": 0,
        "empty": 0,
        "failed": 0,
        "cached": 0,
        "adaptive": 0,
    }
    consecutive_unproductive = 0
    circuit_open = False
    underperforming = []

    async def crawl_target(site_url, role, industries, country, domain):
        industry_label = " | ".join(industries)
        try:
            email_results = await asyncio.wait_for(
                crawler.crawl_site(
                    site_url,
                    role_filters=settings.filter_roles,
                    industry_target=industries,
                    country_target=country,
                ),
                timeout=settings.site_crawl_timeout,
            )
        except TimeoutError:
            print(
                f"    [timeout] Stopped {domain} after "
                f"{settings.site_crawl_timeout:g}s; retry in a future run"
            )
            email_results = []
        except Exception as exc:
            print(f"    [!] Crawl failed for {site_url}: {exc}")
            email_results = []

        if email_results is None:
            tracker_key = tracker_keys.get(domain, domain)
            storage.record_crawled_domain(tracker_data, tracker_key)
            tracker_keys[domain] = tracker_key
            crawl_stats["irrelevant"] += 1
        elif email_results:
            personal_count = sum(
                1 for row in email_results if row.get("email_type") == "personal"
            )
            print(
                f"    [found] {len(email_results)} email(s) "
                f"({personal_count} personal, "
                f"{len(email_results) - personal_count} generic) on {domain}"
            )
            crawl_stats["successful"] += 1
            collector.record(email_results, site_url, role, industry_label, country)
            tracker_key = tracker_keys.get(domain, domain)
            storage.record_crawled_domain(tracker_data, tracker_key)
            tracker_keys[domain] = tracker_key
        else:
            print(f"    [retry] No usable emails on {domain}; retry in a future run")
            crawl_stats["empty_retryable"] += 1
            collector.record(email_results, site_url, role, industry_label, country)

        progress["completed"] += 1
        print(
            f"    [crawl progress] {progress['completed']} completed / "
            f"{progress['queued']} queued so far"
        )

    async def discover(query, search_client):
        backend_key = ",".join(settings.search_backends)
        cache_key = f"ddg:v3:{backend_key}:{settings.max_sites}:{query}"
        if crawler.cache is not None:
            cached = crawler.cache.get_search_results(cache_key)
            if cached:
                return search.SearchOutcome(
                    cached,
                    "success",
                    backend="cache",
                    attempted_backends=("cache",),
                ), True
        outcome = await asyncio.to_thread(
            search.duckduckgo_search,
            query,
            max_results=settings.max_sites,
            client=search_client,
        )
        if not isinstance(outcome, search.SearchOutcome):
            outcome = search.SearchOutcome(
                list(outcome or []),
                "success" if outcome else "empty",
            )
        if crawler.cache is not None and outcome.status == "success":
            crawler.cache.set_search_results(
                cache_key,
                outcome.urls,
                settings.search_cache_ttl_hours * 3600,
            )
        return outcome, False

    async def process_query(spec, phase, search_client):
        nonlocal circuit_open, consecutive_unproductive, skipped_previously
        query, role, industries, country, _location = spec
        search_stats["attempted"] += 1
        if phase == "adaptive":
            search_stats["adaptive"] += 1
        print(
            f"[{search_stats['attempted']}/{settings.search_max_queries} max] "
            f"Searching ({phase}): {query}"
        )
        outcome, from_cache = await discover(query, search_client)
        site_urls = search.rank_candidate_websites(
            outcome.urls,
            role,
            industries,
            country,
        )

        if from_cache:
            search_stats["cached"] += 1
        if outcome.status == "failed":
            search_stats["failed"] += 1
        else:
            search_stats[outcome.status] += 1
        if outcome.status == "success":
            consecutive_unproductive = 0
        else:
            consecutive_unproductive += 1

        notes = []
        if from_cache:
            notes.append("cached")
        elif outcome.attempted_backends:
            notes.append(" -> ".join(outcome.attempted_backends))
        elif outcome.backend:
            notes.append(outcome.backend)
        if outcome.status == "failed":
            notes.append("search failed")
        elif outcome.status == "empty":
            notes.append("valid empty result")
        note = f" ({', '.join(notes)})" if notes else ""
        print(f"  -> {len(site_urls)} candidate sites{note}")

        if phase == "base" and outcome.status != "failed" and (
            len(site_urls) < settings.search_min_results
        ):
            underperforming.append((len(site_urls), spec))

        for site_url in site_urls:
            domain = search.normalized_domain(site_url)
            if domain in seen_sites:
                print(f"  [skip] Already crawled {domain} (this run)")
                crawl_stats["skipped_this_run"] += 1
                continue
            tracker_key = tracker_keys.get(domain)
            if tracker_key is not None:
                print(
                    f"  [skip] Already crawled {domain} "
                    f"(previous run: "
                    f"{tracker_data[tracker_key].get('last_crawled', 'unknown')})"
                )
                skipped_previously += 1
                crawl_stats["skipped_previous"] += 1
                continue

            seen_sites.add(domain)
            progress["queued"] += 1
            print(f"  [queue] {site_url}")
            crawl_tasks.append(
                asyncio.create_task(
                    crawl_target(site_url, role, industries, country, domain)
                )
            )

        if consecutive_unproductive >= settings.search_failure_threshold:
            circuit_open = True
            print(
                f"\n  [circuit breaker] {consecutive_unproductive} consecutive "
                "searches produced no usable sites after backend fallback. "
                "Stopping discovery, finishing active crawls, and saving partial "
                "results."
            )
            return False

        if not from_cache and search_stats["attempted"] < settings.search_max_queries:
            delay = settings.search_delay + random.uniform(0, 0.5)
            print(f"  (next search in {delay:.1f}s; website crawling continues...)")
            await asyncio.sleep(delay)
        return True

    with search.DDGSSearchClient(
        backends=settings.search_backends,
        timeout=settings.search_timeout,
        retry_delay=settings.search_retry_delay,
    ) as search_client:
        for spec in base_queries:
            if not await process_query(spec, "base", search_client):
                break

        if not circuit_open:
            remaining = settings.search_max_queries - search_stats["attempted"]
            adaptive_queries = search.build_adaptive_queries(
                underperforming,
                remaining,
            )
            if adaptive_queries:
                print(
                    f"\nRunning {len(adaptive_queries)} adaptive search(es) for "
                    f"base queries with fewer than {settings.search_min_results} results."
                )
            for spec in adaptive_queries:
                if not await process_query(spec, "adaptive", search_client):
                    break

    print("\nSearch summary:")
    print(f"  Attempted: {search_stats['attempted']} / {settings.search_max_queries} max")
    print(f"  Successful: {search_stats['success']}")
    print(f"  Valid empty: {search_stats['empty']}")
    print(f"  Failed: {search_stats['failed']}")
    print(f"  Cached: {search_stats['cached']}")
    print(f"  Adaptive: {search_stats['adaptive']}")
    print(f"  Circuit breaker opened: {'yes' if circuit_open else 'no'}")

    if crawl_tasks:
        print(
            f"\nSearch discovery finished. Waiting for "
            f"{progress['queued'] - progress['completed']} active website crawl(s)..."
        )
        await asyncio.gather(*crawl_tasks)

    storage.save_tracker(settings.tracker_path, tracker_data)
    if skipped_previously:
        print(f"  Skipped {skipped_previously} site(s) already crawled in previous runs.")
    print("\nCrawl summary:")
    print(f"  Successful sites: {crawl_stats['successful']}")
    print(f"  Empty/failed (retry later): {crawl_stats['empty_retryable']}")
    print(f"  Irrelevant (permanently tracked): {crawl_stats['irrelevant']}")
    print(f"  Skipped from previous runs: {crawl_stats['skipped_previous']}")
    print(f"  Duplicate sites this run: {crawl_stats['skipped_this_run']}")


async def _collect_rows(settings, collector):
    cache_path = settings.crawl_cache_path if settings.crawl_cache_enabled else None
    async with crawling.AsyncCrawler(
        crawl_delay=settings.crawl_delay,
        respect_robots=settings.respect_robots,
        site_concurrency=settings.site_concurrency,
        cache_path=cache_path,
        cache_ttl_hours=settings.crawl_cache_ttl_hours,
        negative_cache_ttl_hours=settings.negative_cache_ttl_hours,
        request_timeout=settings.request_timeout,
        max_robots_crawl_delay=settings.max_robots_crawl_delay,
    ) as crawler:
        await _run_direct_sites(settings, collector, crawler)
        await _run_search(settings, collector, crawler)
    return collector.rows()


def run(settings):
    """Run the configured async pipeline and return a process exit code."""
    settings.validate()
    seen_emails, existing_rows = storage.load_existing_emails(
        settings.output_path,
        fresh_output=settings.fresh_output,
    )
    collector = storage.LeadCollector(seen_emails)

    rows = asyncio.run(_collect_rows(settings, collector))
    rows, validation_stats = validate_lead_rows(
        rows,
        check_mx=settings.check_mx,
        timeout=settings.mx_timeout,
        max_workers=settings.mx_concurrency,
    )
    print(
        "Email validation: "
        f"{validation_stats['normalized']} normalized, "
        f"{validation_stats['invalid_syntax']} invalid syntax, "
        f"{validation_stats['invalid_domain']} without deliverable domains."
    )
    enrich_leads(rows, settings)
    rows = [row for row in rows if row.get("email") or row.get("selected_email")]
    total_in_file = storage.write_results(
        rows,
        settings.output_path,
        fresh_output=settings.fresh_output,
        existing_rows=existing_rows,
    )

    personal_total = sum(1 for row in rows if row.get("email_type") == "personal")
    if rows:
        print(
            f"\nDone. {len(rows)} NEW email(s) ({personal_total} personal) "
            f"across {collector.site_count} site(s) appended to {settings.output_path} "
            f"({total_in_file} total rows in file)."
        )
    else:
        print(
            f"\nDone. No new emails found. {settings.output_path} has "
            f"{total_in_file} row(s) from previous runs."
        )
    return 0


def run_from_args(args):
    """Resolve settings and run the application for a parsed CLI namespace."""
    try:
        settings = from_namespace(args)
        return run(settings)
    except (SettingsError, EnrichmentError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

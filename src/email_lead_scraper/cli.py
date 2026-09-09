"""Command-line parsing for Email Lead Scraper."""

import argparse

from . import __version__
from .application import run_from_args


def build_parser() -> argparse.ArgumentParser:
    """Build the public command-line interface."""
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description=(
            "Scrape public business contact emails by role/industry/country "
            "or from a direct website list."
        ),
        epilog=(
            "Examples:\n"
            "  email-lead-scraper --config config/scraper.json\n"
            "  email-lead-scraper --websites https://example.com --fresh-output"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    targets = parser.add_argument_group("search targets")
    targets.add_argument("--roles", nargs="+", help="Target job roles")
    targets.add_argument("--industries", nargs="+", help="Target industries")
    targets.add_argument("--countries", nargs="+", help="Target countries")
    targets.add_argument("--websites", nargs="+", help="Website URLs to crawl directly")
    targets.add_argument("--websites-file", help="Text file containing one website URL per line")
    targets.add_argument("--filter-roles", nargs="+", help="Only keep emails matching these roles")
    targets.add_argument("--config", help="JSON configuration file")

    crawling = parser.add_argument_group("search and crawling")
    crawling.add_argument("--max-sites", type=int, help="Maximum results per search query")
    crawling.add_argument("--max-pages", type=int, help="Maximum pages per directly supplied site")
    crawling.add_argument("--crawl-delay", type=float, help="Seconds between page requests")
    crawling.add_argument("--search-delay", type=float, help="Seconds between web searches")
    crawling.add_argument(
        "--search-backends",
        nargs="+",
        help="Primary DDGS backend and optional fallback for failures or empty results",
    )
    crawling.add_argument(
        "--search-timeout",
        type=float,
        help="Maximum seconds for a DDGS backend request",
    )
    crawling.add_argument(
        "--search-retry-delay",
        type=float,
        help="Seconds before the one allowed fallback-backend attempt",
    )
    crawling.add_argument(
        "--search-failure-threshold",
        type=int,
        help="Consecutive failed or empty queries before search discovery stops",
    )
    crawling.add_argument(
        "--search-max-queries",
        type=int,
        help="Maximum combined plus adaptive searches per run (maximum 30)",
    )
    crawling.add_argument(
        "--search-min-results",
        type=int,
        help="Base-query result count below which an adaptive search is eligible",
    )
    crawling.add_argument(
        "--site-concurrency",
        type=int,
        help="Maximum simultaneous requests across unrelated domains",
    )
    crawling.add_argument(
        "--request-timeout",
        type=float,
        help="Maximum seconds to wait for a website response",
    )
    crawling.add_argument(
        "--site-crawl-timeout",
        type=float,
        help="Maximum total seconds allowed for one search-result website crawl",
    )
    crawling.add_argument(
        "--max-robots-crawl-delay",
        type=float,
        help="Skip a site when robots.txt requests a longer delay than this many seconds",
    )
    crawling.add_argument(
        "--ignore-robots",
        action="store_true",
        help="Do not consult robots.txt before crawling",
    )
    crawling.add_argument("--tracker-file", help="Path to the crawled-domain tracker JSON")
    crawling.add_argument(
        "--reset-tracker",
        action="store_true",
        help="Ignore prior tracker entries for this run",
    )
    crawling.add_argument(
        "--locations-per-day",
        type=int,
        help="Locations selected per country in the daily search rotation",
    )
    crawling.add_argument("--crawl-cache", help="SQLite crawl-cache path")
    crawling.add_argument(
        "--no-crawl-cache",
        action="store_true",
        help="Disable the reusable HTTP crawl cache",
    )
    crawling.add_argument(
        "--crawl-cache-ttl-hours",
        type=float,
        help="Hours successful pages remain cached",
    )
    crawling.add_argument(
        "--negative-cache-ttl-hours",
        type=float,
        help="Hours missing pages remain cached",
    )
    crawling.add_argument(
        "--search-cache-ttl-hours",
        type=float,
        help="Hours DDGS results remain cached",
    )

    validation = parser.add_argument_group("email validation")
    validation.add_argument(
        "--skip-mx-check",
        action="store_true",
        help="Skip DNS/MX deliverability checks",
    )
    validation.add_argument(
        "--mx-timeout",
        type=float,
        help="DNS timeout in seconds for MX checks",
    )
    validation.add_argument(
        "--mx-concurrency",
        type=int,
        help="Number of email domains validated concurrently",
    )

    output = parser.add_argument_group("output")
    output.add_argument("--output", default="leads.csv", help="Destination CSV path")
    output.add_argument(
        "--fresh-output",
        action="store_true",
        help="Replace the output instead of appending new rows",
    )

    hunter = parser.add_argument_group("Hunter enrichment")
    hunter.add_argument("--hunter-enrich", action="store_true", help="Enable Hunter enrichment")
    hunter.add_argument(
        "--hunter-api-key",
        "--hunter-api-keys",
        dest="hunter_api_keys",
        help="Ordered Hunter key array; prefer the HUNTER_API_KEYS environment variable",
    )
    hunter.add_argument("--hunter-max-requests", type=int, help="Hunter request budget per run")
    hunter.add_argument("--hunter-cache", help="Hunter JSON cache path")

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse command-line arguments and run the application."""
    args = build_parser().parse_args(argv)
    return run_from_args(args)


__all__ = ["build_parser", "main"]

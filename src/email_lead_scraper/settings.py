"""Shared constants and validated runtime settings."""

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any


class SettingsError(ValueError):
    """Raised when command-line or configuration settings are invalid."""


def parse_api_keys(value: Any) -> list[str]:
    """Return unique API keys in the exact priority order supplied.

    JSON arrays are the canonical environment/CLI format. Legacy single-key,
    comma-separated, and newline-separated values remain supported so existing
    installations continue to work during migration.
    """
    raw_keys: list[str] = []

    def collect(candidate: Any) -> None:
        if candidate is None or candidate is False:
            return
        if isinstance(candidate, (list, tuple)):
            for item in candidate:
                collect(item)
            return

        text = str(candidate).strip()
        if not text:
            return
        if text.startswith("["):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError as exc:
                raise SettingsError(
                    "API key arrays must be valid JSON, for example "
                    "[\"key-1\",\"key-2\"]"
                ) from exc
            if not isinstance(decoded, list):
                raise SettingsError("API key JSON must contain an array")
            if any(not isinstance(item, str) for item in decoded):
                raise SettingsError("Every API key in the JSON array must be a string")
            collect(decoded)
            return

        for item in re.split(r"[,;\r\n]+", text):
            key = item.strip()
            if key:
                raw_keys.append(key)

    collect(value)
    return list(dict.fromkeys(raw_keys))


EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
)


EMAIL_BLOCKLIST_PATTERNS = [
    "example.com", "yourdomain", "domain.com", "sentry.io", "wixpress",
    "godaddy", "@2x", "test@", "noreply@", "no-reply@",
]


EMAIL_DOMAIN_BLOCKLIST = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "mail.com", "protonmail.com", "zoho.com", "yandex.com",
    "live.com", "msn.com", "comcast.net", "sbcglobal.net",
}


CONTACT_PAGE_HINTS = [
    "contact", "contact-us", "about", "about-us", "team", "our-team",
    "staff", "leadership", "management", "meet-the-team",
    "providers", "physicians", "doctors", "our-doctors", "our-providers",
    "leadership-team", "our-leadership", "executives", "board",
    "board-of-directors", "people", "faculty", "directory",
    "meet-our-team", "who-we-are", "senior-management", "c-suite",
    "founders", "partners", "associates", "consultants",
    "medical-staff", "clinical-team", "our-physicians",
]


GENERIC_EMAIL_PREFIXES = {
    "info", "contact", "support", "admin", "sales", "hr", "hello",
    "office", "privacy", "legal", "noreply", "no-reply", "billing",
    "enquiries", "inquiries", "reception", "general", "mail",
    "webmaster", "help", "team", "press", "media", "marketing",
    "feedback", "service", "customerservice", "careers", "jobs",
    "accounts", "orders", "bookings", "reservations", "appointments",
    # Healthcare & clinic specific generic prefixes
    "intake", "patientadvocate", "giving", "international", "referrals",
    "records", "medicalrecords", "contactus", "main", "schedule",
    "scheduling", "frontdesk", "patientcare", "clinic", "hospital",
    "information", "pharmacy", "compliance", "complianceoffice",
    # Departments, functions, and role mailboxes seen on institutional sites
    "ceo", "president", "director", "chair", "chairman", "chairwoman",
    "founder", "owner", "executive", "diplomas", "exams", "admissions",
    "academicaffairs", "accreditation", "candidatefeedback", "ceremonies",
    "clinicaltrials", "dissertation", "edinburghexaminers", "fellowship",
    "foundation", "frontstaff", "ict", "library", "medicalstaff",
    "medicalrecordsnssc", "memberships", "memsubs", "patientrelations",
    "physicianrelations", "policy", "proposals", "rector", "rentals",
    "venue", "volunteers", "webteam", "paces", "pacesscenarios",
    "pacesexpenses", "formoffaith", "honorhealthcvo", "honorhealthpricing",
}


OBFUSCATED_EMAIL_REGEXES = [
    re.compile(r"([a-zA-Z0-9._%+-]+)\s*\[\s*(?:at|AT)\s*\]\s*([a-zA-Z0-9.-]+)\s*\[\s*(?:dot|DOT)\s*\]\s*([a-zA-Z]{2,})"),
    re.compile(r"([a-zA-Z0-9._%+-]+)\s*\(\s*(?:at|AT)\s*\)\s*([a-zA-Z0-9.-]+)\s*\(\s*(?:dot|DOT)\s*\)\s*([a-zA-Z]{2,})"),
    re.compile(r"\b([a-zA-Z0-9._%+-]+)\s+(?:at|AT)\s+([a-zA-Z0-9.-]+)\s+(?:dot|DOT)\s+([a-zA-Z]{2,})\b"),
]


TEAM_PAGE_PROBES = [
    "/team", "/our-team", "/about/team", "/about-us/team",
    "/about/our-team", "/about/leadership", "/about-us/leadership",
    "/leadership", "/staff", "/our-staff", "/people",
    "/about/people", "/meet-the-team", "/meet-our-team",
    "/providers", "/our-providers", "/physicians", "/our-physicians",
    "/doctors", "/our-doctors", "/medical-staff", "/clinical-team",
    "/directory", "/faculty", "/management", "/executives",
    "/board", "/board-of-directors", "/founders", "/partners",
    "/about/staff", "/about-us/staff", "/about/management",
    "/contact", "/contact-us",
]


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
}


SKIP_DOMAINS = {
    # --- Social media & user-generated content ---
    "facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com",
    "youtube.com", "tiktok.com", "pinterest.com", "reddit.com",
    "threads.net", "mastodon.social", "tumblr.com", "quora.com",
    "medium.com", "substack.com",

    # --- Directories & review sites ---
    "yelp.com", "yellowpages.com", "bbb.org", "glassdoor.com",
    "indeed.com", "trustpilot.com", "g2.com", "capterra.com",
    "crunchbase.com", "zoominfo.com", "manta.com", "mapquest.com",
    "zippia.com", "cbinsights.com", "go4database.com", "theorg.com",
    "rocketreach.co", "signalhire.com", "contactout.com", "lusha.com",
    "leadiq.com", "seamless.ai", "hunter.io",
    "medicare.gov",

    # --- Search engines ---
    "google.com", "bing.com", "duckduckgo.com", "yahoo.com",
    "baidu.com", "yandex.com",

    # --- E-commerce / app stores ---
    "amazon.com", "ebay.com", "etsy.com", "shopify.com",
    "play.google.com", "apps.apple.com",

    # --- Encyclopedia / reference / dictionaries ---
    "wikipedia.org", "wikimedia.org", "wiktionary.org",
    "oxfordlearnersdictionaries.com", "dictionary.com",
    "merriam-webster.com", "cambridge.org", "britannica.com",
    "betterwordsonline.com", "investopedia.com",
    "thefreedictionary.com", "wordreference.com",

    # --- Major news / media outlets ---
    "usatoday.com", "nytimes.com", "washingtonpost.com",
    "wsj.com", "latimes.com", "chicagotribune.com",
    "nypost.com", "reuters.com", "apnews.com",
    "bbc.com", "bbc.co.uk", "cnn.com", "foxnews.com",
    "nbcnews.com", "cbsnews.com", "abcnews.go.com",
    "msnbc.com", "npr.org", "pbs.org",
    "theguardian.com", "independent.co.uk", "telegraph.co.uk",
    "dailymail.co.uk", "mirror.co.uk", "express.co.uk",
    "forbes.com", "businessinsider.com", "bloomberg.com",
    "cnbc.com", "fortune.com", "inc.com", "fastcompany.com",
    "huffpost.com", "buzzfeed.com", "vox.com", "vice.com",
    "theatlantic.com", "newyorker.com", "slate.com",
    "politico.com", "thehill.com", "axios.com",
    "techcrunch.com", "theverge.com", "arstechnica.com",
    "wired.com", "technologyreview.com", "gizmodo.com",
    "engadget.com", "mashable.com", "cnet.com", "zdnet.com",
    "variety.com", "deadline.com", "hollywoodreporter.com",
    "rollingstone.com", "billboard.com", "ew.com",
    "thedailybeast.com", "salon.com",
    "compuserve.com", "newswire.com", "prnewswire.com",
    "businesswire.com", "globenewswire.com",

    # --- Local / regional news networks ---
    "gannett.com", "tribpub.com", "mcclatchy.com",
    "news4jax.com", "wcpo.com", "wjxt.com",

    # --- Entertainment / culture / lifestyle ---
    "cbr.com", "screenrant.com", "ign.com", "gamespot.com",
    "thecollector.com", "imdb.com", "rottentomatoes.com",

    # --- Education platforms ---
    "coursera.org", "udemy.com", "edx.org", "khanacademy.org",
    "skillshare.com", "pluralsight.com",

    # --- Code hosting / developer tools ---
    "github.com", "gitlab.com", "bitbucket.org",
    "stackoverflow.com", "stackexchange.com",

    # --- Government portals (generic, not agency-specific) ---
    "usa.gov", "gov.uk",

    # --- Local / regional newspapers ---
    "norfolkdailynews.com", "dallasnews.com", "denverpost.com",
    "mercurynews.com", "seattletimes.com", "bostonglobe.com",
    "startribune.com", "oregonlive.com", "azcentral.com",
    "jsonline.com", "dispatch.com", "freep.com",
    "italianamericanherald.com",

    # --- Food / cooking / recipe sites ---
    "kitchenzoes.com", "allrecipes.com", "foodnetwork.com",
    "epicurious.com", "delish.com", "tasty.co", "bonappetit.com",
    "seriouseats.com", "simplyrecipes.com",

    # --- Telecom / retail / e-commerce (non-health) ---
    "cellphones.com.vn", "bestbuy.com", "walmart.com", "target.com",

    # --- Travel / hospitality (non-health) ---
    "inivie.com", "booking.com", "tripadvisor.com", "hotels.com",
    "expedia.com", "airbnb.com", "agoda.com", "liputan6.com",

    # --- Generic business / consulting / trader sites ---
    "consultant4companies.com", "arlettathefriendlytrader.com",

    # --- Universities / colleges (not health facilities) ---
    "euruni.edu", "loucoll.ac.uk", "eumunich.com",
}


DEFAULT_TRACKER_FILE = os.path.join("output", "crawled_sites.json")


LOCATION_POOLS = {
    "United States": [
        "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
        "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
        "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
        "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
        "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
        "New Hampshire", "New Jersey", "New Mexico", "New York",
        "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
        "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
        "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
        "West Virginia", "Wisconsin", "Wyoming",
    ],
    "United Kingdom": [
        "London", "Manchester", "Birmingham", "Leeds", "Glasgow", "Edinburgh",
        "Liverpool", "Bristol", "Sheffield", "Newcastle", "Nottingham",
        "Southampton", "Cardiff", "Belfast", "Leicester", "Brighton",
        "Oxford", "Cambridge", "York", "Bath", "Aberdeen", "Dundee",
        "Coventry", "Plymouth", "Exeter", "Norwich", "Derby", "Swansea",
    ],
    "Canada": [
        "Ontario", "Quebec", "British Columbia", "Alberta", "Manitoba",
        "Saskatchewan", "Nova Scotia", "New Brunswick", "Newfoundland",
        "Prince Edward Island", "Toronto", "Vancouver", "Montreal",
        "Calgary", "Ottawa", "Edmonton", "Winnipeg", "Halifax",
    ],
    "Australia": [
        "New South Wales", "Victoria", "Queensland", "Western Australia",
        "South Australia", "Tasmania", "Sydney", "Melbourne", "Brisbane",
        "Perth", "Adelaide", "Canberra", "Gold Coast", "Hobart",
    ],
    "India": [
        "Maharashtra", "Delhi", "Karnataka", "Tamil Nadu", "Telangana",
        "Gujarat", "West Bengal", "Rajasthan", "Kerala", "Punjab",
        "Mumbai", "Bangalore", "Chennai", "Hyderabad", "Kolkata",
        "Pune", "Ahmedabad", "Jaipur", "Lucknow", "Chandigarh",
    ],
    "Germany": [
        "Berlin", "Munich", "Hamburg", "Frankfurt", "Cologne", "Stuttgart",
        "Düsseldorf", "Leipzig", "Hannover", "Dresden", "Nuremberg", "Bremen",
    ],
}


_ROLE_TITLE_PATTERNS = re.compile(
    r"\b(?:director|founder|ceo|cfo|cto|coo|cmo|cio|president|"
    r"vice[\s-]?president|vp|chief|head|manager|managing|"
    r"administrator|superintendent|chairman|chairwoman|chairperson|"
    r"executive|partner|principal|owner|"
    r"dean|provost|chancellor|"
    r"medical[\s-]?director|clinical[\s-]?director|"
    r"nursing[\s-]?director|physician|surgeon|"
    r"professor|dr\.?)\b",
    re.IGNORECASE,
)


_NAME_PATTERN = re.compile(
    r"\b(?:Dr\.?\s+|Prof\.?\s+|Mr\.?\s+|Mrs\.?\s+|Ms\.?\s+)?"
    r"[A-Z][a-z]{1,20}(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]{1,20}"
    r"(?:\s+[A-Z][a-z]{1,20})?\b"
)


_NON_PERSON_NAME_WORDS = {
    "address", "affairs", "america", "association", "care", "center",
    "college", "consultation", "contact", "department", "email", "faculty",
    "foundation", "group", "health", "hospital", "institute", "medical",
    "office", "physicians", "proposal", "relations", "scenarios", "services",
    "staff", "support", "team", "training", "university",
}


COUNTRY_SUFFIXES = {
    "United Kingdom": {"uk"}, "Ghana": {"gh"}, "United States": {"us"},
    "Canada": {"ca"}, "Australia": {"au"}, "India": {"in"},
    "Germany": {"de"}, "France": {"fr"}, "Ireland": {"ie"},
}


KNOWN_COUNTRY_SUFFIXES = set().union(*COUNTRY_SUFFIXES.values())


INDUSTRY_TERMS = {
    "hospital": ("hospital", "medical center", "health system", "patient",
                 "physician", "clinical care", "healthcare"),
    "clinic": ("clinic", "medical practice", "patient", "physician",
               "provider", "clinical care", "healthcare"),
    "healthcare": ("healthcare", "health care", "patient", "physician",
                   "provider", "clinical", "medical"),
}


_ROLE_PHRASE_PATTERN = re.compile(
    r"\b(?:chief\s+(?:executive|financial|technology|operating|medical|"
    r"information)\s+officer|(?:plan\s+)?president(?:\s*(?:and|&)\s*ceo)?|"
    r"(?:executive|managing|medical|clinical|nursing|senior|associate)?\s*director|"
    r"vice[\s-]+president|founder|co-founder|owner|ceo|cfo|cto|coo|cmo|cio|"
    r"chair(?:man|woman|person)?|dean|provost|chancellor|administrator|"
    r"superintendent|physician|surgeon)\b",
    re.IGNORECASE,
)

OUTPUT_FIELDNAMES = [
    "email", "person_name", "role_title", "email_type",
    "source_url", "role_target", "industry_target", "country_target",
    "hunter_email", "hunter_status", "hunter_score", "hunter_sources",
    "hunter_checked_at", "hunter_action", "selected_email",
    "selected_email_status", "selected_email_provider",
]

ENRICHMENT_FIELDS = tuple(OUTPUT_FIELDNAMES[8:])


@dataclass(slots=True)
class Settings:
    """Resolved settings used by the application pipeline."""

    roles: list[str] | None = None
    industries: list[str] | None = None
    countries: list[str] | None = None
    direct_sites: list[str] = field(default_factory=list)
    filter_roles: list[str] | None = None
    max_sites: int = 30
    max_pages: int = 50
    output_path: str = "leads.csv"
    fresh_output: bool = False
    crawl_delay: float = 0.75
    search_delay: float = 3.0
    search_backends: list[str] = field(
        default_factory=lambda: ["duckduckgo", "brave"]
    )
    search_timeout: float = 5.0
    search_retry_delay: float = 1.0
    search_failure_threshold: int = 3
    search_max_queries: int = 30
    search_min_results: int = 8
    site_concurrency: int = 10
    request_timeout: float = 8.0
    site_crawl_timeout: float = 90.0
    max_robots_crawl_delay: float = 10.0
    respect_robots: bool = True
    tracker_path: str = DEFAULT_TRACKER_FILE
    reset_tracker: bool = False
    locations_per_day: int = 3
    crawl_cache_enabled: bool = True
    crawl_cache_path: str = "output/crawl_cache.sqlite"
    crawl_cache_ttl_hours: float = 24.0
    negative_cache_ttl_hours: float = 168.0
    search_cache_ttl_hours: float = 24.0
    check_mx: bool = True
    mx_timeout: float = 3.0
    mx_concurrency: int = 8
    hunter_enabled: bool = False
    hunter_api_keys: list[str] = field(default_factory=list)
    hunter_max_requests: int = 50
    hunter_cache: str = "output/hunter_cache.json"
    config: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def has_search_criteria(self) -> bool:
        return bool(self.roles and self.industries and self.countries)

    @property
    def has_direct_sites(self) -> bool:
        return bool(self.direct_sites)

    def validate(self) -> None:
        """Reject malformed or unsafe runtime values before network work starts."""
        for name in ("roles", "industries", "countries", "filter_roles"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, list)
                or not value
                or any(not isinstance(item, str) or not item.strip() for item in value)
            ):
                raise SettingsError(f"{name} must be a non-empty list of strings")

        if not isinstance(self.direct_sites, list) or any(
            not isinstance(site, str) or not site.strip() for site in self.direct_sites
        ):
            raise SettingsError("websites must be a list of non-empty strings")

        positive_integers = {
            "max_sites": self.max_sites,
            "max_pages": self.max_pages,
            "locations_per_day": self.locations_per_day,
            "site_concurrency": self.site_concurrency,
            "mx_concurrency": self.mx_concurrency,
            "search_failure_threshold": self.search_failure_threshold,
            "search_max_queries": self.search_max_queries,
        }
        for name, value in positive_integers.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise SettingsError(f"{name} must be a whole number greater than zero")

        budgets = {
            "hunter_max_requests": self.hunter_max_requests,
            "search_min_results": self.search_min_results,
        }
        for name, value in budgets.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SettingsError(f"{name} must be a non-negative whole number")

        for name, value in {
            "crawl_delay": self.crawl_delay,
            "search_delay": self.search_delay,
            "search_retry_delay": self.search_retry_delay,
            "crawl_cache_ttl_hours": self.crawl_cache_ttl_hours,
            "negative_cache_ttl_hours": self.negative_cache_ttl_hours,
            "search_cache_ttl_hours": self.search_cache_ttl_hours,
        }.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise SettingsError(f"{name} must be a non-negative number")

        for name, value in {
            "request_timeout": self.request_timeout,
            "site_crawl_timeout": self.site_crawl_timeout,
            "max_robots_crawl_delay": self.max_robots_crawl_delay,
            "mx_timeout": self.mx_timeout,
            "search_timeout": self.search_timeout,
        }.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise SettingsError(f"{name} must be a number greater than zero")

        if self.search_max_queries > 30:
            raise SettingsError("search_max_queries cannot exceed 30")
        if (
            not isinstance(self.search_backends, list)
            or not 1 <= len(self.search_backends) <= 2
            or any(not isinstance(item, str) or not item.strip() for item in self.search_backends)
        ):
            raise SettingsError("search_backends must contain one or two backend names")

        for name in (
            "output_path",
            "tracker_path",
            "hunter_cache",
            "crawl_cache_path",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise SettingsError(f"{name} must be a non-empty path")


def load_config(path: str | None) -> dict[str, Any]:
    """Load an optional JSON configuration file."""
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SettingsError(f"Could not read config file: {exc}") from exc
    if not isinstance(value, dict):
        raise SettingsError("Config file must contain a JSON object")
    print(f"Loaded config from {path}")
    return value


def _load_direct_sites(websites: list[str] | None, websites_file: str | None) -> list[str]:
    direct_sites = list(websites or [])
    if websites_file:
        try:
            with open(websites_file, "r", encoding="utf-8") as handle:
                direct_sites.extend(
                    line.strip()
                    for line in handle
                    if line.strip() and not line.startswith("#")
                )
        except OSError as exc:
            raise SettingsError(f"Could not read --websites-file: {exc}") from exc
    return direct_sites


def _environment_keys(plural_name: str, singular_name: str) -> list[str]:
    """Prefer an ordered plural key array, falling back to the legacy value."""
    plural_keys = parse_api_keys(os.environ.get(plural_name, ""))
    if plural_keys:
        return plural_keys
    return parse_api_keys(os.environ.get(singular_name, ""))


def from_namespace(args: Any) -> Settings:
    """Resolve command-line values, config defaults, and environment keys."""
    config = load_config(args.config)
    roles = args.roles or config.get("roles")
    industries = args.industries or config.get("industries")
    countries = args.countries or config.get("countries")
    filter_roles = args.filter_roles
    if filter_roles is None and roles:
        filter_roles = roles
        print(f"Auto-using --roles as email role filters: {filter_roles}")

    settings = Settings(
        roles=roles,
        industries=industries,
        countries=countries,
        direct_sites=_load_direct_sites(args.websites, args.websites_file),
        filter_roles=filter_roles,
        max_sites=args.max_sites if args.max_sites is not None else config.get("max_sites", 30),
        max_pages=args.max_pages if args.max_pages is not None else config.get("max_pages", 50),
        output_path=args.output,
        fresh_output=args.fresh_output,
        crawl_delay=(
            args.crawl_delay if args.crawl_delay is not None else config.get("crawl_delay", 0.75)
        ),
        search_delay=(
            args.search_delay if args.search_delay is not None else config.get("search_delay", 3.0)
        ),
        search_backends=(
            args.search_backends
            or config.get("search_backends", ["duckduckgo", "brave"])
        ),
        search_timeout=(
            args.search_timeout
            if args.search_timeout is not None
            else config.get("search_timeout", 5.0)
        ),
        search_retry_delay=(
            args.search_retry_delay
            if args.search_retry_delay is not None
            else config.get("search_retry_delay", 1.0)
        ),
        search_failure_threshold=(
            args.search_failure_threshold
            if args.search_failure_threshold is not None
            else config.get("search_failure_threshold", 3)
        ),
        search_max_queries=(
            args.search_max_queries
            if args.search_max_queries is not None
            else config.get("search_max_queries", 30)
        ),
        search_min_results=(
            args.search_min_results
            if args.search_min_results is not None
            else config.get("search_min_results", 8)
        ),
        site_concurrency=(
            args.site_concurrency
            if args.site_concurrency is not None
            else config.get("site_concurrency", 10)
        ),
        request_timeout=(
            args.request_timeout
            if args.request_timeout is not None
            else config.get("request_timeout", 8.0)
        ),
        site_crawl_timeout=(
            args.site_crawl_timeout
            if args.site_crawl_timeout is not None
            else config.get("site_crawl_timeout", 90.0)
        ),
        max_robots_crawl_delay=(
            args.max_robots_crawl_delay
            if args.max_robots_crawl_delay is not None
            else config.get("max_robots_crawl_delay", 10.0)
        ),
        respect_robots=not args.ignore_robots,
        tracker_path=args.tracker_file or config.get("tracker_file", DEFAULT_TRACKER_FILE),
        reset_tracker=args.reset_tracker,
        locations_per_day=(
            args.locations_per_day
            if args.locations_per_day is not None
            else config.get("locations_per_day", 3)
        ),
        crawl_cache_enabled=(
            not args.no_crawl_cache and config.get("crawl_cache_enabled", True)
        ),
        crawl_cache_path=(
            args.crawl_cache
            or config.get("crawl_cache", "output/crawl_cache.sqlite")
        ),
        crawl_cache_ttl_hours=(
            args.crawl_cache_ttl_hours
            if args.crawl_cache_ttl_hours is not None
            else config.get("crawl_cache_ttl_hours", 24.0)
        ),
        negative_cache_ttl_hours=(
            args.negative_cache_ttl_hours
            if args.negative_cache_ttl_hours is not None
            else config.get("negative_cache_ttl_hours", 168.0)
        ),
        search_cache_ttl_hours=(
            args.search_cache_ttl_hours
            if args.search_cache_ttl_hours is not None
            else config.get("search_cache_ttl_hours", 24.0)
        ),
        check_mx=not args.skip_mx_check and config.get("check_mx", True),
        mx_timeout=(
            args.mx_timeout if args.mx_timeout is not None else config.get("mx_timeout", 3.0)
        ),
        mx_concurrency=(
            args.mx_concurrency
            if args.mx_concurrency is not None
            else config.get("mx_concurrency", 8)
        ),
        hunter_enabled=args.hunter_enrich or config.get("hunter_enrich", False),
        hunter_api_keys=parse_api_keys(
            args.hunter_api_keys
            or _environment_keys("HUNTER_API_KEYS", "HUNTER_API_KEY")
            or config.get("hunter_api_keys", config.get("hunter_api_key", ""))
        ),
        hunter_max_requests=(
            args.hunter_max_requests
            if args.hunter_max_requests is not None
            else config.get("hunter_max_requests", 50)
        ),
        hunter_cache=args.hunter_cache or config.get("hunter_cache", "output/hunter_cache.json"),
        config=config,
    )
    if not settings.has_search_criteria and not settings.has_direct_sites:
        raise SettingsError(
            "Provide either:\n"
            "  --roles ... --industries ... --countries ...   (search-based discovery), or\n"
            "  --websites ...  /  --websites-file ...          (direct crawl)\n"
            "You can also combine both in one run."
        )
    settings.validate()
    return settings

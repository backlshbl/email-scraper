"""Allow ``python -m email_lead_scraper`` to run the scraper CLI."""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())

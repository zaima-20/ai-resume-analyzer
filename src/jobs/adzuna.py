"""Adzuna API job source.

Adzuna only covers certain countries, and the country is part of the URL:
https://api.adzuna.com/v1/api/jobs/<country>/search/1
The supported codes come from Adzuna's official API spec, which the interactive
docs at https://developer.adzuna.com/activedocs load from
https://developer.adzuna.com/swagger/spec/test2.json (the "country" parameter).
"""

import sys
from datetime import date

import requests

from src import cache, config
from src.jobs.base import Job, JobSource, JobSourceError
from src.jobs.remotive import html_to_text

ADZUNA_SEARCH_URL: str = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"
RESULTS_PER_PAGE: int = 50
REQUEST_TIMEOUT: int = 15  # seconds

# ---------- Country lookup (all location-to-country rules live here) ----------

# The 19 countries Adzuna supports, with names for messages.
SUPPORTED_COUNTRIES: dict[str, str] = {
    "gb": "the UK", "us": "the USA", "at": "Austria", "au": "Australia",
    "be": "Belgium", "br": "Brazil", "ca": "Canada", "ch": "Switzerland",
    "de": "Germany", "es": "Spain", "fr": "France", "in": "India",
    "it": "Italy", "mx": "Mexico", "nl": "the Netherlands", "nz": "New Zealand",
    "pl": "Poland", "sg": "Singapore", "za": "South Africa",
}

# Country names, common short forms and big cities -> Adzuna country code.
# Two-letter state codes like "CA" are left out on purpose ("CA" could be California or Canada).
LOCATION_TO_COUNTRY: dict[str, str] = {
    # UK
    "uk": "gb", "united kingdom": "gb", "great britain": "gb", "britain": "gb",
    "england": "gb", "scotland": "gb", "wales": "gb", "london": "gb",
    "manchester": "gb", "birmingham": "gb", "edinburgh": "gb", "glasgow": "gb",
    "leeds": "gb", "bristol": "gb", "cambridge": "gb", "oxford": "gb",
    # USA
    "usa": "us", "us": "us", "united states": "us", "united states of america": "us",
    "america": "us", "new york": "us", "new york city": "us", "nyc": "us",
    "san francisco": "us", "seattle": "us", "boston": "us", "chicago": "us",
    "austin": "us", "los angeles": "us", "washington dc": "us",
    # Other supported countries and their main cities
    "austria": "at", "vienna": "at",
    "australia": "au", "sydney": "au", "melbourne": "au", "brisbane": "au",
    "belgium": "be", "brussels": "be", "antwerp": "be",
    "brazil": "br", "sao paulo": "br", "são paulo": "br", "rio de janeiro": "br",
    "canada": "ca", "toronto": "ca", "vancouver": "ca", "montreal": "ca",
    "switzerland": "ch", "zurich": "ch", "geneva": "ch",
    "germany": "de", "berlin": "de", "munich": "de", "hamburg": "de", "frankfurt": "de",
    "spain": "es", "madrid": "es", "barcelona": "es",
    "france": "fr", "paris": "fr", "lyon": "fr",
    "india": "in", "bangalore": "in", "bengaluru": "in", "mumbai": "in",
    "delhi": "in", "new delhi": "in", "hyderabad": "in", "pune": "in", "chennai": "in",
    "italy": "it", "milan": "it", "rome": "it",
    "mexico": "mx", "mexico city": "mx",
    "netherlands": "nl", "the netherlands": "nl", "holland": "nl", "amsterdam": "nl", "rotterdam": "nl",
    "new zealand": "nz", "auckland": "nz", "wellington": "nz",
    "poland": "pl", "warsaw": "pl", "krakow": "pl",
    "singapore": "sg",
    "south africa": "za", "cape town": "za", "johannesburg": "za",
}

# Places Adzuna does NOT cover -> country name, so the skip message can name it.
UNSUPPORTED_LOCATIONS: dict[str, str] = {
    "pakistan": "Pakistan", "lahore": "Pakistan", "karachi": "Pakistan",
    "islamabad": "Pakistan", "rawalpindi": "Pakistan", "faisalabad": "Pakistan",
    "multan": "Pakistan", "peshawar": "Pakistan",
    "uae": "the UAE", "united arab emirates": "the UAE", "dubai": "the UAE", "abu dhabi": "the UAE",
    "saudi arabia": "Saudi Arabia", "riyadh": "Saudi Arabia", "jeddah": "Saudi Arabia",
    "qatar": "Qatar", "doha": "Qatar",
    "bangladesh": "Bangladesh", "dhaka": "Bangladesh",
    "ireland": "Ireland", "dublin": "Ireland",
    "remote": "remote-only searches",
}

# Words that name a whole country (not a city), so they aren't sent as "where".
COUNTRY_WORDS: set[str] = {
    "uk", "united kingdom", "great britain", "britain", "england", "scotland", "wales",
    "usa", "us", "united states", "united states of america", "america",
    "austria", "australia", "belgium", "brazil", "canada", "switzerland", "germany",
    "spain", "france", "india", "italy", "mexico", "netherlands", "the netherlands",
    "holland", "new zealand", "poland", "south africa",
}


def normalize_place(text: str) -> str:
    """Lowercase a place name and collapse spaces and dots, e.g. " U.K. " -> "uk"."""
    return " ".join(text.replace(".", "").lower().split())


def find_country(location: str) -> tuple[str, str]:
    """Work out the Adzuna country code from a free-text location.

    The location is split on commas and checked from the end, because the
    country usually comes last ("Manchester, UK").

    Args:
        location: e.g. "London", "Berlin, Germany" or "Lahore, Pakistan".

    Returns:
        (country code, the place to send as Adzuna's "where", which may be "").
        Example: "Manchester, UK" -> ("gb", "Manchester").

    Raises:
        JobSourceError: If the location is empty, in a country Adzuna doesn't
            cover, or not recognised.
    """
    parts = [part.strip() for part in location.split(",") if part.strip()]
    if not parts:
        raise JobSourceError(
            "Adzuna needs a location to pick a country (e.g. \"London\" or \"Berlin, Germany\"), "
            "so it was skipped."
        )

    for part in reversed(parts):
        place = normalize_place(part)
        if place in UNSUPPORTED_LOCATIONS:
            raise JobSourceError(
                f"Adzuna doesn't cover {UNSUPPORTED_LOCATIONS[place]}, so it was skipped."
            )
        if place in LOCATION_TO_COUNTRY:
            country = LOCATION_TO_COUNTRY[place]
            # Send everything except country-level words as "where", e.g. "Manchester".
            where_parts = [p for p in parts if normalize_place(p) not in COUNTRY_WORDS]
            return country, ", ".join(where_parts)

    raise JobSourceError(
        f"Adzuna couldn't tell which country \"{location}\" is in, so it was skipped. "
        "Try adding the country, e.g. \"Leeds, UK\"."
    )


# ---------- Converting results ----------


def parse_created(value: object) -> date | None:
    """Read the date from Adzuna's "created" field, e.g. "2026-10-30T09:15:00Z"."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def display_name(value: object, default: str) -> str:
    """Read {"display_name": "..."} objects like company and location."""
    if isinstance(value, dict) and isinstance(value.get("display_name"), str):
        name = value["display_name"].strip()
        if name:
            return name
    return default


def to_job(item: dict) -> Job | None:
    """Convert one Adzuna result into a Job, or None if it has no title or link.

    Adzuna's description is a snippet of at most 500 characters, not the full ad.
    Titles and snippets can contain HTML tags or entities, so they are cleaned up.
    """
    title = html_to_text(item.get("title") or "")
    url = item.get("redirect_url")
    if not title or not isinstance(url, str) or not url:
        return None

    description = html_to_text(item.get("description") or "")
    # Adzuna has no remote flag, so look for the word in the title and snippet.
    remote = "remote" in title.lower() or "remote" in description.lower()
    return Job(
        title=title,
        company=display_name(item.get("company"), "Unknown company"),
        location=display_name(item.get("location"), "Location not given"),
        remote=remote,
        description=description,
        url=url,  # Adzuna's terms require sending users through redirect_url
        source=AdzunaSource.name,
        posted_date=parse_created(item.get("created")),
    )


# ---------- The source ----------


class AdzunaSource(JobSource):
    """Searches the Adzuna API. Needs ADZUNA_APP_ID and ADZUNA_APP_KEY in .env."""

    name = "Adzuna"

    def fetch(self, country: str, what: str, where: str) -> list[dict]:
        """Download raw job dicts from Adzuna for one country.

        Raises:
            JobSourceError: For a rejected key, network errors or bad replies.
        """
        params = {
            "app_id": config.ADZUNA_APP_ID,
            "app_key": config.ADZUNA_APP_KEY,
            "what": what,
            "results_per_page": RESULTS_PER_PAGE,
            "content-type": "application/json",
        }
        if where:
            params["where"] = where

        try:
            response = requests.get(
                ADZUNA_SEARCH_URL.format(country=country), params=params, timeout=REQUEST_TIMEOUT
            )
        except requests.Timeout as error:
            raise JobSourceError("Adzuna took too long to respond. Please try again later.") from error
        except requests.RequestException as error:
            raise JobSourceError("Adzuna could not be reached. Check your internet connection.") from error

        # Adzuna answers 410 "Authorisation failed" for a wrong app ID or key.
        if response.status_code in (401, 403, 410):
            raise JobSourceError(
                "Adzuna rejected the app ID or key. Check ADZUNA_APP_ID and ADZUNA_APP_KEY in your .env file."
            )
        if response.status_code == 429:
            raise JobSourceError("Adzuna's request limit was reached. Please try again later.")
        if response.status_code != 200:
            raise JobSourceError(f"Adzuna returned an error (status {response.status_code}).")

        try:
            body = response.json()
        except ValueError as error:
            raise JobSourceError("Adzuna sent a reply that couldn't be read.") from error

        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            raise JobSourceError("Adzuna sent a reply in an unexpected format.")
        return [item for item in results if isinstance(item, dict)]

    def search(self, query: str, location: str, remote_only: bool) -> list[Job]:
        """Search Adzuna in the location's country, using the cache when possible.

        Args:
            query: Search words, usually the target job title.
            location: Where to search, e.g. "London" or "Berlin, Germany".
            remote_only: If True, "remote" is added to the search words.

        Raises:
            JobSourceError: If keys are missing, the country isn't covered
                (no API call is made then), or the request fails.
        """
        if not (config.has_key("ADZUNA_APP_ID") and config.has_key("ADZUNA_APP_KEY")):
            raise JobSourceError(
                "Adzuna skipped: add ADZUNA_APP_ID and ADZUNA_APP_KEY to your .env file."
            )
        country, where = find_country(location)

        key = cache.make_key(self.name, query, location, remote_only)
        raw_jobs = cache.get(key)
        if raw_jobs is None:
            what = f"{query} remote" if remote_only else query
            raw_jobs = self.fetch(country, what.strip(), where)
            cache.set(key, raw_jobs)  # only successful results are cached

        return [job for job in (to_job(item) for item in raw_jobs) if job is not None]


# Run from the command line:
#   python -m src.jobs.adzuna "Machine Learning Engineer" "London" [--remote]
if __name__ == "__main__":
    args = [arg for arg in sys.argv[1:] if arg != "--remote"]
    if len(args) != 2:
        print('Usage: python -m src.jobs.adzuna "Job title" "Location" [--remote]')
        sys.exit(1)

    try:
        found = AdzunaSource().search(args[0], args[1], remote_only="--remote" in sys.argv)
    except JobSourceError as error:
        print(f"Error: {error}")
        sys.exit(1)

    print(f"{len(found)} job(s) found on Adzuna.")
    for job in found[:5]:
        print(f"- {job.title} at {job.company}\n  {job.url}")

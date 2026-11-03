"""JSearch API source (LinkedIn, Indeed, Glassdoor via Google for Jobs).

JSearch collects postings from Google for Jobs, so this is how the app reaches
LinkedIn, Indeed and Glassdoor jobs without scraping those sites.
The /search-v2 reply looks like: {"status": "OK", "data": {"jobs": [...], "cursor": "..."}}
"""

import sys
from datetime import date, datetime, timezone

import requests

from src import cache, config
from src.jobs.base import Job, JobSource, JobSourceError

JSEARCH_HOST: str = "jsearch.p.rapidapi.com"
REQUEST_TIMEOUT: int = 20  # seconds; JSearch can be slow because it searches many sites


def build_query(title: str, location: str) -> str:
    """Build the JSearch query text, e.g. "Data Analyst in Lahore"."""
    title = " ".join(title.split())
    location = " ".join(location.split())
    return f"{title} in {location}" if location else title


def parse_posted_date(item: dict) -> date | None:
    """Read the posting date from "job_posted_at_datetime_utc", or the Unix timestamp."""
    text = item.get("job_posted_at_datetime_utc")
    if isinstance(text, str):
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            pass
    timestamp = item.get("job_posted_at_timestamp")
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).date()
    return None


def is_remote(item: dict) -> bool:
    """Return True if the job is remote.

    JSearch's job_is_remote flag is sometimes False even for jobs titled
    "Remote ...", so the title is checked too.
    """
    return item.get("job_is_remote") is True or "remote" in str(item.get("job_title", "")).lower()


def apply_url(item: dict) -> str | None:
    """Return the best link to the original posting, or None if there is none."""
    if item.get("job_apply_link"):
        return item["job_apply_link"]
    for option in item.get("apply_options") or []:
        if isinstance(option, dict) and option.get("apply_link"):
            return option["apply_link"]
    return item.get("job_google_link") or None


def to_job(item: dict) -> Job | None:
    """Convert one JSearch result into a Job.

    Returns None if the result has no title or link, so one bad item
    doesn't break the whole search.
    """
    title = item.get("job_title")
    url = apply_url(item)
    if not isinstance(title, str) or not title.strip() or not isinstance(url, str):
        return None

    remote = is_remote(item)
    location = item.get("job_location") or ("Remote" if remote else "Location not given")
    publisher = item.get("job_publisher")
    return Job(
        title=title.strip(),
        company=(item.get("employer_name") or "Unknown company").strip(),
        location=location,
        remote=remote,
        description=item.get("job_description") or "",
        url=url,
        # e.g. "JSearch (LinkedIn)", so users can see where the job was posted.
        source=f"{JSearchSource.name} ({publisher})" if publisher else JSearchSource.name,
        posted_date=parse_posted_date(item),
    )


class JSearchSource(JobSource):
    """Searches the JSearch API on RapidAPI. Needs RAPIDAPI_KEY in .env."""

    name = "JSearch"

    def fetch(self, query: str, remote_only: bool) -> list[dict]:
        """Download raw job dicts from JSearch /search-v2.

        Raises:
            JobSourceError: For a missing or rejected key, an exhausted quota,
                network errors or a reply in an unexpected format.
        """
        if not config.has_key("RAPIDAPI_KEY"):
            raise JobSourceError(
                "JSearch skipped: add RAPIDAPI_KEY to your .env file to search "
                "LinkedIn, Indeed and Glassdoor jobs."
            )

        headers = {"X-RapidAPI-Key": config.RAPIDAPI_KEY, "X-RapidAPI-Host": JSEARCH_HOST}
        params = {"query": query}
        if remote_only:
            params["work_from_home"] = "true"

        try:
            response = requests.get(
                config.JSEARCH_URL, headers=headers, params=params, timeout=REQUEST_TIMEOUT
            )
        except requests.Timeout as error:
            raise JobSourceError("JSearch took too long to respond. Please try again later.") from error
        except requests.RequestException as error:
            raise JobSourceError("JSearch could not be reached. Check your internet connection.") from error

        if response.status_code == 401:
            raise JobSourceError("JSearch rejected the API key. Check RAPIDAPI_KEY in your .env file.")
        if response.status_code == 403:
            raise JobSourceError(
                "Your RapidAPI key is not subscribed to JSearch. Subscribe to the free plan "
                "on the JSearch page on RapidAPI."
            )
        if response.status_code == 429:
            raise JobSourceError(
                "The JSearch monthly free quota is used up (or too many requests at once). "
                "Cached searches still work; new ones will work again when the quota resets."
            )
        if response.status_code != 200:
            raise JobSourceError(f"JSearch returned an error (status {response.status_code}).")

        try:
            body = response.json()
        except ValueError as error:
            raise JobSourceError("JSearch sent a reply that couldn't be read.") from error

        data = body.get("data") if isinstance(body, dict) else None
        # /search-v2 puts the jobs in data["jobs"]; the old /search put them directly in data.
        jobs = data.get("jobs") if isinstance(data, dict) else data
        if not isinstance(jobs, list):
            raise JobSourceError("JSearch sent a reply in an unexpected format.")
        return [item for item in jobs if isinstance(item, dict)]

    def search(self, query: str, location: str, remote_only: bool) -> list[Job]:
        """Search JSearch for "<query> in <location>", using the cache when possible.

        Args:
            query: Search words, usually the target job title.
            location: City or country. May be empty.
            remote_only: If True, ask for remote jobs only and drop any non-remote results.
        """
        key = cache.make_key(self.name, query, location, remote_only)
        raw_jobs = cache.get(key)
        if raw_jobs is None:
            raw_jobs = self.fetch(build_query(query, location), remote_only)
            cache.set(key, raw_jobs)  # only successful results are cached

        jobs = [job for job in (to_job(item) for item in raw_jobs) if job is not None]
        if remote_only:
            jobs = [job for job in jobs if job.remote]
        return jobs


# Run from the command line:
#   python -m src.jobs.jsearch "Machine Learning Engineer" "Lahore" [--remote]
if __name__ == "__main__":
    args = [arg for arg in sys.argv[1:] if arg != "--remote"]
    if not 1 <= len(args) <= 2:
        print('Usage: python -m src.jobs.jsearch "Job title" ["Location"] [--remote]')
        sys.exit(1)

    try:
        found = JSearchSource().search(
            args[0], args[1] if len(args) == 2 else "", remote_only="--remote" in sys.argv
        )
    except JobSourceError as error:
        print(f"Error: {error}")
        sys.exit(1)

    print(f"{len(found)} job(s) found on JSearch.")
    for job in found[:5]:
        print(f"- {job.title} at {job.company}")
        print(f"  Source: {job.source}")
        print(f"  {job.url}")

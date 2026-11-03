"""Remotive API job source (remote jobs, no key needed).

Remotive's terms ask apps to credit Remotive and link back to the original
posting, so every Job keeps source="Remotive" and the Remotive job URL.
They also ask for no more than about 4 requests a day, so results are cached.
"""

import sys
from datetime import date
from html.parser import HTMLParser

import requests

from src import cache
from src.jobs.base import Job, JobSource, JobSourceError

REMOTIVE_URL: str = "https://remotive.com/api/remote-jobs"
DEFAULT_LIMIT: int = 50
REQUEST_TIMEOUT: int = 15  # seconds

# HTML tags that start a new line when converted to text.
BLOCK_TAGS: set[str] = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}
# Tags whose contents are code, not text for the reader.
SKIP_TAGS: set[str] = {"script", "style"}


class _TextExtractor(HTMLParser):
    """Collects the text from HTML, adding line breaks for block tags."""

    def __init__(self) -> None:
        super().__init__()  # convert_charrefs=True turns "&amp;" into "&"
        self.parts: list[str] = []
        self.skip_depth = 0  # > 0 while inside <script> or <style>

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self.skip_depth += 1
        if tag in BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self.skip_depth > 0:
            self.skip_depth -= 1
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth == 0:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Convert an HTML job description to plain text.

    Tags are removed, list items become "- " lines, and blank lines are tidied up.
    Example: "<p>Hi &amp; welcome</p><ul><li>SQL</li></ul>" -> "Hi & welcome\\n- SQL"
    """
    extractor = _TextExtractor()
    extractor.feed(html or "")
    extractor.close()

    lines = [" ".join(line.split()) for line in "".join(extractor.parts).splitlines()]
    return "\n".join(line for line in lines if line)


def parse_date(value: object) -> date | None:
    """Read the date part of a Remotive timestamp like "2026-10-30T09:15:00"."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def to_job(item: dict) -> Job | None:
    """Convert one Remotive job (a dict from the API) into a Job.

    Returns None if the item is missing its title or URL, so one bad item
    doesn't break the whole search.
    """
    title = item.get("title")
    url = item.get("url")
    if not isinstance(title, str) or not title.strip() or not isinstance(url, str) or not url:
        return None

    # All Remotive jobs are remote; this field says where candidates may live.
    region = item.get("candidate_required_location") or "Worldwide"
    return Job(
        title=title.strip(),
        company=(item.get("company_name") or "Unknown company").strip(),
        location=f"Remote ({region})",
        remote=True,
        description=html_to_text(item.get("description") or ""),
        url=url,  # the original Remotive link, as their terms require
        source=RemotiveSource.name,
        posted_date=parse_date(item.get("publication_date")),
    )


class RemotiveSource(JobSource):
    """Searches Remotive's free remote-jobs API. No API key needed."""

    name = "Remotive"

    def __init__(self, limit: int = DEFAULT_LIMIT) -> None:
        """Create the source.

        Args:
            limit: The most jobs to ask Remotive for in one search.
        """
        self.limit = limit

    def fetch(self, query: str) -> list[dict]:
        """Download raw job dicts from Remotive.

        Raises:
            JobSourceError: On network errors, timeouts, error status codes or bad JSON.
        """
        params = {"search": query, "limit": self.limit}
        try:
            response = requests.get(REMOTIVE_URL, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()
        except requests.Timeout as error:
            raise JobSourceError("Remotive took too long to respond. Please try again later.") from error
        except requests.HTTPError as error:
            raise JobSourceError(
                f"Remotive returned an error (status {error.response.status_code})."
            ) from error
        except requests.RequestException as error:
            raise JobSourceError("Remotive could not be reached. Check your internet connection.") from error
        except ValueError as error:  # response.json() failed
            raise JobSourceError("Remotive sent a reply that couldn't be read.") from error

        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise JobSourceError("Remotive sent a reply in an unexpected format.")
        return [item for item in jobs if isinstance(item, dict)]

    def search(self, query: str, location: str, remote_only: bool) -> list[Job]:
        """Search Remotive, using the cache when the same search was done recently.

        Every Remotive job is remote, so remote_only changes nothing here. Remotive
        has no city filter, so `location` isn't sent; the matcher scores location later.
        It is still part of the cache key, so the key works like every other source's.
        """
        key = cache.make_key(self.name, query, location, remote_only)
        raw_jobs = cache.get(key)
        if raw_jobs is None:
            raw_jobs = self.fetch(query)
            cache.set(key, raw_jobs)  # only successful results are cached

        jobs = [to_job(item) for item in raw_jobs]
        return [job for job in jobs if job is not None]


# Run from the command line:  python -m src.jobs.remotive "machine learning"
if __name__ == "__main__":
    if len(sys.argv) != 2:
        print('Usage: python -m src.jobs.remotive "search words"')
        sys.exit(1)

    try:
        found = RemotiveSource().search(sys.argv[1], location="", remote_only=True)
    except JobSourceError as error:
        print(f"Error: {error}")
        sys.exit(1)

    print(f"{len(found)} job(s) found on Remotive.")
    for job in found[:5]:
        print(f"- {job.title} at {job.company}\n  {job.url}")

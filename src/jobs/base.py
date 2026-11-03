"""Common Job dataclass and JobSource interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date


class JobSourceError(Exception):
    """Raised when a job source fails (no key, network error, bad reply).

    The message is safe to show to the user as a warning. The app skips that
    source and keeps showing results from the others.
    """


@dataclass
class Job:
    """One job posting, in the same shape no matter which source it came from.

    Attributes:
        title: Job title, e.g. "Data Analyst".
        company: Company name.
        location: Location as text, e.g. "Lahore, Pakistan" or "Remote".
        remote: True if the job can be done remotely.
        description: Job description text (may be shortened by the source).
        url: Link to the original posting, used for the "Apply" button.
        source: Name of the source it came from, e.g. "Adzuna".
        posted_date: When the job was posted, if the source says.
    """

    title: str
    company: str
    location: str
    remote: bool
    description: str
    url: str
    source: str
    posted_date: date | None = None


class JobSource(ABC):
    """Base class every job source (JSearch, Adzuna, Remotive) builds on.

    Each source sets `name` and implements `search()`, so the app can loop over
    all sources the same way without knowing how each API works.
    """

    name: str = "Unnamed source"

    @abstractmethod
    def search(self, query: str, location: str, remote_only: bool) -> list[Job]:
        """Search for jobs and return them as Job objects.

        Args:
            query: Search words, e.g. "Data Analyst SQL Python".
            location: City or country to search in. May be empty.
            remote_only: If True, only return remote jobs.

        Returns:
            The jobs found. An empty list means the search worked but found nothing.

        Raises:
            JobSourceError: If the source can't be searched (missing key, API error).
        """

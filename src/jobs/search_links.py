"""Builds direct search URLs for LinkedIn, Indeed, Rozee.pk and Glassdoor.

These are normal search links the user opens in their own browser. This module
never fetches or scrapes these sites (that would break their terms of use);
their postings reach the app only through the JSearch API.
"""

import sys
from urllib.parse import quote, urlencode

# Each site's public job search page.
LINKEDIN_URL: str = "https://www.linkedin.com/jobs/search/"
INDEED_URL: str = "https://www.indeed.com/jobs"
GLASSDOOR_URL: str = "https://www.glassdoor.com/Job/jobs.htm"
ROZEE_URL: str = "https://www.rozee.pk/job/jsearch/q/"

# Remote filters, taken from the URLs each site makes when you tick "Remote".
LINKEDIN_REMOTE: dict[str, str] = {"f_WT": "2"}  # work type: 1 on-site, 2 remote, 3 hybrid
INDEED_REMOTE: dict[str, str] = {"remotejobs": "032b3046-06a3-4876-8dfd-474eb5e7ed11"}
GLASSDOOR_REMOTE: dict[str, str] = {"remoteWorkType": "1"}


def build_url(base_url: str, params: dict[str, str]) -> str:
    """Join a base URL and query parameters, URL-encoding the values.

    Empty values are left out, e.g. when no location was given.
    """
    filled = {key: value for key, value in params.items() if value}
    return f"{base_url}?{urlencode(filled)}" if filled else base_url


def linkedin_link(title: str, location: str, remote_only: bool) -> str:
    """Build a LinkedIn job search URL."""
    params = {"keywords": title, "location": location}
    if remote_only:
        params.update(LINKEDIN_REMOTE)
    return build_url(LINKEDIN_URL, params)


def indeed_link(title: str, location: str, remote_only: bool) -> str:
    """Build an Indeed job search URL."""
    params = {"q": title, "l": location}
    if remote_only:
        params.update(INDEED_REMOTE)
    return build_url(INDEED_URL, params)


def glassdoor_link(title: str, location: str, remote_only: bool) -> str:
    """Build a Glassdoor job search URL."""
    params = {"sc.keyword": title, "locKeyword": location}
    if remote_only:
        params.update(GLASSDOOR_REMOTE)
    return build_url(GLASSDOOR_URL, params)


def rozee_link(title: str, location: str, remote_only: bool) -> str:
    """Build a Rozee.pk job search URL.

    Rozee puts the search words in the URL path and filters cities by internal ID,
    so the location is added to the search words instead. Rozee has no remote
    filter in its URLs, so remote_only adds the word "remote" to the search.
    """
    words = " ".join(part for part in [title, location, "remote" if remote_only else ""] if part)
    # safe="" also encodes "/", so a title like "UI/UX Designer" can't break the path.
    return ROZEE_URL + quote(words, safe="")


def build_search_links(title: str, location: str = "", remote_only: bool = False) -> list[tuple[str, str]]:
    """Build job search links for LinkedIn, Indeed, Glassdoor and Rozee.pk.

    Only builds URLs; nothing is downloaded.

    Args:
        title: Job title to search for, e.g. "Machine Learning Engineer".
        location: City or country, e.g. "Lahore". May be empty.
        remote_only: If True, add each site's remote filter where it has one.

    Returns:
        (site name, URL) pairs, in a fixed order.
    """
    title = title.strip()
    location = location.strip()
    return [
        ("LinkedIn", linkedin_link(title, location, remote_only)),
        ("Indeed", indeed_link(title, location, remote_only)),
        ("Glassdoor", glassdoor_link(title, location, remote_only)),
        ("Rozee.pk", rozee_link(title, location, remote_only)),
    ]


# Run from the command line:
#   python -m src.jobs.search_links "Machine Learning Engineer" "Lahore" [--remote]
if __name__ == "__main__":
    args = [arg for arg in sys.argv[1:] if arg != "--remote"]
    if not 1 <= len(args) <= 2:
        print('Usage: python -m src.jobs.search_links "Job title" ["Location"] [--remote]')
        sys.exit(1)

    job_title = args[0]
    job_location = args[1] if len(args) == 2 else ""
    for site, url in build_search_links(job_title, job_location, "--remote" in sys.argv):
        print(f"{site:<10} {url}")

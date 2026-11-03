"""Tests for src/jobs/search_links.py. Only checks the URLs; nothing is fetched."""

from urllib.parse import parse_qs, urlparse

import pytest

from src.jobs import search_links
from src.jobs.search_links import build_search_links


def links_by_site(title: str, location: str = "", remote_only: bool = False) -> dict[str, str]:
    """Return the links as a {site name: URL} dict."""
    return dict(build_search_links(title, location, remote_only))


def query_params(url: str) -> dict[str, list[str]]:
    """Decode a URL's query string, e.g. "?q=a+b" -> {"q": ["a b"]}."""
    return parse_qs(urlparse(url).query)


def test_returns_all_four_sites_in_order() -> None:
    sites = [site for site, _url in build_search_links("Data Analyst", "Lahore")]
    assert sites == ["LinkedIn", "Indeed", "Glassdoor", "Rozee.pk"]


def test_all_links_are_https_on_the_right_domain() -> None:
    expected = {
        "LinkedIn": "www.linkedin.com",
        "Indeed": "www.indeed.com",
        "Glassdoor": "www.glassdoor.com",
        "Rozee.pk": "www.rozee.pk",
    }
    for site, url in links_by_site("Data Analyst", "Lahore").items():
        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == expected[site]


def test_title_and_location_in_query_params() -> None:
    links = links_by_site("Machine Learning Engineer", "Lahore")
    assert query_params(links["LinkedIn"]) == {
        "keywords": ["Machine Learning Engineer"],
        "location": ["Lahore"],
    }
    assert query_params(links["Indeed"]) == {"q": ["Machine Learning Engineer"], "l": ["Lahore"]}
    assert query_params(links["Glassdoor"]) == {
        "sc.keyword": ["Machine Learning Engineer"],
        "locKeyword": ["Lahore"],
    }


def test_rozee_puts_title_and_location_in_path() -> None:
    url = links_by_site("Machine Learning Engineer", "Lahore")["Rozee.pk"]
    assert url == "https://www.rozee.pk/job/jsearch/q/Machine%20Learning%20Engineer%20Lahore"


def test_special_characters_are_encoded() -> None:
    links = links_by_site("C++ & C# Developer", "São Paulo")
    # No raw "+", "&", "#" or spaces leak into the URLs, so they can't break them.
    for url in links.values():
        assert " " not in url
        assert "#" not in url
    # Decoding gives back exactly what we put in.
    assert query_params(links["Indeed"]) == {"q": ["C++ & C# Developer"], "l": ["São Paulo"]}


def test_slash_in_title_does_not_break_rozee_path() -> None:
    url = links_by_site("UI/UX Designer")["Rozee.pk"]
    assert url == "https://www.rozee.pk/job/jsearch/q/UI%2FUX%20Designer"


def test_empty_location_is_left_out() -> None:
    links = links_by_site("Data Analyst", "  ")
    assert query_params(links["LinkedIn"]) == {"keywords": ["Data Analyst"]}
    assert query_params(links["Indeed"]) == {"q": ["Data Analyst"]}
    assert links["Rozee.pk"].endswith("/q/Data%20Analyst")


def test_remote_filters_are_added_when_requested() -> None:
    links = links_by_site("Data Analyst", "Lahore", remote_only=True)
    assert query_params(links["LinkedIn"])["f_WT"] == ["2"]
    assert "remotejobs" in query_params(links["Indeed"])
    assert query_params(links["Glassdoor"])["remoteWorkType"] == ["1"]
    assert links["Rozee.pk"].endswith("%20remote")


def test_no_remote_filters_by_default() -> None:
    links = links_by_site("Data Analyst", "Lahore")
    assert "f_WT" not in links["LinkedIn"]
    assert "remotejobs" not in links["Indeed"]
    assert "remoteWorkType" not in links["Glassdoor"]
    assert "remote" not in links["Rozee.pk"]


@pytest.mark.parametrize("module_name", ["requests", "urllib.request", "httpx"])
def test_module_never_imports_network_libraries(module_name: str) -> None:
    # search_links must only build URLs, never download pages.
    source = open(search_links.__file__, encoding="utf-8").read()
    assert f"import {module_name}" not in source
    assert f"from {module_name}" not in source

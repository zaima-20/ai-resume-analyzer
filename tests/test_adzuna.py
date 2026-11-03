"""Tests for src/jobs/adzuna.py. All network calls are mocked; Adzuna is never contacted.

The fake reply below is made up, but follows the JobSearchResults shape in
Adzuna's official API spec (results[].title, company.display_name, ...).
"""

import copy
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from src import cache, config
from src.jobs import adzuna
from src.jobs.adzuna import (
    LOCATION_TO_COUNTRY,
    COUNTRY_WORDS,
    SUPPORTED_COUNTRIES,
    AdzunaSource,
    find_country,
    to_job,
)
from src.jobs.base import JobSourceError

FAKE_REPLY: dict = {
    "count": 2,
    "mean": 65000,
    "results": [
        {
            "id": "4900000001",
            "title": "<strong>Machine Learning</strong> Engineer",
            "company": {"display_name": "Demo Analytics Ltd"},
            "location": {"display_name": "London, UK", "area": ["UK", "London"]},
            "description": "Build ML models in Python &amp; deploy them. Hybrid, 2 days in office&hellip;",
            "created": "2026-10-30T09:15:00Z",
            "redirect_url": "https://www.adzuna.co.uk/jobs/land/ad/4900000001?se=test",
        },
        {
            "id": "4900000002",
            "title": "Remote Data Scientist",
            "company": {"display_name": "Example Health"},
            "location": {"display_name": "Manchester, Greater Manchester"},
            "description": "Fully remote role working on forecasting models.",
            "created": "2026-10-28T12:00:00Z",
            "redirect_url": "https://www.adzuna.co.uk/jobs/land/ad/4900000002?se=test",
        },
    ],
}


def fake_response(json_data: object = None, status: int = 200) -> MagicMock:
    """A stand-in for requests.Response."""
    response = MagicMock(status_code=status)
    response.json.return_value = copy.deepcopy(json_data)
    return response


@pytest.fixture(autouse=True)
def isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Temporary cache, fake keys, and a guard that fails on any real network call."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    for name, value in [("ADZUNA_APP_ID", "fake-id"), ("ADZUNA_APP_KEY", "fake-key")]:
        monkeypatch.setenv(name, value)
        monkeypatch.setattr(config, name, value)

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("A real network call was made in a test")

    monkeypatch.setattr(adzuna.requests, "get", no_network)


@pytest.fixture
def mock_get(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace requests.get with a mock that returns FAKE_REPLY."""
    mock = MagicMock(return_value=fake_response(FAKE_REPLY))
    monkeypatch.setattr(adzuna.requests, "get", mock)
    return mock


# --- Country lookup ---


@pytest.mark.parametrize(
    ("location", "country", "where"),
    [
        ("London", "gb", "London"),
        ("UK", "gb", ""),
        ("U.K.", "gb", ""),
        ("Manchester, UK", "gb", "Manchester"),
        ("Leeds, United Kingdom", "gb", "Leeds"),
        ("New York", "us", "New York"),
        ("USA", "us", ""),
        ("San Francisco, CA", "us", "San Francisco, CA"),
        ("Berlin, Germany", "de", "Berlin"),
        ("  bengaluru ", "in", "bengaluru"),
        ("Singapore", "sg", "Singapore"),
    ],
)
def test_find_country(location: str, country: str, where: str) -> None:
    assert find_country(location) == (country, where)


@pytest.mark.parametrize(
    ("location", "name"),
    [
        ("Lahore", "Pakistan"),
        ("Pakistan", "Pakistan"),
        ("Lahore, Pakistan", "Pakistan"),
        ("Dubai", "the UAE"),
    ],
)
def test_unsupported_country_is_named(location: str, name: str) -> None:
    with pytest.raises(JobSourceError, match=f"Adzuna doesn't cover {name}, so it was skipped."):
        find_country(location)


def test_unknown_location_asks_for_country() -> None:
    with pytest.raises(JobSourceError, match="couldn't tell which country"):
        find_country("Springfield")


def test_empty_location() -> None:
    with pytest.raises(JobSourceError, match="needs a location"):
        find_country("  ")


def test_mapping_only_uses_supported_codes() -> None:
    assert set(LOCATION_TO_COUNTRY.values()) <= set(SUPPORTED_COUNTRIES)
    assert len(SUPPORTED_COUNTRIES) == 19


def test_country_words_are_all_known_locations() -> None:
    assert COUNTRY_WORDS <= set(LOCATION_TO_COUNTRY)


def test_unsupported_country_makes_no_api_call(mock_get: MagicMock) -> None:
    with pytest.raises(JobSourceError, match="Pakistan"):
        AdzunaSource().search("Machine Learning Engineer", "Lahore", remote_only=False)
    mock_get.assert_not_called()


# --- search() ---


def test_results_are_converted(mock_get: MagicMock) -> None:
    jobs = AdzunaSource().search("Machine Learning Engineer", "London", remote_only=False)

    assert len(jobs) == 2
    first = jobs[0]
    assert first.title == "Machine Learning Engineer"  # <strong> tags removed
    assert first.company == "Demo Analytics Ltd"
    assert first.location == "London, UK"
    assert first.description == "Build ML models in Python & deploy them. Hybrid, 2 days in office…"
    assert first.url == FAKE_REPLY["results"][0]["redirect_url"]
    assert first.source == "Adzuna"
    assert first.posted_date == date(2026, 10, 30)
    assert first.remote is False
    assert jobs[1].remote is True  # "remote" is in the title


def test_request_url_and_params(mock_get: MagicMock) -> None:
    AdzunaSource().search("Machine Learning Engineer", "Manchester, UK", remote_only=False)

    args, kwargs = mock_get.call_args
    assert args == ("https://api.adzuna.com/v1/api/jobs/gb/search/1",)
    assert kwargs["params"] == {
        "app_id": "fake-id",
        "app_key": "fake-key",
        "what": "Machine Learning Engineer",
        "where": "Manchester",
        "results_per_page": adzuna.RESULTS_PER_PAGE,
        "content-type": "application/json",
    }
    assert kwargs["timeout"] == adzuna.REQUEST_TIMEOUT


def test_country_only_location_sends_no_where(mock_get: MagicMock) -> None:
    AdzunaSource().search("Data Analyst", "Germany", remote_only=False)
    args, kwargs = mock_get.call_args
    assert args == ("https://api.adzuna.com/v1/api/jobs/de/search/1",)
    assert "where" not in kwargs["params"]


def test_remote_only_adds_word_to_search(mock_get: MagicMock) -> None:
    AdzunaSource().search("Data Analyst", "London", remote_only=True)
    assert mock_get.call_args.kwargs["params"]["what"] == "Data Analyst remote"


def test_same_search_uses_cache(mock_get: MagicMock) -> None:
    source = AdzunaSource()
    first = source.search("Data Analyst", "London", remote_only=False)
    second = source.search("data analyst", "london", remote_only=False)
    assert first == second
    assert mock_get.call_count == 1


def test_errors_are_not_cached(mock_get: MagicMock) -> None:
    mock_get.side_effect = [requests.ConnectionError("offline"), fake_response(FAKE_REPLY)]
    source = AdzunaSource()
    with pytest.raises(JobSourceError):
        source.search("Data Analyst", "London", remote_only=False)
    assert len(source.search("Data Analyst", "London", remote_only=False)) == 2


def test_no_results(mock_get: MagicMock) -> None:
    mock_get.return_value = fake_response({"count": 0, "results": []})
    assert AdzunaSource().search("Underwater welder", "London", remote_only=False) == []


# --- Errors ---


@pytest.mark.parametrize("missing", ["ADZUNA_APP_ID", "ADZUNA_APP_KEY"])
def test_missing_key(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    monkeypatch.delenv(missing)
    # The autouse guard fails the test if a request is attempted.
    with pytest.raises(JobSourceError, match="ADZUNA_APP_ID and ADZUNA_APP_KEY"):
        AdzunaSource().search("Data Analyst", "London", remote_only=False)


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (410, "rejected the app ID or key"),  # Adzuna's "Authorisation failed"
        (401, "rejected the app ID or key"),
        (429, "request limit"),
        (400, "status 400"),
        (500, "status 500"),
    ],
)
def test_http_errors(mock_get: MagicMock, status: int, message: str) -> None:
    mock_get.return_value = fake_response({"exception": "AUTH_FAIL"}, status=status)
    with pytest.raises(JobSourceError, match=message):
        AdzunaSource().search("Data Analyst", "London", remote_only=False)


@pytest.mark.parametrize(
    ("side_effect", "message"),
    [
        (requests.Timeout("slow"), "too long"),
        (requests.ConnectionError("offline"), "could not be reached"),
    ],
)
def test_network_errors(mock_get: MagicMock, side_effect: Exception, message: str) -> None:
    mock_get.side_effect = side_effect
    with pytest.raises(JobSourceError, match=message):
        AdzunaSource().search("Data Analyst", "London", remote_only=False)


def test_invalid_json(mock_get: MagicMock) -> None:
    response = fake_response()
    response.json.side_effect = ValueError("not json")
    mock_get.return_value = response
    with pytest.raises(JobSourceError, match="couldn't be read"):
        AdzunaSource().search("Data Analyst", "London", remote_only=False)


@pytest.mark.parametrize("body", [{"count": 3}, {"results": "oops"}, ["a", "list"]])
def test_unexpected_shape(mock_get: MagicMock, body: object) -> None:
    mock_get.return_value = fake_response(body)
    with pytest.raises(JobSourceError, match="unexpected format"):
        AdzunaSource().search("Data Analyst", "London", remote_only=False)


# --- to_job() edge cases ---


def test_items_without_title_or_link_are_skipped() -> None:
    assert to_job({"title": "Analyst"}) is None
    assert to_job({"redirect_url": "https://x"}) is None
    assert to_job({"title": "<b></b>", "redirect_url": "https://x"}) is None


def test_missing_company_location_and_date() -> None:
    job = to_job({"title": "Analyst", "redirect_url": "https://x", "created": "soon"})
    assert job.company == "Unknown company"
    assert job.location == "Location not given"
    assert job.posted_date is None
    assert job.description == ""

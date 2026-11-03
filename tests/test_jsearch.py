"""Tests for src/jobs/jsearch.py. Uses a saved /search-v2 reply; the network is always mocked."""

import copy
import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from src import cache, config
from src.jobs import jsearch
from src.jobs.base import JobSourceError
from src.jobs.jsearch import JSearchSource, build_query, to_job

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "jsearch_sample.json").read_text(encoding="utf-8")
)
FIXTURE_JOBS: list[dict] = FIXTURE["data"]["jobs"]


def fake_response(json_data: object = None, status: int = 200) -> MagicMock:
    """A stand-in for requests.Response."""
    response = MagicMock(status_code=status)
    response.json.return_value = copy.deepcopy(json_data)
    return response


@pytest.fixture(autouse=True)
def isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Temporary cache, a fake key, and a guard that fails on any real network call."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("RAPIDAPI_KEY", "fake-test-key")
    monkeypatch.setattr(config, "RAPIDAPI_KEY", "fake-test-key")

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("A real network call was made in a test")

    monkeypatch.setattr(jsearch.requests, "get", no_network)


@pytest.fixture
def mock_get(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace requests.get with a mock that returns the saved fixture."""
    mock = MagicMock(return_value=fake_response(FIXTURE))
    monkeypatch.setattr(jsearch.requests, "get", mock)
    return mock


# --- Converting the fixture ---


def test_fixture_jobs_are_converted(mock_get: MagicMock) -> None:
    jobs = JSearchSource().search("Machine Learning Engineer", "Lahore", remote_only=False)

    assert len(jobs) == 3
    first = jobs[0]
    assert first.title == "Senior AI Solutions Engineer"
    assert first.company == "ibex"
    assert first.location == "Lahore, Pakistan"
    assert first.remote is False
    assert first.url.startswith("https://pk.linkedin.com/jobs/view/")
    assert first.posted_date == date(2026, 10, 29)
    assert "LLM" in first.description


def test_source_shows_original_publisher(mock_get: MagicMock) -> None:
    jobs = JSearchSource().search("Machine Learning Engineer", "Lahore", remote_only=False)
    assert [job.source for job in jobs] == [
        "JSearch (LinkedIn)",
        "JSearch (Workable Jobs)",
        "JSearch (Jobrapido.com)",
    ]


def test_missing_posted_date_is_none(mock_get: MagicMock) -> None:
    jobs = JSearchSource().search("Machine Learning Engineer", "Lahore", remote_only=False)
    assert jobs[1].posted_date is None


# --- The request ---


def test_request_headers_query_and_timeout(mock_get: MagicMock) -> None:
    JSearchSource().search("Machine Learning Engineer", "Lahore", remote_only=False)

    args, kwargs = mock_get.call_args
    assert args == (config.JSEARCH_URL,)
    assert kwargs["headers"] == {
        "X-RapidAPI-Key": "fake-test-key",
        "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
    }
    assert kwargs["params"] == {"query": "Machine Learning Engineer in Lahore"}
    assert kwargs["timeout"] == jsearch.REQUEST_TIMEOUT


def test_remote_only_sends_option_and_filters_results(mock_get: MagicMock) -> None:
    reply = copy.deepcopy(FIXTURE)
    reply["data"]["jobs"][0]["job_is_remote"] = True
    reply["data"]["jobs"][1]["job_title"] = "Remote Data Engineer"  # flag False, title says remote
    mock_get.return_value = fake_response(reply)

    jobs = JSearchSource().search("Engineer", "", remote_only=True)

    assert mock_get.call_args.kwargs["params"] == {"query": "Engineer", "work_from_home": "true"}
    assert [job.title for job in jobs] == ["Senior AI Solutions Engineer", "Remote Data Engineer"]
    assert all(job.remote for job in jobs)


@pytest.mark.parametrize(
    ("title", "location", "expected"),
    [
        ("Data Analyst", "Lahore", "Data Analyst in Lahore"),
        ("  Data   Analyst ", " Lahore, Pakistan ", "Data Analyst in Lahore, Pakistan"),
        ("Data Analyst", "", "Data Analyst"),
    ],
)
def test_build_query(title: str, location: str, expected: str) -> None:
    assert build_query(title, location) == expected


# --- Cache ---


def test_same_search_uses_cache(mock_get: MagicMock) -> None:
    source = JSearchSource()
    first = source.search("Machine Learning Engineer", "Lahore", remote_only=False)
    second = source.search("machine learning engineer", "lahore", remote_only=False)
    assert first == second
    assert mock_get.call_count == 1


def test_remote_setting_is_cached_separately(mock_get: MagicMock) -> None:
    source = JSearchSource()
    source.search("Engineer", "Lahore", remote_only=False)
    source.search("Engineer", "Lahore", remote_only=True)
    assert mock_get.call_count == 2


def test_errors_are_not_cached(mock_get: MagicMock) -> None:
    mock_get.side_effect = [fake_response(status=429), fake_response(FIXTURE)]
    source = JSearchSource()
    with pytest.raises(JobSourceError):
        source.search("Engineer", "Lahore", remote_only=False)
    assert len(source.search("Engineer", "Lahore", remote_only=False)) == 3


# --- Errors ---


@pytest.mark.parametrize("value", [None, "", "your_rapidapi_key_here"])
def test_missing_key(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("RAPIDAPI_KEY")
    else:
        monkeypatch.setenv("RAPIDAPI_KEY", value)
    # The autouse guard fails the test if a request is attempted.
    with pytest.raises(JobSourceError, match="RAPIDAPI_KEY"):
        JSearchSource().search("Engineer", "Lahore", remote_only=False)


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "rejected the API key"),
        (403, "not subscribed"),
        (429, "quota is used up"),
        (500, "status 500"),
    ],
)
def test_http_errors(mock_get: MagicMock, status: int, message: str) -> None:
    mock_get.return_value = fake_response({"message": "error"}, status=status)
    with pytest.raises(JobSourceError, match=message):
        JSearchSource().search("Engineer", "Lahore", remote_only=False)


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
        JSearchSource().search("Engineer", "Lahore", remote_only=False)


def test_invalid_json(mock_get: MagicMock) -> None:
    response = fake_response()
    response.json.side_effect = ValueError("not json")
    mock_get.return_value = response
    with pytest.raises(JobSourceError, match="couldn't be read"):
        JSearchSource().search("Engineer", "Lahore", remote_only=False)


@pytest.mark.parametrize(
    "body",
    [{"status": "OK"}, {"data": {"cursor": "x"}}, {"data": "oops"}, ["not", "a", "dict"]],
)
def test_unexpected_shape(mock_get: MagicMock, body: object) -> None:
    mock_get.return_value = fake_response(body)
    with pytest.raises(JobSourceError, match="unexpected format"):
        JSearchSource().search("Engineer", "Lahore", remote_only=False)


def test_old_search_v1_shape_still_works(mock_get: MagicMock) -> None:
    mock_get.return_value = fake_response({"status": "OK", "data": FIXTURE_JOBS})
    assert len(JSearchSource().search("Engineer", "Lahore", remote_only=False)) == 3


def test_no_results(mock_get: MagicMock) -> None:
    mock_get.return_value = fake_response({"status": "OK", "data": {"jobs": [], "cursor": None}})
    assert JSearchSource().search("Underwater welder", "Lahore", remote_only=False) == []


# --- to_job() edge cases ---


def test_bad_items_are_skipped(mock_get: MagicMock) -> None:
    reply = copy.deepcopy(FIXTURE)
    reply["data"]["jobs"] += [{"job_title": "No link at all"}, {"job_apply_link": "https://x"}, "text"]
    mock_get.return_value = fake_response(reply)
    assert len(JSearchSource().search("Engineer", "Lahore", remote_only=False)) == 3


def test_apply_link_falls_back_to_apply_options_then_google() -> None:
    item = copy.deepcopy(FIXTURE_JOBS[0])
    item["job_apply_link"] = None
    item["apply_options"] = [{"apply_link": "https://example.com/apply", "publisher": "Example"}]
    assert to_job(item).url == "https://example.com/apply"

    item["apply_options"] = []
    assert to_job(item).url.startswith("https://www.google.com/search")


def test_missing_publisher_company_and_location() -> None:
    item = {"job_title": "Engineer", "job_apply_link": "https://x", "job_is_remote": True}
    job = to_job(item)
    assert job.source == "JSearch"
    assert job.company == "Unknown company"
    assert job.location == "Remote"


def test_posted_date_from_timestamp_when_text_missing() -> None:
    item = {**FIXTURE_JOBS[0], "job_posted_at_datetime_utc": None}
    assert to_job(item).posted_date == date(2026, 10, 29)

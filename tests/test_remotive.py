"""Tests for src/jobs/remotive.py. All network calls are mocked; Remotive is never contacted."""

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from src import cache
from src.jobs import remotive
from src.jobs.base import JobSourceError
from src.jobs.remotive import REMOTIVE_URL, RemotiveSource, html_to_text, to_job

SAMPLE_ITEM: dict = {
    "id": 101,
    "url": "https://remotive.com/remote-jobs/data/ml-engineer-101",
    "title": "Machine Learning Engineer",
    "company_name": "Demo AI Ltd",
    "candidate_required_location": "Worldwide",
    "publication_date": "2026-10-30T09:15:00",
    "description": "<p>Build models with <b>Python</b> &amp; SQL.</p><ul><li>PyTorch</li><li>MLOps</li></ul>",
}


def fake_response(json_data: object = None, status: int = 200) -> MagicMock:
    """A stand-in for requests.Response."""
    response = MagicMock(status_code=status)
    response.json.return_value = json_data
    if status >= 400:
        error = requests.HTTPError(response=response)
        response.raise_for_status.side_effect = error
    return response


@pytest.fixture(autouse=True)
def isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Use a temporary cache and make any unmocked network call fail the test."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("A real network call was made in a test")

    monkeypatch.setattr(remotive.requests, "get", no_network)


@pytest.fixture
def mock_get(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace requests.get with a mock that returns one sample job by default."""
    mock = MagicMock(return_value=fake_response({"job-count": 1, "jobs": [SAMPLE_ITEM]}))
    monkeypatch.setattr(remotive.requests, "get", mock)
    return mock


# --- search() ---


def test_search_converts_results_to_jobs(mock_get: MagicMock) -> None:
    jobs = RemotiveSource().search("machine learning", "Lahore", remote_only=False)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Machine Learning Engineer"
    assert job.company == "Demo AI Ltd"
    assert job.location == "Remote (Worldwide)"
    assert job.remote is True
    assert job.source == "Remotive"
    assert job.url == SAMPLE_ITEM["url"]  # original link kept, as Remotive requires
    assert job.posted_date == date(2026, 10, 30)
    assert job.description == "Build models with Python & SQL.\n- PyTorch\n- MLOps"


def test_request_uses_search_limit_and_timeout(mock_get: MagicMock) -> None:
    RemotiveSource(limit=20).search("machine learning", "", remote_only=True)

    args, kwargs = mock_get.call_args
    assert args == (REMOTIVE_URL,)
    assert kwargs["params"] == {"search": "machine learning", "limit": 20}
    assert kwargs["timeout"] == remotive.REQUEST_TIMEOUT


def test_same_search_uses_cache(mock_get: MagicMock) -> None:
    source = RemotiveSource()
    first = source.search("machine learning", "", remote_only=True)
    second = source.search("Machine  Learning", "", remote_only=True)

    assert first == second
    assert mock_get.call_count == 1


def test_different_search_fetches_again(mock_get: MagicMock) -> None:
    source = RemotiveSource()
    source.search("machine learning", "", remote_only=True)
    source.search("data analyst", "", remote_only=True)
    assert mock_get.call_count == 2


def test_errors_are_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = MagicMock(
        side_effect=[
            requests.ConnectionError("offline"),
            fake_response({"jobs": [SAMPLE_ITEM]}),
        ]
    )
    monkeypatch.setattr(remotive.requests, "get", mock)
    source = RemotiveSource()

    with pytest.raises(JobSourceError):
        source.search("machine learning", "", remote_only=True)
    assert len(source.search("machine learning", "", remote_only=True)) == 1
    assert mock.call_count == 2


def test_bad_items_are_skipped(mock_get: MagicMock) -> None:
    mock_get.return_value = fake_response(
        {"jobs": [SAMPLE_ITEM, {"title": "No URL"}, {"url": "https://x"}, "not a dict"]}
    )
    jobs = RemotiveSource().search("anything", "", remote_only=False)
    assert [job.title for job in jobs] == ["Machine Learning Engineer"]


def test_no_results_returns_empty_list(mock_get: MagicMock) -> None:
    mock_get.return_value = fake_response({"job-count": 0, "jobs": []})
    assert RemotiveSource().search("underwater basket weaving", "", remote_only=True) == []


# --- Errors become JobSourceError ---


@pytest.mark.parametrize(
    ("side_effect", "message"),
    [
        (requests.Timeout("slow"), "too long"),
        (requests.ConnectionError("offline"), "could not be reached"),
    ],
)
def test_network_errors(
    monkeypatch: pytest.MonkeyPatch, side_effect: Exception, message: str
) -> None:
    monkeypatch.setattr(remotive.requests, "get", MagicMock(side_effect=side_effect))
    with pytest.raises(JobSourceError, match=message):
        RemotiveSource().search("python", "", remote_only=True)


def test_http_error_status(mock_get: MagicMock) -> None:
    mock_get.return_value = fake_response(status=503)
    with pytest.raises(JobSourceError, match="status 503"):
        RemotiveSource().search("python", "", remote_only=True)


def test_invalid_json(mock_get: MagicMock) -> None:
    response = fake_response()
    response.json.side_effect = ValueError("not json")
    mock_get.return_value = response
    with pytest.raises(JobSourceError, match="couldn't be read"):
        RemotiveSource().search("python", "", remote_only=True)


@pytest.mark.parametrize("data", [{"jobs": "oops"}, {"no_jobs": []}, ["a", "list"]])
def test_unexpected_shape(mock_get: MagicMock, data: object) -> None:
    mock_get.return_value = fake_response(data)
    with pytest.raises(JobSourceError, match="unexpected format"):
        RemotiveSource().search("python", "", remote_only=True)


# --- Helpers ---


def test_to_job_defaults_for_missing_fields() -> None:
    job = to_job({"title": " Analyst ", "url": "https://remotive.com/x"})
    assert job is not None
    assert job.title == "Analyst"
    assert job.company == "Unknown company"
    assert job.location == "Remote (Worldwide)"
    assert job.description == ""
    assert job.posted_date is None


def test_to_job_bad_date_becomes_none() -> None:
    job = to_job({**SAMPLE_ITEM, "publication_date": "last week"})
    assert job is not None and job.posted_date is None


@pytest.mark.parametrize(
    ("html", "text"),
    [
        ("<p>Hello</p><p>World</p>", "Hello\nWorld"),
        ("Line one<br>Line two", "Line one\nLine two"),
        ("<ul><li>SQL</li><li>Python</li></ul>", "- SQL\n- Python"),
        ("Tom &amp; Jerry &lt;3 &nbsp;caf&eacute;", "Tom & Jerry <3 café"),
        ("<p>  lots   of\n\n spaces </p>", "lots of\nspaces"),
        ("<script>ignored()</script><style>p {}</style>Plain", "Plain"),
        ("", ""),
    ],
)
def test_html_to_text(html: str, text: str) -> None:
    assert html_to_text(html) == text

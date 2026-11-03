"""Tests for src/jobs/base.py."""

from datetime import date

import pytest

from src.jobs.base import Job, JobSource, JobSourceError


def make_job(**changes: object) -> Job:
    """Build a Job with sensible test values, overriding any given fields."""
    values: dict = {
        "title": "Data Analyst",
        "company": "Demo Co.",
        "location": "Lahore, Pakistan",
        "remote": False,
        "description": "Analyse sales data with SQL.",
        "url": "https://example.com/jobs/1",
        "source": "Test",
    }
    values.update(changes)
    return Job(**values)


class FakeSource(JobSource):
    """A minimal source that returns fixed jobs, used to test the interface."""

    name = "Fake"

    def search(self, query: str, location: str, remote_only: bool) -> list[Job]:
        jobs = [make_job(source=self.name), make_job(remote=True, source=self.name)]
        return [job for job in jobs if job.remote or not remote_only]


class BrokenSource(JobSource):
    """A source that always fails, like one with a missing API key."""

    name = "Broken"

    def search(self, query: str, location: str, remote_only: bool) -> list[Job]:
        raise JobSourceError("Broken: no API key set.")


def test_posted_date_is_optional() -> None:
    assert make_job().posted_date is None
    assert make_job(posted_date=date(2026, 10, 30)).posted_date == date(2026, 10, 30)


def test_jobs_with_same_values_are_equal() -> None:
    assert make_job() == make_job()
    assert make_job() != make_job(company="Other Co.")


def test_base_class_cannot_be_used_directly() -> None:
    with pytest.raises(TypeError):
        JobSource()  # type: ignore[abstract]


def test_subclass_without_search_cannot_be_created() -> None:
    class NoSearch(JobSource):
        name = "Incomplete"

    with pytest.raises(TypeError):
        NoSearch()  # type: ignore[abstract]


def test_subclass_returns_jobs() -> None:
    source = FakeSource()
    assert source.name == "Fake"
    assert len(source.search("analyst", "Lahore", remote_only=False)) == 2
    remote_jobs = source.search("analyst", "Lahore", remote_only=True)
    assert len(remote_jobs) == 1
    assert remote_jobs[0].remote is True


def test_failing_source_can_be_skipped() -> None:
    # This is how the app will run sources: one failure becomes a warning, not a crash.
    jobs: list[Job] = []
    warnings: list[str] = []
    for source in [BrokenSource(), FakeSource()]:
        try:
            jobs += source.search("analyst", "", remote_only=False)
        except JobSourceError as error:
            warnings.append(str(error))

    assert len(jobs) == 2
    assert warnings == ["Broken: no API key set."]

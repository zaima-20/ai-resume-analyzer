"""Tests for src/timing.py: timing logs only appear when DEBUG_TIMING is on."""

import logging

import pytest

from src import timing


@pytest.fixture
def records(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Collect messages sent to the timing logger."""
    collected: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            collected.append(record.getMessage())

    handler = Collect()
    timing.logger.addHandler(handler)
    monkeypatch.setattr(timing.logger, "level", logging.INFO)
    yield collected
    timing.logger.removeHandler(handler)


def test_nothing_is_logged_when_debug_timing_is_off(
    monkeypatch: pytest.MonkeyPatch, records: list[str]
) -> None:
    monkeypatch.setattr(timing, "DEBUG_TIMING", False)
    with timing.timed("some step"):
        pass
    assert records == []


def test_timed_logs_label_and_milliseconds(
    monkeypatch: pytest.MonkeyPatch, records: list[str]
) -> None:
    monkeypatch.setattr(timing, "DEBUG_TIMING", True)
    with timing.timed("rank_jobs"):
        pass
    [message] = records
    label, ms = message.split(" | ")
    assert label == "rank_jobs"
    assert ms.endswith(" ms") and float(ms.split()[0]) >= 0


def test_timed_still_logs_and_reraises_on_error(
    monkeypatch: pytest.MonkeyPatch, records: list[str]
) -> None:
    monkeypatch.setattr(timing, "DEBUG_TIMING", True)
    with pytest.raises(ValueError):
        with timing.timed("failing step"):
            raise ValueError("boom")
    assert records[0].startswith("failing step | ")


def test_timed_call_uses_function_name_and_keeps_result(
    monkeypatch: pytest.MonkeyPatch, records: list[str]
) -> None:
    monkeypatch.setattr(timing, "DEBUG_TIMING", True)

    @timing.timed_call("supabase")
    def load_profile(x: int) -> int:
        """Docstring kept."""
        return x * 2

    assert load_profile(21) == 42
    assert load_profile.__name__ == "load_profile"
    assert records[0].startswith("supabase load_profile | ")


def test_timing_never_logs_arguments_or_results(
    monkeypatch: pytest.MonkeyPatch, records: list[str]
) -> None:
    # Arguments can include keys or resume data; only names and times are logged.
    monkeypatch.setattr(timing, "DEBUG_TIMING", True)

    @timing.timed_call("supabase")
    def save(secret: str) -> str:
        return secret

    save("super-secret-value")
    assert "super-secret-value" not in records[0]

"""Tests for src/cache.py. Every test uses its own temporary cache folder."""

import json
import time
from pathlib import Path

import pytest

from src import cache
from src.config import CACHE_HOURS

KEY = cache.make_key("Remotive", "data analyst", "Lahore", False)


@pytest.fixture(autouse=True)
def temp_cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the cache at a temporary folder so tests never touch the real cache/."""
    folder = tmp_path / "cache"
    monkeypatch.setattr(cache, "CACHE_DIR", folder)
    return folder


def set_age(key: str, hours: float) -> None:
    """Rewrite a cache file so it looks like it was saved `hours` ago."""
    path = cache.cache_path(key)
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["saved_at"] = time.time() - hours * 3600
    path.write_text(json.dumps(entry), encoding="utf-8")


# --- get() and set() ---


def test_set_then_get_returns_data(temp_cache_dir: Path) -> None:
    data = [{"title": "Data Analyst", "tags": ["sql"]}]
    assert cache.set(KEY, data) is True
    assert cache.get(KEY) == data
    assert temp_cache_dir.is_dir()  # the folder is created when needed


def test_get_unknown_key_returns_none() -> None:
    assert cache.get("never saved") is None


def test_fresh_entry_is_returned() -> None:
    cache.set(KEY, [1, 2, 3])
    set_age(KEY, CACHE_HOURS - 0.1)
    assert cache.get(KEY) == [1, 2, 3]


def test_expired_entry_returns_none() -> None:
    cache.set(KEY, [1, 2, 3])
    set_age(KEY, CACHE_HOURS + 0.1)
    assert cache.get(KEY) is None


def test_entry_from_the_future_returns_none() -> None:
    # A wrong clock shouldn't make an entry live forever.
    cache.set(KEY, [1])
    set_age(KEY, -1)
    assert cache.get(KEY) is None


def test_empty_list_is_cached() -> None:
    # A search that found nothing is still a valid result worth caching.
    cache.set(KEY, [])
    assert cache.get(KEY) == []


def test_set_overwrites_same_key() -> None:
    cache.set(KEY, ["old"])
    cache.set(KEY, ["new"])
    assert cache.get(KEY) == ["new"]


def test_different_keys_do_not_overwrite_each_other() -> None:
    other = cache.make_key("Remotive", "data analyst", "Karachi", False)
    cache.set(KEY, ["Lahore jobs"])
    cache.set(other, ["Karachi jobs"])
    assert cache.get(KEY) == ["Lahore jobs"]
    assert cache.get(other) == ["Karachi jobs"]


# --- Broken files ---


@pytest.mark.parametrize(
    "contents",
    [
        "{not valid json",
        "",
        "[1, 2, 3]",
        json.dumps({"key": KEY, "data": [1]}),  # no saved_at
        json.dumps({"key": KEY, "saved_at": "yesterday", "data": [1]}),
        json.dumps({"saved_at": time.time(), "data": [1]}),  # no key
    ],
)
def test_broken_cache_file_is_ignored(temp_cache_dir: Path, contents: str) -> None:
    temp_cache_dir.mkdir()
    cache.cache_path(KEY).write_text(contents, encoding="utf-8")
    assert cache.get(KEY) is None


def test_broken_file_is_replaced_by_set(temp_cache_dir: Path) -> None:
    temp_cache_dir.mkdir()
    cache.cache_path(KEY).write_text("garbage", encoding="utf-8")
    cache.set(KEY, ["fresh"])
    assert cache.get(KEY) == ["fresh"]


def test_set_returns_false_when_folder_cannot_be_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocker = tmp_path / "a_file"
    blocker.write_text("not a folder")
    monkeypatch.setattr(cache, "CACHE_DIR", blocker / "cache")
    assert cache.set(KEY, [1]) is False
    assert cache.get(KEY) is None


# --- make_key() and cache_path() ---


def test_make_key_includes_every_part() -> None:
    assert cache.make_key("Remotive", "Data Analyst", "Lahore", True) == (
        "remotive|data analyst|lahore|remote"
    )


def test_make_key_ignores_case_and_spaces() -> None:
    assert cache.make_key("Remotive", "  Data   Analyst ", "LAHORE", False) == (
        cache.make_key("remotive", "data analyst", "lahore", False)
    )


@pytest.mark.parametrize(
    "changed",
    [
        ("Adzuna", "data analyst", "Lahore", False),
        ("Remotive", "data scientist", "Lahore", False),
        ("Remotive", "data analyst", "Karachi", False),
        ("Remotive", "data analyst", "Lahore", True),
    ],
)
def test_make_key_differs_when_any_part_changes(changed: tuple) -> None:
    assert cache.make_key(*changed) != KEY


def test_cache_path_is_a_safe_file_name(temp_cache_dir: Path) -> None:
    path = cache.cache_path(cache.make_key("Remotive", "UI/UX C++ dev?", "São Paulo", False))
    assert path.parent == temp_cache_dir
    assert path.suffix == ".json"
    assert path.stem.isalnum()

"""Simple JSON file cache to save API quota."""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from src.config import CACHE_HOURS

# cache/ in the project root (it's in .gitignore), no matter where the app is started from.
CACHE_DIR: Path = Path(__file__).resolve().parent.parent / "cache"


def make_key(source: str, query: str, location: str, remote_only: bool) -> str:
    """Build a cache key for one search, so different searches don't overwrite each other.

    Case and extra spaces are ignored, so "Data Analyst" and " data analyst" share a key.

    Example:
        make_key("Remotive", "Data Analyst", "Lahore", True)
        -> "remotive|data analyst|lahore|remote"
    """
    parts = [source, query, location, "remote" if remote_only else "any"]
    return "|".join(" ".join(part.lower().split()) for part in parts)


def cache_path(key: str) -> Path:
    """Return the file that stores a key.

    The key is hashed, so any text (spaces, slashes, "C++") gives a safe file name.
    """
    file_name = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return CACHE_DIR / f"{file_name}.json"


def get(key: str) -> Any | None:
    """Return the saved data for a key if it is younger than CACHE_HOURS, otherwise None.

    A missing, expired or broken cache file also returns None, so the caller
    simply fetches fresh data.
    """
    path = cache_path(key)
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        saved_at = float(entry["saved_at"])
        stored_key = entry["key"]
        data = entry["data"]
    except (OSError, ValueError, KeyError, TypeError):
        # No file yet, invalid JSON, or missing/wrong fields: treat it as not cached.
        return None

    if stored_key != key:
        return None
    age_hours = (time.time() - saved_at) / 3600
    if not 0 <= age_hours < CACHE_HOURS:
        return None
    return data


def set(key: str, data: Any) -> bool:
    """Save JSON-serialisable data for a key, together with the time it was saved.

    Returns:
        True if it was saved. False if it couldn't be written (e.g. no disk access);
        the app still works then, it just fetches again next time.
    """
    entry = {"key": key, "saved_at": time.time(), "data": data}
    path = cache_path(key)
    temp_path = path.with_suffix(".tmp")
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(json.dumps(entry), encoding="utf-8")
        # Replace in one step, so a crash mid-write can't leave a half-written cache file.
        os.replace(temp_path, path)
    except OSError:
        return False
    return True

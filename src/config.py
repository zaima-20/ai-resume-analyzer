"""Loads the API keys and defines app-wide constants.

Keys are read from Streamlit secrets first (used on Streamlit Community Cloud,
or a local .streamlit/secrets.toml), then from environment variables / .env
(used for local development). Missing keys are simply None.
"""

import os
from collections.abc import Mapping
from typing import Any

import streamlit as st
from dotenv import load_dotenv

# Read the .env file (if it exists) into the process environment.
# Keys already set in the real environment are NOT overwritten.
load_dotenv()


def read_secrets() -> Mapping[str, Any]:
    """Return Streamlit's secrets, or an empty dict if there is no secrets file.

    load_if_toml_exists() quietly returns False when no secrets.toml exists,
    so running locally with only .env never crashes or shows an error.
    """
    try:
        if not st.secrets.load_if_toml_exists():
            return {}
        return st.secrets
    except Exception:  # e.g. a broken secrets.toml: fall back to .env instead of crashing
        return {}


def get_setting(name: str) -> str | None:
    """Return a key from Streamlit secrets, falling back to the environment / .env.

    Only top-level text values in secrets count. Empty values count as missing.
    The value is a secret: never print or log it.

    Args:
        name: e.g. "GEMINI_API_KEY".

    Returns:
        The value, or None if it isn't set anywhere.
    """
    secrets = read_secrets()
    value = secrets.get(name) if name in secrets else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return os.getenv(name) or None


# --- API keys (None if not set) ---
# These values are secrets: never print or log them.
GEMINI_API_KEY: str | None = get_setting("GEMINI_API_KEY")
RAPIDAPI_KEY: str | None = get_setting("RAPIDAPI_KEY")
ADZUNA_APP_ID: str | None = get_setting("ADZUNA_APP_ID")
ADZUNA_APP_KEY: str | None = get_setting("ADZUNA_APP_KEY")
# Supabase project URL and anon/publishable key (safe for the client; Row Level
# Security in supabase/schema.sql protects the data). Never the service_role key.
SUPABASE_URL: str | None = get_setting("SUPABASE_URL")
SUPABASE_KEY: str | None = get_setting("SUPABASE_KEY")

# --- Constants ---
MAX_FILE_SIZE_MB: int = 5  # largest resume upload we accept
CACHE_HOURS: int = 6  # how long job search results stay cached
JSEARCH_URL: str = "https://jsearch.p.rapidapi.com/search-v2"
GEMINI_MODEL: str = "gemini-3.5-flash-lite"  # change here to use a different Gemini model

# Per-user daily limits, enforced by increment_usage() in supabase/schema.sql.
# They protect the free API quotas. Days reset at midnight UTC.
DAILY_SEARCH_LIMIT: int = 5
DAILY_AI_REVIEW_LIMIT: int = 3

# Set DEBUG_TIMING=1 to log how long each part of the app takes (see src/timing.py).
DEBUG_TIMING: bool = (get_setting("DEBUG_TIMING") or "").strip().lower() in {"1", "true", "yes"}


def has_key(name: str) -> bool:
    """Return True if the key `name` is set to a real value in secrets or the environment.

    Only reports whether the key exists; it never returns or prints the value.
    Placeholder values copied from .env.example or secrets.toml.example
    (e.g. "your_..._here") count as missing.

    Args:
        name: Key name, e.g. "GEMINI_API_KEY".

    Returns:
        True if a real value is present, otherwise False.
    """
    value = (get_setting(name) or "").strip()
    if not value:
        return False
    if value.startswith("your_") and value.endswith("_here"):
        return False
    return True

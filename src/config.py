"""Loads environment variables from .env and defines app-wide constants."""

import os

from dotenv import load_dotenv

# Read the .env file (if it exists) into the process environment.
# Keys already set in the real environment are NOT overwritten.
load_dotenv()

# --- API keys (None if not set) ---
# These values are secrets: never print or log them.
GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY")
RAPIDAPI_KEY: str | None = os.getenv("RAPIDAPI_KEY")
ADZUNA_APP_ID: str | None = os.getenv("ADZUNA_APP_ID")
ADZUNA_APP_KEY: str | None = os.getenv("ADZUNA_APP_KEY")
# Supabase project URL and anon/publishable key (safe for the client; Row Level
# Security in supabase/schema.sql protects the data). Never the service_role key.
SUPABASE_URL: str | None = os.getenv("SUPABASE_URL")
SUPABASE_KEY: str | None = os.getenv("SUPABASE_KEY")

# --- Constants ---
MAX_FILE_SIZE_MB: int = 5  # largest resume upload we accept
CACHE_HOURS: int = 6  # how long job search results stay cached
JSEARCH_URL: str = "https://jsearch.p.rapidapi.com/search-v2"
GEMINI_MODEL: str = "gemini-3.5-flash-lite"  # change here to use a different Gemini model

# Per-user daily limits, enforced by increment_usage() in supabase/schema.sql.
# They protect the free API quotas. Days reset at midnight UTC.
DAILY_SEARCH_LIMIT: int = 5
DAILY_AI_REVIEW_LIMIT: int = 3


def has_key(name: str) -> bool:
    """Return True if the environment variable `name` is set to a non-empty value.

    Only reports whether the key exists; it never returns or prints the value.
    Placeholder values copied from .env.example (e.g. "your_..._here") count as missing.

    Args:
        name: Environment variable name, e.g. "GEMINI_API_KEY".

    Returns:
        True if a real value is present, otherwise False.
    """
    value = os.getenv(name, "").strip()
    if not value:
        return False
    if value.startswith("your_") and value.endswith("_here"):
        return False
    return True

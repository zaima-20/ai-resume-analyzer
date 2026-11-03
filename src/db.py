"""Reads and writes the logged-in user's data in Supabase.

Every call uses the visitor's own Supabase client from st.session_state
(see auth.py), so requests carry that user's login token. The database's
Row Level Security rules (supabase/schema.sql) then only allow access to
rows where user_id is that user.

The resume file and its full text are never stored: only the file name,
word count, rule-check issues and AI review.
"""

from collections.abc import MutableMapping
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timezone
from typing import Any

import httpx
from postgrest.exceptions import APIError
from supabase import Client
from supabase_auth.errors import AuthError as SupabaseAuthError

from src.auth import AuthError, current_user, get_client, visitor_state
from src.jobs.base import Job
from src.llm_review import LLMReview
from src.matcher import ScoredJob
from src.profile import UserProfile
from src.rule_checks import Issue

MAX_PAST_ANALYSES: int = 20
MAX_SAVED_JOBS: int = 100
UNIQUE_VIOLATION: str = "23505"  # Postgres error code: a unique rule was broken

# Messages describe the problem only; app.py says what didn't happen ("Not saved...").
NETWORK_MESSAGE = "Couldn't reach the database. The app still works as normal."
LOGIN_EXPIRED_MESSAGE = "Your login has expired. Log out and log in again."


class DatabaseError(Exception):
    """A database problem. The message is safe to show to the user as a warning."""


@dataclass
class SavedAnalysis:
    """One analysis loaded from the database."""

    id: int
    created_at: datetime
    file_name: str
    word_count: int
    issues: list[dict] = field(default_factory=list)
    ai_review: dict | None = None

    @property
    def overall_score(self) -> int | None:
        """The AI overall score, or None if no AI review was saved."""
        return self.ai_review.get("overall_score") if self.ai_review else None

    @property
    def ats_score(self) -> int | None:
        """The AI ATS score, or None if no AI review was saved."""
        return self.ai_review.get("ats_score") if self.ai_review else None


@dataclass
class SavedJob:
    """One job from the user's saved list."""

    job: Job
    score: int
    saved_at: datetime


# ---------- Connection and errors ----------


def friendly_error(error: Exception) -> DatabaseError:
    """Turn a Supabase, Postgres or network error into a DatabaseError with a safe message."""
    if isinstance(error, httpx.HTTPError):
        return DatabaseError(NETWORK_MESSAGE)
    if isinstance(error, APIError):
        code = str(error.code or "")
        # PGRST301/PGRST303: expired or invalid token. 42501: blocked by a security rule.
        if code in {"PGRST301", "PGRST303", "42501"}:
            return DatabaseError(LOGIN_EXPIRED_MESSAGE)
        if code in {"42P01", "PGRST205"}:  # table not found
            return DatabaseError(
                "The database tables are missing. Run supabase/schema.sql in the Supabase SQL Editor."
            )
    return DatabaseError("The database had a problem. Please try again later.")


def user_client(state: MutableMapping[str, Any] | None = None) -> tuple[Client, str]:
    """Return this visitor's client and user ID, with a fresh login token.

    get_session() refreshes the token if it has expired, so long visits keep working.

    Raises:
        DatabaseError: If nobody is logged in or the session can't be refreshed.
    """
    state = visitor_state(state)
    user = current_user(state)
    if user is None:
        raise DatabaseError("Log in to save your data.")
    try:
        client = get_client(state)
        session = client.auth.get_session()
    except AuthError as error:
        raise DatabaseError(str(error)) from error
    except (SupabaseAuthError, httpx.HTTPError) as error:
        raise DatabaseError(NETWORK_MESSAGE) from error
    if session is None:
        raise DatabaseError(LOGIN_EXPIRED_MESSAGE)
    return client, user.id


# ---------- Converting to and from JSON ----------


def profile_to_json(profile: UserProfile) -> dict:
    """Turn a UserProfile into a plain dict for the JSON column."""
    return asdict(profile)


def profile_from_json(data: object) -> UserProfile:
    """Turn the stored JSON back into a UserProfile.

    Unknown keys (e.g. from an older version of the app) are ignored.

    Raises:
        DatabaseError: If the data isn't a dict or has the wrong types.
    """
    if not isinstance(data, dict):
        raise DatabaseError("It couldn't be read, so tab 1 starts empty.")
    known = {f.name for f in fields(UserProfile)}
    try:
        profile = UserProfile(**{key: value for key, value in data.items() if key in known})
        profile.validate()  # a quick check the types are usable (it compares and strips them)
    except (TypeError, AttributeError) as error:
        raise DatabaseError("It couldn't be read, so tab 1 starts empty.") from error
    return profile


# ---------- profiles table ----------


def load_profile(state: MutableMapping[str, Any] | None = None) -> UserProfile | None:
    """Load the logged-in user's saved profile, or None if they haven't saved one yet.

    Raises:
        DatabaseError: If the database can't be reached or the profile is unreadable.
    """
    client, user_id = user_client(state)
    try:
        # RLS already limits this to the user's own row; the filter just makes it explicit.
        response = (
            client.table("profiles").select("profile").eq("user_id", user_id).limit(1).execute()
        )
    except (APIError, httpx.HTTPError) as error:
        raise friendly_error(error) from error

    rows = response.data or []
    return profile_from_json(rows[0].get("profile")) if rows else None


def save_profile(profile: UserProfile, state: MutableMapping[str, Any] | None = None) -> None:
    """Save the profile: insert the user's row the first time, update it after that.

    Raises:
        DatabaseError: If it couldn't be saved.
    """
    client, user_id = user_client(state)
    row = {"user_id": user_id, "profile": profile_to_json(profile)}
    try:
        client.table("profiles").upsert(row, on_conflict="user_id").execute()
    except (APIError, httpx.HTTPError) as error:
        raise friendly_error(error) from error


# ---------- analyses table ----------


def save_analysis(
    file_name: str,
    word_count: int,
    rule_issues: list[Issue],
    ai_review: LLMReview | None,
    state: MutableMapping[str, Any] | None = None,
) -> None:
    """Save the results of an analysis. The resume file and its text are NOT stored.

    Raises:
        DatabaseError: If it couldn't be saved.
    """
    client, user_id = user_client(state)
    row = {
        "user_id": user_id,
        "file_name": file_name,
        "word_count": word_count,
        "issues": [asdict(issue) for issue in rule_issues],
        "ai_review": asdict(ai_review) if ai_review is not None else None,
    }
    try:
        client.table("analyses").insert(row).execute()
    except (APIError, httpx.HTTPError) as error:
        raise friendly_error(error) from error


def parse_timestamp(value: object) -> datetime:
    """Read a Postgres timestamp like "2026-11-03T10:15:30.123+00:00"."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min


def list_analyses(
    limit: int = MAX_PAST_ANALYSES, state: MutableMapping[str, Any] | None = None
) -> list[SavedAnalysis]:
    """Return the logged-in user's saved analyses, newest first.

    Raises:
        DatabaseError: If they couldn't be loaded.
    """
    client, user_id = user_client(state)
    try:
        response = (
            client.table("analyses")
            .select("id, created_at, file_name, word_count, issues, ai_review")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
    except (APIError, httpx.HTTPError) as error:
        raise friendly_error(error) from error

    return [
        SavedAnalysis(
            id=row.get("id", 0),
            created_at=parse_timestamp(row.get("created_at")),
            file_name=row.get("file_name") or "",
            word_count=row.get("word_count") or 0,
            issues=row.get("issues") or [],
            ai_review=row.get("ai_review") if isinstance(row.get("ai_review"), dict) else None,
        )
        for row in (response.data or [])
    ]


# ---------- saved_jobs table ----------


def job_to_json(job: Job) -> dict:
    """Turn a Job into a plain dict; the posted date becomes "YYYY-MM-DD" text."""
    data = asdict(job)
    data["posted_date"] = job.posted_date.isoformat() if job.posted_date else None
    return data


def job_from_json(data: object) -> Job | None:
    """Turn a stored job back into a Job, or None if it is unreadable."""
    if not isinstance(data, dict):
        return None
    known = {f.name for f in fields(Job)}
    values = {key: value for key, value in data.items() if key in known}
    try:
        posted = values.get("posted_date")
        values["posted_date"] = date.fromisoformat(posted) if posted else None
        return Job(**values)
    except (TypeError, ValueError):
        return None


def save_job(scored_job: ScoredJob, state: MutableMapping[str, Any] | None = None) -> bool:
    """Save a job with its score to the user's list.

    Returns:
        True if it was saved now, False if it was already in the list
        (the database allows each job URL only once per user).

    Raises:
        DatabaseError: If it couldn't be saved.
    """
    client, user_id = user_client(state)
    row = {"user_id": user_id, "job": job_to_json(scored_job.job), "score": scored_job.score}
    try:
        client.table("saved_jobs").insert(row).execute()
    except APIError as error:
        if str(error.code) == UNIQUE_VIOLATION:
            return False  # already saved: not a problem
        raise friendly_error(error) from error
    except httpx.HTTPError as error:
        raise friendly_error(error) from error
    return True


def list_saved_jobs(
    limit: int = MAX_SAVED_JOBS, state: MutableMapping[str, Any] | None = None
) -> list[SavedJob]:
    """Return the user's saved jobs, newest first. Unreadable rows are skipped.

    Raises:
        DatabaseError: If they couldn't be loaded.
    """
    client, user_id = user_client(state)
    try:
        response = (
            client.table("saved_jobs")
            .select("job, score, saved_at")
            .eq("user_id", user_id)
            .order("saved_at", desc=True)
            .limit(limit)
            .execute()
        )
    except (APIError, httpx.HTTPError) as error:
        raise friendly_error(error) from error

    saved: list[SavedJob] = []
    for row in response.data or []:
        job = job_from_json(row.get("job"))
        if job is not None:
            saved.append(SavedJob(job, row.get("score") or 0, parse_timestamp(row.get("saved_at"))))
    return saved


def remove_saved_job(job_url: str, state: MutableMapping[str, Any] | None = None) -> None:
    """Remove a job from the user's saved list. Removing one that isn't there is fine.

    Raises:
        DatabaseError: If it couldn't be removed.
    """
    client, user_id = user_client(state)
    try:
        (
            client.table("saved_jobs")
            .delete()
            .eq("user_id", user_id)
            .eq("job_url", job_url)
            .execute()
        )
    except (APIError, httpx.HTTPError) as error:
        raise friendly_error(error) from error


# ---------- usage (daily limits) ----------

# The two counters in the usage table; the names match increment_usage()'s "kind".
SEARCHES: str = "searches"
AI_REVIEWS: str = "ai_reviews"


def use_quota(kind: str, daily_limit: int, state: MutableMapping[str, Any] | None = None) -> bool:
    """Count one use of a limited feature, if the user has any left today.

    Calls increment_usage() in the database, which adds 1 and returns True,
    or returns False (adding nothing) when the daily limit is already reached.
    Users can't change their counts any other way.

    Args:
        kind: SEARCHES or AI_REVIEWS.
        daily_limit: The limit from config.py.

    Raises:
        ValueError: For an unknown kind (a bug in the calling code).
        DatabaseError: If the check couldn't be done. Callers should then NOT
            run the feature, so the API quotas stay protected.
    """
    if kind not in (SEARCHES, AI_REVIEWS):
        raise ValueError(f"Unknown usage kind: {kind}")
    client, _user_id = user_client(state)
    try:
        response = client.rpc(
            "increment_usage", {"kind": kind, "daily_limit": daily_limit}
        ).execute()
    except (APIError, httpx.HTTPError) as error:
        raise friendly_error(error) from error
    if not isinstance(response.data, bool):
        raise DatabaseError("The daily limit check gave an unexpected answer.")
    return response.data


def usage_today(state: MutableMapping[str, Any] | None = None) -> dict[str, int]:
    """Return today's counts, e.g. {"searches": 2, "ai_reviews": 0}.

    "Today" is the UTC date, the same day the database uses.

    Raises:
        DatabaseError: If they couldn't be loaded.
    """
    client, user_id = user_client(state)
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        response = (
            client.table("usage")
            .select("searches, ai_reviews")
            .eq("user_id", user_id)
            .eq("day", today)
            .limit(1)
            .execute()
        )
    except (APIError, httpx.HTTPError) as error:
        raise friendly_error(error) from error

    row = (response.data or [{}])[0]
    return {SEARCHES: row.get(SEARCHES) or 0, AI_REVIEWS: row.get(AI_REVIEWS) or 0}


# ---------- Deleting data ----------

# Tables the user can clear themselves. "usage" is left out on purpose: deleting
# it would reset the daily limits, so it is only removed with the whole account.
USER_DATA_TABLES: list[str] = ["analyses", "saved_jobs", "profiles"]


def delete_my_data(state: MutableMapping[str, Any] | None = None) -> None:
    """Delete the user's saved profile, analyses and saved jobs. The account stays.

    Raises:
        DatabaseError: If something couldn't be deleted. Tables already
            cleared stay cleared; running it again finishes the job.
    """
    client, user_id = user_client(state)
    for table_name in USER_DATA_TABLES:
        try:
            client.table(table_name).delete().eq("user_id", user_id).execute()
        except (APIError, httpx.HTTPError) as error:
            raise friendly_error(error) from error


def delete_my_account(state: MutableMapping[str, Any] | None = None) -> None:
    """Delete the logged-in user's account and, through the cascade, all their rows.

    Calls delete_my_account() from supabase/delete_account.sql, which can only
    ever delete the user who calls it.

    Raises:
        DatabaseError: If the account couldn't be deleted.
    """
    client, _user_id = user_client(state)
    try:
        client.rpc("delete_my_account", {}).execute()
    except APIError as error:
        if str(error.code) in {"42883", "PGRST202"}:  # function not found
            raise DatabaseError(
                "Account deletion isn't set up yet. Run supabase/delete_account.sql "
                "in the Supabase SQL Editor."
            ) from error
        raise friendly_error(error) from error
    except httpx.HTTPError as error:
        raise friendly_error(error) from error

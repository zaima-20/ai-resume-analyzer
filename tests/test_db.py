"""Tests for src/db.py. Supabase is fully mocked: no network calls, no real keys.

Each test passes a plain dict as the visitor's state, holding a fake client
and the logged-in user, the same way auth.py stores them in st.session_state.
"""

from dataclasses import asdict
from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest
from postgrest.exceptions import APIError
from supabase_auth.errors import AuthRetryableError

from src import auth, config
from src.auth import CLIENT_KEY, USER_KEY, AuthUser
from src.db import (
    AI_REVIEWS,
    LOGIN_EXPIRED_MESSAGE,
    NETWORK_MESSAGE,
    SEARCHES,
    USER_DATA_TABLES,
    DatabaseError,
    SavedAnalysis,
    SavedJob,
    delete_my_account,
    delete_my_data,
    job_from_json,
    job_to_json,
    list_analyses,
    list_saved_jobs,
    load_profile,
    profile_from_json,
    profile_to_json,
    remove_saved_job,
    save_analysis,
    save_job,
    save_profile,
    usage_today,
    use_quota,
)
from src.jobs.base import Job
from src.llm_review import LLMReview, Problem, RewrittenBullet
from src.matcher import ScoredJob
from src.profile import UserProfile
from src.rule_checks import Issue

USER_ID = "user-123"

PROFILE = UserProfile(
    target_title="Data Analyst",
    experience_level="junior",
    years_experience=1.5,
    skills={"SQL": 4, "Python": 3},
    location="Lahore",
    work_type="hybrid",
    job_sources=["JSearch", "Remotive"],
)
ISSUES = [Issue("No GitHub profile link was found.", "medium", "Contact details", "Add it.")]
REVIEW = LLMReview(
    overall_score=72,
    ats_score=80,
    strengths=["Clear layout"],
    problems=[Problem("Weak verbs", "Sounds passive", "Use action verbs")],
    rewritten_bullets=[RewrittenBullet("Worked on X", "Built X")],
    missing_keywords=["Tableau"],
)


def api_error(code: str, message: str = "db said no") -> APIError:
    """A PostgREST error like the ones supabase-py raises."""
    return APIError({"message": message, "code": code, "hint": None, "details": "secret detail"})


@pytest.fixture
def client() -> MagicMock:
    """A fake Supabase client whose session is valid."""
    fake = MagicMock(name="client")
    fake.auth.get_session.return_value = MagicMock(name="session")
    return fake


@pytest.fixture
def visitor(client: MagicMock) -> dict:
    """A logged-in visitor's state, as auth.py would leave it."""
    return {CLIENT_KEY: client, USER_KEY: AuthUser(id=USER_ID, email="sam@example.com")}


def table(client: MagicMock) -> MagicMock:
    """The fake query builder returned by client.table(...)."""
    return client.table.return_value


# --- Login and session handling ---


def test_not_logged_in_raises_without_touching_supabase(client: MagicMock) -> None:
    with pytest.raises(DatabaseError, match="Log in"):
        load_profile({CLIENT_KEY: client})
    client.table.assert_not_called()


def test_get_session_is_called_before_every_database_call(
    client: MagicMock, visitor: dict
) -> None:
    query = table(client).select.return_value.eq.return_value.limit.return_value
    query.execute.return_value = MagicMock(data=[])

    load_profile(visitor)
    save_profile(PROFILE, visitor)
    save_analysis("cv.pdf", 300, ISSUES, REVIEW, visitor)

    # Each database call is preceded by a get_session() (which refreshes expired tokens).
    names = [name for name, *_ in client.mock_calls if name in ("auth.get_session", "table")]
    assert names == ["auth.get_session", "table"] * 3


def test_expired_session_that_cannot_refresh(client: MagicMock, visitor: dict) -> None:
    client.auth.get_session.return_value = None
    with pytest.raises(DatabaseError, match="login has expired"):
        save_profile(PROFILE, visitor)
    client.table.assert_not_called()


@pytest.mark.parametrize(
    "error", [AuthRetryableError("down", 503), httpx.ConnectError("offline")]
)
def test_session_refresh_network_error(client: MagicMock, visitor: dict, error: Exception) -> None:
    client.auth.get_session.side_effect = error
    with pytest.raises(DatabaseError, match="Couldn't reach the database"):
        load_profile(visitor)


def test_each_visitor_client_has_its_own_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    # The client writes the user's token into options.headers on login. If two
    # visitors shared that dict, one could send the other's token. They must not.
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "fake-key")
    monkeypatch.setattr(config, "SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setattr(config, "SUPABASE_KEY", "fake-key")
    create = MagicMock()
    monkeypatch.setattr(auth, "create_client", create)

    auth.get_client({})
    auth.get_client({})
    options_a, options_b = (c.args[2] for c in create.call_args_list)
    assert options_a.headers is not options_b.headers
    options_a.headers["Authorization"] = "Bearer token-of-A"
    assert "Authorization" not in options_b.headers


# --- profiles ---


def test_load_profile_when_nothing_saved(client: MagicMock, visitor: dict) -> None:
    query = table(client).select.return_value.eq.return_value.limit.return_value
    query.execute.return_value = MagicMock(data=[])
    assert load_profile(visitor) is None


def test_load_profile_returns_saved_profile(client: MagicMock, visitor: dict) -> None:
    query = table(client).select.return_value.eq.return_value.limit.return_value
    query.execute.return_value = MagicMock(data=[{"profile": profile_to_json(PROFILE)}])

    assert load_profile(visitor) == PROFILE
    client.table.assert_called_with("profiles")
    table(client).select.return_value.eq.assert_called_with("user_id", USER_ID)


def test_load_profile_ignores_unknown_keys(client: MagicMock, visitor: dict) -> None:
    saved = {**profile_to_json(PROFILE), "old_field_from_v0": True}
    query = table(client).select.return_value.eq.return_value.limit.return_value
    query.execute.return_value = MagicMock(data=[{"profile": saved}])
    assert load_profile(visitor) == PROFILE


@pytest.mark.parametrize(
    "bad", [None, "text", [1, 2], {"years_experience": "three"}, {"skills": {"SQL": "high"}}]
)
def test_unreadable_saved_profile(bad: object) -> None:
    with pytest.raises(DatabaseError, match="couldn't be read"):
        profile_from_json(bad)


def test_save_profile_upserts_one_row_per_user(client: MagicMock, visitor: dict) -> None:
    save_profile(PROFILE, visitor)

    client.table.assert_called_with("profiles")
    table(client).upsert.assert_called_once_with(
        {"user_id": USER_ID, "profile": asdict(PROFILE)}, on_conflict="user_id"
    )
    table(client).upsert.return_value.execute.assert_called_once()


def test_profile_json_round_trip() -> None:
    assert profile_from_json(profile_to_json(PROFILE)) == PROFILE


# --- analyses ---


def test_save_analysis_stores_results_but_not_the_resume(client: MagicMock, visitor: dict) -> None:
    save_analysis("my_cv.pdf", 412, ISSUES, REVIEW, visitor)

    client.table.assert_called_with("analyses")
    [row] = table(client).insert.call_args.args
    assert set(row) == {"user_id", "file_name", "word_count", "issues", "ai_review"}
    assert row["user_id"] == USER_ID
    assert row["file_name"] == "my_cv.pdf"
    assert row["word_count"] == 412
    assert row["issues"] == [asdict(ISSUES[0])]
    assert row["ai_review"]["overall_score"] == 72
    assert row["ai_review"]["problems"][0]["why_it_matters"] == "Sounds passive"


def test_save_analysis_returns_what_was_saved(client: MagicMock, visitor: dict) -> None:
    saved = save_analysis("my_cv.pdf", 412, ISSUES, REVIEW, visitor)
    assert isinstance(saved, SavedAnalysis)
    assert (saved.file_name, saved.word_count) == ("my_cv.pdf", 412)
    assert (saved.overall_score, saved.ats_score) == (72, 80)
    assert saved.created_at.tzinfo is not None


def test_save_analysis_without_ai_review(client: MagicMock, visitor: dict) -> None:
    save_analysis("cv.docx", 300, [], None, visitor)
    [row] = table(client).insert.call_args.args
    assert row["ai_review"] is None
    assert row["issues"] == []


def test_list_analyses_newest_first(client: MagicMock, visitor: dict) -> None:
    query = (
        table(client).select.return_value.eq.return_value.order.return_value.limit.return_value
    )
    query.execute.return_value = MagicMock(
        data=[
            {
                "id": 2, "created_at": "2026-11-03T10:15:30.123456+00:00", "file_name": "v2.pdf",
                "word_count": 450, "issues": [asdict(ISSUES[0])], "ai_review": asdict(REVIEW),
            },
            {
                "id": 1, "created_at": "2026-11-01T08:00:00Z", "file_name": "v1.pdf",
                "word_count": 380, "issues": [], "ai_review": None,
            },
        ]
    )

    analyses = list_analyses(state=visitor)

    builder = table(client).select.return_value
    builder.eq.assert_called_with("user_id", USER_ID)
    builder.eq.return_value.order.assert_called_with("created_at", desc=True)
    assert [a.file_name for a in analyses] == ["v2.pdf", "v1.pdf"]
    newest = analyses[0]
    assert newest == SavedAnalysis(
        id=2,
        created_at=datetime(2026, 11, 3, 10, 15, 30, 123456, tzinfo=timezone.utc),
        file_name="v2.pdf",
        word_count=450,
        issues=[asdict(ISSUES[0])],
        ai_review=asdict(REVIEW),
    )
    assert (newest.overall_score, newest.ats_score) == (72, 80)
    assert (analyses[1].overall_score, analyses[1].ats_score) == (None, None)


def test_list_analyses_empty(client: MagicMock, visitor: dict) -> None:
    query = (
        table(client).select.return_value.eq.return_value.order.return_value.limit.return_value
    )
    query.execute.return_value = MagicMock(data=[])
    assert list_analyses(state=visitor) == []


# --- Friendly errors ---


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (api_error("PGRST301", "JWT expired"), LOGIN_EXPIRED_MESSAGE),
        (api_error("42501", "new row violates row-level security policy"), LOGIN_EXPIRED_MESSAGE),
        (api_error("42P01", 'relation "public.analyses" does not exist'), "tables are missing"),
        (api_error("PGRST205", "Could not find the table"), "tables are missing"),
        (api_error("23505", "duplicate key value"), "The database had a problem"),
        (httpx.ConnectError("connection refused"), NETWORK_MESSAGE),
        (httpx.ReadTimeout("timed out"), NETWORK_MESSAGE),
    ],
)
def test_database_errors_are_friendly(
    client: MagicMock, visitor: dict, error: Exception, message: str
) -> None:
    table(client).insert.return_value.execute.side_effect = error
    with pytest.raises(DatabaseError) as raised:
        save_analysis("cv.pdf", 300, ISSUES, REVIEW, visitor)
    assert message in str(raised.value)
    assert "secret detail" not in str(raised.value)  # raw server details are never shown


def test_load_and_list_also_turn_errors_into_database_error(
    client: MagicMock, visitor: dict
) -> None:
    client.table.side_effect = httpx.ConnectError("offline")
    with pytest.raises(DatabaseError, match="Couldn't reach"):
        load_profile(visitor)
    with pytest.raises(DatabaseError, match="Couldn't reach"):
        list_analyses(state=visitor)
    with pytest.raises(DatabaseError, match="Couldn't reach"):
        save_profile(PROFILE, visitor)


# --- saved_jobs ---

JOB = Job(
    title="ML Engineer",
    company="Demo AI",
    location="Lahore, Pakistan",
    remote=False,
    description="Python and PyTorch",
    url="https://example.com/jobs/1",
    source="JSearch (LinkedIn)",
    posted_date=date(2026, 10, 30),
)
SCORED = ScoredJob(job=JOB, score=78, matched_skills=["Python"])


def test_save_job_stores_job_json_and_score(client: MagicMock, visitor: dict) -> None:
    assert save_job(SCORED, visitor) is True

    client.table.assert_called_with("saved_jobs")
    [row] = table(client).insert.call_args.args
    assert row == {"user_id": USER_ID, "job": job_to_json(JOB), "score": 78}
    assert row["job"]["posted_date"] == "2026-10-30"  # dates are stored as text in JSON
    assert row["job"]["url"] == "https://example.com/jobs/1"  # becomes job_url in the table


def test_saving_the_same_job_twice_is_not_an_error(client: MagicMock, visitor: dict) -> None:
    table(client).insert.return_value.execute.side_effect = api_error(
        "23505", 'duplicate key value violates unique constraint "saved_jobs_one_per_user"'
    )
    assert save_job(SCORED, visitor) is False


@pytest.mark.parametrize(
    ("error", "message"),
    [(api_error("42501"), "login has expired"), (httpx.ConnectError("offline"), "Couldn't reach")],
)
def test_save_job_other_errors(
    client: MagicMock, visitor: dict, error: Exception, message: str
) -> None:
    table(client).insert.return_value.execute.side_effect = error
    with pytest.raises(DatabaseError, match=message):
        save_job(SCORED, visitor)


def test_save_job_needs_login(client: MagicMock) -> None:
    with pytest.raises(DatabaseError, match="Log in"):
        save_job(SCORED, {CLIENT_KEY: client})


def saved_jobs_query(client: MagicMock) -> MagicMock:
    """The fake query at the end of list_saved_jobs()' builder chain."""
    return table(client).select.return_value.eq.return_value.order.return_value.limit.return_value


def test_list_saved_jobs_newest_first(client: MagicMock, visitor: dict) -> None:
    older = {**job_to_json(JOB), "url": "https://example.com/jobs/0", "posted_date": None}
    saved_jobs_query(client).execute.return_value = MagicMock(
        data=[
            {"job": job_to_json(JOB), "score": 78, "saved_at": "2026-11-03T09:00:00+00:00"},
            {"job": older, "score": 55, "saved_at": "2026-11-01T09:00:00+00:00"},
        ]
    )

    saved = list_saved_jobs(state=visitor)

    builder = table(client).select.return_value
    builder.eq.assert_called_with("user_id", USER_ID)
    builder.eq.return_value.order.assert_called_with("saved_at", desc=True)
    assert saved[0] == SavedJob(
        job=JOB, score=78, saved_at=datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
    )
    assert saved[1].job.url == "https://example.com/jobs/0"
    assert saved[1].job.posted_date is None


def test_list_saved_jobs_skips_unreadable_rows(client: MagicMock, visitor: dict) -> None:
    saved_jobs_query(client).execute.return_value = MagicMock(
        data=[
            {"job": "not a dict", "score": 10, "saved_at": "2026-11-03T09:00:00+00:00"},
            {"job": {"title": "Missing most fields"}, "score": 10, "saved_at": "2026-11-03T09:00:00+00:00"},
            {"job": job_to_json(JOB), "score": 78, "saved_at": "2026-11-02T09:00:00+00:00"},
        ]
    )
    assert [s.job.title for s in list_saved_jobs(state=visitor)] == ["ML Engineer"]


def test_list_saved_jobs_error(client: MagicMock, visitor: dict) -> None:
    saved_jobs_query(client).execute.side_effect = httpx.ConnectError("offline")
    with pytest.raises(DatabaseError, match="Couldn't reach"):
        list_saved_jobs(state=visitor)


def test_remove_saved_job_deletes_by_user_and_url(client: MagicMock, visitor: dict) -> None:
    remove_saved_job("https://example.com/jobs/1", visitor)

    client.table.assert_called_with("saved_jobs")
    delete = table(client).delete.return_value
    delete.eq.assert_called_once_with("user_id", USER_ID)
    delete.eq.return_value.eq.assert_called_once_with("job_url", "https://example.com/jobs/1")
    delete.eq.return_value.eq.return_value.execute.assert_called_once()


def test_remove_saved_job_error(client: MagicMock, visitor: dict) -> None:
    table(client).delete.side_effect = httpx.ConnectError("offline")
    with pytest.raises(DatabaseError, match="Couldn't reach"):
        remove_saved_job("https://example.com/jobs/1", visitor)


def test_job_json_round_trip() -> None:
    assert job_from_json(job_to_json(JOB)) == JOB
    assert job_from_json({**job_to_json(JOB), "extra": 1}) == JOB  # unknown keys ignored
    assert job_from_json({**job_to_json(JOB), "posted_date": "soon"}) is None


def test_saved_job_calls_refresh_session_first(client: MagicMock, visitor: dict) -> None:
    save_job(SCORED, visitor)
    remove_saved_job(JOB.url, visitor)
    names = [name for name, *_ in client.mock_calls if name in ("auth.get_session", "table")]
    assert names == ["auth.get_session", "table"] * 2


# --- Daily limits (usage) ---


def rpc_result(client: MagicMock) -> MagicMock:
    """The fake execute() of client.rpc(...)."""
    return client.rpc.return_value.execute


@pytest.mark.parametrize("allowed", [True, False])
def test_use_quota_calls_increment_usage(client: MagicMock, visitor: dict, allowed: bool) -> None:
    rpc_result(client).return_value = MagicMock(data=allowed)

    assert use_quota(AI_REVIEWS, 3, visitor) is allowed
    client.rpc.assert_called_once_with("increment_usage", {"kind": "ai_reviews", "daily_limit": 3})


def test_use_quota_refreshes_session_first(client: MagicMock, visitor: dict) -> None:
    rpc_result(client).return_value = MagicMock(data=True)
    use_quota(SEARCHES, 5, visitor)
    names = [name for name, *_ in client.mock_calls if name in ("auth.get_session", "rpc")]
    assert names == ["auth.get_session", "rpc"]


def test_use_quota_rejects_unknown_kind(client: MagicMock, visitor: dict) -> None:
    with pytest.raises(ValueError):
        use_quota("downloads", 5, visitor)
    client.rpc.assert_not_called()


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (httpx.ConnectError("offline"), "Couldn't reach the database"),
        (api_error("PGRST301", "JWT expired"), "login has expired"),
        (api_error("P0001", "You must be signed in"), "The database had a problem"),
    ],
)
def test_use_quota_errors_raise_so_the_action_is_blocked(
    client: MagicMock, visitor: dict, error: Exception, message: str
) -> None:
    rpc_result(client).side_effect = error
    with pytest.raises(DatabaseError, match=message):
        use_quota(SEARCHES, 5, visitor)


@pytest.mark.parametrize("data", [None, "true", 1, [True]])
def test_use_quota_unexpected_answer_is_an_error(client: MagicMock, visitor: dict, data: object) -> None:
    rpc_result(client).return_value = MagicMock(data=data)
    with pytest.raises(DatabaseError, match="unexpected answer"):
        use_quota(SEARCHES, 5, visitor)


def usage_query(client: MagicMock) -> MagicMock:
    """The fake query at the end of usage_today()'s builder chain."""
    return table(client).select.return_value.eq.return_value.eq.return_value.limit.return_value


def test_usage_today_reads_todays_utc_row(client: MagicMock, visitor: dict) -> None:
    usage_query(client).execute.return_value = MagicMock(data=[{"searches": 2, "ai_reviews": 1}])

    assert usage_today(visitor) == {"searches": 2, "ai_reviews": 1}
    client.table.assert_called_with("usage")
    builder = table(client).select.return_value
    builder.eq.assert_called_with("user_id", USER_ID)
    today = datetime.now(timezone.utc).date().isoformat()
    builder.eq.return_value.eq.assert_called_with("day", today)


def test_usage_today_with_no_row_yet(client: MagicMock, visitor: dict) -> None:
    usage_query(client).execute.return_value = MagicMock(data=[])
    assert usage_today(visitor) == {"searches": 0, "ai_reviews": 0}


def test_usage_today_error(client: MagicMock, visitor: dict) -> None:
    usage_query(client).execute.side_effect = httpx.ConnectError("offline")
    with pytest.raises(DatabaseError):
        usage_today(visitor)


# --- Deleting data and the account ---


def test_delete_my_data_clears_three_tables_but_not_usage(client: MagicMock, visitor: dict) -> None:
    delete_my_data(visitor)

    assert [c.args[0] for c in client.table.call_args_list] == ["analyses", "saved_jobs", "profiles"]
    assert table(client).delete.call_count == 3
    for eq_call in table(client).delete.return_value.eq.call_args_list:
        assert eq_call.args == ("user_id", USER_ID)
    assert "usage" not in USER_DATA_TABLES  # deleting usage would reset the daily limits


def test_delete_my_data_error(client: MagicMock, visitor: dict) -> None:
    table(client).delete.return_value.eq.return_value.execute.side_effect = httpx.ConnectError("x")
    with pytest.raises(DatabaseError, match="Couldn't reach"):
        delete_my_data(visitor)


def test_delete_my_data_needs_login(client: MagicMock) -> None:
    with pytest.raises(DatabaseError, match="Log in"):
        delete_my_data({CLIENT_KEY: client})
    client.table.assert_not_called()


def test_delete_my_account_calls_the_function_with_no_arguments(
    client: MagicMock, visitor: dict
) -> None:
    delete_my_account(visitor)
    # No user ID is sent: the database takes it from the login token (auth.uid()).
    client.rpc.assert_called_once_with("delete_my_account", {})
    rpc_result(client).assert_called_once()


@pytest.mark.parametrize("code", ["PGRST202", "42883"])
def test_delete_my_account_when_sql_not_installed(
    client: MagicMock, visitor: dict, code: str
) -> None:
    rpc_result(client).side_effect = api_error(code, "Could not find the function")
    with pytest.raises(DatabaseError, match="delete_account.sql"):
        delete_my_account(visitor)


def test_delete_my_account_network_error(client: MagicMock, visitor: dict) -> None:
    rpc_result(client).side_effect = httpx.ConnectError("offline")
    with pytest.raises(DatabaseError, match="Couldn't reach"):
        delete_my_account(visitor)

"""Tests for src/auth.py. Supabase is fully mocked: no network calls, no real keys.

Each test passes a plain dict as the visitor's state instead of st.session_state.
"""

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from supabase_auth.errors import AuthApiError, AuthRetryableError, AuthWeakPasswordError

from src import auth, config
from src.auth import (
    CLIENT_KEY,
    USER_KEY,
    AuthError,
    AuthUser,
    current_user,
    get_client,
    sign_in,
    sign_out,
    sign_up,
)

FAKE_URL = "https://fake-project.supabase.co"
FAKE_KEY = "fake-anon-key-12345"


def fake_user(email: str = "sam@example.com", identities: list | None = None) -> MagicMock:
    """A stand-in for a Supabase User."""
    return MagicMock(id="user-123", email=email, identities=[{"provider": "email"}] if identities is None else identities)


def fake_response(user: Any = None, session: Any = None) -> MagicMock:
    """A stand-in for a Supabase AuthResponse."""
    return MagicMock(user=user, session=session)


@pytest.fixture(autouse=True)
def fake_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend Supabase is configured, without touching the real .env values."""
    for name, value in [("SUPABASE_URL", FAKE_URL), ("SUPABASE_KEY", FAKE_KEY)]:
        monkeypatch.setenv(name, value)
        monkeypatch.setattr(config, name, value)


@pytest.fixture
def create_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace supabase.create_client: each call returns a NEW fake client."""
    mock = MagicMock(side_effect=lambda *args, **kwargs: MagicMock(name="client"))
    monkeypatch.setattr(auth, "create_client", mock)
    return mock


def client_for(state: dict) -> MagicMock:
    """Create (or get) the fake client for a visitor."""
    return get_client(state)


# --- One client and session per visitor ---


def test_each_visitor_gets_their_own_client(create_client: MagicMock) -> None:
    visitor_a: dict = {}
    visitor_b: dict = {}
    client_a = get_client(visitor_a)
    client_b = get_client(visitor_b)

    assert client_a is not client_b
    assert visitor_a[CLIENT_KEY] is client_a
    assert visitor_b[CLIENT_KEY] is client_b
    assert create_client.call_count == 2


def test_client_is_reused_within_one_visit(create_client: MagicMock) -> None:
    visitor: dict = {}
    assert get_client(visitor) is get_client(visitor)
    assert create_client.call_count == 1


def test_each_client_gets_fresh_session_storage_and_no_refresh_timer(
    create_client: MagicMock,
) -> None:
    get_client({})
    get_client({})
    first, second = (call.args[2] for call in create_client.call_args_list)
    assert first.storage is not second.storage
    assert first.auto_refresh_token is False
    assert create_client.call_args.args[:2] == (FAKE_URL, FAKE_KEY)


def test_module_keeps_no_client_or_session() -> None:
    # Nothing at module level may hold a client or user: that would be shared by all visitors.
    for name, value in vars(auth).items():
        assert not isinstance(value, (AuthUser, MagicMock)), name
        assert type(value).__name__ != "SyncClient", name


def test_login_of_one_visitor_does_not_log_in_another(create_client: MagicMock) -> None:
    visitor_a: dict = {}
    visitor_b: dict = {}
    client_for(visitor_a).auth.sign_in_with_password.return_value = fake_response(
        fake_user("a@example.com"), session=MagicMock()
    )
    sign_in("a@example.com", "password123", visitor_a)

    assert current_user(visitor_a) == AuthUser(id="user-123", email="a@example.com")
    assert current_user(visitor_b) is None


# --- Sign in ---


def test_sign_in_success(create_client: MagicMock) -> None:
    visitor: dict = {}
    client = client_for(visitor)
    client.auth.sign_in_with_password.return_value = fake_response(fake_user(), session=MagicMock())

    user = sign_in("  Sam@Example.com ", "password123", visitor)

    assert user == AuthUser(id="user-123", email="sam@example.com")
    assert visitor[USER_KEY] == user
    client.auth.sign_in_with_password.assert_called_once_with(
        {"email": "sam@example.com", "password": "password123"}
    )


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (AuthApiError("Invalid login credentials", 400, "invalid_credentials"), "Wrong email or password."),
        (AuthApiError("Invalid login credentials", 400, None), "Wrong email or password."),  # older servers
        (AuthApiError("Email not confirmed", 400, "email_not_confirmed"), "confirm your email"),
        (AuthApiError("Rate limit", 429, "over_request_rate_limit"), "Too many attempts"),
        (AuthRetryableError("Server unavailable", 503), "Couldn't reach the login service"),
        (httpx.ConnectError("connection refused"), "Couldn't reach the login service"),
        (httpx.ReadTimeout("timed out"), "Couldn't reach the login service"),
        (AuthApiError("internal db error at 10.0.0.5", 500, "unexpected_failure"), "Something went wrong"),
    ],
)
def test_sign_in_errors_are_friendly(
    create_client: MagicMock, error: Exception, message: str
) -> None:
    visitor: dict = {}
    client_for(visitor).auth.sign_in_with_password.side_effect = error

    with pytest.raises(AuthError, match=message) as raised:
        sign_in("sam@example.com", "password123", visitor)

    assert current_user(visitor) is None
    # The raw error text never reaches the user.
    assert str(error) not in str(raised.value) or str(error) == ""


def test_sign_in_needs_a_password(create_client: MagicMock) -> None:
    with pytest.raises(AuthError, match="enter your password"):
        sign_in("sam@example.com", "", {})
    create_client.assert_not_called()


# --- Sign up ---


def test_sign_up_logs_in_when_no_confirmation_needed(create_client: MagicMock) -> None:
    visitor: dict = {}
    client_for(visitor).auth.sign_up.return_value = fake_response(fake_user(), session=MagicMock())

    assert sign_up("sam@example.com", "password123", visitor) is True
    assert current_user(visitor).email == "sam@example.com"


def test_sign_up_with_email_confirmation_does_not_log_in(create_client: MagicMock) -> None:
    visitor: dict = {}
    client_for(visitor).auth.sign_up.return_value = fake_response(fake_user(), session=None)

    assert sign_up("sam@example.com", "password123", visitor) is False
    assert current_user(visitor) is None


def test_sign_up_existing_email_hidden_by_supabase(create_client: MagicMock) -> None:
    # With confirmation on, Supabase returns a user with no identities for a taken email.
    visitor: dict = {}
    client_for(visitor).auth.sign_up.return_value = fake_response(fake_user(identities=[]), session=None)

    with pytest.raises(AuthError, match="already exists"):
        sign_up("sam@example.com", "password123", visitor)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (AuthApiError("User already registered", 422, "user_already_exists"), "already exists"),
        (AuthApiError("Email exists", 422, "email_exists"), "already exists"),
        (AuthApiError("Bad email", 400, "email_address_invalid"), "doesn't look valid"),
        (AuthWeakPasswordError("Weak", 422, ["characters"]), "too weak"),
        (AuthApiError("Signups not allowed", 422, "signup_disabled"), "turned off"),
        (httpx.ConnectError("no network"), "Couldn't reach the login service"),
    ],
)
def test_sign_up_errors_are_friendly(
    create_client: MagicMock, error: Exception, message: str
) -> None:
    visitor: dict = {}
    client_for(visitor).auth.sign_up.side_effect = error
    with pytest.raises(AuthError, match=message):
        sign_up("sam@example.com", "password123", visitor)
    assert current_user(visitor) is None


@pytest.mark.parametrize("password", ["", "short", "1234567"])
def test_sign_up_password_too_short_is_checked_before_calling_supabase(
    create_client: MagicMock, password: str
) -> None:
    with pytest.raises(AuthError, match="at least 8 characters"):
        sign_up("sam@example.com", password, {})
    create_client.assert_not_called()


@pytest.mark.parametrize("email", ["", "sam", "sam@", "@example.com", "sam@example", "sam example@x.com"])
def test_invalid_email_is_checked_before_calling_supabase(
    create_client: MagicMock, email: str
) -> None:
    with pytest.raises(AuthError, match="valid email"):
        sign_up(email, "password123", {})
    with pytest.raises(AuthError, match="valid email"):
        sign_in(email, "password123", {})
    create_client.assert_not_called()


# --- Sign out ---


def test_sign_out_forgets_user_and_client(create_client: MagicMock) -> None:
    visitor: dict = {}
    client = client_for(visitor)
    client.auth.sign_in_with_password.return_value = fake_response(fake_user(), session=MagicMock())
    sign_in("sam@example.com", "password123", visitor)

    sign_out(visitor)

    client.auth.sign_out.assert_called_once_with({"scope": "local"})
    assert USER_KEY not in visitor
    assert CLIENT_KEY not in visitor
    assert current_user(visitor) is None


def test_sign_out_works_offline(create_client: MagicMock) -> None:
    visitor: dict = {}
    client = client_for(visitor)
    client.auth.sign_out.side_effect = httpx.ConnectError("offline")
    visitor[USER_KEY] = AuthUser(id="user-123", email="sam@example.com")

    sign_out(visitor)  # must not raise

    assert current_user(visitor) is None


def test_sign_out_when_never_logged_in() -> None:
    visitor: dict = {}
    sign_out(visitor)
    assert visitor == {}


# --- Configuration ---


@pytest.mark.parametrize("missing", ["SUPABASE_URL", "SUPABASE_KEY"])
def test_missing_config(monkeypatch: pytest.MonkeyPatch, create_client: MagicMock, missing: str) -> None:
    monkeypatch.delenv(missing)
    with pytest.raises(AuthError, match="SUPABASE_URL and SUPABASE_KEY"):
        sign_in("sam@example.com", "password123", {})
    create_client.assert_not_called()


def test_bad_config_error_does_not_leak_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "create_client", MagicMock(side_effect=ValueError(f"bad key {FAKE_KEY}")))
    with pytest.raises(AuthError) as raised:
        get_client({})
    assert FAKE_KEY not in str(raised.value)
    assert "looks wrong" in str(raised.value)

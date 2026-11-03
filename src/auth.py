"""Sign up, log in and log out with Supabase Auth.

IMPORTANT: Streamlit serves every visitor from the same Python process, so
anything stored in a module-level variable is shared by all visitors. This
module therefore keeps NO client or session at module level. Each visitor
gets their own Supabase client, created on first use and stored in that
visitor's own st.session_state, together with the logged-in user.
"""

import re
from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any

import httpx
import streamlit as st
from supabase import Client, ClientOptions, create_client
from supabase_auth import SyncMemoryStorage
from supabase_auth.errors import AuthApiError, AuthRetryableError, AuthWeakPasswordError
from supabase_auth.errors import AuthError as SupabaseAuthError

from src import config

MIN_PASSWORD_LENGTH: int = 8
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Keys in st.session_state. Only ever stored per visitor, never globally.
CLIENT_KEY: str = "auth_client"
USER_KEY: str = "auth_user"

NETWORK_MESSAGE = "Couldn't reach the login service. Check your internet connection and try again."
ALREADY_REGISTERED_MESSAGE = "An account with this email already exists. Try logging in instead."

# Supabase error codes -> messages that are safe and helpful to show.
FRIENDLY_MESSAGES: dict[str, str] = {
    "invalid_credentials": "Wrong email or password.",
    "email_exists": ALREADY_REGISTERED_MESSAGE,
    "user_already_exists": ALREADY_REGISTERED_MESSAGE,
    "weak_password": f"That password is too weak. Use at least {MIN_PASSWORD_LENGTH} characters, "
    "mixing letters and numbers.",
    "email_address_invalid": "That email address doesn't look valid.",
    "validation_failed": "Please check that your email and password are filled in correctly.",
    "email_not_confirmed": "Please confirm your email first: click the link we sent you, then log in.",
    "over_email_send_rate_limit": "Too many attempts. Please wait a few minutes and try again.",
    "over_request_rate_limit": "Too many attempts. Please wait a few minutes and try again.",
    "signup_disabled": "New sign-ups are turned off right now.",
}


class AuthError(Exception):
    """A login problem. The message is always safe to show to the user."""


@dataclass
class AuthUser:
    """The logged-in user, as kept in this visitor's session_state."""

    id: str
    email: str


def visitor_state(state: MutableMapping[str, Any] | None) -> MutableMapping[str, Any]:
    """Return the store for this visitor: st.session_state, or a dict passed in by tests."""
    return st.session_state if state is None else state


# ---------- The per-visitor client ----------


def get_client(state: MutableMapping[str, Any] | None = None) -> Client:
    """Return this visitor's Supabase client, creating it on first use.

    The client is stored in the visitor's own session_state, never in a
    module-level variable, so two visitors can never share a session.

    Raises:
        AuthError: If SUPABASE_URL or SUPABASE_KEY is missing or invalid.
    """
    state = visitor_state(state)
    if CLIENT_KEY not in state:
        if not (config.has_key("SUPABASE_URL") and config.has_key("SUPABASE_KEY")):
            raise AuthError(
                "Accounts aren't set up yet: add SUPABASE_URL and SUPABASE_KEY to your .env file."
            )
        options = ClientOptions(
            # A brand-new, empty session store for this visitor only.
            storage=SyncMemoryStorage(),
            # No background refresh timer per visitor (it would keep running after
            # they close the tab). get_session() refreshes an expired session when needed.
            auto_refresh_token=False,
        )
        try:
            state[CLIENT_KEY] = create_client(config.SUPABASE_URL, config.SUPABASE_KEY, options)
        except Exception as error:  # e.g. a malformed URL; never show its details
            raise AuthError("SUPABASE_URL or SUPABASE_KEY in your .env file looks wrong.") from error
    return state[CLIENT_KEY]


def current_user(state: MutableMapping[str, Any] | None = None) -> AuthUser | None:
    """Return the logged-in user for this visitor, or None."""
    return visitor_state(state).get(USER_KEY)


# ---------- Errors and validation ----------


def friendly_error(error: Exception) -> AuthError:
    """Turn a Supabase or network error into an AuthError with a safe message.

    The original error is never shown, as it can contain server details.
    """
    if isinstance(error, (AuthRetryableError, httpx.HTTPError)):
        return AuthError(NETWORK_MESSAGE)
    if isinstance(error, AuthWeakPasswordError):
        return AuthError(FRIENDLY_MESSAGES["weak_password"])
    if isinstance(error, AuthApiError):
        if error.code in FRIENDLY_MESSAGES:
            return AuthError(FRIENDLY_MESSAGES[error.code])
        # Older Supabase servers send no code for a wrong password.
        if "invalid login credentials" in str(error.message).lower():
            return AuthError(FRIENDLY_MESSAGES["invalid_credentials"])
    return AuthError("Something went wrong with the login service. Please try again.")


def clean_email(email: str) -> str:
    """Trim and lowercase an email, and check it looks like an email address.

    Raises:
        AuthError: If it is empty or not shaped like name@example.com.
    """
    email = email.strip().lower()
    if not EMAIL_PATTERN.match(email):
        raise AuthError("Please enter a valid email address, like name@example.com.")
    return email


def remember_user(state: MutableMapping[str, Any], user: Any) -> AuthUser:
    """Store the logged-in Supabase user in this visitor's state."""
    auth_user = AuthUser(id=str(user.id), email=user.email or "")
    state[USER_KEY] = auth_user
    return auth_user


# ---------- Sign up, log in, log out ----------


def sign_up(email: str, password: str, state: MutableMapping[str, Any] | None = None) -> bool:
    """Create an account.

    Returns:
        True if the user is logged in straight away. False if Supabase sent a
        confirmation email first (the default setting): they log in after clicking it.

    Raises:
        AuthError: For an invalid email, a short password, an existing account,
            or a network or server problem.
    """
    state = visitor_state(state)
    email = clean_email(email)
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Your password must be at least {MIN_PASSWORD_LENGTH} characters long.")

    try:
        response = get_client(state).auth.sign_up({"email": email, "password": password})
    except (SupabaseAuthError, httpx.HTTPError) as error:
        raise friendly_error(error) from error

    user = response.user
    # With email confirmation on, Supabase doesn't reveal that an email is taken
    # (to stop people probing for accounts). It returns a user with no identities instead.
    if user is not None and user.identities == []:
        raise AuthError(ALREADY_REGISTERED_MESSAGE)
    if user is None or response.session is None:
        return False
    remember_user(state, user)
    return True


def sign_in(email: str, password: str, state: MutableMapping[str, Any] | None = None) -> AuthUser:
    """Log in with email and password.

    Raises:
        AuthError: For a wrong email or password, an unconfirmed email,
            or a network or server problem.
    """
    state = visitor_state(state)
    email = clean_email(email)
    if not password:
        raise AuthError("Please enter your password.")

    try:
        response = get_client(state).auth.sign_in_with_password(
            {"email": email, "password": password}
        )
    except (SupabaseAuthError, httpx.HTTPError) as error:
        raise friendly_error(error) from error

    if response.user is None or response.session is None:
        raise AuthError("Login didn't complete. Please try again.")
    return remember_user(state, response.user)


def sign_out(state: MutableMapping[str, Any] | None = None) -> None:
    """Log this visitor out and forget their client and user.

    Logging out locally always works, even if Supabase can't be reached.
    The caller (app.py) then clears the rest of the visitor's session_state.
    """
    state = visitor_state(state)
    client = state.get(CLIENT_KEY)
    if client is not None:
        try:
            # "local": log out this browser only, not the user's other devices.
            client.auth.sign_out({"scope": "local"})
        except (SupabaseAuthError, httpx.HTTPError):
            pass  # the session is dropped below either way
    state.pop(USER_KEY, None)
    state.pop(CLIENT_KEY, None)

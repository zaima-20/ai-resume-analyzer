"""Shared pytest setup for all tests."""

import pytest

from src import config


@pytest.fixture(autouse=True)
def no_streamlit_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide any real .streamlit/secrets.toml from the tests.

    Keys are then read only from environment variables, which each test
    controls with monkeypatch. Real keys can never leak into a test.
    """
    monkeypatch.setattr(config, "read_secrets", lambda: {})

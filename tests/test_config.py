"""Tests for src/config.py: keys come from st.secrets first, then the environment / .env."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import tomllib
from streamlit import config as streamlit_config
from streamlit.runtime.secrets import Secrets

from src import config

EXAMPLE_FILE = Path(__file__).parent.parent / ".streamlit" / "secrets.toml.example"
KEY_NAMES = [
    "GEMINI_API_KEY", "RAPIDAPI_KEY", "ADZUNA_APP_ID",
    "ADZUNA_APP_KEY", "SUPABASE_URL", "SUPABASE_KEY",
]


def use_secrets(monkeypatch: pytest.MonkeyPatch, secrets: dict) -> None:
    """Pretend Streamlit secrets contain `secrets` (overrides the conftest default of {})."""
    monkeypatch.setattr(config, "read_secrets", lambda: secrets)


def use_real_secrets_loader(monkeypatch: pytest.MonkeyPatch, secrets_path: Path) -> None:
    """Use the real read_secrets() and a fresh st.secrets that only looks at `secrets_path`."""
    monkeypatch.undo()  # drop the conftest stand-in for read_secrets()
    original_get_option = streamlit_config.get_option

    def get_option(name: str) -> object:
        # Only redirect where secrets are read from; every other setting stays real.
        return [str(secrets_path)] if name == "secrets.files" else original_get_option(name)

    monkeypatch.setattr(streamlit_config, "get_option", get_option)
    monkeypatch.setattr(config.st, "secrets", Secrets())


# --- get_setting(): secrets first, then environment ---


def test_secret_is_used_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    use_secrets(monkeypatch, {"GEMINI_API_KEY": "from-secrets"})
    assert config.get_setting("GEMINI_API_KEY") == "from-secrets"


def test_falls_back_to_environment_when_secret_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    use_secrets(monkeypatch, {"OTHER_KEY": "x"})
    assert config.get_setting("GEMINI_API_KEY") == "from-env"


def test_works_with_only_env_and_no_secrets_file(monkeypatch: pytest.MonkeyPatch) -> None:
    # conftest makes read_secrets() return {}, like a machine with only .env.
    monkeypatch.setenv("RAPIDAPI_KEY", "from-env")
    assert config.get_setting("RAPIDAPI_KEY") == "from-env"


@pytest.mark.parametrize("secret_value", ["", "   ", 12345, {"nested": "table"}, None])
def test_empty_or_non_text_secret_falls_back(
    monkeypatch: pytest.MonkeyPatch, secret_value: object
) -> None:
    monkeypatch.setenv("SUPABASE_URL", "https://from-env.supabase.co")
    use_secrets(monkeypatch, {"SUPABASE_URL": secret_value})
    assert config.get_setting("SUPABASE_URL") == "https://from-env.supabase.co"


def test_missing_everywhere_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    assert config.get_setting("ADZUNA_APP_ID") is None


def test_secret_whitespace_is_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    use_secrets(monkeypatch, {"SUPABASE_KEY": "  abc123  "})
    assert config.get_setting("SUPABASE_KEY") == "abc123"


# --- has_key() ---


def test_has_key_sees_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    use_secrets(monkeypatch, {"GEMINI_API_KEY": "real-looking-value"})
    assert config.has_key("GEMINI_API_KEY") is True


def test_has_key_treats_placeholder_secret_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    use_secrets(monkeypatch, {"GEMINI_API_KEY": "your_gemini_api_key_here"})
    assert config.has_key("GEMINI_API_KEY") is False


def test_has_key_false_when_missing_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert config.has_key("GEMINI_API_KEY") is False


# --- read_secrets(): never crashes ---


def test_read_secrets_without_a_secrets_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_real_secrets_loader(monkeypatch, tmp_path / "does_not_exist.toml")
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")

    assert config.st.secrets.load_if_toml_exists() is False  # really no file
    assert dict(config.read_secrets()) == {}
    assert config.get_setting("GEMINI_API_KEY") == "from-env"


def test_read_secrets_with_a_real_secrets_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secrets_file = tmp_path / "secrets.toml"
    secrets_file.write_text('GEMINI_API_KEY = "from-file"\n', encoding="utf-8")
    use_real_secrets_loader(monkeypatch, secrets_file)
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")

    assert config.read_secrets()["GEMINI_API_KEY"] == "from-file"
    assert config.get_setting("GEMINI_API_KEY") == "from-file"  # secrets win over .env


def test_read_secrets_with_a_broken_secrets_file_falls_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secrets_file = tmp_path / "secrets.toml"
    secrets_file.write_text("this is = = not toml", encoding="utf-8")
    use_real_secrets_loader(monkeypatch, secrets_file)
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")

    assert dict(config.read_secrets()) == {}
    assert config.get_setting("GEMINI_API_KEY") == "from-env"


def test_read_secrets_survives_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    broken = MagicMock()
    broken.load_if_toml_exists.side_effect = RuntimeError("boom")
    monkeypatch.setattr(config.st, "secrets", broken)
    assert config.read_secrets() == {}


# --- The example file ---


def test_secrets_example_has_all_keys_as_placeholders() -> None:
    example = tomllib.loads(EXAMPLE_FILE.read_text(encoding="utf-8"))
    assert sorted(example) == sorted(KEY_NAMES)
    for name, value in example.items():
        assert isinstance(value, str)
        if name != "SUPABASE_URL":
            assert value.startswith("your_") and value.endswith("_here"), name


def test_real_secrets_file_is_git_ignored() -> None:
    gitignore = (Path(__file__).parent.parent / ".gitignore").read_text(encoding="utf-8")
    assert ".streamlit/secrets.toml" in gitignore.splitlines()

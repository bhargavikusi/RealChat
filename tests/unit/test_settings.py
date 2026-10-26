from pathlib import Path

import pytest

from backend.config.settings import SettingsError, load_settings

DB_SECRET = "db-pass-8f3k2"
REDIS_SECRET = "redis-pass-71ab"
DATABASE_URL = f"postgresql+asyncpg://realchat:{DB_SECRET}@localhost:5432/realchat"
REDIS_URL = f"redis://:{REDIS_SECRET}@localhost:6379/0"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("APP_ENV", "LOG_LEVEL", "DATABASE_URL", "REDIS_URL"):
        monkeypatch.delenv(name, raising=False)


def test_loads_valid_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)

    settings = load_settings(env_file=None)

    assert settings.app_env == "test"
    assert settings.log_level == "INFO"
    assert settings.database_url.get_secret_value() == DATABASE_URL


def test_secrets_hidden_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)

    settings = load_settings(env_file=None)

    assert DB_SECRET not in repr(settings)
    assert REDIS_SECRET not in str(settings)


def test_missing_required_setting_names_it_without_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)

    with pytest.raises(SettingsError) as excinfo:
        load_settings(env_file=None)

    message = str(excinfo.value)
    assert "REDIS_URL" in message
    assert DB_SECRET not in message


def test_invalid_value_is_not_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    rejected = "not-an-env-q9z"
    monkeypatch.setenv("APP_ENV", rejected)
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)

    with pytest.raises(SettingsError) as excinfo:
        load_settings(env_file=None)

    message = str(excinfo.value)
    assert "APP_ENV" in message
    assert rejected not in message
    assert DB_SECRET not in message
    # The original pydantic error (which contains inputs) must not be chained.
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__


def test_reads_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(f"DATABASE_URL={DATABASE_URL}\nREDIS_URL={REDIS_URL}\nLOG_LEVEL=DEBUG\n")

    settings = load_settings(env_file=str(env_file))

    assert settings.log_level == "DEBUG"
    assert settings.redis_url.get_secret_value() == REDIS_URL

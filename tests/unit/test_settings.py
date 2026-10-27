from pathlib import Path

import pytest

from backend.config.settings import SettingsError, load_bot_settings, load_settings

DB_SECRET = "db-pass-8f3k2"
REDIS_SECRET = "redis-pass-71ab"
DATABASE_URL = f"postgresql+asyncpg://realchat:{DB_SECRET}@localhost:5432/realchat"
REDIS_URL = f"redis://:{REDIS_SECRET}@localhost:6379/0"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "APP_ENV",
        "LOG_LEVEL",
        "DATABASE_URL",
        "REDIS_URL",
        "TELEGRAM_BOT_TOKEN",
        "MIN_AGE",
    ):
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


BOT_TOKEN = "123456789:TEST-fake-token-for-settings-tests"


def test_bot_settings_load_valid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)

    settings = load_bot_settings(env_file=None)

    assert settings.telegram_bot_token.get_secret_value() == BOT_TOKEN
    assert BOT_TOKEN not in repr(settings)


def test_bot_settings_require_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)

    with pytest.raises(SettingsError) as excinfo:
        load_bot_settings(env_file=None)

    assert "TELEGRAM_BOT_TOKEN" in str(excinfo.value)


def test_malformed_bot_token_is_rejected_without_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    malformed = "not-a-token-x8k3"
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", malformed)

    with pytest.raises(SettingsError) as excinfo:
        load_bot_settings(env_file=None)

    message = str(excinfo.value)
    assert "TELEGRAM_BOT_TOKEN" in message
    assert malformed not in message


def test_non_bot_processes_do_not_need_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)

    assert load_settings(env_file=None).app_env == "development"


def test_production_requires_explicit_min_age(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)

    with pytest.raises(SettingsError, match="MIN_AGE must be set explicitly in production"):
        load_bot_settings(env_file=None)

    monkeypatch.setenv("MIN_AGE", "18")
    assert load_bot_settings(env_file=None).min_age == 18

"""Application settings loaded from environment variables and an optional `.env` file."""

from typing import Literal

from aiogram.utils.token import TokenValidationError, validate_token
from pydantic import (
    Field,
    HttpUrl,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class SettingsError(Exception):
    """Raised when configuration is missing or invalid. The message never contains values."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Connection URLs can embed passwords, so they are stored as secrets.
    database_url: SecretStr
    redis_url: SecretStr

    # D-019: age and gender can each be changed once per this many days after onboarding.
    profile_change_cooldown_days: int = Field(default=30, ge=1, le=365)

    # D-009, D-070: users must be 18 or older, so the documents' "adults only" holds; it
    # may be set higher. The bot and the web pages both show it.
    min_age: int = Field(default=18, ge=18, le=99)


class BotSettings(Settings):
    """Settings for the processes that need the bot token: the bot, the worker (which
    sends messages), and the API (which checks the moderation Mini App's sign-in, D-071).
    Other tools, such as migrations, do not need it."""

    telegram_bot_token: SecretStr
    # Connect to Telegram's test environment instead (a separate network with its own
    # accounts and bots), to test Stars payments without real Stars (D-061).
    telegram_test_environment: bool = False

    # Per-user flood control: at most `bot_flood_limit` updates per window.
    bot_flood_limit: int = Field(default=20, ge=1)
    bot_flood_window_seconds: int = Field(default=10, ge=1)
    # How long a processed update_id is remembered to discard redeliveries.
    bot_update_dedup_ttl_seconds: int = Field(default=86_400, ge=60)
    # How long an update stays claimed while being processed; a safety net in case
    # the claim is never settled (it is normally released or marked done at once).
    bot_update_lease_seconds: int = Field(default=60, ge=5)

    # D-023: optional links shown in /help only when set.
    announcement_channel_url: HttpUrl | None = None
    support_contact: HttpUrl | None = None
    # Where the API serves the terms and privacy web pages (D-069); /terms links to them
    # only when set.
    public_base_url: HttpUrl | None = None

    @field_validator(
        "announcement_channel_url", "support_contact", "public_base_url", mode="before"
    )
    @classmethod
    def _empty_link_is_unset(cls, value: object) -> object:
        # An empty value in .env means the link is not configured.
        return None if value == "" else value

    @field_validator("announcement_channel_url", "support_contact", "public_base_url")
    @classmethod
    def _require_https(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and value.scheme != "https":
            raise ValueError("must be an https:// link")
        return value

    @field_validator("public_base_url")
    @classmethod
    def _plain_base_url(cls, value: HttpUrl | None) -> HttpUrl | None:
        # The page paths are appended to it, so it can't carry a query or fragment.
        if value is not None and (value.query or value.fragment):
            raise ValueError("must not contain ? or #")
        return value

    @field_validator("telegram_bot_token")
    @classmethod
    def _check_token_format(cls, value: SecretStr) -> SecretStr:
        try:
            validate_token(value.get_secret_value())
        except TokenValidationError:
            # Raised without the original message, which may describe the token.
            raise ValueError("not a valid Telegram bot token format") from None
        return value

    @model_validator(mode="after")
    def _require_production_support(self) -> "BotSettings":
        # Telegram requires bots that take payments to offer support (D-061).
        if self.app_env == "production" and self.support_contact is None:
            raise ValueError("SUPPORT_CONTACT must be set in production")
        return self

    @model_validator(mode="after")
    def _refuse_production_test_environment(self) -> "BotSettings":
        if self.app_env == "production" and self.telegram_test_environment:
            raise ValueError("TELEGRAM_TEST_ENVIRONMENT can't be used in production")
        return self


def load_settings(env_file: str | None = ".env") -> Settings:
    return _load(Settings, env_file)


def load_bot_settings(env_file: str | None = ".env") -> BotSettings:
    return _load(BotSettings, env_file)


def _load[SettingsT: Settings](settings_cls: type[SettingsT], env_file: str | None) -> SettingsT:
    """Load settings, failing fast with a readable error.

    Pydantic's own error text includes the rejected input, which could be a secret,
    so it is replaced with a message that names only the setting and the problem.
    """
    try:
        return settings_cls(_env_file=env_file)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(part) for part in error['loc']).upper()}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        ]
        raise SettingsError("Invalid configuration:\n  " + "\n  ".join(problems)) from None

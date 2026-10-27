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


class BotSettings(Settings):
    """Settings for the Telegram bot process. Other processes do not need the token."""

    telegram_bot_token: SecretStr

    # D-009: development value 18; the launch value is still open (O-001).
    min_age: int = Field(default=18, ge=13, le=99)

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

    @field_validator("announcement_channel_url", "support_contact", mode="before")
    @classmethod
    def _empty_link_is_unset(cls, value: object) -> object:
        # An empty value in .env means the link is not configured.
        return None if value == "" else value

    @field_validator("announcement_channel_url", "support_contact")
    @classmethod
    def _require_https(cls, value: HttpUrl | None) -> HttpUrl | None:
        if value is not None and value.scheme != "https":
            raise ValueError("must be an https:// link")
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
    def _require_explicit_production_age(self) -> "BotSettings":
        # The default is a development value; the launch value is still open (O-001),
        # so production must never fall back to it silently.
        if self.app_env == "production" and "min_age" not in self.model_fields_set:
            raise ValueError("MIN_AGE must be set explicitly in production")
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

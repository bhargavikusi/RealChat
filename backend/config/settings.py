"""Application settings loaded from environment variables and an optional `.env` file."""

from typing import Literal

from pydantic import SecretStr, ValidationError
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


def load_settings(env_file: str | None = ".env") -> Settings:
    """Load settings, failing fast with a readable error.

    Pydantic's own error text includes the rejected input, which could be a secret,
    so it is replaced with a message that names only the setting and the problem.
    """
    try:
        return Settings(_env_file=env_file)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(part) for part in error['loc']).upper()}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        ]
        raise SettingsError("Invalid configuration:\n  " + "\n  ".join(problems)) from None

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Admin panel credentials
    admin_username: str = "admin"
    admin_password: str = "changeme"

    # Security
    secret_key: str = "dev-secret-key-change-in-production"

    # Google AI
    google_ai_api_key: str = ""

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/tbcbot"

    # Telegram bot (optional: seeds the DB on first run if provided)
    bot_token: str = ""

    # Webhook
    webhook_base_url: str = "http://localhost:8000"

    # File storage
    uploads_dir: str = "/data/uploads"

    # App
    port: int = 8000
    debug: bool = False

    @property
    def uploads_path(self) -> Path:
        return Path(self.uploads_dir)

    @property
    def webhook_url(self) -> str:
        return f"{self.webhook_base_url.rstrip('/')}/bot/webhook"


@lru_cache
def get_settings() -> Settings:
    return Settings()

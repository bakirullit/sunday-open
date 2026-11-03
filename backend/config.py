import json
from typing import Any
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ALLOWED_TABLES: tuple[str, ...] = (
    "portal",
    "steps",
    "sundays",
    "comms",
    "guests",
    "audiences",
    "bank",
    "pillars",
    "channels",
    "cycle",
    "weekly",
    "bookings",
    "risks",
    "requests",
    "materials",
    "briefs",
    "lifecycle",
    "charter",
    "domains",
    "raci",
    "finq",
    "standup",
    "fwtasks",
    "portfolio",
    "pmotasks",
    "perimeters",
    "docs",
    "robot",
    "portalmap",
    "route",
    "roles",
    "stakeholders",
    "office",
    "rules",
    "calc",
)

WRITABLE_TABLES: tuple[str, ...] = tuple(t for t in ALLOWED_TABLES if t != "calc")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/sunday_open"
    REDIS_URL: str = "redis://localhost:6379/0"

    TG_BOT_TOKEN: str = ""
    TG_WEBAPP_URL: str = ""
    PUBLIC_BASE_URL: str = ""

    OPENROUTER_KEY: str = ""

    SUNDAY_MODERATION: str = "4"
    SUNDAY_MODERATOR_EMAILS: str = "[]"
    SUNDAY_NOTIFICATION_EMAIL: str = ""

    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "Sunday Open <noreply@sunday-open.local>"
    SMTP_TLS: bool = True

    ADMIN_SECRET: str = "sunday-open-admin-secret-key"
    TZ: str = "Asia/Almaty"
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    @property
    def is_moderated(self) -> bool:
        return str(self.SUNDAY_MODERATION).strip() == "4"

    @property
    def protocol_version(self) -> int:
        return 4 if self.is_moderated else 3

    @property
    def moderator_emails(self) -> list[str]:
        try:
            raw: Any = json.loads(self.SUNDAY_MODERATOR_EMAILS or "[]")
            if isinstance(raw, list):
                return [
                    x.strip().lower()
                    for x in raw
                    if isinstance(x, str) and x.strip()
                ]
        except Exception:
            pass
        return []


settings = Settings()

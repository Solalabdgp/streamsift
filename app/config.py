from datetime import time

from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Telegram bot (notifier)
    bot_token: str = ""
    owner_telegram_id: int = 0

    # Infra
    database_url: str = "postgresql+asyncpg://jobradar:jobradar@postgres:5432/jobradar"
    redis_url: str = "redis://redis:6379/0"

    # Scoring
    notify_threshold: int = 10
    grey_low: int = 8
    grey_high: int = 15
    stack_score_cap: int = 9
    hiring_score_cap: int = 8
    noise_penalty_cap: int = -8

    # ML
    ml_enabled: bool = False
    ml_threshold: float = 0.5
    ml_min_samples: int = 150
    ml_model_dir: str = "/data/models"

    # General
    max_message_age_min: int = 180
    source_backfill_days: int = 7
    quiet_hours_start: str = "23:00"
    quiet_hours_end: str = "08:00"
    tz: str = "Asia/Almaty"
    rules_version: str = "v1"
    log_level: str = "INFO"

    @property
    def quiet_start(self) -> time:
        return _parse_hhmm(self.quiet_hours_start)

    @property
    def quiet_end(self) -> time:
        return _parse_hhmm(self.quiet_hours_end)


settings = Settings()

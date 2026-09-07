"""Конфигурация сервера. Единственный источник значений — окружение и `.env`."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
LogFormat = Literal["console", "json"]


class Settings(BaseSettings):
    """Настройки процесса.

    Путь к БД берётся только отсюда: пользовательский ввод в него не интерполируется
    (см. раздел «Безопасность» в docs/DESIGN.md).
    """

    model_config = SettingsConfigDict(
        env_prefix="TVTRACKER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_path: Path = Path("data/tvtracker.db")

    tvmaze_base_url: str = "https://api.tvmaze.com"
    http_timeout: float = Field(default=10.0, gt=0)
    rate_limit_per_second: float = Field(default=1.8, gt=0, le=2.0)
    max_retries: int = Field(default=3, ge=0, le=10)
    backoff_base: float = Field(default=0.5, gt=0)

    log_level: LogLevel = "INFO"
    log_format: LogFormat = "console"
    log_file: Path | None = None

    summary_max_chars: int = Field(default=200, ge=50, le=1000)
    default_limit: int = Field(default=8, ge=1, le=50)

    @field_validator("log_file", mode="before")
    @classmethod
    def _empty_log_file_means_stderr_only(cls, value: object) -> object:
        """Пустая переменная окружения — это «не задано», а не пустой путь.

        `TVTRACKER_LOG_FILE=` в `.env` — обычный способ выключить файловый лог. Без этой
        нормализации получается `Path(".")`, и обработчик пытается открыть каталог как файл.
        """
        return None if isinstance(value, str) and not value.strip() else value


def load_settings() -> Settings:
    """Читает настройки. Отдельная функция, чтобы тесты подменяли её без импорт-эффектов."""
    return Settings()

"""Конфигурация: границы значений, которые дороже всего чинить в проде."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tests.conftest import make_settings

pytestmark = pytest.mark.unit


def test_defaults_are_safe_for_tvmaze_limits() -> None:
    """TVmaze разрешает ~20 запросов / 10 сек; дефолт обязан оставаться под лимитом."""
    settings = make_settings()
    assert settings.rate_limit_per_second <= 2.0
    assert settings.max_retries >= 1


def test_env_prefix_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TVTRACKER_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("TVTRACKER_DB_PATH", "custom/x.db")

    settings = make_settings()
    assert settings.log_level == "DEBUG"
    assert settings.db_path == Path("custom/x.db")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rate_limit_per_second", 5.0),
        ("http_timeout", 0),
        ("max_retries", -1),
    ],
)
def test_out_of_range_values_are_rejected(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_empty_log_file_env_var_means_stderr_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """`TVTRACKER_LOG_FILE=` в `.env` — это «файлового лога нет», а не путь `.`."""
    monkeypatch.setenv("TVTRACKER_LOG_FILE", "")

    assert make_settings().log_file is None


def test_log_file_path_is_kept_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TVTRACKER_LOG_FILE", "logs/tvtracker.log")

    assert make_settings().log_file == Path("logs/tvtracker.log")

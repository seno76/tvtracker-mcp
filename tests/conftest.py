"""Общие фикстуры. Всё, что здесь есть, работает без сети и без файлов на диске."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tvtracker.config import Settings


def make_settings(**overrides: Any) -> Settings:
    """Настройки, изолированные от `.env` разработчика.

    `_env_file` — служебный аргумент pydantic-settings, его нет в аннотациях модели,
    поэтому единственное подавление типов на весь набор тестов живёт здесь.
    """
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


@pytest.fixture
def anyio_backend() -> str:
    """Асинхронные тесты гоняем на asyncio (см. документацию anyio по бэкендам)."""
    return "asyncio"


@pytest.fixture
def settings_factory() -> Callable[..., Settings]:
    return make_settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(db_path=tmp_path / "test.db", log_level="DEBUG", log_format="json")


@pytest.fixture
def reset_logging() -> Iterator[None]:
    """Возвращает корневой логгер в исходное состояние: тесты логирования не липнут друг к другу."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)

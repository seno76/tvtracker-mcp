"""Логирование.

Три решения, которые здесь зафиксированы:

1. **Только stderr.** Сервер живёт на stdio-транспорте: stdout принадлежит протоколу MCP,
   и любая посторонняя строка там ломает сессию. Стандартный `logging` пишет в stderr
   по умолчанию, поэтому мы просто не мешаем ему — и никогда не используем `print`.
2. **Конфигурируем раньше, чем создаём сервер.** `MCPServer` сам вызывает
   `logging.basicConfig(...)`, а тот не трогает уже существующие обработчики. Значит,
   `configure_logging()` до создания сервера — это наша конфигурация, а не SDK-шная.
3. **Контекст запроса живёт в `contextvars`.** Идентификатор вызова и имя инструмента
   попадают в каждую запись автоматически, поэтому в самих обработчиках нет ни одного
   аргумента вида `request_id=...`.

Ссылки: https://py.sdk.modelcontextprotocol.io/handlers/logging/
        https://docs.python.org/3/library/logging.html
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_context: ContextVar[dict[str, Any] | None] = ContextVar("tvtracker_log_context", default=None)


def _current() -> dict[str, Any]:
    return _context.get() or {}

# Поля, которые `logging` кладёт в каждую запись сам. Всё, чего здесь нет, — это `extra`.
_RESERVED = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _RESERVED}


class ContextFilter(logging.Filter):
    """Переносит текущий контекст запроса в запись лога."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in _current().items():
            if key not in record.__dict__:
                record.__dict__[key] = value
        return True


class JsonFormatter(logging.Formatter):
    """Одна строка JSON на запись — формат для прода и для разбора логов."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            **_extras(record),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    """Читаемый формат для разработки: сообщение, затем `key=value` из `extra`."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extras = " ".join(f"{k}={v}" for k, v in _extras(record).items())
        return f"{line} | {extras}" if extras else line


def configure_logging(
    level: str = "INFO",
    fmt: str = "console",
    log_file: Path | None = None,
) -> None:
    """Настраивает корневой логгер. Идемпотентна: повторный вызов заменяет обработчики."""
    formatter: logging.Formatter = JsonFormatter() if fmt == "json" else ConsoleFormatter()
    context_filter = ContextFilter()

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3))

    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(context_filter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)

    # Транспортный шум SDK и httpx не нужен на INFO — он заглушает наши собственные события.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@contextmanager
def bind(**fields: Any) -> Iterator[None]:
    """Добавляет поля в контекст логирования на время блока."""
    token = _context.set({**_current(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


@contextmanager
def request_scope(operation: str, **fields: Any) -> Iterator[str]:
    """Оборачивает один вызов инструмента/ресурса: id, длительность, исход.

    Успех логируется на DEBUG, отказ — на WARNING с длительностью. Именно эта пара
    строк отвечает на вопрос «почему сценарий стоил четыре вызова вместо одного».
    """
    request_id = uuid.uuid4().hex[:8]
    logger = logging.getLogger("tvtracker.request")
    started = time.perf_counter()
    with bind(request_id=request_id, operation=operation, **fields):
        try:
            yield request_id
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000
            logger.warning(
                "operation failed",
                extra={"duration_ms": round(elapsed, 1), "error": type(exc).__name__},
            )
            raise
        else:
            elapsed = (time.perf_counter() - started) * 1000
            logger.debug("operation ok", extra={"duration_ms": round(elapsed, 1)})


def get_logger(name: str) -> logging.Logger:
    """Логгер модуля. Вызывается один раз на модуль, на уровне импорта."""
    return logging.getLogger(name)

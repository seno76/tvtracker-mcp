"""Логирование: контракт, на который опираются все остальные модули."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from tvtracker.log import bind, configure_logging, get_logger, request_scope

pytestmark = pytest.mark.unit


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    err = capsys.readouterr().err
    return [json.loads(line) for line in err.splitlines() if line.strip()]


def test_logs_go_to_stderr_never_stdout(
    reset_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """stdout принадлежит MCP-протоколу: одна лишняя строка там ломает stdio-сессию."""
    configure_logging(level="INFO", fmt="json")
    get_logger("tvtracker.test").info("hello")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err.strip())["msg"] == "hello"


def test_json_format_carries_extra_fields(
    reset_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(level="INFO", fmt="json")
    get_logger("tvtracker.test").info("search done", extra={"hits": 8, "took_ms": 12.5})

    record = _lines(capsys)[0]
    assert record["level"] == "INFO"
    assert record["hits"] == 8
    assert record["took_ms"] == 12.5


def test_bind_adds_context_and_restores_it(
    reset_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(level="INFO", fmt="json")
    logger = get_logger("tvtracker.test")

    with bind(tool="show_search"):
        logger.info("inside")
    logger.info("outside")

    inside, outside = _lines(capsys)
    assert inside["tool"] == "show_search"
    assert "tool" not in outside


def test_request_scope_measures_and_reraises(
    reset_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(level="DEBUG", fmt="json")

    with pytest.raises(ValueError), request_scope("show_search", by="description"):
        raise ValueError("boom")

    failure = _lines(capsys)[-1]
    assert failure["level"] == "WARNING"
    assert failure["operation"] == "show_search"
    assert failure["by"] == "description"
    assert failure["error"] == "ValueError"
    assert isinstance(failure["duration_ms"], float)
    assert len(str(failure["request_id"])) == 8


def test_configure_is_idempotent(reset_logging: None, capsys: pytest.CaptureFixture[str]) -> None:
    """Повторный вызов не должен множить обработчики — иначе строки задваиваются."""
    configure_logging(level="INFO", fmt="json")
    configure_logging(level="INFO", fmt="json")
    get_logger("tvtracker.test").info("once")

    assert len(_lines(capsys)) == 1
    assert len(logging.getLogger().handlers) == 1


def test_existing_configuration_wins_when_not_forced(
    reset_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Импорт модуля не должен затирать уровень, который выставил вызывающий."""
    configure_logging(level="WARNING", fmt="json")
    configure_logging(level="DEBUG", fmt="json", force=False)

    get_logger("tvtracker.test").info("должно быть отфильтровано")

    assert _lines(capsys) == []


def test_forced_configuration_replaces_the_previous_one(
    reset_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(level="WARNING", fmt="json")
    configure_logging(level="DEBUG", fmt="json")

    get_logger("tvtracker.test").info("видно")

    assert len(_lines(capsys)) == 1

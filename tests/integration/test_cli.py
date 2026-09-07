"""CLI: разбор аргументов и путь `status`.

Синхронизация здесь не гоняется — она покрыта в `test_sync.py`. Проверяется то, что
ломается при рефакторинге парсера: обязательность режима и код возврата.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tvtracker.cli import build_parser, main

pytestmark = pytest.mark.integration


def test_sync_requires_a_mode() -> None:
    """`sync` без `--full`/`--updates` — ошибка аргументов, а не молчаливый обход каталога."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["sync"])


def test_full_and_updates_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["sync", "--full", "--updates"])


def test_parses_incremental_sync() -> None:
    args = build_parser().parse_args(["sync", "--updates", "--since", "day"])

    assert args.updates is True
    assert args.since == "day"
    assert args.start_page == 0


def test_status_reports_empty_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TVTRACKER_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setattr("sys.argv", ["tvtracker", "status"])

    code = main()

    out = capsys.readouterr().out
    assert code == 0
    assert "сериалов: 0" in out
    assert "не выполнялась" in out

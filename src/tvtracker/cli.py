"""CLI: обслуживание базы вне MCP-сессии.

Здесь живёт всё, что занимает минуты: полный обход каталога и инкрементальное обновление.
Внутри MCP-инструмента такому места нет — хост оборвёт вызов по таймауту задолго до конца.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from tvtracker.config import load_settings
from tvtracker.errors import TvtrackerError
from tvtracker.log import configure_logging, get_logger
from tvtracker.storage import repo as repo_module
from tvtracker.storage.repo import Repo
from tvtracker.storage.sync import sync_full, sync_updates
from tvtracker.tvmaze.client import TVmazeClient

logger = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tvtracker", description="Обслуживание базы tvtracker")
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="синхронизация корпуса с TVmaze")
    mode = sync.add_mutually_exclusive_group(required=True)
    mode.add_argument("--full", action="store_true", help="полный обход каталога (~4 минуты)")
    mode.add_argument("--updates", action="store_true", help="только изменившееся с прошлого раза")
    sync.add_argument(
        "--since",
        default="week",
        choices=("day", "week", "month"),
        help="глубина инкремента для --updates (по умолчанию week)",
    )
    sync.add_argument("--start-page", type=int, default=0, help="продолжить обход с этой страницы")

    sub.add_parser("status", help="что лежит в базе")
    return parser


async def _run(args: argparse.Namespace) -> int:
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_format, settings.log_file)
    conn = repo_module.connect(settings.db_path)
    repo = Repo(conn)

    try:
        if args.command == "status":
            # В CLI stdout принадлежит пользователю, а не MCP-протоколу: печатать можно.
            print(
                f"сериалов: {repo.count_shows()}\n"
                f"полная синхронизация: {repo.last_full_sync or 'не выполнялась'}\n"
                f"инкремент: {repo.last_updates_sync or 'не выполнялся'}"
            )
            return 0

        async with TVmazeClient(settings) as client:
            report = (
                await sync_full(client, repo, args.start_page)
                if args.full
                else await sync_updates(client, repo, args.since)
            )
        print(f"записано сериалов: {report.shows} за {report.elapsed_s} с")
        return 0
    except TvtrackerError as exc:
        logger.error("sync failed", extra={"error": type(exc).__name__})
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def main() -> int:
    """Точка входа `tvtracker`. Прерывание с клавиатуры — не ошибка, а обычный выход."""
    args = build_parser().parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("прервано", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

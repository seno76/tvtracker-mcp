"""Синхронизация корпуса с TVmaze.

Три режима, и все три идемпотентны — повторный запуск не портит базу и не плодит дубли:

* **полный обход** `/shows?page=0..N` — один раз при установке, ~380 запросов, ~4 минуты;
* **инкремент** `/updates/shows` — только изменившееся, дёшево, можно раз в сутки;
* **эпизоды одного сериала** — лениво, в момент `tracking_update(action="track")`.

Синхронизация запускается командой `tvtracker sync`, а не MCP-инструментом: четыре минуты
внутри вызова инструмента — это гарантированный таймаут у хоста.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from tvtracker.errors import NotFoundError
from tvtracker.log import get_logger
from tvtracker.storage.repo import Repo, transaction
from tvtracker.tvmaze.client import TVmazeClient
from tvtracker.tvmaze.models import Episode, Show

logger = get_logger(__name__)


@dataclass(frozen=True)
class SyncReport:
    """Итог прогона. Возвращается наружу, чтобы CLI печатал числа, а не догадки."""

    pages: int
    shows: int
    elapsed_s: float


async def sync_full(client: TVmazeClient, repo: Repo, start_page: int = 0) -> SyncReport:
    """Полный обход каталога.

    Каждая страница пишется своей транзакцией: обход длинный, и обрыв на 200-й странице
    не должен откатывать первые 199.
    """
    started = time.perf_counter()
    pages = written = 0

    async for page, rows in client.iter_show_pages(start_page):
        shows = [Show.parse(row) for row in rows]
        with transaction(repo.conn):
            written += repo.upsert_shows(shows)
        pages += 1
        logger.info(
            "catalog page stored",
            extra={
                "page": page,
                "rows": len(shows),
                "total": written,
                "elapsed_s": round(time.perf_counter() - started, 1),
            },
        )

    with transaction(repo.conn):
        repo.mark_full_sync()
    elapsed = round(time.perf_counter() - started, 1)
    logger.info("full sync done", extra={"pages": pages, "shows": written, "elapsed_s": elapsed})
    return SyncReport(pages=pages, shows=written, elapsed_s=elapsed)


async def sync_updates(client: TVmazeClient, repo: Repo, since: str = "week") -> SyncReport:
    """Инкремент: тянем только те сериалы, что изменились у источника.

    `/updates/shows` отдаёт карту `id → timestamp`; всё, что новее нашей отметки,
    перезагружается поштучно. Сериал, исчезнувший из каталога (404), просто пропускается.
    """
    started = time.perf_counter()
    updates = await client.get_updates(since)
    known = repo.last_updates_sync
    logger.info("updates fetched", extra={"count": len(updates), "since": since, "known": known})

    written = 0
    for raw_id in updates:
        try:
            payload = await client.get_show_with_episodes(int(raw_id))
        except NotFoundError:
            logger.debug("show gone from source", extra={"tvmaze_id": raw_id})
            continue
        with transaction(repo.conn):
            written += repo.upsert_shows([Show.parse(payload)])
        _store_embedded_episodes(repo, payload)

    with transaction(repo.conn):
        repo.mark_updates_sync()
    elapsed = round(time.perf_counter() - started, 1)
    logger.info("updates sync done", extra={"shows": written, "elapsed_s": elapsed})
    return SyncReport(pages=0, shows=written, elapsed_s=elapsed)


async def sync_show_episodes(client: TVmazeClient, repo: Repo, tvmaze_id: int) -> int:
    """Загружает сериал вместе с эпизодами одним запросом. Возвращает число серий."""
    payload = await client.get_show_with_episodes(tvmaze_id)
    with transaction(repo.conn):
        repo.upsert_shows([Show.parse(payload)])
    return _store_embedded_episodes(repo, payload)


def _store_embedded_episodes(repo: Repo, payload: dict[str, Any]) -> int:
    """Достаёт эпизоды из `_embedded` и кладёт их под slug сериала."""
    embedded = payload.get("_embedded")
    raw_episodes = embedded.get("episodes") if isinstance(embedded, dict) else None
    if not isinstance(raw_episodes, list):
        return 0

    slug = repo.slug_for_tvmaze_id(int(payload["id"]))
    if slug is None:  # pragma: no cover — сериал всегда пишется строкой выше
        return 0

    episodes = [Episode.parse(row) for row in raw_episodes]
    with transaction(repo.conn):
        stored = repo.replace_episodes(slug, episodes)
    logger.debug("episodes stored", extra={"show": slug, "episodes": stored})
    return stored

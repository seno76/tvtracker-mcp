"""Доступ к SQLite.

Весь SQL проекта живёт здесь и в `schema.sql`. Правила, которые отсюда не выходят:

* **только параметризованные запросы** — пользовательская строка никогда не склеивается
  с текстом SQL;
* **путь к БД берётся из `Settings`** — в него не интерполируется ввод пользователя;
* соединение открывается один раз на процесс (в lifespan сервера) и переиспользуется.

Справочник: https://docs.python.org/3/library/sqlite3.html
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from tvtracker.log import get_logger
from tvtracker.tvmaze.models import Episode, Show

logger = get_logger(__name__)

_LAST_FULL_SYNC = "last_full_sync"
_LAST_UPDATES_SYNC = "last_updates_sync"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _load_schema() -> str:
    return resources.files("tvtracker.storage").joinpath("schema.sql").read_text("utf-8")


def connect(db_path: Path) -> sqlite3.Connection:
    """Открывает соединение и приводит БД к актуальной схеме.

    `WAL` и `synchronous=NORMAL` — обычный компромисс для однопользовательской локальной
    базы: запись не блокирует чтение, а потеря последней транзакции при отключении питания
    для кэша каталога не катастрофа.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_load_schema())
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Одна явная транзакция: соединение открыто в autocommit, границы задаём сами."""
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


class Repo:
    """Тонкий слой над SQLite. Ничего не знает ни про MCP, ни про HTTP."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # --- Корпус сериалов ----------------------------------------------------------------

    def upsert_shows(self, shows: Iterable[Show]) -> int:
        """Вставляет или обновляет сериалы пачкой. Возвращает число записанных строк.

        Ключ — `tvmaze_id`, а не slug: у источника меняются и название, и год премьеры,
        и такое изменение должно обновлять строку, а не плодить дубль.
        """
        rows = [
            (
                self._unique_slug(show),
                show.tvmaze_id,
                show.name,
                show.type,
                show.language,
                json.dumps(show.genres, ensure_ascii=False),
                show.status,
                show.premiered,
                show.ended,
                show.avg_runtime,
                show.rating,
                show.weight,
                show.network,
                show.summary_clean,
                _now(),
            )
            for show in shows
        ]
        if not rows:
            return 0
        self.conn.executemany(
            """
            INSERT INTO shows (slug, tvmaze_id, name, type, language, genres_json, status,
                               premiered, ended, avg_runtime, rating, weight, network,
                               summary_clean, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tvmaze_id) DO UPDATE SET
                name = excluded.name,
                type = excluded.type,
                language = excluded.language,
                genres_json = excluded.genres_json,
                status = excluded.status,
                premiered = excluded.premiered,
                ended = excluded.ended,
                avg_runtime = excluded.avg_runtime,
                rating = excluded.rating,
                weight = excluded.weight,
                network = excluded.network,
                summary_clean = excluded.summary_clean,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        return len(rows)

    def _unique_slug(self, show: Show) -> str:
        """Разрешает коллизию slug суффиксом `-2`, `-3`, …

        Занятым считается slug, принадлежащий **другому** `tvmaze_id`: повторная
        синхронизация того же сериала обязана попасть в свою же строку.
        """
        base = show.slug
        candidate, suffix = base, 1
        while True:
            row = self.conn.execute(
                "SELECT tvmaze_id FROM shows WHERE slug = ?", (candidate,)
            ).fetchone()
            if row is None or row["tvmaze_id"] == show.tvmaze_id:
                return candidate
            suffix += 1
            candidate = f"{base}-{suffix}"

    def slug_for_tvmaze_id(self, tvmaze_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT slug FROM shows WHERE tvmaze_id = ?", (tvmaze_id,)
        ).fetchone()
        return str(row["slug"]) if row else None

    def count_shows(self) -> int:
        return int(self.conn.execute("SELECT count(*) AS n FROM shows").fetchone()["n"])

    # --- Эпизоды ------------------------------------------------------------------------

    def replace_episodes(self, show_slug: str, episodes: Iterable[Episode]) -> int:
        """Заменяет эпизоды сериала целиком.

        Замена, а не слияние: у источника серии переименовывают и переносят между сезонами,
        и частичное обновление оставило бы в базе призраков.
        """
        rows = [
            (
                show_slug,
                episode.season,
                episode.number,
                episode.name,
                episode.airstamp,
                episode.runtime,
                episode.rating,
                episode.summary_clean,
                episode.kind,
            )
            for episode in episodes
            if episode.number is not None
        ]
        self.conn.execute("DELETE FROM episodes WHERE show_slug = ?", (show_slug,))
        if rows:
            self.conn.executemany(
                """
                INSERT INTO episodes (show_slug, season, number, name, airstamp, runtime,
                                      rating, summary_clean, kind)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def count_episodes(self, show_slug: str) -> int:
        row = self.conn.execute(
            "SELECT count(*) AS n FROM episodes WHERE show_slug = ?", (show_slug,)
        ).fetchone()
        return int(row["n"])

    # --- Отметки синхронизации ----------------------------------------------------------

    def get_sync_marker(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_sync_marker(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO sync_state (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    @property
    def last_full_sync(self) -> str | None:
        return self.get_sync_marker(_LAST_FULL_SYNC)

    def mark_full_sync(self) -> None:
        self.set_sync_marker(_LAST_FULL_SYNC, _now())

    @property
    def last_updates_sync(self) -> str | None:
        return self.get_sync_marker(_LAST_UPDATES_SYNC)

    def mark_updates_sync(self) -> None:
        self.set_sync_marker(_LAST_UPDATES_SYNC, _now())

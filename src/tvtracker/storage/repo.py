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
        taken: set[str] = set()
        rows = [
            (
                self._unique_slug(show, taken),
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

    def _unique_slug(self, show: Show, taken: set[str] | None = None) -> str:
        """Разрешает коллизию slug суффиксом `-2`, `-3`, …

        Занятым считается slug, принадлежащий **другому** `tvmaze_id`: повторная
        синхронизация того же сериала обязана попасть в свою же строку.

        `taken` — ключи, уже розданные в этой же пачке. Без него два одноимённых сериала
        с одной страницы каталога получают один slug: в базе на момент проверки нет ещё
        ни одного из них, и `executemany` падает на UNIQUE.
        """
        seen = taken if taken is not None else set()
        base = show.slug
        candidate, suffix = base, 1
        while True:
            row = self.conn.execute(
                "SELECT tvmaze_id FROM shows WHERE slug = ?", (candidate,)
            ).fetchone()
            free_in_db = row is None or row["tvmaze_id"] == show.tvmaze_id
            if free_in_db and candidate not in seen:
                seen.add(candidate)
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

    # --- Чтение для инструментов и ресурсов ---------------------------------------------

    def get_show(self, slug: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM shows WHERE slug = ?", (slug,)
        ).fetchone()
        return row

    def resolve_show(self, reference: str) -> sqlite3.Row | None:
        """Находит сериал по slug или по названию.

        Модель оперирует человеческими именами, а внутри у нас slug. Точное совпадение
        имени бьёт частичное, а среди частичных выигрывает более популярный — иначе
        «The Office» отдаёт малоизвестный ремейк вместо оригинала.
        """
        row = self.get_show(reference)
        if row is not None:
            return row
        found: sqlite3.Row | None = self.conn.execute(
            """
            SELECT * FROM shows
            WHERE lower(name) = lower(?) OR lower(name) LIKE lower(?)
            ORDER BY CASE WHEN lower(name) = lower(?) THEN 0 ELSE 1 END,
                     weight DESC NULLS LAST
            LIMIT 1
            """,
            (reference, f"%{reference}%", reference),
        ).fetchone()
        return found

    def episodes_for(self, show_slug: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM episodes WHERE show_slug = ? ORDER BY season, number",
            (show_slug,),
        ).fetchall()

    def facets(self) -> dict[str, list[tuple[str, int]]]:
        """Значения фильтров, реально встречающиеся в корпусе.

        Без справочника модель угадывает написание (`Sci-Fi` вместо `Science-Fiction`),
        получает пустую выдачу и делает ложный вывод «в каталоге ничего нет».
        """
        genres = self.conn.execute(
            """
            SELECT value AS name, count(*) AS n
            FROM shows, json_each(shows.genres_json)
            GROUP BY value ORDER BY n DESC
            """
        ).fetchall()
        # Группировка идёт по самой колонке, а не по псевдониму `name`: у таблицы `shows`
        # есть собственная колонка `name` (название сериала), и `GROUP BY name` собрал бы
        # статистику по названиям, а не по статусам.
        simple = {
            key: self.conn.execute(
                f"SELECT {key} AS name, count(*) AS n FROM shows "  # noqa: S608
                f"WHERE {key} IS NOT NULL AND {key} != '' "
                f"GROUP BY {key} ORDER BY n DESC LIMIT 30"
            ).fetchall()
            for key in ("status", "type", "language")
        }
        return {
            "genre": [(str(r["name"]), int(r["n"])) for r in genres],
            **{k: [(str(r["name"]), int(r["n"])) for r in rows] for k, rows in simple.items()},
        }

    # --- Трекинг ------------------------------------------------------------------------

    def tracking_for(self, slugs: Iterable[str]) -> dict[str, sqlite3.Row]:
        """Состояние отслеживания для набора сериалов.

        Каждая выдача помечается собственным состоянием — этого в TVmaze нет ни при
        каком запросе, и именно это отличает ответ сервера от ответа обёртки.
        """
        keys = list(slugs)
        if not keys:
            return {}
        placeholders = ",".join("?" * len(keys))
        rows = self.conn.execute(
            f"SELECT * FROM tracking WHERE show_slug IN ({placeholders})",  # noqa: S608
            keys,
        ).fetchall()
        return {str(row["show_slug"]): row for row in rows}

    def get_tracking(self, slug: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM tracking WHERE show_slug = ?", (slug,)
        ).fetchone()
        return row

    def list_tracking(self, states: Iterable[str] | None = None) -> list[sqlite3.Row]:
        """Записи трекинга вместе с метаданными сериала, свежие сверху."""
        sql = """
            SELECT t.*, s.name, s.genres_json, s.avg_runtime, s.network, s.language, s.status
            FROM tracking AS t JOIN shows AS s ON s.slug = t.show_slug
        """
        params: list[object] = []
        wanted = list(states) if states else []
        if wanted:
            sql += f" WHERE t.state IN ({','.join('?' * len(wanted))})"
            params = list(wanted)
        sql += " ORDER BY t.updated_at DESC"
        return self.conn.execute(sql, params).fetchall()

    def upsert_tracking(
        self,
        slug: str,
        state: str,
        season: int | None = None,
        number: int | None = None,
        rating: int | None = None,
        note: str | None = None,
    ) -> None:
        """Пишет состояние. Переданные `None` не затирают уже сохранённое.

        Так `action="rate"` не сбрасывает позицию, а `action="progress"` не стирает оценку:
        одно действие меняет ровно одно поле.
        """
        self.conn.execute(
            """
            INSERT INTO tracking (show_slug, state, season, number, rating, note,
                                  started_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(show_slug) DO UPDATE SET
                state = excluded.state,
                season = coalesce(excluded.season, tracking.season),
                number = coalesce(excluded.number, tracking.number),
                rating = coalesce(excluded.rating, tracking.rating),
                note = coalesce(excluded.note, tracking.note),
                updated_at = excluded.updated_at
            """,
            (slug, state, season, number, rating, note, _now(), _now()),
        )

    def delete_tracking(self, slug: str) -> bool:
        cursor = self.conn.execute("DELETE FROM tracking WHERE show_slug = ?", (slug,))
        return cursor.rowcount > 0

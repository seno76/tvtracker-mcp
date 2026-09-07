"""Хранилище поверх настоящей SQLite во временном каталоге.

Проверяется то, что ломается тихо: идемпотентность повторной синхронизации, коллизии
slug, согласованность FTS-индекса с таблицей и полная замена эпизодов.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from tvtracker.storage.repo import Repo, connect, transaction
from tvtracker.tvmaze.models import Episode, Show

pytestmark = pytest.mark.integration


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[Repo]:
    conn = connect(tmp_path / "test.db")
    yield Repo(conn)
    conn.close()


def show(tvmaze_id: int, name: str, premiered: str | None = "2022-01-01", **extra: object) -> Show:
    return Show.parse({"id": tvmaze_id, "name": name, "premiered": premiered, **extra})


def test_schema_is_applied_idempotently(tmp_path: Path) -> None:
    """Соединение открывается при каждом запуске сервера — схема обязана переживать это."""
    path = tmp_path / "twice.db"
    connect(path).close()
    conn = connect(path)
    tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master")}
    conn.close()

    assert {"shows", "shows_fts", "episodes", "episodes_fts", "tracking", "sync_state"} <= tables


def test_upsert_is_idempotent(repo: Repo) -> None:
    with transaction(repo.conn):
        repo.upsert_shows([show(67, "Severance")])
    with transaction(repo.conn):
        repo.upsert_shows([show(67, "Severance")])

    assert repo.count_shows() == 1


def test_upsert_updates_existing_row_by_tvmaze_id(repo: Repo) -> None:
    """У источника меняются и название, и статус: это обновление строки, а не новый сериал."""
    with transaction(repo.conn):
        repo.upsert_shows([show(67, "Severance", status="Running")])
    with transaction(repo.conn):
        repo.upsert_shows([show(67, "Severance", status="Ended")])

    row = repo.conn.execute("SELECT status FROM shows WHERE tvmaze_id = 67").fetchone()
    assert repo.count_shows() == 1
    assert row["status"] == "Ended"


def test_slug_collision_gets_suffix(repo: Repo) -> None:
    with transaction(repo.conn):
        repo.upsert_shows([show(1, "The Office", "2005-03-24")])
        repo.upsert_shows([show(2, "The Office", "2005-11-01")])

    slugs = {row["slug"] for row in repo.conn.execute("SELECT slug FROM shows")}
    assert slugs == {"the-office-2005", "the-office-2005-2"}


def test_slug_collision_inside_one_batch(repo: Repo) -> None:
    """Одноимённые сериалы приходят одной страницей каталога — в БД их ещё нет обоих."""
    with transaction(repo.conn):
        written = repo.upsert_shows(
            [
                show(1, "The Office", "2005-03-24"),
                show(2, "The Office", "2005-11-01"),
                show(3, "The Office", "2005-12-01"),
            ]
        )

    slugs = {row["slug"] for row in repo.conn.execute("SELECT slug FROM shows")}
    assert written == 3
    assert slugs == {"the-office-2005", "the-office-2005-2", "the-office-2005-3"}


def test_repeated_sync_keeps_the_same_slug(repo: Repo) -> None:
    """Суффикс не должен нарастать при каждом обходе — иначе ключ сериала «уползает»."""
    for _ in range(3):
        with transaction(repo.conn):
            repo.upsert_shows([show(1, "The Office", "2005-03-24")])

    assert repo.slug_for_tvmaze_id(1) == "the-office-2005"


def test_fts_index_follows_the_table(repo: Repo) -> None:
    with transaction(repo.conn):
        repo.upsert_shows([show(67, "Severance", summary="<p>Employees split their memories.</p>")])

    hits = repo.conn.execute(
        "SELECT rowid FROM shows_fts WHERE shows_fts MATCH ?", ("memories",)
    ).fetchall()
    assert len(hits) == 1

    with transaction(repo.conn):
        repo.conn.execute("DELETE FROM shows WHERE tvmaze_id = 67")

    stale = repo.conn.execute(
        "SELECT rowid FROM shows_fts WHERE shows_fts MATCH ?", ("memories",)
    ).fetchall()
    assert stale == []


def test_replace_episodes_removes_ghosts(repo: Repo) -> None:
    """Серии переносят между сезонами; слияние оставило бы в базе призраков."""
    with transaction(repo.conn):
        repo.upsert_shows([show(67, "Severance")])
    slug = "severance-2022"

    with transaction(repo.conn):
        repo.replace_episodes(
            slug,
            [
                Episode.parse({"season": 1, "number": 1, "name": "Good News"}),
                Episode.parse({"season": 1, "number": 2, "name": "Half Loop"}),
            ],
        )
    with transaction(repo.conn):
        repo.replace_episodes(slug, [Episode.parse({"season": 1, "number": 1, "name": "Renamed"})])

    rows = repo.conn.execute(
        "SELECT number, name FROM episodes WHERE show_slug = ? ORDER BY number", (slug,)
    ).fetchall()
    assert [(r["number"], r["name"]) for r in rows] == [(1, "Renamed")]


def test_specials_without_number_are_skipped(repo: Repo) -> None:
    """Спецвыпуск без номера не влезает в ключ (show, season, number) и в бэклоге не нужен."""
    with transaction(repo.conn):
        repo.upsert_shows([show(67, "Severance")])
    with transaction(repo.conn):
        stored = repo.replace_episodes(
            "severance-2022",
            [
                Episode.parse({"season": 1, "number": 1}),
                Episode.parse({"season": 1, "number": None, "type": "significant_special"}),
            ],
        )

    assert stored == 1


def test_deleting_show_cascades_to_episodes(repo: Repo) -> None:
    with transaction(repo.conn):
        repo.upsert_shows([show(67, "Severance")])
    with transaction(repo.conn):
        repo.replace_episodes("severance-2022", [Episode.parse({"season": 1, "number": 1})])
    with transaction(repo.conn):
        repo.conn.execute("DELETE FROM shows WHERE tvmaze_id = 67")

    assert repo.count_episodes("severance-2022") == 0


def test_transaction_rolls_back_on_error(repo: Repo) -> None:
    def failing_write() -> None:
        with transaction(repo.conn):
            repo.upsert_shows([show(67, "Severance")])
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        failing_write()

    assert repo.count_shows() == 0


def test_tracking_state_is_constrained(repo: Repo) -> None:
    """Состояние трекинга — закрытый список; опечатка должна падать в БД, а не в отчёте."""
    with transaction(repo.conn):
        repo.upsert_shows([show(67, "Severance")])

    with pytest.raises(sqlite3.IntegrityError):
        repo.conn.execute(
            "INSERT INTO tracking (show_slug, state, updated_at) VALUES (?, ?, ?)",
            ("severance-2022", "wathcing", "2026-09-08"),
        )


def test_sync_markers_round_trip(repo: Repo) -> None:
    """Отметки независимы: полный обход не выдаёт себя за инкремент и наоборот."""
    before = repo.last_full_sync
    with transaction(repo.conn):
        repo.mark_full_sync()
    after = repo.last_full_sync

    assert before is None
    assert after is not None
    assert repo.last_updates_sync is None

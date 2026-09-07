"""Ресурсы вызываются напрямую, без поднятого сервера.

Это и есть выигрыш от того, что сборка текста живёт в обычных функциях: граничные случаи
проверяются на БД в `tmp_path`, а не через протокол.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tvtracker.resources import (
    SCHEDULE_HORIZON_DAYS,
    render_backlog,
    render_facets,
    render_schedule,
    render_taste,
    render_watching,
)
from tvtracker.storage.repo import Repo, connect, transaction
from tvtracker.tvmaze.models import Episode, Show

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def at(delta_days: int) -> str:
    return (NOW + timedelta(days=delta_days)).isoformat()


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[Repo]:
    conn: sqlite3.Connection = connect(tmp_path / "resources.db")
    yield Repo(conn)
    conn.close()


def add_show(repo: Repo, **fields: object) -> None:
    with transaction(repo.conn):
        repo.upsert_shows([Show.parse({"premiered": "2022-01-01", **fields})])


def test_watching_says_so_when_nothing_is_tracked(repo: Repo) -> None:
    assert render_watching(repo) == "Пока ничего не отслеживается."


def test_watching_shows_position_and_paused_separately(repo: Repo) -> None:
    add_show(repo, id=1, name="Severance", premiered="2022-02-18")
    add_show(repo, id=2, name="Foundation", premiered="2021-09-24")
    with transaction(repo.conn):
        repo.upsert_tracking("severance-2022", state="watching", season=2, number=3, rating=9)
        repo.upsert_tracking("foundation-2021", state="paused", season=2, number=4)

    body = render_watching(repo)

    assert "Severance — 2x03" in body
    assert "моя оценка 9" in body
    assert "На паузе (1): Foundation (2x04)" in body


def test_backlog_is_explicit_about_an_empty_state(repo: Repo) -> None:
    assert render_backlog(repo, NOW) == "Бэклог пуст: отслеживаемых сериалов нет."


def test_finished_show_reports_zero_instead_of_disappearing(repo: Repo) -> None:
    """«Сериал завершён» — это ответ; молча пропасть из выдачи он не должен."""
    add_show(repo, id=1, name="Shogun", premiered="2024-02-27", status="Ended")
    with transaction(repo.conn):
        repo.replace_episodes(
            "shogun-2024", [Episode.parse({"season": 1, "number": 10, "airstamp": at(-100)})]
        )
        repo.upsert_tracking("shogun-2024", state="watching", season=1, number=10)

    body = render_backlog(repo, NOW)

    assert "Shogun — 0 серий, сериал завершён" in body


def test_backlog_counts_hours_and_ignores_specials(repo: Repo) -> None:
    add_show(repo, id=1, name="Severance", premiered="2022-02-18", averageRuntime=50)
    with transaction(repo.conn):
        repo.replace_episodes(
            "severance-2022",
            [
                Episode.parse({"season": 2, "number": 4, "airstamp": at(-3), "runtime": 50}),
                Episode.parse({"season": 2, "number": 5, "airstamp": at(-2), "runtime": 60}),
                Episode.parse(
                    {
                        "season": 2,
                        "number": 6,
                        "airstamp": at(-1),
                        "runtime": 20,
                        "type": "insignificant_special",
                    }
                ),
                Episode.parse({"season": 2, "number": 7, "airstamp": at(5), "runtime": 50}),
            ],
        )
        repo.upsert_tracking("severance-2022", state="watching", season=2, number=3)

    body = render_backlog(repo, NOW)

    assert "Severance — 2 сер." in body  # спешл и невышедшая не в долге
    assert "1 ч 50 мин" in body
    assert "Спецвыпуски не учитываются." in body


def test_schedule_lists_only_the_next_two_weeks(repo: Repo) -> None:
    add_show(repo, id=1, name="Slow Horses", premiered="2022-04-01")
    with transaction(repo.conn):
        repo.replace_episodes(
            "slow-horses-2022",
            [
                Episode.parse({"season": 4, "number": 7, "name": "Cold Water", "airstamp": at(6)}),
                Episode.parse({"season": 4, "number": 8, "airstamp": at(60)}),
                Episode.parse({"season": 4, "number": 6, "airstamp": at(-2)}),
            ],
        )
        repo.upsert_tracking("slow-horses-2022", state="watching", season=4, number=6)

    body = render_schedule(repo, NOW)

    assert "Slow Horses 4x07 «Cold Water»" in body
    assert "4x08" not in body  # за горизонтом
    assert "4x06" not in body  # уже вышла


def test_schedule_is_honest_when_nothing_airs(repo: Repo) -> None:
    body = render_schedule(repo, NOW)

    assert f"Ближайшие {SCHEDULE_HORIZON_DAYS} дней" in body
    assert "ничего из отслеживаемого не выходит" in body


def test_taste_admits_a_small_sample(repo: Repo) -> None:
    add_show(repo, id=1, name="Severance", premiered="2022-02-18", genres=["Drama"])
    with transaction(repo.conn):
        repo.upsert_tracking("severance-2022", state="finished", season=1, number=9, rating=9)

    body = render_taste(repo)

    assert "по 1 сериалам, из них 1 с оценкой" in body
    assert "Выборка мала" in body


def test_taste_is_empty_without_history(repo: Repo) -> None:
    assert "не по чему считать" in render_taste(repo)


def test_facets_tell_the_user_to_sync_an_empty_corpus(repo: Repo) -> None:
    """Пустой справочник должен объяснять, что делать, а не показывать пустоту."""
    assert "tvtracker sync --full" in render_facets(repo)


def test_facets_warn_about_genre_spelling(repo: Repo) -> None:
    add_show(repo, id=1, name="A", genres=["Science-Fiction"], status="Running", language="English")

    body = render_facets(repo)

    assert "Science-Fiction" in body
    assert '"Sci-Fi"' in body

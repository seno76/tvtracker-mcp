"""Фиксированный срез данных для прогона evals.

Замеры должны быть воспроизводимы: одни и те же цифры при каждом прогоне. Поэтому здесь
не настоящий корпус на 89 664 сериала, а срез, где каждый сериал отвечает за конкретный
сценарий, и один длинный — за проверку бюджета контекста.

Даты заданы относительно `NOW`, а не абсолютно: иначе через месяц «вышло 3 дня назад»
превратится в «вышло месяц назад», и отчёт начнёт врать.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tvtracker.storage.repo import Repo, connect, transaction
from tvtracker.tvmaze.models import Episode, Show

# Точка отсчёта прогона. Все `airstamp` считаются от неё, чтобы «свежесть» была стабильной.
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def days(delta: int) -> str:
    return (NOW + timedelta(days=delta)).isoformat()


SHOWS: list[dict[str, Any]] = [
    {
        "id": 67,
        "name": "Severance",
        "premiered": "2022-02-18",
        "status": "Running",
        "genres": ["Drama", "Science-Fiction", "Mystery"],
        "averageRuntime": 49,
        "rating": {"average": 7.6},
        "language": "English",
        "weight": 99,
        "webChannel": {"name": "Apple TV+"},
        "summary": "<p>Mark Scout leads a team at Lumon Industries, whose employees have "
        "undergone a severance procedure, which surgically divides their memories between "
        "their work and personal lives.</p>",
    },
    {
        "id": 44458,
        "name": "The Bear",
        "premiered": "2022-06-23",
        "status": "Running",
        "genres": ["Drama", "Comedy"],
        "averageRuntime": 30,
        "rating": {"average": 8.0},
        "language": "English",
        "weight": 95,
        "webChannel": {"name": "Hulu"},
        "summary": "<p>A young chef returns home to run his family sandwich shop after a "
        "death in the family.</p>",
    },
    {
        "id": 51022,
        "name": "Shogun",
        "premiered": "2024-02-27",
        "ended": "2024-04-23",
        "status": "Ended",
        "genres": ["Drama", "History", "War"],
        "averageRuntime": 60,
        "rating": {"average": 8.4},
        "language": "English",
        "weight": 97,
        "network": {"name": "FX"},
        "summary": "<p>A shipwrecked English sailor becomes a pawn in the struggle for power "
        "in feudal Japan.</p>",
    },
    {
        "id": 44963,
        "name": "Slow Horses",
        "premiered": "2022-04-01",
        "status": "Running",
        "genres": ["Drama", "Espionage", "Thriller"],
        "averageRuntime": 55,
        "rating": {"average": 8.1},
        "language": "English",
        "weight": 93,
        "webChannel": {"name": "Apple TV+"},
        "summary": "<p>A dysfunctional team of MI5 agents navigates the espionage world to "
        "defend England from sinister forces.</p>",
    },
    {
        "id": 3,
        "name": "The IT Crowd",
        "premiered": "2006-02-03",
        "ended": "2013-09-27",
        "status": "Ended",
        "genres": ["Comedy"],
        "averageRuntime": 30,
        "rating": {"average": 8.7},
        "language": "English",
        "weight": 90,
        "network": {"name": "Channel 4"},
        "summary": "<p>The comedic misadventures of Roy, Moss and their supervisor Jen, a "
        "rag-tag team of IT support workers at a large corporation.</p>",
    },
    {
        "id": 4,
        "name": "Arrested Development",
        "premiered": "2003-11-02",
        "ended": "2019-03-15",
        "status": "Ended",
        "genres": ["Comedy"],
        "averageRuntime": 22,
        "rating": {"average": 8.5},
        "language": "English",
        "weight": 88,
        "network": {"name": "FOX"},
        "summary": "<p>A wealthy family loses everything and the one sane son has no choice "
        "but to keep them together.</p>",
    },
    {
        "id": 5,
        "name": "Daily Chronicle",
        "premiered": "2018-01-01",
        "status": "Running",
        "genres": ["News"],
        "averageRuntime": 25,
        "rating": {"average": 6.1},
        "language": "English",
        "weight": 40,
        "network": {"name": "PBS"},
        "summary": "<p>A daily news programme covering world events, numbered by year.</p>",
    },
    {
        "id": 6,
        "name": "The Long Haul",
        "premiered": "1995-01-01",
        "status": "Running",
        "genres": ["Comedy", "Family"],
        "averageRuntime": 22,
        "rating": {"average": 7.2},
        "language": "English",
        "weight": 70,
        "network": {"name": "FOX"},
        "summary": "<p>A long-running animated comedy about a suburban family, still on the "
        "air after three decades.</p>",
    },
    {
        "id": 7,
        "name": "I Need Romance",
        "premiered": "2011-06-13",
        "ended": "2014-06-01",
        "status": "Ended",
        "genres": ["Drama", "Comedy", "Romance"],
        "averageRuntime": 60,
        "rating": {"average": 6.8},
        "language": "Korean",
        "weight": 50,
        "network": {"name": "tvN"},
        "summary": "<p>A drama about the lives of employees at a home shopping company and "
        "the love between friends in their 30s.</p>",
    },
]


def _severance_episodes() -> list[dict[str, Any]]:
    """Сезон 1 вышел, сезон 2 выходит; есть спешл и невышедшая серия."""
    season_one = [
        {
            "season": 1,
            "number": number,
            "name": name,
            "airstamp": days(-700 + number * 7),
            "runtime": runtime,
            "rating": {"average": rating},
            "summary": f"<p>{summary}</p>",
        }
        for number, name, runtime, rating, summary in [
            (1, "Good News About Hell", 57, 7.9, "Mark is offered a promotion."),
            (2, "Half Loop", 46, 7.7, "The team gets stuck in an elevator during a drill."),
            (3, "In Perpetuity", 45, 8.1, "A tour of the perpetuity wing."),
            (4, "The You You Are", 40, 8.0, "A wellness session goes sideways."),
            (5, "The Grim Barbarity of Optics", 44, 8.2, "A funeral outside the office."),
            (6, "Hide and Seek", 40, 8.3, "Secrets surface on the severed floor."),
            (7, "Defiant Jazz", 43, 8.4, "A music dance experience is granted."),
        ]
    ]
    # Описания разные по обе стороны позиции просмотра (2x03): иначе проверка
    # спойлер-фильтра не отличит «показал просмотренное» от «утекло непросмотренное».
    season_two = [
        {
            "season": 2,
            "number": number,
            "name": f"Season Two, Part {number}",
            "airstamp": days(offset),
            "runtime": 50,
            "rating": {"average": 7.5 + number * 0.05},
            "summary": f"<p>The severed floor deals with the {marker} in the corridor.</p>",
        }
        for number, offset, marker in [
            (1, -30, "aftermath"),
            (2, -23, "aftermath"),
            (3, -16, "aftermath"),
            (4, -9, "defection"),
            (5, -3, "defection"),
        ]
    ]
    special = {
        "season": 1,
        "number": 8,
        "name": "Behind the Severance",
        "type": "insignificant_special",
        "airstamp": days(-600),
        "runtime": 20,
        "summary": "<p>A making-of featurette.</p>",
    }
    upcoming = {
        "season": 2,
        "number": 6,
        "name": "The Next One",
        "airstamp": days(4),
        "runtime": 50,
        "summary": "<p>Not aired yet.</p>",
    }
    return [*season_one, special, *season_two, upcoming]


EPISODES: dict[str, list[dict[str, Any]]] = {
    "severance-2022": _severance_episodes(),
    "the-bear-2022": [
        {
            "season": 3,
            "number": number,
            "name": f"Course {number}",
            "airstamp": days(-40 + number),
            "runtime": 30,
            "rating": {"average": 7.8},
            "summary": "<p>Service continues in the kitchen.</p>",
        }
        for number in (8, 9, 10)
    ],
    "shogun-2024": [
        {
            "season": 1,
            "number": number,
            "name": f"Chapter {number}",
            "airstamp": days(-500 + number),
            "runtime": 60,
            "rating": {"average": 8.4},
            "summary": "<p>The struggle for power continues.</p>",
        }
        for number in (9, 10)
    ],
    "slow-horses-2022": [
        {
            "season": 4,
            "number": 7,
            "name": "Cold Water",
            "airstamp": days(6),
            "runtime": 55,
            "summary": "<p>Upcoming.</p>",
        }
    ],
    "daily-chronicle-2018": [
        {
            "season": 2026,
            "number": number,
            "name": f"Edition {number}",
            "airstamp": days(-5 + number - 176),
            "runtime": 25,
            "summary": "<p>Today's headlines.</p>",
        }
        for number in (176, 177, 178)
    ],
    # Проверка бюджета: 450 серий, которые в контекст не попадают никогда.
    "the-long-haul-1995": [
        {
            "season": season,
            "number": number,
            "name": f"Episode {number}",
            "airstamp": days(-3000 + season * 20 + number),
            "runtime": 22,
            "rating": {"average": 7.0 + (season % 5) * 0.1},
            "summary": "<p>Filler that must never reach the context in bulk.</p>",
        }
        for season in range(1, 31)
        for number in range(1, 16)
    ],
}

# Личное состояние: позиции и оценки, от которых считаются бэклог и профиль вкуса.
TRACKING: list[dict[str, Any]] = [
    {"slug": "severance-2022", "state": "watching", "season": 2, "number": 3, "rating": 9},
    {"slug": "the-bear-2022", "state": "watching", "season": 3, "number": 8, "rating": 7},
    {"slug": "shogun-2024", "state": "finished", "season": 1, "number": 10, "rating": 9},
    {"slug": "slow-horses-2022", "state": "watching", "season": 4, "number": 6, "rating": 8},
    {"slug": "i-need-romance-2011", "state": "dropped", "season": 1, "number": 4, "rating": 5},
    {"slug": "the-it-crowd-2006", "state": "finished", "season": 4, "number": 6, "rating": 9},
]


def build(db_path: Path) -> sqlite3.Connection:
    """Собирает базу для прогона с нуля. Повторный вызов даёт ту же самую базу."""
    db_path.unlink(missing_ok=True)
    conn = connect(db_path)
    repo = Repo(conn)

    with transaction(conn):
        repo.upsert_shows([Show.parse(row) for row in SHOWS])
    for slug, episodes in EPISODES.items():
        with transaction(conn):
            repo.replace_episodes(slug, [Episode.parse(row) for row in episodes])
    with transaction(conn):
        for record in TRACKING:
            repo.upsert_tracking(
                record["slug"],
                state=record["state"],
                season=record["season"],
                number=record["number"],
                rating=record["rating"],
            )
    return conn

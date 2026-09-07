"""Строки SQLite → структуры домена.

Отдельный модуль, а не хелпер внутри `tools/`: этими же преобразованиями пользуются
ресурсы и сервисы, а импорт из пакета инструментов затянул бы за собой регистрацию
всех пяти — и замкнул бы кольцо зависимостей.
"""

from __future__ import annotations

import json
import sqlite3

from tvtracker.domain.backlog import EpisodeRow
from tvtracker.domain.episodes import EpisodeRef, format_code
from tvtracker.domain.format import ShowLine
from tvtracker.storage.repo import Repo


def genres_of(row: sqlite3.Row) -> list[str]:
    try:
        return list(json.loads(row["genres_json"]))
    except (KeyError, TypeError, ValueError):
        return []


def position_of(row: sqlite3.Row) -> EpisodeRef | None:
    """Позиция просмотра из записи трекинга. `None` — сериал ещё не начат."""
    if row["season"] is None or row["number"] is None:
        return None
    return EpisodeRef(int(row["season"]), int(row["number"]))


def to_show_line(row: sqlite3.Row, tracking: sqlite3.Row | None = None) -> ShowLine:
    """Строка БД → то, что увидит модель. Здесь же приклеивается своё состояние."""
    state = str(tracking["state"]) if tracking is not None else None
    position = format_code(tracking["season"], tracking["number"]) if tracking is not None else None
    return ShowLine(
        slug=str(row["slug"]),
        name=str(row["name"]),
        premiered=row["premiered"],
        ended=row["ended"],
        status=row["status"],
        network=row["network"],
        genres=genres_of(row),
        avg_runtime=row["avg_runtime"],
        rating=row["rating"],
        summary=str(row["summary_clean"] or ""),
        tracking_state=state,
        tracking_position=position,
    )


def decorate(repo: Repo, rows: list[sqlite3.Row]) -> list[ShowLine]:
    """Помечает всю выдачу состоянием отслеживания одним запросом, а не N."""
    tracking = repo.tracking_for(str(row["slug"]) for row in rows)
    return [to_show_line(row, tracking.get(str(row["slug"]))) for row in rows]


def to_episode_rows(rows: list[sqlite3.Row]) -> list[EpisodeRow]:
    return [
        EpisodeRow(
            season=int(row["season"]),
            number=int(row["number"]),
            name=str(row["name"] or ""),
            airstamp=row["airstamp"],
            runtime=row["runtime"],
            rating=row["rating"],
            kind=str(row["kind"]),
        )
        for row in rows
    ]

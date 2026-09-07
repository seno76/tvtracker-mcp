"""Сервисы: доменные вычисления поверх репозитория.

Между чистым доменом (`domain/`) и слоем MCP есть тонкая прослойка — то, что читает БД
и собирает из неё доменные объекты. Она живёт здесь, а не в ресурсах: считать бэклог
нужно и ресурсу `tracking://backlog`, и инструменту `watch_next`, и класть общий код
в один из них означало бы, что второй импортирует первый.
"""

from __future__ import annotations

from typing import Any

from tvtracker.domain.backlog import ShowBacklog, build_show_backlog, sort_by_freshness
from tvtracker.mapping import position_of, to_episode_rows
from tvtracker.storage.repo import Repo
from tvtracker.utils import utcnow


def collect_backlogs(
    repo: Repo, states: tuple[str, ...] | None = ("watching",)
) -> tuple[list[ShowBacklog], dict[str, dict[str, Any]]]:
    """Считает долг по отслеживаемым сериалам.

    Возвращает ещё и метаданные (длительность, своя оценка, статус) — они нужны и ресурсу,
    и ранжированию в `watch_next`, а второй проход по БД ради них был бы лишним.
    """
    now = utcnow()
    backlogs: list[ShowBacklog] = []
    meta: dict[str, dict[str, Any]] = {}

    for row in repo.list_tracking(states):
        slug = str(row["show_slug"])
        backlogs.append(
            build_show_backlog(
                slug=slug,
                name=str(row["name"]),
                episodes=to_episode_rows(repo.episodes_for(slug)),
                position=position_of(row),
                now=now,
                avg_runtime=row["avg_runtime"],
            )
        )
        meta[slug] = {
            "avg_runtime": row["avg_runtime"],
            "rating": row["rating"],
            "state": str(row["state"]),
            "status": row["status"],
        }
    return sort_by_freshness(backlogs), meta

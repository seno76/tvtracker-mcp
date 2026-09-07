"""`show_profile` — «расскажи про сериал» за один вызов.

Агрегат четырёх эндпоинтов TVmaze плюс два вычисляемых блока, которых в API нет:
рейтинговый профиль по сезонам и позиция пользователя. Обёртка потратила бы здесь
четыре вызова и не дала бы ни того ни другого.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from tvtracker.domain.backlog import episode_minutes, unwatched
from tvtracker.domain.episodes import EpisodeRef, format_code
from tvtracker.domain.format import render_show_line
from tvtracker.domain.verdict import season_profile, trend
from tvtracker.log import get_logger, request_scope
from tvtracker.mapping import to_episode_rows, to_show_line
from tvtracker.tools._common import app
from tvtracker.utils import utcnow

logger = get_logger(__name__)


def register(server: MCPServer) -> None:
    @server.tool(
        title="Профиль сериала",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def show_profile(
        ctx: Context[Any],
        show: Annotated[str, Field(description='Slug или название, напр. "severance-2022".')],
        response_format: Literal["concise", "detailed"] = "concise",
    ) -> str:
        """Профиль сериала: факты, рейтинги по сезонам и твоя позиция в нём.

        Показывает, где сериал раскачивается и где проседает, — этого нет ни в одном
        клиенте TVmaze, хотя оценка каждой серии в API лежит.
        """
        context = app(ctx)
        repo = context.repo

        with request_scope("show_profile", show=show):
            row = repo.resolve_show(show)
            if row is None:
                raise ToolError(
                    f"Сериал «{show}» не найден. Попробуй show_search — он умеет искать "
                    "и по названию, и по описанию сюжета."
                )

            slug = str(row["slug"])
            tracking = repo.get_tracking(slug)
            limit = 400 if response_format == "detailed" else context.settings.summary_max_chars
            parts = [render_show_line(to_show_line(row, tracking), limit)]

            episodes = to_episode_rows(repo.episodes_for(slug))
            if not episodes:
                parts.append(
                    'Эпизоды не загружены. Они подтягиваются при tracking_update(action="track").'
                )
                return "\n\n".join(parts)

            profile = season_profile(episodes)
            if profile:
                seasons = " · ".join(f"с{s.season}: {s.average} ({s.episodes})" for s in profile)
                parts.append(f"Рейтинг по сезонам — {seasons}. Тренд: {trend(profile)}.")

            if tracking is not None:
                parts.append(_position_block(episodes, tracking, row))

            return "\n\n".join(parts)


def _position_block(episodes: list[Any], tracking: Any, show_row: Any) -> str:
    """Где пользователь остановился и сколько ему осталось до конца сезона."""
    position = None
    if tracking["season"] is not None and tracking["number"] is not None:
        position = EpisodeRef(int(tracking["season"]), int(tracking["number"]))

    pending = unwatched(episodes, position, utcnow())
    if not pending:
        return "Ты в курсе всего вышедшего."

    fallback = show_row["avg_runtime"]
    minutes = sum(episode_minutes(e, fallback) for e in pending)
    same_season = [e for e in pending if position is None or e.season == position.season]
    where = format_code(tracking["season"], tracking["number"]) or "начало"
    tail = (
        f" До конца сезона — {len(same_season)}."
        if same_season and len(same_season) != len(pending)
        else ""
    )
    return (
        f"Ты на {where}. Впереди {len(pending)} серий, ~{minutes // 60} ч {minutes % 60} мин.{tail}"
    )

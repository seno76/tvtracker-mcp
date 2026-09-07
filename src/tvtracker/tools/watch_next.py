"""`watch_next` — не поиск, а решение.

Тот вызов, ради которого затевается сервер: вопрос «что включить» закрывается **одним**
обращением, без предварительных поисков. Берёт бэклог, фильтрует по свободному времени,
ранжирует по своим оценкам и размеру долга, возвращает 2–3 варианта с обоснованием.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import Field

from tvtracker.domain.verdict import choose
from tvtracker.log import get_logger, request_scope
from tvtracker.runtime import app_from
from tvtracker.services import collect_backlogs

logger = get_logger(__name__)


def register(server: MCPServer) -> None:
    @server.tool(
        title="Что включить",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def watch_next(
        ctx: Context[Any],
        minutes: Annotated[
            int, Field(ge=5, le=600, description="Сколько свободного времени есть.")
        ] = 60,
        include_new: Annotated[
            bool, Field(description="Разрешить советовать сериалы, которые ещё не начаты.")
        ] = False,
        limit: Annotated[int, Field(ge=1, le=5)] = 3,
    ) -> str:
        """Выбрать, что посмотреть прямо сейчас, из накопившегося.

        Предлагает только вышедшие серии из бэклога и только те, что укладываются
        в заданное время.
        """
        repo = app_from(ctx).repo

        with request_scope("watch_next", minutes=minutes):
            states = None if include_new else ("watching",)
            backlogs, meta = collect_backlogs(repo, states)
            picks = choose(
                backlogs,
                minutes,
                {slug: info["avg_runtime"] for slug, info in meta.items()},
                {slug: info["rating"] for slug, info in meta.items()},
                limit,
            )
            logger.info("watch_next", extra={"candidates": len(backlogs), "picks": len(picks)})

            if not picks:
                return _nothing_fits(backlogs, minutes)

            lines = [
                f"{pick.name} {pick.episode.ref}"
                + (f" «{pick.episode.name}»" if pick.episode.name else "")
                + f" — {pick.reason}"
                for pick in picks
            ]
            return "Из накопившегося за " + f"{minutes} мин:\n" + "\n".join(lines)


def _nothing_fits(backlogs: list[Any], minutes: int) -> str:
    """Если ничего не подходит — одна альтернатива, а не список.

    Список из шести вариантов возвращает пользователя ровно в ту растерянность,
    из-за которой он и спросил.
    """
    pending = [b for b in backlogs if b.count]
    if not pending:
        return (
            "Бэклог пуст — всё вышедшее просмотрено. Можно начать что-то новое: "
            'опиши, чего хочется, и я поищу (show_search с by="description").'
        )
    shortest = min(pending, key=lambda b: b.episodes[0].runtime or 999)
    length = shortest.episodes[0].runtime
    return (
        f"За {minutes} мин ничего из бэклога не уложится. Самое короткое — "
        f"{shortest.name} {shortest.episodes[0].ref}" + (f", {length} мин." if length else ".")
    )

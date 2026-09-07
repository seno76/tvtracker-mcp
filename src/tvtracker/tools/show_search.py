"""`show_search` — один инструмент вместо трёх.

Поиск по названию, поиск по описанию и фильтрация каталога — это один сценарий «найди
сериал», а не три эндпоинта. Режим `by="auto"` сам решает, что ему дали: короткий запрос
похож на название, длинная фраза — на пересказ сюжета.

Поиск по описанию — главная функция сервера: в TVmaze такого запроса нет вообще.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import Field

from tvtracker.domain.format import no_results, render_results
from tvtracker.index.fts import (
    SearchFilters,
    browse,
    looks_like_description,
    nearest_titles,
    search_by_description,
    search_by_title,
)
from tvtracker.log import get_logger, request_scope
from tvtracker.mapping import decorate
from tvtracker.runtime import app_from

logger = get_logger(__name__)


def register(server: MCPServer) -> None:
    @server.tool(
        title="Найти сериал",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def show_search(
        ctx: Context[Any],
        query: Annotated[
            str,
            Field(description="Название или описание сюжета. Пусто — просмотр по фильтрам."),
        ] = "",
        by: Annotated[
            Literal["auto", "title", "description"],
            Field(description='"auto" сам решает; "description" ищет по тексту описаний.'),
        ] = "auto",
        genre: Annotated[
            str | None, Field(description='Точное написание из catalog://facets, напр. "Drama".')
        ] = None,
        status: str | None = None,
        language: str | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        min_rating: Annotated[float | None, Field(ge=0, le=10)] = None,
        limit: Annotated[int, Field(ge=1, le=25)] = 8,
        response_format: Literal["concise", "detailed"] = "concise",
    ) -> str:
        """Найти сериал по названию, по описанию сюжета или отобрать по фильтрам.

        Три режима в одном инструменте:
        - название: `query="Severance"`;
        - описание сюжета: `query="офис, где сотрудникам разделяют память"` — так TVmaze
          искать не умеет вообще;
        - только фильтры, **без query**: `genre="Comedy", status="Ended", min_rating=8`
          для «комедии, которые уже закончились».

        Точное написание значений фильтров — в ресурсе `catalog://facets`
        (`"Science-Fiction"`, не `"Sci-Fi"`). Каждый результат помечен твоим состоянием
        отслеживания.
        """
        context = app_from(ctx)
        repo = context.repo
        filters = SearchFilters(genre, status, language, year_from, year_to, min_rating)
        summary_limit = 400 if response_format == "detailed" else context.settings.summary_max_chars

        with request_scope("show_search", by=by, limit=limit):
            if not query.strip():
                rows, total = browse(repo.conn, filters, limit)
                if not rows:
                    return (
                        "По этим фильтрам ничего нет. Проверь написание значений "
                        "в справочнике catalog://facets."
                    )
                return render_results(
                    decorate(repo, rows),
                    total,
                    "фильтры",
                    _hints(filters),
                    summary_limit,
                    exact=True,
                )

            mode = by
            if mode == "auto":
                mode = "description" if looks_like_description(query) else "title"

            if mode == "title":
                rows, total = search_by_title(repo.conn, query, filters, limit)
                # Название не нашлось, но запрос может оказаться описанием — пробуем корпус,
                # прежде чем сказать «ничего». Это экономит модели целый лишний вызов.
                if not rows and by == "auto":
                    rows, total = search_by_description(repo.conn, query, filters, limit)
                    mode = "description"
            else:
                rows, total = search_by_description(repo.conn, query, filters, limit)

            logger.info("search done", extra={"mode": mode, "hits": len(rows), "total": total})

            if not rows:
                return no_results(query, nearest_titles(repo.conn, query))

            # При поиске по описанию слова объединяются через OR, поэтому «совпадений»
            # у корпуса десятки тысяч. Показывать это число бессмысленно: оно говорит
            # не о числе подходящих сериалов, а о частотности служебных слов.
            return render_results(
                decorate(repo, rows),
                total,
                query,
                _hints(filters),
                summary_limit,
                exact=mode == "title",
            )


def _hints(filters: SearchFilters) -> list[str]:
    """Подсказка по сужению — только те фильтры, которые ещё не заданы."""
    available = []
    if not filters.genre:
        available.append('genre="Science-Fiction"')
    if filters.year_from is None:
        available.append("year_from=2020")
    if filters.min_rating is None:
        available.append("min_rating=7.5")
    return available[:2]

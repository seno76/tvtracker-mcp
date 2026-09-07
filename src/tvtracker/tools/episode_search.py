"""`episode_search` — поиск серии по описанию, с защитой от спойлеров.

«Серия, где они застряли в лифте» — запрос, на который TVmaze не отвечает вообще.
Работает по кэшу эпизодов, то есть по отслеживаемым сериалам.

Защита от спойлеров — то, чего у обёртки не может быть в принципе: она требует знания
прогресса. По умолчанию описания серий **после** текущей позиции не показываются,
только факт наличия.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from tvtracker.domain.episodes import EpisodeRef
from tvtracker.domain.format import truncate
from tvtracker.index.fts import escape_fts
from tvtracker.log import get_logger, request_scope
from tvtracker.runtime import app_from
from tvtracker.storage.sync import sync_show_episodes

logger = get_logger(__name__)


def register(server: MCPServer) -> None:
    @server.tool(
        title="Найти серию",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    )
    async def episode_search(
        ctx: Context[Any],
        query: Annotated[str, Field(description="Что происходило в серии.")],
        show: Annotated[str | None, Field(description="Сузить до одного сериала.")] = None,
        season: int | None = None,
        include_specials: bool = False,
        include_unwatched: Annotated[
            bool, Field(description="Показывать описания непросмотренных серий (спойлеры).")
        ] = False,
        limit: Annotated[int, Field(ge=1, le=15)] = 5,
    ) -> str:
        """Найти серию по описанию среди отслеживаемых сериалов.

        Описания серий, до которых ты ещё не дошёл, по умолчанию скрыты — показывается
        только факт, что такая серия есть.
        """
        context = app_from(ctx)
        repo = context.repo

        with request_scope("episode_search", scoped=show is not None):
            slug = None
            if show is not None:
                row = repo.resolve_show(show)
                if row is None:
                    raise ToolError(f"Сериал «{show}» не найден.")
                slug = str(row["slug"])
                if repo.count_episodes(slug) == 0:
                    await _load(context, repo, row)

            match = escape_fts(query)
            if not match:
                raise ToolError("В запросе нет слов для поиска. Опиши, что происходило в серии.")

            rows = _search(repo, match, slug, season, include_specials, limit)
            if not rows:
                return _empty(repo, slug)

            positions = _positions(repo)
            lines = [_render(row, positions, include_unwatched) for row in rows]
            return "\n".join(lines)


def _search(
    repo: Any,
    match: str,
    slug: str | None,
    season: int | None,
    include_specials: bool,
    limit: int,
) -> list[Any]:
    clauses = ["episodes_fts MATCH ?"]
    params: list[object] = [match]
    if slug:
        clauses.append("e.show_slug = ?")
        params.append(slug)
    if season is not None:
        clauses.append("e.season = ?")
        params.append(season)
    if not include_specials:
        clauses.append("e.kind = 'regular'")

    sql = f"""
        SELECT e.*, s.name AS show_name
        FROM episodes_fts
        JOIN episodes AS e ON e.rowid = episodes_fts.rowid
        JOIN shows AS s ON s.slug = e.show_slug
        WHERE {" AND ".join(clauses)}
        ORDER BY bm25(episodes_fts, 4.0, 1.0)
        LIMIT ?
    """  # noqa: S608
    rows: list[Any] = repo.conn.execute(sql, [*params, limit]).fetchall()
    return rows


def _positions(repo: Any) -> dict[str, EpisodeRef]:
    """Текущая позиция по каждому отслеживаемому сериалу — основа спойлер-фильтра."""
    positions: dict[str, EpisodeRef] = {}
    for row in repo.list_tracking():
        if row["season"] is not None and row["number"] is not None:
            positions[str(row["show_slug"])] = EpisodeRef(int(row["season"]), int(row["number"]))
    return positions


def _render(row: Any, positions: dict[str, EpisodeRef], include_unwatched: bool) -> str:
    ref = EpisodeRef(int(row["season"]), int(row["number"]))
    head = f"{row['show_name']} {ref} «{row['name']}»"

    position = positions.get(str(row["show_slug"]))
    watched = position is not None and ref <= position
    if watched or include_unwatched:
        summary = truncate(str(row["summary_clean"] or ""), 160)
        return f"{head} — {summary}" if summary else head
    return f"{head} — ты до неё ещё не дошёл, описание скрыто (include_unwatched=true покажет)."


def _empty(repo: Any, slug: str | None) -> str:
    if slug is None and not repo.list_tracking():
        return (
            "Поиск по сериям работает по отслеживаемым сериалам, а их пока нет. "
            'Добавь сериал: tracking_update(show="…", action="track").'
        )
    return (
        "Ничего не нашлось. Поиск идёт по тексту описаний серий — попробуй другие слова, "
        "ближе к тому, как это могли записать в аннотации."
    )


async def _load(context: Any, repo: Any, row: Any) -> None:
    stored = await sync_show_episodes(context.client, repo, int(row["tvmaze_id"]))
    logger.info("episodes loaded on demand", extra={"show": row["slug"], "episodes": stored})

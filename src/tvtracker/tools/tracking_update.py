"""`tracking_update` — все изменения состояния в одном инструменте.

Шесть отдельных инструментов (`track`, `untrack`, `progress`, `rate`, `pause`, `finish`)
заняли бы шесть слотов в списке и заставили бы модель каждый раз выбирать между ними.
Один инструмент с enum-действием решает ту же задачу и оставляет список коротким.

Ответ всегда подтверждает изменение и сразу даёт следующий шаг — чтобы «отметил серию»
не превращалось во второй вызов «а что дальше».
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from tvtracker.domain.backlog import EpisodeRow, unwatched
from tvtracker.domain.episodes import EpisodeCodeError, EpisodeRef, parse_code
from tvtracker.errors import SourceUnavailableError
from tvtracker.log import get_logger, request_scope
from tvtracker.mapping import to_episode_rows
from tvtracker.storage.repo import transaction
from tvtracker.storage.sync import sync_show_episodes
from tvtracker.tools._common import app
from tvtracker.utils import utcnow

logger = get_logger(__name__)

Action = Literal["track", "untrack", "progress", "rate", "pause", "finish"]

# Действие → состояние трекинга. `progress` и `rate` состояние не меняют:
# отметить серию в сериале на паузе не значит снять его с паузы.
_STATE_FOR: dict[str, str | None] = {
    "track": "watching",
    "progress": None,
    "rate": None,
    "pause": "paused",
    "finish": "finished",
}


def register(server: MCPServer) -> None:
    @server.tool(
        title="Отметить просмотр",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=True
        ),
    )
    async def tracking_update(
        ctx: Context[Any],
        show: Annotated[str, Field(description="Slug или название сериала.")],
        action: Annotated[Action, Field(description="Что сделать с записью просмотра.")],
        episode: Annotated[str | None, Field(description='Номер серии в формате "2x03".')] = None,
        rating: Annotated[int | None, Field(ge=1, le=10)] = None,
        note: str | None = None,
    ) -> str:
        """Изменить состояние просмотра: подписаться, отметить серию, оценить, бросить.

        При `action="track"` эпизоды сериала загружаются и индексируются — после этого
        по ним работает поиск и считается бэклог.
        """
        context = app(ctx)
        repo = context.repo

        with request_scope("tracking_update", action=action):
            row = repo.resolve_show(show)
            if row is None:
                raise ToolError(
                    f"Сериал «{show}» не найден. Найди его через show_search и передай "
                    "сюда slug из выдачи."
                )
            slug = str(row["slug"])

            position = _parse_position(episode)
            if action == "untrack":
                return _untrack(repo, slug, str(row["name"]))
            if action == "rate" and rating is None:
                raise ToolError('Для action="rate" нужен параметр rating от 1 до 10.')

            source_ok = True
            if action == "track":
                source_ok = await _load_episodes(context, repo, row)

            with transaction(repo.conn):
                repo.upsert_tracking(
                    slug,
                    state=_state_for(repo, slug, action),
                    season=position.season if position else None,
                    number=position.number if position else None,
                    rating=rating,
                    note=note,
                )

            # Ресурсы считаются из этой же таблицы, поэтому хост должен их перечитать.
            await ctx.notify_resource_updated("tracking://watching")
            await ctx.notify_resource_updated("tracking://backlog")
            await ctx.notify_resource_updated("tracking://taste")

            return _confirmation(repo, row, action, position, rating, source_ok)


def _parse_position(episode: str | None) -> EpisodeRef | None:
    """Разбирает `2x03`, превращая ошибку формата в подсказку для следующего вызова."""
    if episode is None:
        return None
    try:
        return parse_code(episode)
    except EpisodeCodeError as exc:
        raise ToolError(str(exc)) from exc


def _state_for(repo: Any, slug: str, action: str) -> str:
    """Новое состояние. Для `progress`/`rate` — прежнее, а если записи не было — `watching`."""
    new_state = _STATE_FOR[action]
    if new_state is not None:
        return new_state
    existing = repo.get_tracking(slug)
    return str(existing["state"]) if existing else "watching"


def _untrack(repo: Any, slug: str, name: str) -> str:
    with transaction(repo.conn):
        removed = repo.delete_tracking(slug)
    if not removed:
        return f"{name} и так не отслеживается."
    return f"{name} снят с отслеживания. Эпизоды остались в кэше."


async def _load_episodes(context: Any, repo: Any, row: Any) -> bool:
    """Ленивая загрузка эпизодов — just-in-time retrieval в момент подписки.

    Отказ источника не должен ронять саму подписку: трекинг локальный и обязан работать,
    даже когда TVmaze лежит. Поэтому исключение гасится — но наружу уходит признак того,
    что загрузки не было: сказать «эпизоды подгружаются», когда источник отказал, значит
    соврать модели, и она будет ждать данных, которые не придут.
    """
    if repo.count_episodes(str(row["slug"])) > 0:
        return True
    try:
        stored = await sync_show_episodes(context.client, repo, int(row["tvmaze_id"]))
        logger.info("episodes loaded", extra={"show": row["slug"], "episodes": stored})
    except SourceUnavailableError as exc:
        logger.warning("episode load deferred", extra={"show": row["slug"], "why": str(exc)})
        return False
    return True


def _confirmation(
    repo: Any,
    row: Any,
    action: str,
    position: EpisodeRef | None,
    rating: int | None,
    source_ok: bool = True,
) -> str:
    """Подтверждение плюс следующий шаг: чем закончить, чтобы не спрашивали второй раз."""
    name = str(row["name"])
    slug = str(row["slug"])

    if action == "rate":
        return f"Оценка сохранена: {name} — {rating}."
    if action == "pause":
        return f"{name} на паузе. Вернуться можно в любой момент — позиция сохранена."
    if action == "finish":
        return f"{name} отмечен как досмотренный."

    head = f"Отмечено: {name}" + (f" {position}." if position else ".")
    episodes = to_episode_rows(repo.episodes_for(slug))
    if not episodes:
        if not source_ok:
            return (
                head + " Список серий подтянуть не удалось: источник временно ограничил "
                "доступ. Сама подписка сохранена, повтори через ~10 секунд — "
                'tracking_update(action="track") догрузит серии.'
            )
        return head + " Эпизоды ещё подгружаются — бэклог обновится через несколько секунд."

    ahead = unwatched(episodes, position, utcnow())
    if not ahead:
        return head + " Всё вышедшее просмотрено."
    return head + f" Дальше — {_describe(ahead[0], row['avg_runtime'])}."


def _describe(episode: EpisodeRow, fallback: int | None) -> str:
    bits = [str(episode.ref)]
    if episode.name:
        bits.append(f"«{episode.name}»")
    minutes = episode.runtime or fallback
    if minutes:
        bits.append(f"{minutes} мин")
    return ", ".join(bits) + ", вышла"

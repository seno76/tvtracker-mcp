"""Двенадцать сценариев.

Каждый описывает **план вызовов**, которым хорошо ведущий себя агент закрывает вопрос
пользователя, и проверку того, что результат достигнут. Прогон считает цену этого плана.

Правило, из-за которого сценарии выглядят именно так: контекст, который хост подставляет
ресурсами, **не считается вызовом инструмента**. Поэтому «что посмотреть» стоит одного
вызова, а не трёх, — история уже в контексте.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import Client

from evals import corpus
from evals.runner import Measurement, Recorder, estimate_tokens
from tvtracker.config import Settings
from tvtracker.errors import SourceUnavailableError

DB_PATH = Path("evals/reports/evals.db")

# Ресурсы, которые хост подставляет в контекст до начала рассуждения. Их чтение
# засчитывается отдельной колонкой: это фиксированная цена, а не действие модели.
PRELOADED = ("tracking://watching", "tracking://backlog", "tracking://taste")


class DeadSource:
    """Клиент TVmaze, который всегда отвечает отказом. Для сценария деградации."""

    async def get_show_with_episodes(self, tvmaze_id: int) -> dict[str, Any]:
        raise SourceUnavailableError

    async def aclose(self) -> None:
        return None


@asynccontextmanager
async def session(dead_source: bool = False) -> AsyncIterator[Recorder]:
    """Поднимает сервер на фиксированном срезе и отдаёт считающий обёртку клиент."""
    from tvtracker.server import build_test_server

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = corpus.build(DB_PATH)
    settings = Settings(db_path=DB_PATH, log_level="WARNING", _env_file=None)  # type: ignore[call-arg]
    server = build_test_server(conn, settings)

    async with Client(server, raise_exceptions=True) as client:
        if dead_source:
            from tvtracker.runtime import current_app

            current_app().client = DeadSource()
        yield Recorder(client)
    conn.close()


async def preload(rec: Recorder) -> str:
    """Читает ресурсы, которые хост подставляет сам."""
    parts = [await rec.resource(uri) for uri in PRELOADED]
    return "\n".join(parts)


# --- Сценарии -----------------------------------------------------------------------------


async def scenario_01(rec: Recorder) -> tuple[bool, str]:
    """Сериал, где сотрудникам разделяют память."""
    body = await rec.tool(
        "show_search",
        {
            "query": "employees have their memories surgically divided between work and home",
            "by": "description",
            "limit": 3,
        },
    )
    top_three = body.split("\n\n")[:3]
    return any("Severance" in block for block in top_three), ""


async def scenario_02(rec: Recorder) -> tuple[bool, str]:
    """Что посмотреть, свободен час."""
    context = await preload(rec)
    body = await rec.tool("watch_next", {"minutes": 60})
    got_history_for_free = "Severance" in context
    return (got_history_for_free and "Severance" in body), (
        "" if got_history_for_free else "история не пришла ресурсами"
    )


async def scenario_03(rec: Recorder) -> tuple[bool, str]:
    """Что вышло, пока меня не было две недели."""
    backlog = await rec.resource("tracking://backlog")
    schedule = await rec.resource("tracking://schedule")
    return ("Severance" in backlog and "Slow Horses" in schedule), ""


async def scenario_04(rec: Recorder) -> tuple[bool, str]:
    """Отметь, что досмотрел пятую серию второго сезона Severance."""
    body = await rec.tool(
        "tracking_update", {"show": "Severance", "action": "progress", "episode": "2x05"}
    )
    return ("Отмечено" in body and "2x05" in body), ""


async def scenario_05(rec: Recorder) -> tuple[bool, str]:
    """Серия, где они застряли в лифте."""
    body = await rec.tool("episode_search", {"query": "stuck in an elevator"})
    return "Half Loop" in body, ""


async def scenario_06(rec: Recorder) -> tuple[bool, str]:
    """Стоит ли продолжать смотреть Severance."""
    await preload(rec)
    body = await rec.tool("show_profile", {"show": "severance-2022"})
    return ("Рейтинг по сезонам" in body and "Ты на 2x03" in body), ""


async def scenario_07(rec: Recorder) -> tuple[bool, str]:
    """Найди что-то похожее на то, что я оцениваю выше восьми."""
    taste = await rec.resource("tracking://taste")
    body = await rec.tool("show_search", {"genre": "Drama", "min_rating": 8.0, "limit": 5})
    return ("Люблю" in taste and "Shogun" in body), ""


async def scenario_08(rec: Recorder) -> tuple[bool, str]:
    """Комедии до 30 минут, которые уже закончились."""
    body = await rec.tool(
        "show_search", {"genre": "Comedy", "status": "Ended", "min_rating": 8.0, "limit": 5}
    )
    return ("The IT Crowd" in body and "I Need Romance" not in body), ""


async def scenario_09(rec: Recorder) -> tuple[bool, str]:
    """Несуществующий сериал: ошибка должна вести к верной второй попытке."""
    first = await rec.tool("show_search", {"query": "Zzqqwx", "by": "title"})
    if 'by="description"' not in first:
        return False, "ошибка не подсказывает, что делать дальше"
    second = await rec.tool(
        "show_search", {"query": "severed memories at the office", "by": "description", "limit": 3}
    )
    return "Severance" in second, ""


async def scenario_10(rec: Recorder) -> tuple[bool, str]:
    """Запрос во время отказа источника: подписка обязана пройти локально.

    Эпизодов у сериала в кэше нет, источник мёртв — но трекинг локальный, и подписка
    не имеет права падать вместе с TVmaze.
    """
    body = await rec.tool("tracking_update", {"show": "Arrested Development", "action": "track"})
    degraded = "источник временно ограничил доступ" in body
    return ("Отмечено" in body and degraded), (
        "" if degraded else "отказ источника не объяснён пользователю"
    )


async def scenario_11(rec: Recorder) -> tuple[bool, str]:
    """Спойлерный запрос по непросмотренной серии.

    Проверяется именно граница: пользователь на 2x03, значит описание 2x01–2x03 показать
    можно и нужно, а описание 2x04–2x05 — нельзя. Слово `defection` встречается только
    в непросмотренных, `aftermath` — только в просмотренных.
    """
    body = await rec.tool(
        "episode_search", {"query": "severed floor corridor", "show": "severance-2022"}
    )
    leaked = "defection" in body.lower()
    shows_watched = "aftermath" in body.lower()
    hidden = "описание скрыто" in body
    if leaked:
        return False, "описание непросмотренной серии утекло в ответ"
    if not shows_watched:
        return False, "фильтр скрыл и просмотренное тоже — это уже не защита, а потеря данных"
    return hidden, "" if hidden else "непросмотренные серии не помечены как скрытые"


async def scenario_12(rec: Recorder) -> tuple[bool, str]:
    """Сериал с 450 сериями: контекст не должен взорваться."""
    body = await rec.tool("show_profile", {"show": "The Long Haul"})
    return ("Filler" not in body and estimate_tokens(body) <= 2000), ""


SCENARIOS = {
    1: scenario_01,
    2: scenario_02,
    3: scenario_03,
    4: scenario_04,
    5: scenario_05,
    6: scenario_06,
    7: scenario_07,
    8: scenario_08,
    9: scenario_09,
    10: scenario_10,
    11: scenario_11,
    12: scenario_12,
}

# Сценарий 10 проверяет деградацию, поэтому источник для него мёртв.
DEAD_SOURCE = {10}


async def run_one(scenario_id: int, entry: dict[str, Any]) -> Measurement:
    async with session(dead_source=scenario_id in DEAD_SOURCE) as rec:
        passed, note = await SCENARIOS[scenario_id](rec)

    return Measurement(
        id=scenario_id,
        ask=str(entry["ask"]),
        tool_calls=rec.tool_calls,
        resource_reads=rec.resource_reads,
        chars=rec.chars,
        tokens=estimate_tokens("x" * rec.chars),
        passed=passed,
        max_calls=int(entry["max_calls"]),
        note=note,
        calls=rec.calls,
    )

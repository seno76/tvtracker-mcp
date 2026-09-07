"""Контракт MCP-примитивов через in-memory клиент SDK.

`Client(mcp)` подключается к объекту сервера напрямую — без подпроцесса, порта и сети.
`raise_exceptions=True` показывает настоящую ошибку вместо санитизированной
(https://py.sdk.modelcontextprotocol.io/get-started/testing/).
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from tests.conftest import make_settings
from tvtracker.storage.repo import Repo, connect, transaction
from tvtracker.tvmaze.models import Episode, Show

pytestmark = [pytest.mark.contract, pytest.mark.anyio]

SEVERANCE = {
    "id": 67,
    "name": "Severance",
    "premiered": "2022-02-18",
    "genres": ["Drama", "Science-Fiction", "Mystery"],
    "averageRuntime": 49,
    "rating": {"average": 7.6},
    "status": "Running",
    "language": "English",
    "webChannel": {"name": "Apple TV+"},
    "summary": "<p>Employees have their memories surgically split between work and home.</p>",
    "image": {"medium": "https://example/x.jpg"},
}

BEAR = {
    "id": 44458,
    "name": "The Bear",
    "premiered": "2022-06-23",
    "genres": ["Drama", "Comedy"],
    "averageRuntime": 30,
    "rating": {"average": 8.0},
    "status": "Running",
    "language": "English",
    "webChannel": {"name": "Hulu"},
    "summary": "<p>A young chef returns home to run his family sandwich shop.</p>",
}

EPISODES = [
    {
        "season": 1,
        "number": 1,
        "name": "Good News",
        "runtime": 57,
        "airstamp": "2022-02-18T05:00:00+00:00",
        "rating": {"average": 7.9},
        "summary": "<p>Mark is offered a promotion after a colleague vanishes.</p>",
    },
    {
        "season": 1,
        "number": 2,
        "name": "Half Loop",
        "runtime": 46,
        "airstamp": "2022-02-18T05:00:00+00:00",
        "rating": {"average": 7.7},
        "summary": "<p>The team gets stuck in an elevator during a drill.</p>",
    },
    {
        "season": 1,
        "number": 3,
        "name": "In Perpetuity",
        "runtime": 45,
        "airstamp": "2022-02-25T05:00:00+00:00",
        "rating": {"average": 8.1},
        "summary": "<p>A tour of the perpetuity wing reveals the history of the founder.</p>",
    },
    {
        "season": 1,
        "number": 4,
        "name": "Christmas Special",
        "runtime": 20,
        "airstamp": "2022-03-04T05:00:00+00:00",
        "type": "significant_special",
        "summary": "<p>A holiday detour.</p>",
    },
    {
        "season": 2,
        "number": 1,
        "name": "Hello, Ms. Cobel",
        "runtime": 60,
        "airstamp": "2099-01-01T05:00:00+00:00",
        "rating": {"average": 8.4},
        "summary": "<p>Not aired yet.</p>",
    },
]


@pytest.fixture
def db(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "contract.db")
    repo = Repo(conn)
    with transaction(conn):
        repo.upsert_shows([Show.parse(SEVERANCE), Show.parse(BEAR)])
    with transaction(conn):
        repo.replace_episodes("severance-2022", [Episode.parse(e) for e in EPISODES])
    return conn


@pytest.fixture
async def client(db: sqlite3.Connection, tmp_path: Path) -> AsyncIterator[Client]:
    from tvtracker.server import build_test_server

    settings = make_settings(db_path=tmp_path / "contract.db")
    async with Client(build_test_server(db, settings), raise_exceptions=True) as connected:
        yield connected


def text_of(result: Any) -> str:
    return "\n".join(block.text for block in result.content if block.type == "text")


def resource_text(result: Any) -> str:
    return str(result.contents[0].text)


def prompt_text(result: Any) -> str:
    return str(result.messages[0].content.text)


# --- Объявление примитивов ---------------------------------------------------------------


async def test_five_tools_are_exposed(client: Client) -> None:
    """Ориентир отрасли — 1–10 активных инструментов; консолидация держит это число."""
    tools = await client.list_tools()

    assert {t.name for t in tools.tools} == {
        "show_search",
        "show_profile",
        "episode_search",
        "tracking_update",
        "watch_next",
    }


async def test_read_only_tools_are_annotated(client: Client) -> None:
    """Клиент решает по аннотациям, спрашивать ли пользователя перед вызовом."""
    tools = {t.name: t for t in (await client.list_tools()).tools}

    read_only = tools["show_search"].annotations
    mutating = tools["tracking_update"].annotations
    assert read_only is not None
    assert mutating is not None

    assert read_only.read_only_hint is True
    assert mutating.read_only_hint is False


async def test_five_resources_are_exposed_as_plain_text(client: Client) -> None:
    """`text/plain`, а не JSON: скобки и повторяющиеся имена полей стоят лишних токенов."""
    resources = (await client.list_resources()).resources

    assert {str(r.uri) for r in resources} == {
        "tracking://watching",
        "tracking://backlog",
        "tracking://schedule",
        "tracking://taste",
        "catalog://facets",
    }
    assert all(r.mime_type == "text/plain" for r in resources)


async def test_four_prompts_are_exposed(client: Client) -> None:
    prompts = (await client.list_prompts()).prompts

    assert {p.name for p in prompts} == {
        "whats_next",
        "catch_up",
        "find_by_vibe",
        "season_verdict",
    }


# --- Поиск --------------------------------------------------------------------------------


async def test_search_by_title_returns_text_not_json(client: Client) -> None:
    result = await client.call_tool("show_search", {"query": "Severance"})

    body = text_of(result)
    assert "Severance (2022–, Apple TV+, идёт)" in body
    assert "Drama/Science-Fiction/Mystery" in body
    assert "49 мин" in body
    assert "{" not in body
    assert "image" not in body


async def test_search_by_description_finds_show_without_its_title(client: Client) -> None:
    """Главная функция сервера: в TVmaze поиска по тексту описаний нет вообще."""
    result = await client.call_tool(
        "show_search", {"query": "employees memories surgically split", "by": "description"}
    )

    assert "Severance" in text_of(result)


async def test_auto_mode_picks_description_for_a_phrase(client: Client) -> None:
    result = await client.call_tool("show_search", {"query": "young chef returns home to run shop"})

    assert "The Bear" in text_of(result)


async def test_fts_special_characters_do_not_break_the_query(client: Client) -> None:
    """Ввод пользователя экранируется: спецсинтаксис FTS5 не выполняется и не роняет запрос."""
    result = await client.call_tool(
        "show_search", {"query": 'memories" OR bear* NEAR', "by": "description"}
    )

    assert result.is_error is False


async def test_empty_result_explains_the_next_move(client: Client) -> None:
    """Ошибка чинит следующий вызов, а не констатирует неудачу."""
    result = await client.call_tool("show_search", {"query": "zzzzqqqq"})

    body = text_of(result)
    assert "ничего не найдено" in body
    assert 'by="description"' in body


# --- Трекинг ------------------------------------------------------------------------------


async def test_tracking_confirms_and_gives_the_next_step(client: Client) -> None:
    """Ответ сразу даёт следующий шаг — иначе «отметил серию» стоит второго вызова."""
    await client.call_tool("tracking_update", {"show": "severance-2022", "action": "track"})
    result = await client.call_tool(
        "tracking_update",
        {"show": "severance-2022", "action": "progress", "episode": "1x02"},
    )

    body = text_of(result)
    assert "Отмечено: Severance 1x02." in body
    assert "1x03" in body
    assert "In Perpetuity" in body


async def test_bad_episode_code_says_what_to_send_instead(client: Client) -> None:
    result = await client.call_tool(
        "tracking_update",
        {"show": "severance-2022", "action": "progress", "episode": "S02E03"},
    )

    assert result.is_error is True
    assert 'Ожидаю "2x03"' in text_of(result)
    assert "S02E03" in text_of(result)


async def test_unknown_show_is_a_tool_error_with_a_hint(client: Client) -> None:
    result = await client.call_tool("tracking_update", {"show": "nope-9999", "action": "track"})

    assert result.is_error is True
    assert "show_search" in text_of(result)


async def test_search_marks_tracked_shows(client: Client) -> None:
    """Собственное состояние в выдаче — то, чего в TVmaze нет ни при каком запросе."""
    await client.call_tool(
        "tracking_update", {"show": "severance-2022", "action": "progress", "episode": "1x02"}
    )
    result = await client.call_tool("show_search", {"query": "Severance"})

    assert "→ отслеживаю, остановился на 1x02" in text_of(result)


# --- Ресурсы ------------------------------------------------------------------------------


async def test_backlog_counts_only_aired_regular_episodes(client: Client) -> None:
    """Спешл не в долге, невышедшая серия не в долге — обе проверки в одном ответе."""
    await client.call_tool(
        "tracking_update", {"show": "severance-2022", "action": "progress", "episode": "1x02"}
    )
    result = await client.read_resource("tracking://backlog")

    body = resource_text(result)
    assert "Severance — 1 сер." in body
    assert "45 мин" in body


async def test_watching_resource_shows_position(client: Client) -> None:
    await client.call_tool(
        "tracking_update", {"show": "severance-2022", "action": "progress", "episode": "1x02"}
    )
    result = await client.read_resource("tracking://watching")

    assert "Severance — 1x02" in resource_text(result)


async def test_facets_warn_about_genre_spelling(client: Client) -> None:
    """Профилактика самой частой ошибки: `Sci-Fi` вместо `Science-Fiction`."""
    result = await client.read_resource("catalog://facets")

    body = resource_text(result)
    assert "Science-Fiction" in body
    assert '"Sci-Fi"' in body


async def test_taste_admits_a_small_sample(client: Client) -> None:
    """Без оговорки модель выдаёт статистику из двух сериалов за устойчивый вкус."""
    await client.call_tool("tracking_update", {"show": "severance-2022", "action": "track"})
    await client.call_tool(
        "tracking_update", {"show": "severance-2022", "action": "rate", "rating": 9}
    )
    result = await client.read_resource("tracking://taste")

    assert "Выборка мала" in resource_text(result)


# --- Спойлеры и профиль -------------------------------------------------------------------


async def test_episode_search_hides_unwatched_summaries(client: Client) -> None:
    """Защита от спойлеров требует знания прогресса — у обёртки её быть не может."""
    await client.call_tool(
        "tracking_update", {"show": "severance-2022", "action": "progress", "episode": "1x01"}
    )
    result = await client.call_tool(
        "episode_search", {"query": "elevator", "show": "severance-2022"}
    )

    body = text_of(result)
    assert "Half Loop" in body
    assert "stuck in an elevator" not in body
    assert "описание скрыто" in body


async def test_episode_search_can_be_unlocked(client: Client) -> None:
    await client.call_tool(
        "tracking_update", {"show": "severance-2022", "action": "progress", "episode": "1x01"}
    )
    result = await client.call_tool(
        "episode_search",
        {"query": "elevator", "show": "severance-2022", "include_unwatched": True},
    )

    assert "elevator" in text_of(result)


async def test_show_profile_reports_season_ratings(client: Client) -> None:
    """Рейтинговый профиль по сезонам — вычисление, которого в API нет."""
    result = await client.call_tool("show_profile", {"show": "Severance"})

    body = text_of(result)
    assert "Рейтинг по сезонам" in body
    assert "с1:" in body


async def test_watch_next_answers_in_one_call(client: Client) -> None:
    """Тот вызов, ради которого затевается сервер: решение, а не список кандидатов."""
    await client.call_tool(
        "tracking_update", {"show": "severance-2022", "action": "progress", "episode": "1x02"}
    )
    result = await client.call_tool("watch_next", {"minutes": 60})

    body = text_of(result)
    assert "Severance 1x03" in body
    assert "45 мин" in body


async def test_watch_next_offers_one_alternative_when_nothing_fits(client: Client) -> None:
    """Список из шести вариантов вернул бы пользователя в ту же растерянность."""
    await client.call_tool(
        "tracking_update", {"show": "severance-2022", "action": "progress", "episode": "1x02"}
    )
    result = await client.call_tool("watch_next", {"minutes": 10})

    body = text_of(result)
    assert "не уложится" in body
    assert body.count("\n") <= 1


# --- Промпты ------------------------------------------------------------------------------


async def test_whats_next_prompt_pulls_context_from_resources(client: Client) -> None:
    """Шаблон, начинающийся с «сходи за историей инструментом», означал бы ошибку разложения."""
    result = await client.get_prompt("whats_next", {"minutes": "60"})

    text = prompt_text(result)
    assert "tracking://backlog" in text
    assert "60 минут" in text
    assert "никаких описаний непросмотренных серий" in text.lower()


async def test_season_verdict_allows_dropping(client: Client) -> None:
    result = await client.get_prompt("season_verdict", {"show": "Severance"})

    assert "«бросай»" in prompt_text(result)

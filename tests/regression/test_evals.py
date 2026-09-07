"""Сценарии из `evals/tasks.yaml`, закреплённые тестами.

Каждый тест здесь — ошибка, которая уже случилась на настоящем корпусе из 89 664
сериалов. Набор только растёт: закрытый сценарий остаётся в нём навсегда.
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
from tvtracker.tvmaze.models import Show

pytestmark = [pytest.mark.regression, pytest.mark.anyio]

# Срез корпуса: сериалы с непересекающимися описаниями плюс шум, на котором
# лексический поиск обязан не сбиться.
CORPUS = [
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
        "undergone a severance procedure, which surgically divides their memories "
        "between their work and personal lives.</p>",
    },
    {
        "id": 2,
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
        "summary": "<p>The comedic misadventures of IT support workers at a corporation.</p>",
    },
    {
        "id": 4,
        "name": "Nature Watch",
        "premiered": "2015-01-01",
        "status": "Running",
        "genres": ["Nature"],
        "averageRuntime": 45,
        "rating": {"average": 6.0},
        "language": "English",
        "weight": 10,
        "summary": "<p>Wildlife photography from the work of field biologists.</p>",
    },
]


@pytest.fixture
def client_factory(tmp_path: Path) -> Any:
    def build() -> tuple[sqlite3.Connection, Any]:
        from tvtracker.server import build_test_server

        conn = connect(tmp_path / "evals.db")
        with transaction(conn):
            Repo(conn).upsert_shows([Show.parse(row) for row in CORPUS])
        return conn, build_test_server(conn, make_settings(db_path=tmp_path / "evals.db"))

    return build


@pytest.fixture
async def client(client_factory: Any) -> AsyncIterator[Client]:
    conn, server = client_factory()
    async with Client(server, raise_exceptions=True) as connected:
        yield connected
    conn.close()


def text_of(result: Any) -> str:
    return "\n".join(block.text for block in result.content if block.type == "text")


def resource_text(result: Any) -> str:
    return str(result.contents[0].text)


async def test_eval_01_description_search_does_not_require_every_word(client: Client) -> None:
    """Запрос-пересказ не совпадает с аннотацией дословно.

    На реальном корпусе поиск возвращал НОЛЬ результатов: слова объединялись через
    подразумеваемый `AND`, и совпасть должны были все десять сразу. Отсев делает BM25,
    а не обязательность каждого слова.
    """
    result = await client.call_tool(
        "show_search",
        {
            "query": "employees have their memories surgically divided between work and home",
            "by": "description",
            "limit": 3,
        },
    )

    body = text_of(result)
    assert "Severance" in body
    assert body.index("Severance") < body.index("I Need Romance")


async def test_eval_01_counter_does_not_pretend_or_matches_are_hits(client: Client) -> None:
    """«Показаны 3 из 64714» — точная цифра, которая врёт.

    При `OR` число «совпадений» измеряет частотность служебных слов, а не количество
    подходящих сериалов.
    """
    result = await client.call_tool(
        "show_search", {"query": "employees work and memories", "by": "description", "limit": 2}
    )

    body = text_of(result)
    assert "лучшие совпадения по описанию" in body
    assert "из 4" not in body


async def test_eval_08_catalog_browse_without_a_query(client: Client) -> None:
    """«Комедии до 30 минут, которые уже закончились» — фильтрация, а не поиск.

    Третий сценарий `show_search`: искать нечего, надо отобрать и отранжировать.
    """
    result = await client.call_tool(
        "show_search", {"genre": "Comedy", "status": "Ended", "min_rating": 8.0, "limit": 5}
    )

    body = text_of(result)
    assert "The IT Crowd" in body
    assert "I Need Romance" not in body  # рейтинг 6.8 ниже порога


async def test_facets_group_by_the_column_not_by_the_show_title(client: Client) -> None:
    """`GROUP BY name` собирал статистику по названиям сериалов.

    У таблицы `shows` есть собственная колонка `name`, и псевдоним `AS name` её не
    перекрывает: справочник выдавал «Running | Ended | Running | Ended | …».
    """
    result = await client.read_resource("catalog://facets")

    body = resource_text(result)
    statuses = next(line for line in body.splitlines() if line.startswith("status:"))
    values = [v.strip() for v in statuses.removeprefix("status:").split("|")]

    assert len(values) == len(set(values))
    assert set(values) == {"Running", "Ended"}


async def test_facets_language_counts_are_real(client: Client) -> None:
    result = await client.read_resource("catalog://facets")

    body = resource_text(result)
    assert "English (3)" in body
    assert "Korean (1)" in body


async def test_eval_09_unknown_show_leads_to_a_correct_second_attempt(client: Client) -> None:
    result = await client.call_tool("show_search", {"query": "Zzqqwx", "by": "title"})

    body = text_of(result)
    assert 'by="description"' in body


async def test_eval_12_long_running_show_does_not_blow_up_the_context(
    client_factory: Any,
) -> None:
    """Сериал с 450 сериями: список эпизодов целиком не отдаётся никогда.

    Профиль сжимает их до строки со средними по сезонам — это и есть граница между
    агрегацией на нашей стороне и пересылкой сырого ответа API.
    """
    from tvtracker.tvmaze.models import Episode

    conn, server = client_factory()
    episodes = [
        Episode.parse(
            {
                "season": season,
                "number": number,
                "name": f"Episode {number}",
                "airstamp": "2020-01-01T00:00:00+00:00",
                "runtime": 22,
                "rating": {"average": 7.0 + season * 0.1},
                "summary": "<p>Filler text that must never reach the context in bulk.</p>",
            }
        )
        for season in range(1, 31)
        for number in range(1, 16)
    ]
    with transaction(conn):
        Repo(conn).replace_episodes("severance-2022", episodes)

    async with Client(server, raise_exceptions=True) as connected:
        result = await connected.call_tool("show_profile", {"show": "severance-2022"})

    body = text_of(result)
    conn.close()

    assert len(episodes) == 450
    assert len(body) < 2000 * 4  # порог дизайна: 2000 токенов на вызов
    assert "Filler text" not in body
    assert "Рейтинг по сезонам" in body

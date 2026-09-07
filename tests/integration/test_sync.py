"""Синхронизация: источник (respx) → SQLite (tmp_path). Сети нет."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from tests.conftest import make_settings
from tvtracker.errors import SourceUnavailableError
from tvtracker.storage.repo import Repo, connect
from tvtracker.storage.sync import sync_full, sync_show_episodes, sync_updates
from tvtracker.tvmaze.client import TVmazeClient

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

BASE = "https://api.tvmaze.com"


def row_keys(row: sqlite3.Row) -> set[str]:
    return set(row.keys())


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[Repo]:
    conn = connect(tmp_path / "sync.db")
    yield Repo(conn)
    conn.close()


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ограничитель частоты и backoff не должны замедлять тесты."""

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr("tvtracker.tvmaze.client.asyncio.sleep", fake_sleep)


def client() -> TVmazeClient:
    return TVmazeClient(make_settings(max_retries=1))


SEVERANCE = {
    "id": 67,
    "name": "Severance",
    "premiered": "2022-02-18",
    "genres": ["Drama", "Science-Fiction"],
    "averageRuntime": 49,
    "rating": {"average": 7.6},
    "webChannel": {"name": "Apple TV+"},
    "summary": "<p>Employees have their memories split.</p>",
    "image": {"medium": "https://example/x.jpg"},
    "_links": {"self": {"href": "https://example"}},
}


@respx.mock
async def test_full_sync_stores_only_the_fields_we_keep(repo: Repo) -> None:
    respx.get(f"{BASE}/shows", params={"page": "0"}).mock(
        return_value=httpx.Response(200, json=[SEVERANCE])
    )
    respx.get(f"{BASE}/shows", params={"page": "1"}).mock(return_value=httpx.Response(200, json=[]))

    async with client() as c:
        report = await sync_full(c, repo)

    row = repo.conn.execute("SELECT * FROM shows").fetchone()
    assert report.shows == 1
    assert row["slug"] == "severance-2022"
    assert row["network"] == "Apple TV+"
    assert row["summary_clean"] == "Employees have their memories split."
    assert "image" not in row_keys(row)
    assert repo.last_full_sync is not None


@respx.mock
async def test_full_sync_is_idempotent(repo: Repo) -> None:
    """Обход обрывается и перезапускается — база от этого не должна расти."""
    respx.get(f"{BASE}/shows", params={"page": "0"}).mock(
        return_value=httpx.Response(200, json=[SEVERANCE])
    )
    respx.get(f"{BASE}/shows", params={"page": "1"}).mock(return_value=httpx.Response(200, json=[]))

    async with client() as c:
        await sync_full(c, repo)
        await sync_full(c, repo)

    assert repo.count_shows() == 1


@respx.mock
async def test_page_failure_keeps_earlier_pages(repo: Repo) -> None:
    """Каждая страница — своя транзакция: обрыв на второй не откатывает первую."""
    respx.get(f"{BASE}/shows", params={"page": "0"}).mock(
        return_value=httpx.Response(200, json=[SEVERANCE])
    )
    respx.get(f"{BASE}/shows", params={"page": "1"}).mock(return_value=httpx.Response(429))

    async with client() as c:
        with pytest.raises(SourceUnavailableError):
            await sync_full(c, repo)

    assert repo.count_shows() == 1


@respx.mock
async def test_lazy_episode_load_stores_episodes_and_kinds(repo: Repo) -> None:
    """`?embed=episodes` — один запрос вместо двух; спешлы помечаются, а не теряются."""
    payload = SEVERANCE | {
        "_embedded": {
            "episodes": [
                {"season": 1, "number": 1, "name": "Good News", "runtime": 57},
                {"season": 1, "number": 2, "name": "Half Loop", "runtime": 46},
                {"season": 1, "number": 3, "name": "Recap", "type": "insignificant_special"},
            ]
        }
    }
    respx.get(f"{BASE}/shows/67", params={"embed": "episodes"}).mock(
        return_value=httpx.Response(200, json=payload)
    )

    async with client() as c:
        stored = await sync_show_episodes(c, repo, 67)

    kinds = {
        row["number"]: row["kind"]
        for row in repo.conn.execute("SELECT number, kind FROM episodes ORDER BY number")
    }
    assert stored == 3
    assert kinds == {1: "regular", 2: "regular", 3: "insignificant"}


@respx.mock
async def test_updates_sync_skips_shows_gone_from_source(repo: Repo) -> None:
    """Сериал удалили у источника — это не повод падать посреди инкремента."""
    respx.get(f"{BASE}/updates/shows", params={"since": "week"}).mock(
        return_value=httpx.Response(200, json={"67": 1757000000, "999": 1757000001})
    )
    respx.get(f"{BASE}/shows/67", params={"embed": "episodes"}).mock(
        return_value=httpx.Response(200, json=SEVERANCE)
    )
    respx.get(f"{BASE}/shows/999", params={"embed": "episodes"}).mock(
        return_value=httpx.Response(404)
    )

    async with client() as c:
        report = await sync_updates(c, repo)

    assert report.shows == 1
    assert repo.count_shows() == 1
    assert repo.last_updates_sync is not None

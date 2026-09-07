"""Клиент TVmaze поверх respx. Сети нет ни в одном тесте.

Главное, что здесь доказывается: лимит источника (~20 запросов / 10 сек) не роняет
обход, а исчерпание попыток превращается в доменную ошибку с внятным текстом,
а не в `httpx.HTTPStatusError` где-то в середине стека.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from tests.conftest import make_settings
from tvtracker.errors import NotFoundError, SourceUnavailableError
from tvtracker.tvmaze.client import TVmazeClient

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

BASE = "https://api.tvmaze.com"


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Ни один тест не ждёт по-настоящему; список хранит все запрошенные паузы."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("tvtracker.tvmaze.client.asyncio.sleep", fake_sleep)
    return slept


def backoff_pauses(caplog: pytest.LogCaptureFixture) -> list[float]:
    """Паузы именно backoff.

    Через `asyncio.sleep` ходит и ограничитель частоты, поэтому по списку пауз их
    не различить. Разделяет их собственный лог клиента: backoff пишет `sleep_s`.
    """
    return [record.sleep_s for record in caplog.records if hasattr(record, "sleep_s")]


def client() -> TVmazeClient:
    return TVmazeClient(make_settings(max_retries=2, backoff_base=0.5))


@respx.mock
async def test_retries_after_429_and_succeeds() -> None:
    route = respx.get(f"{BASE}/shows", params={"page": "0"}).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=[{"id": 1, "name": "A"}]),
        ]
    )

    async with client() as c:
        rows = await c.get_show_page(0)

    assert rows == [{"id": 1, "name": "A"}]
    assert route.call_count == 2


@respx.mock
async def test_retry_ceiling_is_enforced(caplog: pytest.LogCaptureFixture) -> None:
    """Бесконечного ретрая нет: попыток ровно `max_retries + 1`, дальше — деградация."""
    route = respx.get(f"{BASE}/shows").mock(return_value=httpx.Response(429))

    with caplog.at_level("WARNING", logger="tvtracker.tvmaze.client"):
        async with client() as c:
            with pytest.raises(SourceUnavailableError) as excinfo:
                await c.get_show_page(0)

    assert route.call_count == 3  # max_retries=2 плюс исходная попытка
    assert len(backoff_pauses(caplog)) == 2
    assert "локальный поиск и трекинг работают" in str(excinfo.value)


@respx.mock
async def test_backoff_grows_and_is_jittered(caplog: pytest.LogCaptureFixture) -> None:
    respx.get(f"{BASE}/shows").mock(return_value=httpx.Response(503))

    with caplog.at_level("WARNING", logger="tvtracker.tvmaze.client"):
        async with client() as c:
            with pytest.raises(SourceUnavailableError):
                await c.get_show_page(0)

    first, second = backoff_pauses(caplog)
    assert 0.5 <= first < 1.0  # base + jitter
    assert second > first  # экспонента


@respx.mock
async def test_retry_after_header_wins_over_backoff(caplog: pytest.LogCaptureFixture) -> None:
    """Источник сам сказал, сколько ждать, — свою экспоненту в этом случае не считаем."""
    respx.get(f"{BASE}/shows").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json=[]),
        ]
    )

    with caplog.at_level("WARNING", logger="tvtracker.tvmaze.client"):
        async with client() as c:
            await c.get_show_page(0)

    assert backoff_pauses(caplog) == [7.0]


@respx.mock
async def test_timeout_is_retried_then_degrades() -> None:
    route = respx.get(f"{BASE}/shows").mock(side_effect=httpx.ConnectTimeout("slow"))

    async with client() as c:
        with pytest.raises(SourceUnavailableError):
            await c.get_show_page(0)

    assert route.call_count == 3


@respx.mock
async def test_malformed_json_becomes_domain_error() -> None:
    respx.get(f"{BASE}/shows").mock(return_value=httpx.Response(200, content=b"<html>nope"))

    async with client() as c:
        with pytest.raises(SourceUnavailableError) as excinfo:
            await c.get_show_page(0)

    assert "нечитаемый" in str(excinfo.value)


@respx.mock
async def test_404_is_not_found_not_degradation() -> None:
    respx.get(f"{BASE}/shows/999").mock(return_value=httpx.Response(404))

    async with client() as c:
        with pytest.raises(NotFoundError):
            await c.get_show_with_episodes(999)


@respx.mock
async def test_iter_show_pages_stops_on_empty_page() -> None:
    """Конец каталога TVmaze обозначает пустой страницей — на ней обход и заканчивается."""
    respx.get(f"{BASE}/shows", params={"page": "0"}).mock(
        return_value=httpx.Response(200, json=[{"id": 1, "name": "A"}])
    )
    respx.get(f"{BASE}/shows", params={"page": "1"}).mock(
        return_value=httpx.Response(200, json=[{"id": 2, "name": "B"}])
    )
    respx.get(f"{BASE}/shows", params={"page": "2"}).mock(return_value=httpx.Response(200, json=[]))

    seen: list[tuple[int, int]] = []
    async with client() as c:
        async for page, rows in c.iter_show_pages():
            seen.append((page, len(rows)))

    assert seen == [(0, 1), (1, 1)]


@respx.mock
async def test_rate_limiter_spaces_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Запросы разносятся во времени — иначе 380 страниц подряд упрутся в лимит."""
    respx.get(f"{BASE}/shows").mock(return_value=httpx.Response(200, json=[]))
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr("tvtracker.tvmaze.client.asyncio.sleep", fake_sleep)

    settings = make_settings(rate_limit_per_second=2.0)
    async with TVmazeClient(settings) as c:
        await c.get_show_page(0)
        await c.get_show_page(1)

    assert waits, "второй запрос обязан подождать интервал ограничителя"
    assert waits[0] <= 0.5


def test_client_reuses_injected_httpx_client() -> None:
    """Внедрённый клиент не закрывается нами: его жизненным циклом владеет вызывающий."""
    injected: Any = httpx.AsyncClient(base_url=BASE)
    c = TVmazeClient(make_settings(), client=injected)
    # Обращение к приватному полю намеренное: проверяется именно решение о владении.
    assert c._client is injected

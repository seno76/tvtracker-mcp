"""HTTP-клиент TVmaze.

Два обязательства, ради которых этот файл существует.

**Лимит.** TVmaze разрешает «не менее 20 запросов за 10 секунд на IP» и отвечает 429 на
превышение (https://www.tvmaze.com/api). Полный обход каталога — 380 запросов подряд,
то есть лимит достигается на первой же минуте. Поэтому запросы разносит `_RateLimiter`,
а на 429 клиент уходит в экспоненциальный backoff с джиттером.

**Потолок.** Число попыток ограничено `Settings.max_retries`: бесконечный ретрай
исключён явно, а не «по здравому смыслу». Когда попытки кончились, наружу летит
`SourceUnavailableError` — доменная ошибка, не `httpx.HTTPStatusError`.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any, Self

import httpx

from tvtracker.config import Settings
from tvtracker.errors import NotFoundError, SourceUnavailableError
from tvtracker.log import get_logger

logger = get_logger(__name__)

_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class _RateLimiter:
    """Разносит запросы во времени: не более `rate` штук в секунду.

    Одна блокировка и один момент времени вместо счётчика в окне — этого достаточно,
    потому что клиент однопоточный и последовательный.
    """

    def __init__(self, rate: float) -> None:
        self._interval = 1.0 / rate
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self._interval


class TVmazeClient:
    """Асинхронный клиент. Один экземпляр на процесс, живёт внутри lifespan сервера."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._limiter = _RateLimiter(settings.rate_limit_per_second)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.tvmaze_base_url,
            timeout=settings.http_timeout,
            headers={"User-Agent": "tvtracker-mcp/0.1 (+https://www.tvmaze.com)"},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Один GET со всеми повторами. Возвращает разобранный JSON."""
        attempts = self._settings.max_retries + 1
        for attempt in range(1, attempts + 1):
            await self._limiter.acquire()
            started = time.perf_counter()
            try:
                response = await self._client.get(path, params=params)
            except httpx.TimeoutException:
                if attempt == attempts:
                    logger.error("attempts exhausted", extra={"path": path, "attempts": attempts})
                    raise SourceUnavailableError from None
                await self._backoff(attempt, path, reason="timeout")
                continue
            except httpx.HTTPError as exc:
                logger.error("transport error", extra={"path": path, "error": type(exc).__name__})
                raise SourceUnavailableError from exc

            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            logger.debug(
                "request done",
                extra={
                    "path": path,
                    "status": response.status_code,
                    "duration_ms": elapsed_ms,
                    "attempt": attempt,
                },
            )

            if response.status_code == 404:
                raise NotFoundError(path)
            if response.status_code in _RETRY_STATUSES:
                if attempt == attempts:
                    logger.error("attempts exhausted", extra={"path": path, "attempts": attempts})
                    raise SourceUnavailableError
                await self._backoff(
                    attempt, path, reason=str(response.status_code), header=response.headers
                )
                continue

            response.raise_for_status()
            try:
                return response.json()
            except ValueError as exc:
                logger.error("malformed json", extra={"path": path})
                raise SourceUnavailableError("Источник вернул нечитаемый ответ.") from exc

        raise SourceUnavailableError  # pragma: no cover — цикл всегда выходит через return/raise

    async def _backoff(
        self,
        attempt: int,
        path: str,
        reason: str,
        header: httpx.Headers | None = None,
    ) -> None:
        """Экспоненциальная пауза с джиттером; `Retry-After` уважается, если он пришёл.

        Джиттер нужен даже одному клиенту: без него повторы после серии 429 выстраиваются
        в ту же самую пачку, которая лимит и вызвала.
        """
        retry_after = header.get("Retry-After") if header else None
        if retry_after and retry_after.isdigit():
            sleep_s = float(retry_after)
        else:
            sleep_s = self._settings.backoff_base * 2 ** (attempt - 1)
            sleep_s += random.uniform(0, self._settings.backoff_base)  # noqa: S311
        logger.warning(
            "backing off",
            extra={"path": path, "attempt": attempt, "sleep_s": round(sleep_s, 2), "why": reason},
        )
        await asyncio.sleep(sleep_s)

    # --- Эндпоинты, которые нам нужны ---------------------------------------------------

    async def get_show_page(self, page: int) -> list[dict[str, Any]]:
        """Страница индекса каталога. Пустой список означает, что страницы кончились."""
        try:
            payload = await self._get("/shows", {"page": page})
        except NotFoundError:
            return []
        return payload if isinstance(payload, list) else []

    async def iter_show_pages(
        self, start: int = 0
    ) -> AsyncIterator[tuple[int, list[dict[str, Any]]]]:
        """Обходит каталог целиком: `/shows?page=0..N` до первой пустой страницы."""
        page = start
        while True:
            rows = await self.get_show_page(page)
            if not rows:
                return
            yield page, rows
            page += 1

    async def get_updates(self, since: str | None = None) -> dict[str, int]:
        """`/updates/shows` — карта `id → метка последнего изменения`."""
        params = {"since": since} if since else None
        payload = await self._get("/updates/shows", params)
        return payload if isinstance(payload, dict) else {}

    async def get_show_with_episodes(self, tvmaze_id: int) -> dict[str, Any]:
        """`/shows/:id?embed=episodes` — один запрос вместо двух.

        Так эпизоды приезжают вместе с сериалом ровно тогда, когда его начинают
        отслеживать, и ни секундой раньше.
        """
        payload = await self._get(f"/shows/{tvmaze_id}", {"embed": "episodes"})
        return payload if isinstance(payload, dict) else {}

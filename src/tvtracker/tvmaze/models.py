"""Модели ответов TVmaze.

Здесь проходит первая граница бюджета контекста: `image`, `_links`, `externals`, `url`,
`dvdCountry` и прочий мусор отбрасываются **на входе**, а не при рендере ответа. Всё, чего
нет в этих моделях, не попадёт в БД и физически не сможет утечь в контекст модели.

Формат данных: https://www.tvmaze.com/api
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser
from typing import Any

from pydantic import BaseModel, Field, field_validator

_WHITESPACE = re.compile(r"\s+")
# Оставляем любые буквы и цифры, а не только ASCII: в корпусе 25 языков, и
# «тьма-2017» — осмысленный ключ, тогда как схлопнутый «show-2017» столкнётся
# со всеми остальными нелатинскими названиями того же года.
_NON_SLUG = re.compile(r"[^\w]+", re.UNICODE)


class _TextExtractor(HTMLParser):
    """Вытаскивает текст из HTML. `html.parser` из stdlib — зависимость не нужна."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self.chunks.append(data)


def strip_html(raw: str | None) -> str:
    """Чистит `summary`: TVmaze отдаёт его как `<p>…</p>` с сущностями внутри.

    Чистим один раз при загрузке — в БД и в индексе лежит уже чистый текст.
    """
    if not raw:
        return ""
    parser = _TextExtractor()
    parser.feed(raw)
    parser.close()
    return _WHITESPACE.sub(" ", unescape("".join(parser.chunks))).strip()


def truncate(text: str, limit: int) -> str:
    """Режет текст по границе слова и ставит многоточие. Пустая строка остаётся пустой."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:—-")
    return f"{cut}…"


def rating_average(value: Any) -> Any:
    """`rating` приходит как `{"average": 7.6}` либо как `{"average": null}`."""
    return value.get("average") if isinstance(value, dict) else value


def make_slug(name: str, premiered: str | None) -> str:
    """`severance-2022` = имя + год премьеры.

    Год обязателен там, где он есть: без него «The Office» из двух стран и трёх десятилетий
    схлопывается в один ключ. Коллизии остатка разрешает хранилище суффиксом.
    """
    base = _NON_SLUG.sub("-", name.lower()).strip("-_") or "show"
    year = premiered[:4] if premiered and len(premiered) >= 4 else None
    return f"{base}-{year}" if year else base


class Show(BaseModel):
    """Сериал в том объёме, в каком он нужен нам, — 13 полей вместо сорока."""

    tvmaze_id: int = Field(alias="id")
    name: str
    type: str | None = None
    language: str | None = None
    genres: list[str] = Field(default_factory=list)
    status: str | None = None
    premiered: str | None = None
    ended: str | None = None
    avg_runtime: int | None = Field(default=None, alias="averageRuntime")
    rating: float | None = None
    weight: int | None = None
    network: str | None = None
    summary_clean: str = ""

    model_config = {"populate_by_name": True, "extra": "ignore"}

    _unwrap_rating = field_validator("rating", mode="before")(rating_average)

    @field_validator("network", mode="before")
    @classmethod
    def _flatten_network(cls, value: Any) -> Any:
        """Площадка — это строка. Вложенный объект с `country` и `officialSite` нам не нужен."""
        return value.get("name") if isinstance(value, dict) else value

    @field_validator("summary_clean", mode="before")
    @classmethod
    def _clean_summary(cls, value: Any) -> str:
        return strip_html(value if isinstance(value, str) else None)

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> Show:
        """Строит модель из сырого ответа TVmaze, подставляя площадку из `webChannel`.

        У стриминговых сериалов `network` пуст, а площадка лежит в `webChannel` —
        без этой склейки половина каталога осталась бы без площадки.
        """
        data = dict(payload)
        if not data.get("network"):
            data["network"] = data.get("webChannel")
        if data.get("summary") is not None:
            data["summary_clean"] = data["summary"]
        return cls.model_validate(data)

    @property
    def slug(self) -> str:
        return make_slug(self.name, self.premiered)


class Episode(BaseModel):
    """Эпизод. `airstamp` в UTC — `airdate` не годится (см. docs/DESIGN.md)."""

    season: int
    number: int | None = None
    name: str = ""
    airstamp: str | None = None
    runtime: int | None = None
    rating: float | None = None
    summary_clean: str = ""
    kind: str = "regular"

    model_config = {"populate_by_name": True, "extra": "ignore"}

    _unwrap_rating = field_validator("rating", mode="before")(rating_average)

    @field_validator("summary_clean", mode="before")
    @classmethod
    def _clean_summary(cls, value: Any) -> str:
        return strip_html(value if isinstance(value, str) else None)

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> Episode:
        """Определяет `kind` из полей `type` и `number`.

        Без этого бэклог требует досмотреть рождественский спешл, которого нет
        в сюжетной линии. `number = null` у TVmaze означает именно спецвыпуск.
        """
        data = dict(payload)
        if data.get("summary") is not None:
            data["summary_clean"] = data["summary"]
        raw_type = (data.get("type") or "regular").lower()
        if raw_type == "insignificant_special":
            data["kind"] = "insignificant"
        elif raw_type != "regular" or data.get("number") is None:
            data["kind"] = "special"
        else:
            data["kind"] = "regular"
        return cls.model_validate(data)

    @property
    def code(self) -> str:
        """Человекочитаемый номер: `2x03`. Спецвыпуски без номера — `2xS`."""
        return f"{self.season}x{self.number:02d}" if self.number is not None else f"{self.season}xS"

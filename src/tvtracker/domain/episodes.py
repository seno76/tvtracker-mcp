"""Работа с номерами серий.

Модель разговаривает человеческим форматом `2x03`, а не парой чисел. Разбор вынесен
сюда, потому что им пользуются и `tracking_update`, и `episode_search`, и рендер.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_CODE = re.compile(r"^\s*(\d{1,4})\s*[xх]\s*(\d{1,4})\s*$", re.IGNORECASE)


class EpisodeCodeError(ValueError):
    """Неверный формат номера серии.

    Текст ошибки — часть контракта: он показывает и ожидаемый формат, и то, что пришло.
    Без второго модель повторяет ту же ошибку, потому что не видит своей опечатки.
    """

    def __init__(self, given: str) -> None:
        super().__init__(f'Ожидаю "2x03" или season=2, number=3. Получено "{given}".')


@dataclass(frozen=True, order=True)
class EpisodeRef:
    """Позиция в сериале: сезон и номер."""

    season: int
    number: int

    def __str__(self) -> str:
        return f"{self.season}x{self.number:02d}"


def parse_code(code: str) -> EpisodeRef:
    """Разбирает `2x03`.

    Кириллическая «х» принимается наравне с латинской `x`: на русской раскладке её
    набирают чаще, и отвергать такой ввод — значит тратить раунд на опечатку.
    """
    match = _CODE.match(code)
    if not match:
        raise EpisodeCodeError(code)
    return EpisodeRef(int(match.group(1)), int(match.group(2)))


def format_code(season: int | None, number: int | None) -> str | None:
    """Обратное преобразование. `None`, если позиции нет."""
    if season is None or number is None:
        return None
    return str(EpisodeRef(season, number))

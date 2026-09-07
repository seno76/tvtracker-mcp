"""Мелочи, общие для слоёв.

`utcnow` вынесена сюда одной функцией не ради красоты: доменные функции принимают «сейчас»
аргументом, чтобы граничные случаи поддавались проверке, и единственное место, где время
берётся из системы, — граница слоя MCP.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Текущий момент в UTC. `airstamp` от TVmaze тоже в UTC — сравнение корректно."""
    return datetime.now(UTC)


def humanize_since(airstamp: str | None, now: datetime) -> str:
    """«3 дня назад», «2 недели назад» — то, как о сроках говорит человек."""
    from tvtracker.domain.backlog import parse_airstamp

    aired_at = parse_airstamp(airstamp)
    if aired_at is None:
        return ""
    days = (now - aired_at).days
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    if days < 7:
        return f"{days} дн. назад"
    if days < 31:
        weeks = days // 7
        return f"{weeks} нед. назад"
    return f"{days // 30} мес. назад"


def format_hours(minutes: int) -> str:
    """`5 ч 45 мин`, `50 мин`. Ноль превращается в пустую строку — его не показываем."""
    if minutes <= 0:
        return ""
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} ч {rest} мин"
    return f"{hours} ч" if hours else f"{rest} мин"

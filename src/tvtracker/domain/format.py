"""Рендер ответов инструментов.

Инструмент возвращает **текст**, а не JSON API. Причина в бюджете: JSON тратит на скобки,
кавычки и повторяющиеся имена полей в полтора-два раза больше токенов при той же информации,
а модель читает плотный текст не хуже.

Формат строки результата зафиксирован дизайном:

    Severance (2022–, Apple TV+, идёт) · драма/фантастика · 49 мин · 7.6
    Сотрудникам корпорации хирургически разделяют память.
    → отслеживаю, остановился на 2x03

Шесть полей, а не сорок. Функции здесь чистые: на вход — данные, на выход — строка.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from tvtracker.domain.backlog import parse_airstamp

# Статусы TVmaze по-русски. Незнакомый статус показываем как есть — молча ронять
# информацию хуже, чем показать английское слово.
_STATUS = {
    "Running": "идёт",
    "Ended": "завершён",
    "To Be Determined": "судьба неясна",
    "In Development": "в разработке",
}

# 11–14 — исключение из правила согласования, а не магические числа.
TEENS_START = 11
TEENS_END = 14

DAYS_IN_WEEK = 7
DAYS_IN_MONTH = 30

_TRACKING = {
    "watching": "отслеживаю",
    "paused": "на паузе",
    "finished": "досмотрел",
    "dropped": "брошен",
}


def truncate(text: str, limit: int) -> str:
    """Режет текст по границе слова и ставит многоточие. Пустая строка остаётся пустой."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:—-")
    return f"{cut}…"


def format_hours(minutes: int) -> str:
    """`5 ч 45 мин`, `50 мин`. Ноль даёт пустую строку — его не показываем."""
    if minutes <= 0:
        return ""
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} ч {rest} мин"
    return f"{hours} ч" if hours else f"{rest} мин"


def plural_episodes(count: int) -> str:
    """«1 серия», «3 серии», «14 серий» — русское согласование по числу.

    Не косметика: этот текст читает модель и пересказывает пользователю. «Впереди
    1 серий» она воспроизведёт дословно.
    """
    tail = count % 100
    if TEENS_START <= tail <= TEENS_END:
        return f"{count} серий"
    match count % 10:
        case 1:
            return f"{count} серия"
        case 2 | 3 | 4:
            return f"{count} серии"
        case _:
            return f"{count} серий"


def humanize_since(airstamp: str | None, now: datetime) -> str:
    """«3 дн. назад», «2 нед. назад» — то, как о сроках говорит человек."""
    aired_at = parse_airstamp(airstamp)
    if aired_at is None:
        return ""
    days = (now - aired_at).days
    if days <= 0:
        return "сегодня"
    if days == 1:
        return "вчера"
    if days < DAYS_IN_WEEK:
        return f"{days} дн. назад"
    if days < DAYS_IN_MONTH:
        return f"{days // DAYS_IN_WEEK} нед. назад"
    return f"{days // DAYS_IN_MONTH} мес. назад"


@dataclass(frozen=True)
class ShowLine:
    """Одна строка выдачи. Ровно то, что модель увидит про сериал, — и ничего больше."""

    slug: str
    name: str
    premiered: str | None = None
    ended: str | None = None
    status: str | None = None
    network: str | None = None
    genres: list[str] = field(default_factory=list)
    avg_runtime: int | None = None
    rating: float | None = None
    summary: str = ""
    tracking_state: str | None = None
    tracking_position: str | None = None


def years(premiered: str | None, ended: str | None) -> str:
    """`2022–2025`, `2022–` для идущего, пустая строка если года нет вовсе."""
    start = premiered[:4] if premiered else ""
    finish = ended[:4] if ended else ""
    if not start:
        return finish
    return f"{start}–{finish}" if finish else f"{start}–"


def status_ru(status: str | None) -> str:
    return _STATUS.get(status or "", status or "")


def tracking_note(state: str | None, position: str | None) -> str:
    """Пометка о своём состоянии — то, чего в TVmaze нет ни при каком запросе."""
    if state is None:
        return ""
    label = _TRACKING.get(state, state)
    if state == "watching" and position:
        return f"{label}, остановился на {position}"
    if position:
        return f"{label} ({position})"
    return label


def render_show_line(show: ShowLine, summary_limit: int = 200) -> str:
    """Собирает одну карточку результата. Пустые поля просто выпадают из строки."""
    head_bits = [
        b for b in (years(show.premiered, show.ended), show.network, status_ru(show.status)) if b
    ]
    head = f"{show.name} ({', '.join(head_bits)})" if head_bits else show.name

    facts = [head]
    if show.genres:
        facts.append("/".join(show.genres))
    if show.avg_runtime:
        facts.append(f"{show.avg_runtime} мин")
    if show.rating is not None:
        facts.append(f"{show.rating:g}")

    lines = [" · ".join(facts)]
    if show.summary:
        lines.append(truncate(show.summary, summary_limit))
    note = tracking_note(show.tracking_state, show.tracking_position)
    if note:
        lines.append(f"→ {note}")
    return "\n".join(lines)


def render_results(
    shows: list[ShowLine],
    total: int,
    query: str,
    hints: list[str] | None = None,
    summary_limit: int = 200,
    exact: bool = True,
) -> str:
    """Собирает весь ответ инструмента: результаты, счётчик и подсказка по сужению.

    Усечение **всегда** сопровождается счётчиком и конкретным способом сузить запрос:
    без него модель принимает первые восемь результатов за весь каталог.

    `exact=False` — для поиска по описанию. Там слова объединяются через `OR`, и число
    «совпадений» измеряет частотность служебных слов, а не количество подходящих
    сериалов: сказать «3 из 64714» — значит соврать точной цифрой.
    """
    if not shows:
        return no_results(query)

    body = "\n\n".join(render_show_line(show, summary_limit) for show in shows)
    if exact and total <= len(shows):
        return body

    tail = (
        f"\n\nПоказаны {len(shows)} из {total}."
        if exact
        else "\n\nЭто лучшие совпадения по описанию, а не полный список."
    )
    if hints:
        tail += f" Сузить: {' или '.join(hints)}."
    return body + tail


def no_results(query: str, nearest: list[str] | None = None) -> str:
    """Пустая выдача — это не пустой список, а инструкция для следующего вызова."""
    text = f"По запросу «{query}» ничего не найдено."
    if nearest:
        text += f" Ближайшие по названию: {', '.join(nearest)}."
    return text + ' Или опиши сюжет — я умею искать по описанию (by="description").'

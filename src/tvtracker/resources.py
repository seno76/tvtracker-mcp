"""Пять ресурсов.

Ресурс — данные, которые подаёт **приложение**, а не добывает модель. Критерий включения
один: содержимое нужно в контексте до того, как модель начала рассуждать.

Три общих решения:

* **`text/plain`, а не JSON.** JSON тратит на скобки и повторяющиеся имена полей
  в полтора-два раза больше токенов при той же информации; здесь его читает только модель.
* **Всё считается из локальной БД.** `resources/read` отвечает за миллисекунды и работает,
  даже когда TVmaze лежит.
* **Бюджет.** Суммарно все пять — порядка 1100 токенов. Это цена, которую платишь
  в каждом запросе, где хост их подставил.

Шаблонных ресурсов (`show://{slug}`) здесь сознательно нет: данные по конкретному сериалу
нужны не всегда, а по запросу — это работа `show_profile`.

Устройство модуля повторяет `tools/`: сборка текста живёт в обычных функциях, которые
принимают репозиторий и «сейчас», а `register_resources` — тонкая обвязка. Так каждый
ресурс вызывается в тесте напрямую, без поднятого сервера.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from mcp.server import MCPServer

from tvtracker.clock import utcnow
from tvtracker.domain.backlog import parse_airstamp
from tvtracker.domain.episodes import EpisodeRef, format_code
from tvtracker.domain.format import format_hours, humanize_since
from tvtracker.domain.taste import GenreTaste, TrackedShow, build_profile
from tvtracker.mapping import genres_of
from tvtracker.runtime import current_app
from tvtracker.services import collect_backlogs
from tvtracker.storage.repo import Repo

SCHEDULE_HORIZON_DAYS = 14
SCHEDULE_MAX_ROWS = 12
SMALL_SAMPLE = 5
_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _repo() -> Repo:
    """Репозиторий из держателя контекста (почему не из `ctx` — см. `tvtracker.runtime`)."""
    return current_app().repo  # type: ignore[no-any-return]


# --- Сборка содержимого -------------------------------------------------------------------


def render_watching(repo: Repo) -> str:
    """Сериалы в активном просмотре с текущей позицией."""
    active = repo.list_tracking(("watching",))
    paused = repo.list_tracking(("paused",))
    if not active and not paused:
        return "Пока ничего не отслеживается."

    lines = [f"Смотрю сейчас ({len(active)}):"]
    for row in active:
        position = format_code(row["season"], row["number"]) or "не начат"
        bits = [f"{row['name']} — {position}", f"обновлено {str(row['updated_at'])[:10]}"]
        if row["rating"] is not None:
            bits.append(f"моя оценка {row['rating']}")
        lines.append(", ".join(bits))

    if paused:
        items = ", ".join(
            f"{row['name']} ({format_code(row['season'], row['number']) or '—'})" for row in paused
        )
        lines.append(f"\nНа паузе ({len(paused)}): {items}")
    return "\n".join(lines)


def render_backlog(repo: Repo, now: datetime) -> str:
    """Вышедшие, но не просмотренные эпизоды по отслеживаемым сериалам."""
    backlogs, meta = collect_backlogs(repo)
    if not backlogs:
        return "Бэклог пуст: отслеживаемых сериалов нет."

    lines = ["Накопилось (по свежести выхода):"]
    total_episodes = total_minutes = 0

    for item in backlogs:
        if item.count == 0:
            # Ноль серий — это ответ, а не пустота: видно, ждать ли продолжения.
            ended = meta[item.slug]["status"] == "Ended"
            tail = "сериал завершён" if ended else "новых серий нет"
            lines.append(f"{item.name} — 0 серий, {tail}")
            continue
        total_episodes += item.count
        total_minutes += item.minutes
        parts = [
            f"{item.name} — {item.count} сер.",
            format_hours(item.minutes) or "длительность неизвестна",
            f"свежая вышла {humanize_since(item.latest_airstamp, now)}",
            item.span,
        ]
        lines.append(", ".join(part for part in parts if part))

    if total_episodes:
        lines.append(
            f"\nИтого: {total_episodes} сер., {format_hours(total_minutes)}. "
            "Спецвыпуски не учитываются."
        )
    return "\n".join(lines)


def render_schedule(repo: Repo, now: datetime) -> str:
    """Что из отслеживаемого выходит в ближайшие две недели."""
    horizon = now + timedelta(days=SCHEDULE_HORIZON_DAYS)
    header = f"Ближайшие {SCHEDULE_HORIZON_DAYS} дней (сегодня {now:%Y-%m-%d})"

    upcoming: list[tuple[datetime, str, str]] = []
    for row in repo.list_tracking(("watching", "paused")):
        for episode in repo.episodes_for(str(row["show_slug"])):
            airs_at = parse_airstamp(episode["airstamp"])
            if airs_at and now < airs_at <= horizon:
                upcoming.append((airs_at, str(row["name"]), _episode_label(episode)))

    if not upcoming:
        return f"{header}: ничего из отслеживаемого не выходит."

    upcoming.sort(key=lambda item: item[0])
    lines = [f"{header}:"]
    lines += [
        f"{_WEEKDAYS[airs_at.weekday()]} {airs_at:%d.%m}  {name} {label}"
        for airs_at, name, label in upcoming[:SCHEDULE_MAX_ROWS]
    ]
    return "\n".join(lines)


def render_taste(repo: Repo) -> str:
    """Агрегат по истории: жанры со средней оценкой, доля брошенного, длина."""
    shows = [
        TrackedShow(
            slug=str(row["show_slug"]),
            name=str(row["name"]),
            state=str(row["state"]),
            genres=genres_of(row),
            rating=row["rating"],
            avg_runtime=row["avg_runtime"],
            network=row["network"],
            language=row["language"],
        )
        for row in repo.list_tracking()
    ]
    if not shows:
        return "Истории просмотра пока нет — профиль вкуса не по чему считать."

    profile = build_profile(shows)
    lines = [
        f"Профиль вкуса (по {profile.total} сериалам, из них {profile.rated_count} с оценкой):"
    ]
    if profile.liked:
        lines.append("Люблю:      " + _genre_line(profile.liked))
    if profile.cool:
        lines.append("Прохладно:  " + _genre_line(profile.cool))
    if profile.completion_rate is not None:
        lines.append(f"Досматриваю: {profile.completion_rate:.0%} из закрытого.")
    if profile.preferred_runtime:
        low, high = profile.preferred_runtime
        lines.append(f"Длина:      предпочитаю {low}–{high} мин.")
    if profile.networks:
        lines.append("Площадки:   " + ", ".join(f"{n} ({c})" for n, c in profile.networks))
    if profile.languages:
        lines.append("Языки:      " + ", ".join(f"{n} ({c})" for n, c in profile.languages))
    if profile.recent_ratings:
        lines.append(
            "\nПоследние оценки: "
            + ", ".join(f"{name} {rating}" for name, rating, _ in profile.recent_ratings)
        )
    if profile.rated_count < SMALL_SAMPLE:
        # Без оговорки модель выдаёт статистику из четырёх сериалов за устойчивый вкус.
        lines.append("\nВыборка мала — считай эти цифры наброском, а не устойчивым вкусом.")
    return "\n".join(lines)


def render_facets(repo: Repo) -> str:
    """Жанры, статусы, типы и языки, реально встречающиеся в локальном корпусе."""
    data = repo.facets()
    if not data["genre"]:
        return "Корпус пуст. Выполни `tvtracker sync --full`, чтобы наполнить каталог."

    languages = ", ".join(f"{name} ({_thousands(count)})" for name, count in data["language"][:6])
    return "\n".join(
        [
            "Значения для фильтров show_search:",
            "genre:    " + ", ".join(name for name, _ in data["genre"]),
            "status:   " + " | ".join(name for name, _ in data["status"]),
            "type:     " + " | ".join(name for name, _ in data["type"][:8]),
            f"language: {languages}, … всего {len(data['language'])}",
            # Последняя строка — не украшение, а профилактика самой частой ошибки.
            '\nЖанры чувствительны к написанию: "Science-Fiction", не "Sci-Fi" и не "sci fi".',
        ]
    )


# --- Регистрация --------------------------------------------------------------------------


def register_resources(server: MCPServer) -> None:
    """Обработчики объявлены `async` намеренно.

    Синхронную функцию SDK уводит в рабочий поток, а соединение SQLite привязано к своему —
    из чужого потока оно бросает `ProgrammingError`. Чтения здесь миллисекундные, цикл
    событий они не держат.
    """

    @server.resource("tracking://watching", title="Что я смотрю сейчас", mime_type="text/plain")
    async def watching() -> str:
        """Сериалы в активном просмотре с текущей позицией и датой последнего просмотра."""
        return render_watching(_repo())

    @server.resource("tracking://backlog", title="Накопившиеся серии", mime_type="text/plain")
    async def backlog() -> str:
        """Вышедшие, но не просмотренные эпизоды по отслеживаемым сериалам."""
        return render_backlog(_repo(), utcnow())

    @server.resource("tracking://schedule", title="Календарь моих сериалов", mime_type="text/plain")
    async def schedule() -> str:
        """Что из отслеживаемого выходит в ближайшие две недели."""
        return render_schedule(_repo(), utcnow())

    @server.resource("tracking://taste", title="Профиль вкуса", mime_type="text/plain")
    async def taste() -> str:
        """Агрегат по истории: жанры со средней оценкой, доля брошенного, длина."""
        return render_taste(_repo())

    @server.resource(
        "catalog://facets", title="Допустимые значения фильтров", mime_type="text/plain"
    )
    async def facets() -> str:
        """Жанры, статусы, типы и языки, реально встречающиеся в локальном корпусе."""
        return render_facets(_repo())


def _genre_line(tastes: Sequence[GenreTaste]) -> str:
    return " · ".join(f"{t.genre} {t.average} ({t.count})" for t in tastes)


def _episode_label(episode: Any) -> str:
    ref = EpisodeRef(int(episode["season"]), int(episode["number"]))
    return f"{ref} «{episode['name']}»" if episode["name"] else str(ref)


def _thousands(count: int) -> str:
    return f"{count / 1000:.1f}k" if count >= 1000 else str(count)

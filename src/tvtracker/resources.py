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
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from mcp.server import MCPServer

from tvtracker.domain.backlog import parse_airstamp
from tvtracker.domain.episodes import EpisodeRef, format_code
from tvtracker.domain.taste import TrackedShow, build_profile
from tvtracker.runtime import current_app
from tvtracker.services import collect_backlogs
from tvtracker.storage.repo import Repo
from tvtracker.utils import format_hours, humanize_since, utcnow

SCHEDULE_HORIZON_DAYS = 14
_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _repo() -> Repo:
    """Репозиторий из держателя контекста: статическим ресурсам SDK контекст не инжектит.

    Обработчики ресурсов объявлены `async`: синхронную функцию SDK уводит в рабочий поток,
    а соединение SQLite привязано к своему — из чужого потока оно бросает
    `ProgrammingError`. Чтения здесь миллисекундные, цикл событий они не держат.
    """
    return current_app().repo  # type: ignore[no-any-return]


def register_resources(server: MCPServer) -> None:
    @server.resource(
        "tracking://watching",
        title="Что я смотрю сейчас",
        mime_type="text/plain",
    )
    async def watching() -> str:
        """Сериалы в активном просмотре с текущей позицией и датой последнего просмотра."""
        repo = _repo()
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
                f"{row['name']} ({format_code(row['season'], row['number']) or '—'})"
                for row in paused
            )
            lines.append(f"\nНа паузе ({len(paused)}): {items}")
        return "\n".join(lines)

    @server.resource(
        "tracking://backlog",
        title="Накопившиеся серии",
        mime_type="text/plain",
    )
    async def backlog() -> str:
        """Вышедшие, но не просмотренные эпизоды по отслеживаемым сериалам."""
        repo = _repo()
        backlogs, meta = collect_backlogs(repo)
        if not backlogs:
            return "Бэклог пуст: отслеживаемых сериалов нет."

        now = utcnow()
        lines = ["Накопилось (по свежести выхода):"]
        total_episodes = total_minutes = 0

        for item in backlogs:
            if item.count == 0:
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
            lines.append(", ".join(p for p in parts if p))

        if total_episodes:
            lines.append(
                f"\nИтого: {total_episodes} сер., {format_hours(total_minutes)}. "
                "Спецвыпуски не учитываются."
            )
        return "\n".join(lines)

    @server.resource(
        "tracking://schedule",
        title="Календарь моих сериалов",
        mime_type="text/plain",
    )
    async def schedule() -> str:
        """Что из отслеживаемого выходит в ближайшие две недели."""
        repo = _repo()
        now = utcnow()
        horizon = now + timedelta(days=SCHEDULE_HORIZON_DAYS)

        upcoming: list[tuple[Any, str, str]] = []
        for row in repo.list_tracking(("watching", "paused")):
            for episode in repo.episodes_for(str(row["show_slug"])):
                airs_at = parse_airstamp(episode["airstamp"])
                if airs_at and now < airs_at <= horizon:
                    upcoming.append((airs_at, str(row["name"]), _episode_label(episode)))

        if not upcoming:
            return (
                f"Ближайшие {SCHEDULE_HORIZON_DAYS} дней (сегодня {now:%Y-%m-%d}): "
                "ничего из отслеживаемого не выходит."
            )

        upcoming.sort(key=lambda item: item[0])
        lines = [f"Ближайшие {SCHEDULE_HORIZON_DAYS} дней (сегодня {now:%Y-%m-%d}):"]
        lines += [
            f"{_WEEKDAYS[airs_at.weekday()]} {airs_at:%d.%m}  {name} {label}"
            for airs_at, name, label in upcoming[:12]
        ]
        return "\n".join(lines)

    @server.resource(
        "tracking://taste",
        title="Профиль вкуса",
        mime_type="text/plain",
    )
    async def taste() -> str:
        """Агрегат по истории: жанры со средней оценкой, доля брошенного, длина."""
        repo = _repo()
        shows = [
            TrackedShow(
                slug=str(row["show_slug"]),
                name=str(row["name"]),
                state=str(row["state"]),
                genres=list(json.loads(row["genres_json"] or "[]")),
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
            lines.append(
                "Люблю:      "
                + " · ".join(f"{t.genre} {t.average} ({t.count})" for t in profile.liked)
            )
        if profile.cool:
            lines.append(
                "Прохладно:  "
                + " · ".join(f"{t.genre} {t.average} ({t.count})" for t in profile.cool)
            )
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
        if profile.rated_count < 5:
            lines.append("\nВыборка мала — считай эти цифры наброском, а не устойчивым вкусом.")
        return "\n".join(lines)

    @server.resource(
        "catalog://facets",
        title="Допустимые значения фильтров",
        mime_type="text/plain",
    )
    async def facets() -> str:
        """Жанры, статусы, типы и языки, реально встречающиеся в локальном корпусе."""
        repo = _repo()
        data = repo.facets()
        if not data["genre"]:
            return "Корпус пуст. Выполни `tvtracker sync --full`, чтобы наполнить каталог."

        lines = ["Значения для фильтров show_search:"]
        lines.append("genre:    " + ", ".join(name for name, _ in data["genre"]))
        lines.append("status:   " + " | ".join(name for name, _ in data["status"]))
        lines.append("type:     " + " | ".join(name for name, _ in data["type"][:8]))
        languages = ", ".join(
            f"{name} ({_thousands(count)})" for name, count in data["language"][:6]
        )
        lines.append(f"language: {languages}, … всего {len(data['language'])}")
        lines.append(
            '\nЖанры чувствительны к написанию: "Science-Fiction", не "Sci-Fi" и не "sci fi".'
        )
        return "\n".join(lines)


def _episode_label(episode: Any) -> str:
    ref = EpisodeRef(int(episode["season"]), int(episode["number"]))
    return f"{ref} «{episode['name']}»" if episode["name"] else str(ref)


def _thousands(count: int) -> str:
    return f"{count / 1000:.1f}k" if count >= 1000 else str(count)

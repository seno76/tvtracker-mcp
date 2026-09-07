"""Прогон evals: замер числа вызовов и токенов на сценарий.

Без замеров «лучшие практики» остаются словами. Здесь считаются три вещи на каждый
сценарий: **сколько вызовов инструментов** он стоил, **сколько токенов** занял ответ
и **достигнут ли результат**.

Что этот прогон измеряет и чего не измеряет — граница важная. Он измеряет **цену
намеченного плана вызовов**: сценарий описывает ту последовательность, которую должен
пройти хорошо ведущий себя агент, а прогон считает её стоимость. Он **не** измеряет,
угадает ли модель этот план, — для этого нужен агент в петле. Числа отсюда отвечают на
вопрос «сколько стоит правильный путь», а не «часто ли модель на него встаёт».

Токены оцениваются приближённо: точный счёт требует токенизатора провайдера, а
зависимости ради отчёта проект не тянет. Твёрдая цифра — символы; токены даны как
оценка и помечены как оценка. Для сравнения «было/стало» этого достаточно: обе
величины считаются одинаково.

Запуск: `uv run python -m evals.runner`
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from tvtracker.log import configure_logging

REPORTS = Path("evals/reports")
SPEC = Path("evals/tasks.yaml")

# Эмпирический делитель: латиница даёт ~4 символа на токен, кириллица ощутимо меньше.
# Ответы сервера смешанные, поэтому берётся общая оценка и честно называется оценкой.
CHARS_PER_TOKEN = 3.4


def estimate_tokens(text: str) -> int:
    return round(len(text) / CHARS_PER_TOKEN)


@dataclass
class Measurement:
    """Результат одного сценария."""

    id: int
    ask: str
    tool_calls: int
    resource_reads: int
    chars: int
    tokens: int
    passed: bool
    max_calls: int
    note: str = ""
    calls: list[str] = field(default_factory=list)

    @property
    def within_budget(self) -> bool:
        return self.tool_calls <= self.max_calls


class Recorder:
    """Обёртка над клиентом, считающая обращения и объём ответов.

    Считать в самом сценарии нельзя: там легко забыть инкремент и получить красивую
    неправду. Здесь счётчик стоит на единственном пути, которым сценарий ходит к серверу.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.tool_calls = 0
        self.resource_reads = 0
        self.chars = 0
        self.calls: list[str] = []

    async def tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        result = await self._client.call_tool(name, arguments or {})
        body = "\n".join(b.text for b in result.content if b.type == "text")
        self.tool_calls += 1
        self.chars += len(body)
        self.calls.append(name)
        return body

    async def resource(self, uri: str) -> str:
        result = await self._client.read_resource(uri)
        body = str(result.contents[0].text)
        self.resource_reads += 1
        self.chars += len(body)
        self.calls.append(uri)
        return body

    async def prompt(self, name: str, arguments: dict[str, str]) -> str:
        result = await self._client.get_prompt(name, arguments)
        body = str(result.messages[0].content.text)
        self.chars += len(body)
        self.calls.append(f"prompt:{name}")
        return body


def load_spec() -> dict[int, dict[str, Any]]:
    """Читает `tasks.yaml`. Прогон обязан покрывать ровно то, что в нём объявлено."""
    entries = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    return {int(entry["id"]): entry for entry in entries}


async def run_all(only: int | None = None) -> list[Measurement]:
    from evals import scenarios

    spec = load_spec()
    declared = set(spec)
    implemented = set(scenarios.SCENARIOS)
    if declared != implemented:
        missing = declared - implemented
        extra = implemented - declared
        raise SystemExit(
            f"Сценарии разошлись со спецификацией: не реализованы {sorted(missing)}, "
            f"нет в tasks.yaml {sorted(extra)}"
        )

    results: list[Measurement] = []
    for scenario_id in sorted(spec):
        if only is not None and scenario_id != only:
            continue
        entry = spec[scenario_id]
        results.append(await scenarios.run_one(scenario_id, entry))
    return results


def render_report(results: list[Measurement]) -> str:
    """Таблица, ради которой этап и затевался."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Прогон evals",
        "",
        f"Замер {stamp}. Токены — оценка ({CHARS_PER_TOKEN} символа на токен), "
        "твёрдая цифра — символы.",
        "",
        "Прогон измеряет цену намеченного плана вызовов, а не то, угадает ли его модель.",
        "",
        "| # | Сценарий | Вызовов | Ресурсов | Символов | ~Токенов | Бюджет | Итог |",
        "| --- | --- | ---: | ---: | ---: | ---: | :---: | :---: |",
    ]
    for row in results:
        budget = "✓" if row.within_budget else f"✗ >{row.max_calls}"
        verdict = "✓" if row.passed else "✗"
        lines.append(
            f"| {row.id} | {row.ask} | {row.tool_calls} | {row.resource_reads} | "
            f"{row.chars} | {row.tokens} | {budget} | {verdict} |"
        )

    passed = sum(1 for r in results if r.passed)
    within = sum(1 for r in results if r.within_budget)
    total_tokens = sum(r.tokens for r in results)
    worst = max(results, key=lambda r: r.tokens)

    lines += [
        "",
        "## Итоги",
        "",
        f"- Достигнут результат: **{passed} из {len(results)}**",
        f"- Уложились в бюджет вызовов: **{within} из {len(results)}**",
        f"- Суммарно по всем сценариям: **~{total_tokens} токенов**",
        f"- Самый дорогой: №{worst.id} — ~{worst.tokens} токенов",
        "",
        "## Пороги дизайна",
        "",
        "| Порог | Значение | Факт |",
        "| --- | --- | --- |",
        f"| Типовой сценарий | 1–2 вызова | {_typical_calls(results)} |",
        f"| Ответ concise | до ~600 токенов | максимум {worst.tokens} (№{worst.id}) |",
        f"| Сценарий 12 | до 2000 токенов на вызов | {_scenario_tokens(results, 12)} |",
    ]

    failures = [r for r in results if not r.passed or not r.within_budget]
    if failures:
        lines += ["", "## Разбор несоответствий", ""]
        lines += [f"- **№{r.id}** — {r.note}" for r in failures if r.note]

    notes = [r for r in results if r.note and r not in failures]
    if notes:
        lines += ["", "## Замечания", ""]
        lines += [f"- №{r.id}: {r.note}" for r in notes]

    return "\n".join(lines) + "\n"


def _typical_calls(results: list[Measurement]) -> str:
    typical = sorted(r.tool_calls for r in results)
    return f"медиана {typical[len(typical) // 2]}, максимум {typical[-1]}"


def _scenario_tokens(results: list[Measurement], scenario_id: int) -> str:
    match = next((r for r in results if r.id == scenario_id), None)
    return f"{match.tokens} токенов" if match else "не прогонялся"


def main() -> int:
    """Точка входа.

    Синхронная: асинхронен только сам прогон сценариев, а запись отчёта — обычный
    файловый ввод-вывод, которому в цикле событий делать нечего.
    """
    parser = argparse.ArgumentParser(prog="evals", description="Прогон сценариев оценки")
    parser.add_argument("--only", type=int, help="прогнать один сценарий по номеру")
    parser.add_argument("--quiet", action="store_true", help="не печатать отчёт в stdout")
    args = parser.parse_args()

    # Консоль Windows по умолчанию не cp65001: без этого отчёт с галочками падает
    # на UnicodeEncodeError, хотя сам файл пишется в UTF-8 и в порядке.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    configure_logging("WARNING", "console")

    results = asyncio.run(run_all(args.only))
    report = render_report(results)

    REPORTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    (REPORTS / f"{stamp}.md").write_text(report, encoding="utf-8")
    (REPORTS / f"{stamp}.json").write_text(
        json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (REPORTS / "latest.md").write_text(report, encoding="utf-8")

    if not args.quiet:
        print(report)
    return 0 if all(r.passed and r.within_budget for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

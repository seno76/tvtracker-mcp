"""Сам прогонщик evals тоже под тестом.

Отчёт — инструмент принятия решений: по нему правится дизайн. Молча испортившийся
счётчик хуже отсутствующего, потому что его цифрам верят.
"""

from __future__ import annotations

import pytest
from evals import scenarios
from evals.runner import Measurement, estimate_tokens, load_spec, render_report

pytestmark = pytest.mark.regression


def measurement(**overrides: object) -> Measurement:
    base = {
        "id": 1,
        "ask": "Сценарий",
        "tool_calls": 1,
        "resource_reads": 0,
        "chars": 340,
        "tokens": 100,
        "passed": True,
        "max_calls": 1,
    }
    return Measurement(**{**base, **overrides})  # type: ignore[arg-type]


def test_every_declared_scenario_is_implemented() -> None:
    """Расхождение спецификации и кода означает, что отчёт врёт молчанием."""
    assert set(load_spec()) == set(scenarios.SCENARIOS)


def test_spec_declares_twelve_scenarios() -> None:
    spec = load_spec()

    assert len(spec) == 12
    assert all("ask" in entry and "max_calls" in entry for entry in spec.values())


def test_budget_overrun_is_visible_in_the_report() -> None:
    report = render_report([measurement(tool_calls=3, max_calls=1)])

    assert "✗ >1" in report
    assert "Уложились в бюджет вызовов: **0 из 1**" in report


def test_failure_note_reaches_the_report() -> None:
    report = render_report(
        [measurement(passed=False, note="описание непросмотренной серии утекло")]
    )

    assert "Разбор несоответствий" in report
    assert "описание непросмотренной серии утекло" in report


def test_report_names_the_most_expensive_scenario() -> None:
    report = render_report(
        [measurement(id=1, tokens=100), measurement(id=6, tokens=351, ask="Профиль")]
    )

    assert "Самый дорогой: №6 — ~351 токенов" in report


def test_token_estimate_is_monotonic() -> None:
    """Точность оценки не важна, важно, что сравнение «было/стало» не переворачивается."""
    assert estimate_tokens("x" * 340) < estimate_tokens("x" * 680)
    assert estimate_tokens("") == 0

"""Адаптер между модулем events и WindowComparator.

Принимает результат events.core.assess() и конвертирует оценки по механизмам
в формат, совместимый с WindowComparator._factor() — объекты с атрибутами
risk_score, data_coverage_pct, high_risk_minutes, missing_data.

Модуль не изменяет ни events, ни WindowComparator; он только адаптирует формат.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class EventsFactorResult:
    """Результат оценки одного механизма из events, совместимый с WindowComparator."""

    risk_score: float = 0.0
    data_coverage_pct: float = 0.0
    high_risk_minutes: float = 0.0
    missing_data: list = field(default_factory=list)


def _compute_risk_score(factor: Dict[str, Any], duration_hours: float) -> float:
    """Прозрачная эвристика: доля опасного перекрытия + вклад числа событий.

    Не является вероятностью; аналогична подходу DebrisCalculator.
    """
    duration_min = duration_hours * 60.0
    if duration_min <= 0:
        return 0.0

    overlap = float(factor.get('known_interval_overlap_minutes', 0))
    event_count = int(factor.get('event_count', 0))

    # Основной вклад — доля покрытия окна опасными интервалами
    overlap_score = min(100.0, (overlap / duration_min) * 100.0)

    # Дополнительный вклад — наличие событий без перекрытия (ongoing, instant)
    count_bonus = min(20.0, event_count * 5.0) if event_count > 0 and overlap == 0 else 0.0

    return min(100.0, overlap_score + count_bonus)


def adapt_events_for_comparator(
    assess_result: Dict[str, Any],
) -> Dict[str, Dict[str, EventsFactorResult]]:
    """Конвертирует результат assess() в формат для WindowComparator.compare().

    Возвращает dict[window_id, dict[mechanism_name, EventsFactorResult]],
    где window_id формируется как 'events_window_{i}'.

    Ключи механизмов сохраняются как в events: radiation, geomagnetic,
    communications, tracked_debris, meteoroids.
    """
    result: Dict[str, Dict[str, EventsFactorResult]] = {}

    windows = assess_result.get('windows', [])
    for i, window in enumerate(windows):
        window_id = f"events_window_{i}"
        factors: Dict[str, EventsFactorResult] = {}
        duration_hours = float(window.get('duration_hours', 6))

        for mechanism, mdata in window.get('factors', {}).items():
            coverage_fraction = float(mdata.get('coverage_fraction', 0))
            coverage_pct = coverage_fraction * 100.0

            overlap_minutes = float(mdata.get('known_interval_overlap_minutes', 0))

            missing: List[str] = []
            status = mdata.get('status', '')
            if 'insufficient' in status:
                missing.append(f'{mechanism}: недостаточно данных')
            if mdata.get('possible_ongoing_event_ids'):
                ids = ', '.join(mdata['possible_ongoing_event_ids'][:3])
                missing.append(f'{mechanism}: возможно продолжающиеся события ({ids})')

            risk = _compute_risk_score(mdata, duration_hours)

            factors[mechanism] = EventsFactorResult(
                risk_score=risk,
                data_coverage_pct=coverage_pct,
                high_risk_minutes=overlap_minutes,
                missing_data=missing,
            )

        result[window_id] = factors

    return result


def merge_factor_results(
    conditions_factors: Dict[str, Dict[str, Any]],
    events_factors: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Объединяет результаты conditions и events для одного набора окон.

    Ключи из events (radiation, tracked_debris и т.д.) и conditions
    (debris, protons и т.д.) различаются, поэтому конфликтов нет — они
    попадают в comparator как разные факторы.
    """
    merged: Dict[str, Dict[str, Any]] = {}

    for wid in set(list(conditions_factors.keys()) + list(events_factors.keys())):
        merged[wid] = {}
        merged[wid].update(conditions_factors.get(wid, {}))
        merged[wid].update(events_factors.get(wid, {}))

    return merged

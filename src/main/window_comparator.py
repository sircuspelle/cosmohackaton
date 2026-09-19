"""Сравнение кандидатных окон ВКД.

Компаратор принимает результаты любых калькуляторов, если у результата есть
`risk_score`, либо одно из известных полей (например `t_exceed_eff_min` или
`time_no_link_min`). Недостаток данных не превращается в нулевой риск.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Mapping, Sequence, Optional, Tuple

from src.main.conditions.warnings import WarningAssessment
from src.main.window import Window


@dataclass(frozen=True)
class FactorAssessment:
    name: str
    risk_score: Optional[float]
    data_coverage_pct: float = 100.0
    overlap_minutes: float = 0.0
    missing_data: Tuple[str, ...] = ()
    explanation: str = ""
    warning_assessment: Optional[WarningAssessment] = None


@dataclass(frozen=True)
class WindowAssessment:
    window_id: str
    total_score: Optional[float]
    recommendation: str
    factors: tuple[FactorAssessment, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WindowComparison:
    assessments: tuple[WindowAssessment, ...]
    preferred_window_id: Optional[str]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "preferred_window_id": self.preferred_window_id,
            "reason": self.reason,
            "windows": [item.to_dict() for item in self.assessments],
        }


class WindowComparator:
    """Ранжирует окна по объяснимому взвешенному риску."""

    DEFAULT_WEIGHTS = {"debris": 1.4, "meteoroid": 1.4, "protons": 1.2,
                       "thermal": 1.0, "illumination": 0.8, "communication": 0.6}

    @classmethod
    def compare(
        cls,
        windows: Sequence[Window],
        factor_results: Mapping[str, Mapping[str, Any]],
        weights: Optional[Mapping[str, float]] = None,
        tie_tolerance: float = 2.0,
    ) -> WindowComparison:
        if len(windows) < 2:
            raise ValueError("at least two windows are required")
        durations = {round(w.duration.total_seconds()) for w in windows}
        if len(durations) != 1:
            raise ValueError("all windows must have equal duration")
        if tie_tolerance < 0:
            raise ValueError("tie_tolerance must be non-negative")

        cfg = dict(cls.DEFAULT_WEIGHTS)
        cfg.update(weights or {})
        scored: list[tuple[WindowAssessment, float, bool]] = []
        for window in windows:
            factors = []
            weighted = 0.0
            weight_sum = 0.0
            incomplete = False
            for name, result in factor_results.get(window.id, {}).items():
                factor = cls._factor(name, result)
                factors.append(factor)
                w = max(0.0, cfg.get(name.lower(), 1.0))
                if factor.risk_score is None or factor.data_coverage_pct < 50.0:
                    incomplete = True
                    continue
                weighted += factor.risk_score * w
                weight_sum += w
            score = weighted / weight_sum if weight_sum else None
            sort_score = score if score is not None else float("inf")
            scored.append((WindowAssessment(window.id, score, "", tuple(factors)), sort_score, incomplete))

        known = [x for x in scored if x[0].total_score is not None and not x[2]]
        preferred = None
        reason = "Недостаточно данных для надёжного выбора окна."
        if known:
            known.sort(key=lambda x: x[1])
            best = known[0]
            second = known[1] if len(known) > 1 else None
            if second and abs(best[1] - second[1]) <= tie_tolerance:
                reason = "Разница рисков меньше порога; требуется дополнительная проверка."
            else:
                preferred = best[0].window_id
                reason = f"Минимальный взвешенный риск: {best[1]:.1f}/100."

        final = []
        for assessment, score, incomplete in scored:
            label = "недостаточно данных" if incomplete or score == float("inf") else f"риск {score:.1f}/100"
            final.append(WindowAssessment(assessment.window_id, assessment.total_score, label, assessment.factors))
        return WindowComparison(tuple(final), preferred, reason)

    @staticmethod
    def _factor(name: str, result: Any) -> FactorAssessment:
        score = getattr(result, "risk_score", None)
        overlap = getattr(result, "high_risk_minutes", getattr(result, "t_exceed_eff_min", 0.0))
        coverage = float(getattr(result, "data_coverage_pct", 100.0))
        missing = tuple(getattr(result, "missing_data", ()) or ())
        if score is None:
            if hasattr(result, "t_exceed_eff_min"):
                score = min(100.0, float(result.t_exceed_eff_min) / 60.0 * 100.0)
            elif hasattr(result, "time_no_link_min"):
                score = min(100.0, float(result.time_no_link_min) / max(1.0, float(getattr(result, "window_end") - getattr(result, "window_start")).total_seconds() / 60.0) * 100.0)
            elif hasattr(result, "time_cold_min") and hasattr(result, "time_hot_min"):
                score = min(100.0, float(result.time_cold_min + result.time_hot_min) / 60.0 * 100.0)
            else:
                score = None
        return FactorAssessment(name, None if score is None else max(0.0, min(100.0, float(score))), coverage, float(overlap or 0.0), missing, "расчёт калькулятора")

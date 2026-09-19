from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from math import isfinite
from typing import Iterable, Optional, Dict, Any

from src.main.window import Window
from src.main.conditions.warnings import WarningAssessment

@dataclass(frozen=True)
class DebrisObservation:
    time: datetime
    miss_distance_km: float
    object_id: str
    relative_speed_km_s: float = 0.0
    source: str = "SOCRATES"
    published_at: Optional[datetime] = None
    uncertainty_km: Optional[float] = None

@dataclass(frozen=True)
class MeteoroidSample:
    time: datetime
    flux_m2_s: float
    source: str = "model"
    published_at: Optional[datetime] = None

@dataclass
class DebrisMetrics:
    window_id: str
    n_conjunctions: int
    closest_distance_km: Optional[float]
    closest_object_id: Optional[str]
    conjunction_minutes: float
    high_risk_minutes: float
    meteoroid_fluence_m2: float
    max_meteoroid_flux_m2_s: float
    risk_score: float
    data_coverage_pct: float
    missing_data: list[str]
    source: str = "SOCRATES + meteoroid flux"
    warning_assessment: Optional[WarningAssessment] = None  # <--- Добавили поле

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if self.warning_assessment:
            d["warning_assessment"] = self.warning_assessment.to_dict()
        return d


class DebrisCalculator:
    DEFAULT_EVENT_DURATION = timedelta(minutes=10)
    HIGH_RISK_DISTANCE_KM = 25.0
    MAX_CONJUNCTION_DISTANCE_KM = 100.0

    @classmethod
    def calculate_metrics(
        cls,
        window: Window,
        conjunctions: Iterable[DebrisObservation],
        meteoroids: Iterable[MeteoroidSample] = (),
        event_duration: timedelta = DEFAULT_EVENT_DURATION,
        max_distance_km: float = MAX_CONJUNCTION_DISTANCE_KM,
    ) -> DebrisMetrics:
        events = [e for e in conjunctions if cls._valid_event(e)]
        events.sort(key=lambda e: e.time)
        in_window = []
        for event in events:
            event_time = Window._utc_naive(event.time)
            half = event_duration / 2
            if window.overlap(event_time - half, event_time + half) and event.miss_distance_km <= max_distance_km:
                in_window.append(event)

        samples = [m for m in meteoroids if cls._valid_sample(m) and window.contains(m.time)]
        samples.sort(key=lambda m: m.time)
        duration_s = window.duration.total_seconds()
        fluence = sum(m.flux_m2_s for m in samples) * (duration_s / max(1, len(samples)))

        conjunction_minutes = len(in_window) * event_duration.total_seconds() / 60.0
        high_minutes = sum(event_duration.total_seconds() / 60.0 for e in in_window
                           if e.miss_distance_km <= cls.HIGH_RISK_DISTANCE_KM)
        closest = min(in_window, key=lambda e: e.miss_distance_km, default=None)

        debris_score = max((100.0 * max(0.0, 1.0 - e.miss_distance_km / max_distance_km)
                            for e in in_window), default=0.0)
        flux_score = min(30.0, fluence * 1e6)
        risk_score = min(100.0, debris_score + flux_score)
        coverage = min(100.0, 100.0 * len(samples) / max(1, int(duration_s / 60)))
        
        missing = []
        if not in_window and not events:
            missing.append("нет данных о сближениях")
        if not samples:
            missing.append("нет ряда потока метеороидов")
        if coverage < 50.0:
            missing.append("ряд метеороидов покрывает менее половины окна")

        # Формирование детального предупреждения
        impact_str = (
            f"Мин. дистанция {closest.miss_distance_km:.1f} км (объект {closest.object_id})"
            if closest else "Опасных сближений в окне не зафиксировано"
        )
        warning = WarningAssessment(
            factor_name="debris_and_meteoroids",
            eva_impact_value=impact_str,
            source=closest.source if closest else "SOCRATES + model",
            published_at=closest.published_at if closest else None,
            model_or_rule="Линейная эвристика близости (порог <= 100 км) + интеграл потока",
            confidence_justification=f"Покрытие данных: {coverage:.1f}%. Проблемы: {', '.join(missing) if missing else 'нет'}.",
            risk_score=risk_score
        )

        return DebrisMetrics(
            window_id=window.id,
            n_conjunctions=len(in_window),
            closest_distance_km=closest.miss_distance_km if closest else None,
            closest_object_id=closest.object_id if closest else None,
            conjunction_minutes=conjunction_minutes,
            high_risk_minutes=high_minutes,
            meteoroid_fluence_m2=fluence,
            max_meteoroid_flux_m2_s=max((m.flux_m2_s for m in samples), default=0.0),
            risk_score=risk_score,
            data_coverage_pct=coverage,
            missing_data=missing,
            warning_assessment=warning
        )

    @staticmethod
    def _valid_event(event: DebrisObservation) -> bool:
        return (isinstance(event.time, datetime) and bool(event.object_id) and
                isfinite(float(event.miss_distance_km)) and event.miss_distance_km >= 0 and
                isfinite(float(event.relative_speed_km_s)) and event.relative_speed_km_s >= 0)

    @staticmethod
    def _valid_sample(sample: MeteoroidSample) -> bool:
        return (isinstance(sample.time, datetime) and isfinite(float(sample.flux_m2_s)) and
                sample.flux_m2_s >= 0)
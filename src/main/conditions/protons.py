"""
Расчёт протонных метрик с учётом траектории станции.

Модуль предоставляет:
  * ProtonPoint          — точка измерения;
  * GeomagneticModel     — упрощённая геомагнитная модель;
  * ProtonMetrics        — dataclass с результатами;
  * ProtonReport         — обёртка с удобными методами;
  * TrajectoryAwareProtonCalculator — основной калькулятор.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import datetime
from statistics import median
from typing import List, Dict, Optional, Sequence, Iterable

from src.main.window import Window
import math  # убедись, что math импортирован в начале файла

@dataclass
class ProtonPoint:
    """Точка измерения протонов >=10 MeV."""
    time: datetime
    flux: float
    lat_deg: Optional[float] = None
    lon_deg: Optional[float] = None
    alt_km: Optional[float] = None
    mag_lat_deg: Optional[float] = None
    l_shell: Optional[float] = None

    def has_position(self) -> bool:
        """True, если заполнены все нужные координаты."""
        return (self.lat_deg is not None and
                self.lon_deg is not None and
                self.alt_km is not None and
                self.mag_lat_deg is not None)

    def is_finite(self) -> bool:
        """True, если все числовые поля конечны (не NaN, не inf)."""
        try:
            if not math.isfinite(float(self.flux)):
                return False
        except (TypeError, ValueError):
            return False
        for v in (self.lat_deg, self.lon_deg, self.alt_km,
                  self.mag_lat_deg, self.l_shell):
            if v is None:
                continue
            try:
                if not math.isfinite(float(v)):
                    return False
            except (TypeError, ValueError):
                return False
        return True


class GeomagneticModel:
    """Упрощённая модель геомагнитного поля для весовой функции."""

    POLE_LAT = 80.7
    POLE_LON = -72.7

    SAA_LAT = -26.0
    SAA_LON = -53.0
    SAA_RADIUS_DEG = 25.0
    SAA_AMPLIFICATION = 3.0

    R_EARTH_KM = 6371.0

    @classmethod
    def to_geomagnetic_lat(cls, lat_deg: float, lon_deg: float) -> float:
        phi = math.radians(lat_deg)
        lam = math.radians(lon_deg)
        phi_p = math.radians(cls.POLE_LAT)
        lam_p = math.radians(cls.POLE_LON)

        cos_d = (math.sin(phi) * math.sin(phi_p) +
                 math.cos(phi) * math.cos(phi_p) * math.cos(lam - lam_p))
        cos_d = max(-1.0, min(1.0, cos_d))
        return 90.0 - math.degrees(math.acos(cos_d))

    @classmethod
    def l_shell(cls, mag_lat_deg: float, alt_km: float) -> float:
        r = cls.R_EARTH_KM + alt_km
        cos2 = math.cos(math.radians(mag_lat_deg)) ** 2
        if cos2 < 1e-6:
            return 1e6
        return r / (cls.R_EARTH_KM * cos2)

    @classmethod
    def saa_factor(cls, lat_deg: float, lon_deg: float) -> float:
        dlat = lat_deg - cls.SAA_LAT
        dlon = (lon_deg - cls.SAA_LON + 180.0) % 360.0 - 180.0
        dlon_scaled = dlon * math.cos(math.radians(lat_deg))
        dist = math.sqrt(dlat ** 2 + dlon_scaled ** 2)
        if dist >= cls.SAA_RADIUS_DEG:
            return 1.0
        sigma = cls.SAA_RADIUS_DEG / 2.0
        bump = math.exp(-dist ** 2 / (2.0 * sigma ** 2))
        return 1.0 + (cls.SAA_AMPLIFICATION - 1.0) * bump

    @classmethod
    def transmission(cls, mag_lat_deg: float, alt_km: float) -> float:
        abs_lat = abs(mag_lat_deg)
        if abs_lat >= 60.0:
            return 1.0
        x = abs_lat / 60.0
        return 0.05 + 0.95 * (x ** 2)

    @classmethod
    def weight(cls, p: ProtonPoint) -> float:
        """Весовой множитель: transmission · SAA. 1.0, если координат нет."""
        if not p.has_position():
            return 1.0
        w_trans = cls.transmission(p.mag_lat_deg, p.alt_km)
        w_saa = cls.saa_factor(p.lat_deg, p.lon_deg)
        return w_trans * w_saa


@dataclass
class ProtonMetrics:
    """Метрики протонной обстановки в окне ВКД."""
    window_id: str
    n_points: int

    # Сырые (наблюдаемые) метрики
    max_flux_raw: float
    integral_flux_raw: float           # ∫ F dt, pfu·с

    # Эффективные (с учётом траектории) метрики
    max_flux_eff: float
    integral_flux_eff: float           # ∫ F_eff dt, pfu·с
    integral_flux_path: float          # ∫ F_eff dL, pfu·км (0 если не считали)

    # Пороговые метрики
    t_exceed_raw_min: float            # время, когда F_raw >= порога
    t_exceed_eff_min: float            # время, когда F_eff >= порога

    # Доза
    dose_rad: float

    # Метаданные
    trajectory_aware: bool
    integrated_by: str                 # 'time' | 'path'
    threshold_pfu: float
    shielding_g_cm2: Optional[float]

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


class ProtonReport:
    """Удобная обёртка над ProtonMetrics."""

    def __init__(self, metrics: ProtonMetrics):
        self.metrics = metrics

    def is_threshold_exceeded(self) -> bool:
        return self.metrics.t_exceed_eff_min > 0.0

    def is_safe(self,
                max_t_exceed_min: float = 10.0,
                max_dose_rad: float = 1.0) -> bool:
        """
        Универсальный критерий безопасности.
        Пороги можно переопределить на месте.
        """
        m = self.metrics
        if m.t_exceed_eff_min > max_t_exceed_min:
            return False
        if m.dose_rad > max_dose_rad:
            return False
        return True

    def summary(self) -> str:
        m = self.metrics
        lines = [
            f"Окно: {m.window_id}  (точек: {m.n_points})",
            f"  F_max  raw/eff: {m.max_flux_raw:8.2f} / {m.max_flux_eff:8.2f} pfu",
            f"  ∫F     raw/eff: {m.integral_flux_raw:12.1f} / "
            f"{m.integral_flux_eff:12.1f} pfu·с",
            f"  T_exceed raw/eff: {m.t_exceed_raw_min:6.2f} / "
            f"{m.t_exceed_eff_min:6.2f} мин",
            f"  Доза: {m.dose_rad:.4e} рад "
            f"(экранировка: {m.shielding_g_cm2} г/см²)",
            f"  Учёт траектории: {m.trajectory_aware}, "
            f"интегрирование: {m.integrated_by}",
        ]
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, object]:
        return self.metrics.to_dict()


class TrajectoryAwareProtonCalculator:
    """
    Расчёт протонных метрик с учётом траектории станции.

    Отличия от предыдущей версии:
      * возвращает ProtonMetrics/ProtonReport, а не словарь;
      * разделяет «сырые» и «эффективные» метрики;
      * медианный шаг вместо фиксированных 5 минут;
      * корректная доза только по времени;
      * валидация входных данных;
      * кэш F_eff в пределах одного вызова.
    """

    PROTON_THRESHOLD_PFU = 10.0        # S1
    DOSE_COEFF_PER_PFU_S = 1.0e-6      # рад на (pfu·с)
    ATTENUATION_G_CM2 = 30.0
    MIN_STEP_SEC = 1.0
    MAX_STEP_SEC = 30 * 60             # 30 минут — защита от «дыр»
    ISS_SPEED_KM_S = 7.66

    @classmethod
    def calculate_proton_metrics(
        cls,
        window: Window,
        data_series: Sequence[ProtonPoint],
        shielding_g_cm2: Optional[float] = None,
        use_path: bool = False,
        threshold_pfu: Optional[float] = None,
        with_report: bool = False,
    ) -> "ProtonMetrics | ProtonReport":
        """
        Основной метод. Возвращает ProtonMetrics (или ProtonReport).

        Параметры:
            window            — окно ВКД
            data_series       — точки протонов
            shielding_g_cm2   — толщина экранировки для оценки дозы
            use_path          — считать ли также ∫ F_eff dL
            threshold_pfu     — порог S1 (по умолчанию 10 pfu)
            with_report       — вернуть ProtonReport вместо ProtonMetrics
        """
        threshold = (threshold_pfu
                     if threshold_pfu is not None
                     else cls.PROTON_THRESHOLD_PFU)

        pts = cls._filter_window(window, data_series)
        n_points = len(pts)

        empty = cls._empty_metrics(window, n_points, use_path,
                                   threshold, shielding_g_cm2)

        if not pts:
            return ProtonReport(empty) if with_report else empty

        pts = cls._validate_and_sort(pts)
        if not pts:
            return ProtonReport(empty) if with_report else empty

        # Кэш F_eff, чтобы не считать дважды
        f_eff_cache: Dict[int, float] = {}
        for idx, p in enumerate(pts):
            f_eff_cache[idx] = cls._effective_flux(p)

        median_step = cls._median_step(pts)
        trajectory_aware = any(p.has_position() for p in pts)

        max_flux_raw = max(p.flux for p in pts)
        max_flux_eff = max(f_eff_cache.values())

        integral_raw = 0.0
        integral_eff = 0.0
        integral_path = 0.0
        t_exceed_raw_sec = 0.0
        t_exceed_eff_sec = 0.0

        for i in range(len(pts) - 1):
            p0, p1 = pts[i], pts[i + 1]
            dt = (p1.time - p0.time).total_seconds()
            if dt <= 0:
                continue
            dt = min(dt, cls.MAX_STEP_SEC)

            f_raw = p0.flux
            f_eff = f_eff_cache[i]

            integral_raw += f_raw * dt
            integral_eff += f_eff * dt

            if use_path:
                dL = cls._great_circle_km(p0, p1)
                if dL <= 0.0:
                    dL = cls.ISS_SPEED_KM_S * dt
                integral_path += f_eff * dL

            if f_raw >= threshold:
                t_exceed_raw_sec += dt
            if f_eff >= threshold:
                t_exceed_eff_sec += dt

        # Последняя точка — медианный шаг ряда
        last_idx = len(pts) - 1
        last = pts[last_idx]
        dt_last = median_step
        f_raw_last = last.flux
        f_eff_last = f_eff_cache[last_idx]

        integral_raw += f_raw_last * dt_last
        integral_eff += f_eff_last * dt_last
        if use_path:
            integral_path += f_eff_last * cls.ISS_SPEED_KM_S * dt_last

        if f_raw_last >= threshold:
            t_exceed_raw_sec += dt_last
        if f_eff_last >= threshold:
            t_exceed_eff_sec += dt_last

        # Доза — только по времени (размерность: pfu·с)
        dose = 0.0
        if shielding_g_cm2 is not None:
            dose = cls._estimate_dose(integral_eff, shielding_g_cm2)

        metrics = ProtonMetrics(
            window_id=window.id,
            n_points=n_points,
            max_flux_raw=max_flux_raw,
            integral_flux_raw=integral_raw,
            max_flux_eff=max_flux_eff,
            integral_flux_eff=integral_eff,
            integral_flux_path=integral_path if use_path else 0.0,
            t_exceed_raw_min=t_exceed_raw_sec / 60.0,
            t_exceed_eff_min=t_exceed_eff_sec / 60.0,
            dose_rad=dose,
            trajectory_aware=trajectory_aware,
            integrated_by='path' if use_path else 'time',
            threshold_pfu=threshold,
            shielding_g_cm2=shielding_g_cm2,
        )
        return ProtonReport(metrics) if with_report else metrics

    # ---- вспомогательные --------------------------------------------------

    @staticmethod
    def _filter_window(window: Window,
                       data_series: Iterable[ProtonPoint]
                       ) -> List[ProtonPoint]:
        return [p for p in data_series
                if window.start <= p.time <= window.end]

    @staticmethod
    def _validate_and_sort(pts: List[ProtonPoint]) -> List[ProtonPoint]:
        """Фильтрует некорректные точки и сортирует по времени."""
        clean = []
        for p in pts:
            # Защита от None и отрицательных/NaN значений
            try:
                flux = float(p.flux)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(flux) or flux < 0:
                continue
            clean.append(p)
        clean.sort(key=lambda x: x.time)
        return clean
    @staticmethod
    def _median_step(pts: List[ProtonPoint]) -> float:
        if len(pts) < 2:
            return TrajectoryAwareProtonCalculator.MAX_STEP_SEC / 2
        diffs = [
            (pts[i + 1].time - pts[i].time).total_seconds()
            for i in range(len(pts) - 1)
        ]
        diffs = [d for d in diffs if d > 0]
        if not diffs:
            return TrajectoryAwareProtonCalculator.MAX_STEP_SEC / 2
        return max(
            TrajectoryAwareProtonCalculator.MIN_STEP_SEC,
            min(median(diffs), TrajectoryAwareProtonCalculator.MAX_STEP_SEC),
        )

    @classmethod
    def _effective_flux(cls, p: ProtonPoint) -> float:
        return p.flux * GeomagneticModel.weight(p)

    @staticmethod
    def _great_circle_km(p0: ProtonPoint, p1: ProtonPoint) -> float:
        if (p0.lat_deg is None or p0.lon_deg is None or
                p1.lat_deg is None or p1.lon_deg is None):
            return 0.0
        R = GeomagneticModel.R_EARTH_KM
        lat1, lon1 = math.radians(p0.lat_deg), math.radians(p0.lon_deg)
        lat2, lon2 = math.radians(p1.lat_deg), math.radians(p1.lon_deg)
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = (math.sin(dlat / 2) ** 2 +
             math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
        c = 2 * math.asin(min(1.0, math.sqrt(a)))
        h0 = p0.alt_km or 0.0
        h1 = p1.alt_km or 0.0
        return (R + 0.5 * (h0 + h1)) * c

    @classmethod
    def _estimate_dose(cls, fluence_pfu_s: float,
                       shielding_g_cm2: float) -> float:
        attenuation = math.exp(-shielding_g_cm2 / cls.ATTENUATION_G_CM2)
        return cls.DOSE_COEFF_PER_PFU_S * fluence_pfu_s * attenuation

    @staticmethod
    def _empty_metrics(window: Window,
                       n_points: int,
                       use_path: bool,
                       threshold_pfu: float,
                       shielding_g_cm2: Optional[float]) -> ProtonMetrics:
        return ProtonMetrics(
            window_id=window.id,
            n_points=n_points,
            max_flux_raw=0.0,
            integral_flux_raw=0.0,
            max_flux_eff=0.0,
            integral_flux_eff=0.0,
            integral_flux_path=0.0,
            t_exceed_raw_min=0.0,
            t_exceed_eff_min=0.0,
            dose_rad=0.0,
            trajectory_aware=False,
            integrated_by='path' if use_path else 'time',
            threshold_pfu=threshold_pfu,
            shielding_g_cm2=shielding_g_cm2,
        )
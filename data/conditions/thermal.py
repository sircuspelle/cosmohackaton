# -*- coding: utf-8 -*-
"""
Модуль расчёта тепловых метрик на орбите.

Предоставляет:
  * ThermalSample      — одна точка (время + потоки + температура);
  * ThermalMetrics     — агрегированные метрики окна;
  * ThermalReport      — обёртка с удобными методами;
  * ThermalCalculator  — основной калькулятор на базе Skyfield.

Физика:
  * Прямое Солнце         — 1361 Вт/м² (solar constant) с учётом расстояния;
  * Альбедо Земли         — отражённый свет, зависит от широты и облачности;
  * ИК-излучение Земли    — тепловое излучение планеты;
  * Собственное излучение — скафандр/станция излучает по Стефану-Больцману.

Все datetime трактуются как UTC (naive помечается как UTC в точках
вызова Skyfield через _ensure_utc, без мутации исходных объектов).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone as _tz
from typing import List, Optional, Dict, Sequence

try:
    from skyfield.api import load, EarthSatellite, wgs84, utc
    _SKYFIELD_AVAILABLE = True
except ImportError:
    load = EarthSatellite = wgs84 = utc = None
    _SKYFIELD_AVAILABLE = False
    print("Внимание: библиотека skyfield не установлена. "
          "Расчёт тепловых метрик будет недоступен.")
    print("Установите: pip install skyfield")


# ---------------------------------------------------------------------------
# Хелпер UTC
# ---------------------------------------------------------------------------

def _ensure_utc(dt: datetime) -> datetime:
    """Возвращает tz-aware datetime в UTC. Не мутирует исходный объект."""
    target_tz = utc if utc is not None else _tz.utc
    if dt.tzinfo is None:
        return dt.replace(tzinfo=target_tz)
    return dt.astimezone(target_tz)


# ---------------------------------------------------------------------------
# Данные
# ---------------------------------------------------------------------------

@dataclass
class ThermalSample:
    """Одна точка тепловых измерений."""
    time: datetime
    is_sunlit: bool
    sun_elevation_deg: float           # угол Солнца над горизонтом
    solar_flux_w_m2: float             # прямое Солнце
    albedo_flux_w_m2: float            # отражённый свет от Земли
    earth_ir_flux_w_m2: float          # тепловое излучение Земли
    total_flux_w_m2: float             # суммарный входящий поток
    equilibrium_temp_c: float          # равновесная температура поверхности
    lat_deg: Optional[float] = None
    lon_deg: Optional[float] = None
    alt_km: Optional[float] = None


@dataclass
class ThermalMetrics:
    """Агрегированные тепловые метрики окна."""

    window_id: str
    window_start: datetime
    window_end: datetime
    step_seconds: float
    n_samples: int

    # ---- Солнце -----------------------------------------------------------
    solar_flux_min_w_m2: float
    solar_flux_max_w_m2: float
    solar_flux_mean_w_m2: float
    sun_elevation_min_deg: float
    sun_elevation_max_deg: float

    # ---- Альбедо ----------------------------------------------------------
    albedo_flux_min_w_m2: float
    albedo_flux_max_w_m2: float
    albedo_flux_mean_w_m2: float

    # ---- ИК Земли ---------------------------------------------------------
    earth_ir_flux_min_w_m2: float
    earth_ir_flux_max_w_m2: float
    earth_ir_flux_mean_w_m2: float

    # ---- Суммарный поток --------------------------------------------------
    total_flux_min_w_m2: float
    total_flux_max_w_m2: float
    total_flux_mean_w_m2: float

    # ---- Температура поверхности ------------------------------------------
    temp_min_c: float
    temp_max_c: float
    temp_mean_c: float

    # ---- Тепловой запас ---------------------------------------------------
    # Пределы скафандра (по умолчанию — типичные для EVA suit)
    temp_limit_min_c: float
    temp_limit_max_c: float
    thermal_margin_min_c: float        # запас до нижнего предела
    thermal_margin_max_c: float        # запас до верхнего предела

    # ---- Время в зонах ----------------------------------------------------
    time_hot_min: float                # T > hot_threshold
    time_cold_min: float               # T < cold_threshold
    time_comfort_min: float            # между порогами
    hot_threshold_c: float
    cold_threshold_c: float

    # ---- Прочее -----------------------------------------------------------
    time_to_first_hot_min: Optional[float]
    time_to_first_cold_min: Optional[float]
    data_coverage_pct: float

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


class ThermalReport:
    """Удобная обёртка над ThermalMetrics."""

    def __init__(self, metrics: ThermalMetrics,
                 samples: Optional[List[ThermalSample]] = None):
        self.metrics = metrics
        self.samples = samples or []

    # ---- проверки ---------------------------------------------------------

    def is_safe(self,
                temp_max_threshold_c: Optional[float] = None,
                temp_min_threshold_c: Optional[float] = None,
                max_hot_min: Optional[float] = None,
                max_cold_min: Optional[float] = None) -> bool:
        """
        Проверка тепловой безопасности.
        По умолчанию использует пороги из метрик.
        """
        m = self.metrics
        tmax = temp_max_threshold_c if temp_max_threshold_c is not None \
            else m.temp_limit_max_c
        tmin = temp_min_threshold_c if temp_min_threshold_c is not None \
            else m.temp_limit_min_c

        if m.temp_max_c > tmax:
            return False
        if m.temp_min_c < tmin:
            return False
        if max_hot_min is not None and m.time_hot_min > max_hot_min:
            return False
        if max_cold_min is not None and m.time_cold_min > max_cold_min:
            return False
        return True

    def temp_at(self, dt: datetime) -> Optional[float]:
        """Равновесная температура в конкретный момент (по ближайшему сэмплу)."""
        best: Optional[ThermalSample] = None
        best_delta: Optional[float] = None
        for s in self.samples:
            delta = abs((s.time - dt).total_seconds())
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best = s
        return best.equilibrium_temp_c if best else None

    def hot_intervals(self) -> List[tuple]:
        """Интервалы, где T > hot_threshold."""
        return self._intervals(lambda s: s.equilibrium_temp_c > self.metrics.hot_threshold_c)

    def cold_intervals(self) -> List[tuple]:
        """Интервалы, где T < cold_threshold."""
        return self._intervals(lambda s: s.equilibrium_temp_c < self.metrics.cold_threshold_c)

    def _intervals(self, predicate) -> List[tuple]:
        intervals = []
        start = None
        prev = None
        for s in self.samples:
            if predicate(s):
                if start is None:
                    start = s.time
                prev = s.time
            else:
                if start is not None:
                    intervals.append((start, prev))
                    start = None
                    prev = None
        if start is not None and prev is not None:
            intervals.append((start, prev))
        return intervals

    # ---- представление ----------------------------------------------------

    def summary(self) -> str:
        m = self.metrics
        lines = [
            f"Окно: {m.window_id}  (точек: {m.n_samples}, шаг {m.step_seconds:.0f} с)",
            f"  Солнце:  min/max/mean = "
            f"{m.solar_flux_min_w_m2:7.1f} / {m.solar_flux_max_w_m2:7.1f} / "
            f"{m.solar_flux_mean_w_m2:7.1f} Вт/м²",
            f"  Альбедо: min/max/mean = "
            f"{m.albedo_flux_min_w_m2:7.1f} / {m.albedo_flux_max_w_m2:7.1f} / "
            f"{m.albedo_flux_mean_w_m2:7.1f} Вт/м²",
            f"  ИК Земли: min/max/mean = "
            f"{m.earth_ir_flux_min_w_m2:7.1f} / {m.earth_ir_flux_max_w_m2:7.1f} / "
            f"{m.earth_ir_flux_mean_w_m2:7.1f} Вт/м²",
            f"  Суммарный поток: min/max/mean = "
            f"{m.total_flux_min_w_m2:7.1f} / {m.total_flux_max_w_m2:7.1f} / "
            f"{m.total_flux_mean_w_m2:7.1f} Вт/м²",
            f"  Температура: min/max/mean = "
            f"{m.temp_min_c:7.1f} / {m.temp_max_c:7.1f} / "
            f"{m.temp_mean_c:7.1f} °C",
            f"  Пределы скафандра: [{m.temp_limit_min_c:.1f}; "
            f"{m.temp_limit_max_c:.1f}] °C",
            f"  Запас: до холода {m.thermal_margin_min_c:.1f} °C, "
            f"до жары {m.thermal_margin_max_c:.1f} °C",
            f"  Время: жарко {m.time_hot_min:.1f} мин, "
            f"холодно {m.time_cold_min:.1f} мин, "
            f"комфорт {m.time_comfort_min:.1f} мин",
        ]
        if m.time_to_first_hot_min is not None:
            lines.append(f"  До первого перегрева: {m.time_to_first_hot_min:.1f} мин")
        if m.time_to_first_cold_min is not None:
            lines.append(f"  До первого переохлаждения: {m.time_to_first_cold_min:.1f} мин")
        lines.append(f"  Покрытие данных: {m.data_coverage_pct:.1f}%")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, object]:
        return self.metrics.to_dict()


# ---------------------------------------------------------------------------
# Калькулятор
# ---------------------------------------------------------------------------

class ThermalCalculator:
    """
    Расчёт тепловых метрик по TLE через Skyfield.

    Кэширует timescale и эфемериды на уровне класса.
    """

    _ts = None
    _eph = None

    # Физические константы
    SOLAR_CONSTANT_W_M2 = 1361.0       # на 1 а.е.
    EARTH_IR_FLUX_W_M2 = 237.0         # среднее по планете (уходящее ИК)
    EARTH_ALBEDO = 0.30                # среднее альбедо Земли
    STEFAN_BOLTZMANN = 5.670374419e-8  # Вт/(м²·К⁴)
    EMISSIVITY_DEFAULT = 0.85          # эффективная излучательная способность

    # Пределы скафандра (типичные для EVA suit)
    SUIT_TEMP_LIMIT_MIN_C = -120.0
    SUIT_TEMP_LIMIT_MAX_C = 120.0

    # Пороги «жарко/холодно» для времени в зонах
    HOT_THRESHOLD_C = 50.0
    COLD_THRESHOLD_C = -50.0

    # ---------------------------------------------------------------------

    @classmethod
    def _get_ts_eph(cls):
        if not _SKYFIELD_AVAILABLE:
            raise RuntimeError("skyfield не установлен")
        if cls._ts is None:
            cls._ts = load.timescale()
        if cls._eph is None:
            cls._eph = load('de421.bsp')
        return cls._ts, cls._eph

    # ---------------------------------------------------------------------
    # Физика: потоки
    # ---------------------------------------------------------------------

    @classmethod
    def _solar_flux(cls, sun_distance_au: float) -> float:
        """Прямой солнечный поток с учётом расстояния до Солнца (Вт/м²)."""
        if sun_distance_au <= 0:
            return 0.0
        return cls.SOLAR_CONSTANT_W_M2 / (sun_distance_au ** 2)

    @classmethod
    def _albedo_flux(cls,
                     is_sunlit: bool,
                     sun_elevation_deg: float,
                     lat_deg: float) -> float:
        """
        Отражённый от Земли свет.
        Грубая эмпирика: зависит от освещённости, высоты Солнца и широты.
        """
        if not is_sunlit:
            return 0.0
        # Доля освещённой поверхности под спутником:
        # если Солнце высоко — альбедо больше, если низко — меньше.
        elev_rad = math.radians(max(0.0, sun_elevation_deg))
        geometry = math.sin(elev_rad) if elev_rad < math.pi / 2 else 1.0
        # Широтный множитель: на экваторе альбедо выше (облака), у полюсов — снег/лёд
        lat_factor = 1.0 + 0.3 * math.cos(math.radians(lat_deg))
        return (cls.SOLAR_CONSTANT_W_M2 *
                cls.EARTH_ALBEDO *
                geometry *
                lat_factor * 0.25)  # 0.25 — доля полусферы, видимой со спутника

    @classmethod
    def _earth_ir_flux(cls,
                       alt_km: float,
                       lat_deg: float) -> float:
        """
        Тепловое ИК-излучение Земли.
        Зависит от высоты (телесный угол) и широты (температура поверхности).
        """
        r_earth = 6371.0
        # Доля полусферы, видимой с высоты h:
        # sin(θ) = R / (R + h)
        sin_theta = r_earth / (r_earth + max(0.0, alt_km))
        solid_angle_factor = sin_theta ** 2
        # Широтный множитель: экватор теплее, полюса холоднее
        lat_factor = 1.0 + 0.15 * math.cos(math.radians(2.0 * lat_deg))
        return cls.EARTH_IR_FLUX_W_M2 * solid_angle_factor * lat_factor

    @classmethod
    def _equilibrium_temperature_c(cls, total_flux_w_m2: float) -> float:
        """
        Равновесная температура поверхности:
            T = (F / (ε·σ))^(1/4)
        Возвращает в °C.
        """
        if total_flux_w_m2 <= 0:
            return -273.15
        T_k = (total_flux_w_m2 /
               (cls.EMISSIVITY_DEFAULT * cls.STEFAN_BOLTZMANN)) ** 0.25
        return T_k - 273.15

    # ---------------------------------------------------------------------
    # Основной расчёт
    # ---------------------------------------------------------------------

    @classmethod
    def compute_samples(
        cls,
        window,
        tle_line1: str,
        tle_line2: str,
        step_seconds: int = 60,
        satellite_name: str = 'SAT',
    ) -> List[ThermalSample]:
        """Возвращает список тепловых сэмплов с заданным шагом."""
        if not _SKYFIELD_AVAILABLE:
            print("skyfield недоступен — расчёт тепловых метрик пропущен.")
            return []

        ts, eph = cls._get_ts_eph()
        sat = EarthSatellite(tle_line1, tle_line2, satellite_name, ts)
        sun = eph['sun']
        earth = eph['earth']

        step = timedelta(seconds=step_seconds)
        samples: List[ThermalSample] = []

        current = window.start
        while current <= window.end:
            t = ts.from_datetime(_ensure_utc(current))
            sat_at = sat.at(t)

            is_sunlit = sat_at.is_sunlit(eph)
            sub = wgs84.subpoint(sat_at)
            lat = sub.latitude.degrees
            lon = sub.longitude.degrees
            alt = sub.elevation.km

            # Солнечная элевация относительно подспутниковой точки
            observer = earth + wgs84.latlon(lat, lon)
            alt_az = observer.at(t).observe(sun).apparent().altaz()
            sun_elev = alt_az[0].degrees

            # Расстояние до Солнца (для поправки солнечной постоянной)
            sun_dist = (sun - earth).at(t).distance().au

            # Потоки
            solar_flux = cls._solar_flux(sun_dist) if is_sunlit else 0.0
            albedo_flux = cls._albedo_flux(is_sunlit, sun_elev, lat)
            earth_ir = cls._earth_ir_flux(alt, lat)
            total_flux = solar_flux + albedo_flux + earth_ir

            temp_c = cls._equilibrium_temperature_c(total_flux)

            samples.append(ThermalSample(
                time=current,
                is_sunlit=is_sunlit,
                sun_elevation_deg=sun_elev,
                solar_flux_w_m2=solar_flux,
                albedo_flux_w_m2=albedo_flux,
                earth_ir_flux_w_m2=earth_ir,
                total_flux_w_m2=total_flux,
                equilibrium_temp_c=temp_c,
                lat_deg=lat,
                lon_deg=lon,
                alt_km=alt,
            ))

            current += step

        return samples

    # ---------------------------------------------------------------------

    @classmethod
    def calculate_metrics(
        cls,
        window,
        tle_line1: str,
        tle_line2: str,
        step_seconds: int = 60,
        temp_limit_min_c: Optional[float] = None,
        temp_limit_max_c: Optional[float] = None,
        hot_threshold_c: Optional[float] = None,
        cold_threshold_c: Optional[float] = None,
        with_report: bool = False,
    ) -> "ThermalMetrics | ThermalReport":
        """Считает агрегированные тепловые метрики окна."""
        samples = cls.compute_samples(
            window, tle_line1, tle_line2,
            step_seconds=step_seconds,
        )

        if not samples:
            empty = cls._empty_metrics(window, step_seconds)
            return ThermalReport(empty) if with_report else empty

        metrics = cls._aggregate(
            window, samples, step_seconds,
            temp_limit_min_c=temp_limit_min_c,
            temp_limit_max_c=temp_limit_max_c,
            hot_threshold_c=hot_threshold_c,
            cold_threshold_c=cold_threshold_c,
        )
        return ThermalReport(metrics, samples) if with_report else metrics

    # ---------------------------------------------------------------------

    @classmethod
    def _aggregate(
        cls,
        window,
        samples: List[ThermalSample],
        step_seconds: int,
        temp_limit_min_c: Optional[float],
        temp_limit_max_c: Optional[float],
        hot_threshold_c: Optional[float],
        cold_threshold_c: Optional[float],
    ) -> ThermalMetrics:
        step_min = step_seconds / 60.0
        n = len(samples)

        def agg(values):
            return min(values), max(values), sum(values) / len(values)

        solar_min, solar_max, solar_mean = agg([s.solar_flux_w_m2 for s in samples])
        alb_min, alb_max, alb_mean = agg([s.albedo_flux_w_m2 for s in samples])
        ir_min, ir_max, ir_mean = agg([s.earth_ir_flux_w_m2 for s in samples])
        tot_min, tot_max, tot_mean = agg([s.total_flux_w_m2 for s in samples])
        t_min, t_max, t_mean = agg([s.equilibrium_temp_c for s in samples])
        elev_min, elev_max, _ = agg([s.sun_elevation_deg for s in samples])

        # Пределы и пороги
        limit_min = (temp_limit_min_c
                     if temp_limit_min_c is not None
                     else cls.SUIT_TEMP_LIMIT_MIN_C)
        limit_max = (temp_limit_max_c
                     if temp_limit_max_c is not None
                     else cls.SUIT_TEMP_LIMIT_MAX_C)
        hot_thr = (hot_threshold_c
                   if hot_threshold_c is not None
                   else cls.HOT_THRESHOLD_C)
        cold_thr = (cold_threshold_c
                    if cold_threshold_c is not None
                    else cls.COLD_THRESHOLD_C)

        # Запас
        margin_min = t_min - limit_min
        margin_max = limit_max - t_max

        # Время в зонах
        time_hot = sum(step_min for s in samples
                       if s.equilibrium_temp_c > hot_thr)
        time_cold = sum(step_min for s in samples
                        if s.equilibrium_temp_c < cold_thr)
        time_comfort = n * step_min - time_hot - time_cold

        # Время до первого события
        time_to_hot: Optional[float] = None
        time_to_cold: Optional[float] = None
        for s in samples:
            if time_to_hot is None and s.equilibrium_temp_c > hot_thr:
                time_to_hot = (s.time - window.start).total_seconds() / 60.0
            if time_to_cold is None and s.equilibrium_temp_c < cold_thr:
                time_to_cold = (s.time - window.start).total_seconds() / 60.0
            if time_to_hot is not None and time_to_cold is not None:
                break

        # Покрытие данных
        expected_n = max(1, int(((window.end - window.start).total_seconds() / 60.0) / step_min) + 1)
        coverage = min(100.0, 100.0 * n / expected_n)

        return ThermalMetrics(
            window_id=getattr(window, 'id', 'N/A'),
            window_start=window.start,
            window_end=window.end,
            step_seconds=float(step_seconds),
            n_samples=n,
            solar_flux_min_w_m2=solar_min,
            solar_flux_max_w_m2=solar_max,
            solar_flux_mean_w_m2=solar_mean,
            sun_elevation_min_deg=elev_min,
            sun_elevation_max_deg=elev_max,
            albedo_flux_min_w_m2=alb_min,
            albedo_flux_max_w_m2=alb_max,
            albedo_flux_mean_w_m2=alb_mean,
            earth_ir_flux_min_w_m2=ir_min,
            earth_ir_flux_max_w_m2=ir_max,
            earth_ir_flux_mean_w_m2=ir_mean,
            total_flux_min_w_m2=tot_min,
            total_flux_max_w_m2=tot_max,
            total_flux_mean_w_m2=tot_mean,
            temp_min_c=t_min,
            temp_max_c=t_max,
            temp_mean_c=t_mean,
            temp_limit_min_c=limit_min,
            temp_limit_max_c=limit_max,
            thermal_margin_min_c=margin_min,
            thermal_margin_max_c=margin_max,
            time_hot_min=time_hot,
            time_cold_min=time_cold,
            time_comfort_min=time_comfort,
            hot_threshold_c=hot_thr,
            cold_threshold_c=cold_thr,
            time_to_first_hot_min=time_to_hot,
            time_to_first_cold_min=time_to_cold,
            data_coverage_pct=coverage,
        )

    # ---------------------------------------------------------------------

    @staticmethod
    def _empty_metrics(window, step_seconds: int) -> ThermalMetrics:
        return ThermalMetrics(
            window_id=getattr(window, 'id', 'N/A'),
            window_start=window.start,
            window_end=window.end,
            step_seconds=float(step_seconds),
            n_samples=0,
            solar_flux_min_w_m2=0.0,
            solar_flux_max_w_m2=0.0,
            solar_flux_mean_w_m2=0.0,
            sun_elevation_min_deg=0.0,
            sun_elevation_max_deg=0.0,
            albedo_flux_min_w_m2=0.0,
            albedo_flux_max_w_m2=0.0,
            albedo_flux_mean_w_m2=0.0,
            earth_ir_flux_min_w_m2=0.0,
            earth_ir_flux_max_w_m2=0.0,
            earth_ir_flux_mean_w_m2=0.0,
            total_flux_min_w_m2=0.0,
            total_flux_max_w_m2=0.0,
            total_flux_mean_w_m2=0.0,
            temp_min_c=0.0,
            temp_max_c=0.0,
            temp_mean_c=0.0,
            temp_limit_min_c=ThermalCalculator.SUIT_TEMP_LIMIT_MIN_C,
            temp_limit_max_c=ThermalCalculator.SUIT_TEMP_LIMIT_MAX_C,
            thermal_margin_min_c=0.0,
            thermal_margin_max_c=0.0,
            time_hot_min=0.0,
            time_cold_min=0.0,
            time_comfort_min=0.0,
            hot_threshold_c=ThermalCalculator.HOT_THRESHOLD_C,
            cold_threshold_c=ThermalCalculator.COLD_THRESHOLD_C,
            time_to_first_hot_min=None,
            time_to_first_cold_min=None,
            data_coverage_pct=0.0,
        )


# ---------------------------------------------------------------------------
# Пример использования
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    from datetime import datetime

    @dataclass
    class W:
        id: str
        start: datetime
        end: datetime

    window = W(
        id="WKD-001",
        start=datetime(2025, 3, 1, 10, 0),
        end=datetime(2025, 3, 1, 11, 30),
    )

    tle1 = "1 25544U 98067A   25060.50000000  .00016717  00000+0  10270-3 0  9993"
    tle2 = "2 25544  51.6400 100.0000 0004000  90.0000 270.0000 15.50000000    12"

    # 1. Простой расчёт — метрики
    metrics = ThermalCalculator.calculate_metrics(window, tle1, tle2)
    print("Тени/свет:", metrics.temp_min_c, metrics.temp_max_c)
    print(metrics.to_dict())

    # 2. С отчётом
    report = ThermalCalculator.calculate_metrics(
        window, tle1, tle2,
        step_seconds=30,
        with_report=True,
    )
    print(report.summary())

    # 3. Проверка безопасности
    print("Тепловая безопасность:", report.is_safe())

    # 4. Интервалы жары/холода
    print("Жаркие интервалы:")
    for start, end in report.hot_intervals():
        print(f"  {start} — {end}")

    # 5. Температура в конкретный момент
    t_check = window.start + timedelta(minutes=20)
    print(f"T в {t_check}: {report.temp_at(t_check):.1f} °C")
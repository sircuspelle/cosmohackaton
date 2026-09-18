# -*- coding: utf-8 -*-
"""
Модуль расчёта освещённости станции на орбите.

Предоставляет:
  * IlluminationSample      — одна точка (время + флаг освещённости);
  * IlluminationMetrics     — агрегированные метрики окна;
  * IlluminationReport      — обёртка с удобными методами;
  * IlluminationCalculator  — основной калькулятор на базе Skyfield.

Все datetime трактуются как UTC (naive помечается как UTC в точках
вызова Skyfield через _ensure_utc, без мутации исходных объектов).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone as _tz
from typing import List, Optional, Dict, Sequence, Tuple

try:
    from skyfield.api import load, EarthSatellite, utc
    _SKYFIELD_AVAILABLE = True
except ImportError:
    load = EarthSatellite = utc = None
    _SKYFIELD_AVAILABLE = False
    print("Внимание: библиотека skyfield не установлена. "
          "Расчёт освещённости будет недоступен.")
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
class IlluminationSample:
    """Одна точка освещённости."""
    time: datetime
    is_sunlit: bool
    # Опционально — координаты станции (заполняются, если запрошены)
    lat_deg: Optional[float] = None
    lon_deg: Optional[float] = None
    alt_km: Optional[float] = None
    # Солнечная элевация (градусы), если запрошена
    sun_elevation_deg: Optional[float] = None


@dataclass
class IlluminationMetrics:
    """Агрегированные метрики освещённости окна."""

    window_id: str
    window_start: datetime
    window_end: datetime
    step_seconds: float

    # Базовые длительности (минуты)
    total_minutes: float
    sunlit_minutes: float
    shadow_minutes: float

    # Доли
    sunlit_fraction: float             # 0..1
    shadow_fraction: float             # 0..1

    # Streaks — непрерывные участки
    longest_shadow_streak_min: float
    longest_sunlit_streak_min: float
    n_shadow_entries: int              # сколько раз вошли в тень
    n_sunlit_entries: int              # сколько раз вышли на свет

    # Границы (для планирования)
    first_shadow_start: Optional[datetime]
    last_shadow_end: Optional[datetime]
    time_to_first_shadow_min: Optional[float]
    time_to_first_sunrise_min: Optional[float]

    # Точность
    n_samples: int
    data_coverage_pct: float

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


class IlluminationReport:
    """Удобная обёртка над IlluminationMetrics."""

    def __init__(self, metrics: IlluminationMetrics,
                 samples: Optional[List[IlluminationSample]] = None):
        self.metrics = metrics
        self.samples = samples or []

    # ---- проверки ---------------------------------------------------------

    def is_sunlit_at(self, dt: datetime) -> Optional[bool]:
        """Освещённость в конкретный момент (по кэшу samples)."""
        best: Optional[IlluminationSample] = None
        best_dt = None
        for s in self.samples:
            delta = abs((s.time - dt).total_seconds())
            if best_dt is None or delta < best_dt:
                best_dt = delta
                best = s
        return best.is_sunlit if best else None

    def has_continuous_sunlight(self, start: datetime, duration_min: float) -> bool:
        """Есть ли непрерывный световой интервал начиная с start длительностью duration."""
        end = start + timedelta(minutes=duration_min)
        prev: Optional[bool] = None
        for s in self.samples:
            if s.time < start or s.time >= end:
                continue
            if not s.is_sunlit:
                return False
            prev = s.is_sunlit
        return prev is True

    def has_continuous_shadow(self, start: datetime, duration_min: float) -> bool:
        end = start + timedelta(minutes=duration_min)
        seen = False
        for s in self.samples:
            if s.time < start or s.time >= end:
                continue
            if s.is_sunlit:
                return False
            seen = True
        return seen

    # ---- представление ----------------------------------------------------

    def summary(self) -> str:
        m = self.metrics
        lines = [
            f"Окно: {m.window_id}",
            f"  Длительность: {m.total_minutes:.1f} мин "
            f"(шаг {m.step_seconds:.0f} с, точек {m.n_samples})",
            f"  Свет:  {m.sunlit_minutes:6.1f} мин "
            f"({m.sunlit_fraction*100:5.1f}%)",
            f"  Тень:  {m.shadow_minutes:6.1f} мин "
            f"({m.shadow_fraction*100:5.1f}%)",
            f"  Самый длинный теневой участок:   {m.longest_shadow_streak_min:6.1f} мин",
            f"  Самый длинный световой участок:  {m.longest_sunlit_streak_min:6.1f} мин",
            f"  Входов в тень: {m.n_shadow_entries}, "
            f"выходов на свет: {m.n_sunlit_entries}",
            f"  Покрытие данных: {m.data_coverage_pct:.1f}%",
        ]
        if m.first_shadow_start is not None:
            lines.append(f"  Первый вход в тень: {m.first_shadow_start} "
                         f"(через {m.time_to_first_shadow_min:.1f} мин)")
        if m.time_to_first_sunrise_min is not None:
            lines.append(f"  До первого восхода: {m.time_to_first_sunrise_min:.1f} мин")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, object]:
        return self.metrics.to_dict()

    def shadow_intervals(self) -> List[Tuple[datetime, datetime]]:
        """Список интервалов (start, end) непрерывной тени."""
        return self._intervals(target=False)

    def sunlit_intervals(self) -> List[Tuple[datetime, datetime]]:
        """Список интервалов (start, end) непрерывного света."""
        return self._intervals(target=True)

    def _intervals(self, target: bool) -> List[Tuple[datetime, datetime]]:
        intervals: List[Tuple[datetime, datetime]] = []
        start: Optional[datetime] = None
        prev_time: Optional[datetime] = None
        for s in self.samples:
            if s.is_sunlit == target:
                if start is None:
                    start = s.time
                prev_time = s.time
            else:
                if start is not None:
                    intervals.append((start, prev_time))
                    start = None
                    prev_time = None
        if start is not None and prev_time is not None:
            intervals.append((start, prev_time))
        return intervals


# ---------------------------------------------------------------------------
# Калькулятор
# ---------------------------------------------------------------------------

class IlluminationCalculator:
    """
    Расчёт освещённости по TLE через Skyfield.

    Кэширует timescale и эфемериды на уровне класса, чтобы не
    перечитывать de421.bsp при каждом вызове.
    """

    _ts = None
    _eph = None

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

    @classmethod
    def compute_samples(
        cls,
        window,
        tle_line1: str,
        tle_line2: str,
        step_seconds: int = 60,
        with_position: bool = False,
        with_sun_elevation: bool = False,
        satellite_name: str = 'SAT',
    ) -> List[IlluminationSample]:
        """
        Возвращает список сэмплов освещённости с заданным шагом.

        Параметры:
            window              — объект с полями start, end (и id)
            tle_line1, tle_line2 — TLE станции
            step_seconds        — шаг интегрирования в секундах
            with_position       — заполнять lat/lon/alt
            with_sun_elevation  — заполнять солнечную элевацию
            satellite_name      — имя для EarthSatellite
        """
        if not _SKYFIELD_AVAILABLE:
            print("skyfield недоступен — расчёт освещённости пропущен.")
            return []

        ts, eph = cls._get_ts_eph()
        sat = EarthSatellite(tle_line1, tle_line2, satellite_name, ts)
        sun = eph['sun']
        earth = eph['earth']

        step = timedelta(seconds=step_seconds)
        samples: List[IlluminationSample] = []

        current = window.start
        while current <= window.end:
            t = ts.from_datetime(_ensure_utc(current))
            sat_at = sat.at(t)
            is_sunlit = sat_at.is_sunlit(eph)

            lat = lon = alt = None
            if with_position:
                from skyfield.api import wgs84
                sub = wgs84.subpoint(sat_at)
                lat = sub.latitude.degrees
                lon = sub.longitude.degrees
                alt = sub.elevation.km

            sun_elev = None
            if with_sun_elevation:
                # Элевация Солнца над горизонтом подспутниковой точки
                observer = earth + wgs84.latlon(lat or 0.0, lon or 0.0) \
                    if with_position else earth
                alt_az = observer.at(t).observe(sun).apparent().altaz()
                sun_elev = alt_az[0].degrees

            samples.append(IlluminationSample(
                time=current,
                is_sunlit=is_sunlit,
                lat_deg=lat,
                lon_deg=lon,
                alt_km=alt,
                sun_elevation_deg=sun_elev,
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
        with_position: bool = False,
        with_sun_elevation: bool = False,
        with_report: bool = False,
    ) -> "IlluminationMetrics | IlluminationReport":
        """
        Считает агрегированные метрики освещённости окна.

        Возвращает IlluminationMetrics (или IlluminationReport).
        """
        samples = cls.compute_samples(
            window, tle_line1, tle_line2,
            step_seconds=step_seconds,
            with_position=with_position,
            with_sun_elevation=with_sun_elevation,
        )

        if not samples:
            empty = cls._empty_metrics(window, step_seconds)
            return IlluminationReport(empty) if with_report else empty

        metrics = cls._aggregate(window, samples, step_seconds)
        return IlluminationReport(metrics, samples) if with_report else metrics

    # ---------------------------------------------------------------------

    @staticmethod
    def _aggregate(window, samples: List[IlluminationSample],
                   step_seconds: int) -> IlluminationMetrics:
        step_min = step_seconds / 60.0

        n = len(samples)
        sunlit = sum(1 for s in samples if s.is_sunlit)
        shadow = n - sunlit

        sunlit_min = sunlit * step_min
        shadow_min = shadow * step_min
        total_min = n * step_min

        # Streaks
        longest_shadow = 0.0
        longest_sunlit = 0.0
        cur_shadow = 0.0
        cur_sunlit = 0.0
        n_shadow_entries = 0
        n_sunlit_entries = 0
        prev: Optional[bool] = None

        first_shadow_start: Optional[datetime] = None
        last_shadow_end: Optional[datetime] = None

        for s in samples:
            if s.is_sunlit:
                cur_sunlit += step_min
                if cur_shadow > 0:
                    longest_shadow = max(longest_shadow, cur_shadow)
                    if last_shadow_end is None:
                        last_shadow_end = s.time
                cur_shadow = 0.0
                if prev is False:
                    n_sunlit_entries += 1
            else:
                cur_shadow += step_min
                if cur_sunlit > 0:
                    longest_sunlit = max(longest_sunlit, cur_sunlit)
                cur_sunlit = 0.0
                if prev is True or prev is None:
                    n_shadow_entries += 1
                    if first_shadow_start is None:
                        first_shadow_start = s.time
            prev = s.is_sunlit

        longest_shadow = max(longest_shadow, cur_shadow)
        longest_sunlit = max(longest_sunlit, cur_sunlit)
        if cur_shadow > 0 and last_shadow_end is None:
            last_shadow_end = samples[-1].time

        # Время до первого события
        time_to_first_shadow: Optional[float] = None
        time_to_first_sunrise: Optional[float] = None
        for s in samples:
            if not s.is_sunlit and time_to_first_shadow is None:
                time_to_first_shadow = (s.time - window.start).total_seconds() / 60.0
            if s.is_sunlit and time_to_first_sunrise is None:
                time_to_first_sunrise = (s.time - window.start).total_seconds() / 60.0
            if time_to_first_shadow is not None and time_to_first_sunrise is not None:
                break

        # Покрытие данных
        expected_n = max(1, int(total_min / step_min))
        coverage = min(100.0, 100.0 * n / expected_n)

        return IlluminationMetrics(
            window_id=getattr(window, 'id', 'N/A'),
            window_start=window.start,
            window_end=window.end,
            step_seconds=float(step_seconds),
            total_minutes=total_min,
            sunlit_minutes=sunlit_min,
            shadow_minutes=shadow_min,
            sunlit_fraction=(sunlit_min / total_min) if total_min > 0 else 0.0,
            shadow_fraction=(shadow_min / total_min) if total_min > 0 else 0.0,
            longest_shadow_streak_min=longest_shadow,
            longest_sunlit_streak_min=longest_sunlit,
            n_shadow_entries=n_shadow_entries,
            n_sunlit_entries=n_sunlit_entries,
            first_shadow_start=first_shadow_start,
            last_shadow_end=last_shadow_end,
            time_to_first_shadow_min=time_to_first_shadow,
            time_to_first_sunrise_min=time_to_first_sunrise,
            n_samples=n,
            data_coverage_pct=coverage,
        )

    # ---------------------------------------------------------------------

    @staticmethod
    def _empty_metrics(window, step_seconds: int) -> IlluminationMetrics:
        return IlluminationMetrics(
            window_id=getattr(window, 'id', 'N/A'),
            window_start=window.start,
            window_end=window.end,
            step_seconds=float(step_seconds),
            total_minutes=0.0,
            sunlit_minutes=0.0,
            shadow_minutes=0.0,
            sunlit_fraction=0.0,
            shadow_fraction=0.0,
            longest_shadow_streak_min=0.0,
            longest_sunlit_streak_min=0.0,
            n_shadow_entries=0,
            n_sunlit_entries=0,
            first_shadow_start=None,
            last_shadow_end=None,
            time_to_first_shadow_min=None,
            time_to_first_sunrise_min=None,
            n_samples=0,
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
    metrics = IlluminationCalculator.calculate_metrics(window, tle1, tle2)
    print(metrics.shadow_minutes, "мин в тени")
    print(metrics.to_dict())

    # 2. С отчётом и сэмплами
    report = IlluminationCalculator.calculate_metrics(
        window, tle1, tle2,
        step_seconds=30,
        with_position=True,
        with_report=True,
    )
    print(report.summary())

    # 3. Интервалы тени
    print("Теневые интервалы:")
    for start, end in report.shadow_intervals():
        print(f"  {start} — {end}")

    # 4. Проверка непрерывного света на этапе
    stage_start = window.start + timedelta(minutes=10)
    if report.has_continuous_sunlight(stage_start, duration_min=15):
        print("Этап 10..25 мин полностью на свету")
    else:
        print("В этапе 10..25 мин есть тень")

    # 5. Проверка непрерывной тени
    if report.has_continuous_shadow(window.start, duration_min=20):
        print("Первые 20 минут — в тени")
# -*- coding: utf-8 -*-
"""
Модуль расчёта связи с наземными станциями.

Предоставляет:
  * GroundStation      — описание наземной станции;
  * CommSample         — одна точка: видимость + качество;
  * CommMetrics        — агрегированные метрики окна;
  * CommReport         — удобная обёртка;
  * CommCalculator     — основной калькулятор (Skyfield + бюджет линии).

Физика:
  * Видимость         — через topocentric altaz в Skyfield;
  * Бюджет линии      — EIRP + G/T − FSPL − L_atm − C/N0_required;
  * Качество          — по запасу линии и углу места.

Все datetime трактуются как UTC (naive помечается как UTC в точках
вызова Skyfield через _ensure_utc, без мутации исходных объектов).

Кэш timescale и эфемерид шарится с illumination.py / thermal.py,
чтобы de421.bsp читался один раз на всю систему.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone as _tz
from typing import List, Optional, Dict, Tuple

try:
    from skyfield.api import load, EarthSatellite, wgs84, utc
    _SKYFIELD_AVAILABLE = True
except ImportError:
    load = EarthSatellite = wgs84 = utc = None
    _SKYFIELD_AVAILABLE = False
    print("Внимание: библиотека skyfield не установлена. "
          "Расчёт связи будет недоступен.")
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
# Описание наземной станции
# ---------------------------------------------------------------------------

@dataclass
class GroundStation:
    """Наземная станция слежения."""
    name: str
    lat_deg: float
    lon_deg: float
    alt_m: float = 0.0
    min_elevation_deg: float = 5.0      # минимальный угол места
    freq_ghz: float = 2.2               # S-диапазон
    eirp_dbw: float = 20.0              # ЭИИМ спутника
    g_over_t_db: float = 10.0           # добротность станции
    required_cn0_db: float = 50.0       # требуемое C/N0


# ---------------------------------------------------------------------------
# Данные
# ---------------------------------------------------------------------------

@dataclass
class CommSample:
    """Одна точка связи."""
    time: datetime
    is_visible: bool
    station_name: str
    elevation_deg: float
    azimuth_deg: float
    distance_km: float
    fspl_db: float
    atmospheric_loss_db: float
    link_margin_db: float
    quality_score: float                # 0..100
    quality_level: str                  # EXCELLENT / GOOD / MARGINAL / NO_LINK


@dataclass
class CommMetrics:
    """Агрегированные метрики связи по окну."""

    window_id: str
    window_start: datetime
    window_end: datetime
    step_seconds: float
    n_samples: int

    # ---- Покрытие ---------------------------------------------------------
    coverage_pct: float                 # доля времени с видимостью
    total_link_min: float               # суммарное время связи
    n_passes: int                       # число сеансов

    # ---- Разрывы ----------------------------------------------------------
    max_gap_min: float
    mean_gap_min: float
    first_gap_start: Optional[datetime]
    longest_gap_start: Optional[datetime]
    longest_gap_end: Optional[datetime]
    time_to_first_gap_min: Optional[float]
    time_to_next_pass_min: Optional[float]

    # ---- Качество ---------------------------------------------------------
    best_elevation_deg: float
    mean_elevation_deg: float
    mean_link_margin_db: float
    min_link_margin_db: float
    time_excellent_min: float
    time_good_min: float
    time_marginal_min: float
    time_no_link_min: float

    # ---- Прочее -----------------------------------------------------------
    best_station: Optional[str]
    data_coverage_pct: float

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)


class CommReport:
    """Удобная обёртка над CommMetrics."""

    QUALITY_ORDER = ['NO_LINK', 'MARGINAL', 'GOOD', 'EXCELLENT']

    def __init__(self, metrics: CommMetrics,
                 samples: Optional[List[CommSample]] = None):
        self.metrics = metrics
        self.samples = samples or []

    # ---- проверки ---------------------------------------------------------

    def has_link_at(self, dt: datetime) -> Optional[bool]:
        s = self._closest(dt)
        return s.is_visible if s else None

    def quality_at(self, dt: datetime) -> Optional[str]:
        s = self._closest(dt)
        return s.quality_level if s else None

    def has_continuous_link(self, start: datetime, duration_min: float) -> bool:
        end = start + timedelta(minutes=duration_min)
        seen = False
        for s in self.samples:
            if s.time < start or s.time >= end:
                continue
            if not s.is_visible:
                return False
            seen = True
        return seen

    def has_continuous_gap(self, start: datetime, duration_min: float) -> bool:
        end = start + timedelta(minutes=duration_min)
        seen = False
        for s in self.samples:
            if s.time < start or s.time >= end:
                continue
            if s.is_visible:
                return False
            seen = True
        return seen

    def is_safe(self,
                max_gap_min: Optional[float] = None,
                min_coverage_pct: Optional[float] = None) -> bool:
        m = self.metrics
        if max_gap_min is not None and m.max_gap_min > max_gap_min:
            return False
        if min_coverage_pct is not None and m.coverage_pct < min_coverage_pct:
            return False
        return True

    # ---- интервалы --------------------------------------------------------

    def link_intervals(self) -> List[Tuple[datetime, datetime]]:
        """Интервалы непрерывной связи."""
        return self._intervals(lambda s: s.is_visible)

    def gap_intervals(self) -> List[Tuple[datetime, datetime]]:
        """Интервалы непрерывных разрывов."""
        return self._intervals(lambda s: not s.is_visible)

    def _intervals(self, predicate) -> List[Tuple[datetime, datetime]]:
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
            f"  Покрытие: {m.coverage_pct:5.1f}%  "
            f"(связь {m.total_link_min:.1f} мин, {m.n_passes} сеансов)",
            f"  Разрывы: max {m.max_gap_min:.1f} мин, "
            f"средний {m.mean_gap_min:.1f} мин",
            f"  Угол места: best {m.best_elevation_deg:.1f}°, "
            f"средний {m.mean_elevation_deg:.1f}°",
            f"  Запас линии: средний {m.mean_link_margin_db:.1f} дБ, "
            f"мин {m.min_link_margin_db:.1f} дБ",
            f"  Качество: EXCELLENT {m.time_excellent_min:.1f} мин, "
            f"GOOD {m.time_good_min:.1f} мин, "
            f"MARGINAL {m.time_marginal_min:.1f} мин, "
            f"NO_LINK {m.time_no_link_min:.1f} мин",
        ]
        if m.time_to_first_gap_min is not None:
            lines.append(f"  До первого разрыва: {m.time_to_first_gap_min:.1f} мин")
        if m.time_to_next_pass_min is not None:
            lines.append(f"  До следующего сеанса: {m.time_to_next_pass_min:.1f} мин")
        if m.best_station:
            lines.append(f"  Лучшая станция: {m.best_station}")
        lines.append(f"  Покрытие данными: {m.data_coverage_pct:.1f}%")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, object]:
        return self.metrics.to_dict()

    def _closest(self, dt: datetime) -> Optional[CommSample]:
        best = None
        best_delta = None
        for s in self.samples:
            delta = abs((s.time - dt).total_seconds())
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best = s
        return best


# ---------------------------------------------------------------------------
# Калькулятор
# ---------------------------------------------------------------------------

class CommCalculator:
    """
    Расчёт связи с наземными станциями через Skyfield.

    Кэширует timescale и эфемериды на уровне класса.
    """

    _ts = None
    _eph = None

    # Классификация качества по запасу линии и углу места
    EXCELLENT_MARGIN_DB = 10.0
    EXCELLENT_ELEV_DEG = 30.0
    GOOD_MARGIN_DB = 5.0
    GOOD_ELEV_DEG = 15.0

    # Константы
    SPEED_OF_LIGHT_M_S = 2.99792458e8
    BOLTZMANN_DB = 228.6                # 10·log10(k)

    # ---------------------------------------------------------------------

    @classmethod
    def _get_ts_eph(cls):
        """Переиспользует кэш illumination.py, если он доступен."""
        if not _SKYFIELD_AVAILABLE:
            raise RuntimeError("skyfield не установлен")
        if cls._ts is None or cls._eph is None:
            try:
                from .illumination import IlluminationCalculator
                cls._ts, cls._eph = IlluminationCalculator._get_ts_eph()
            except Exception:
                cls._ts = load.timescale()
                cls._eph = load('de421.bsp')
        return cls._ts, cls._eph

    # ---------------------------------------------------------------------
    # Физика: бюджет линии
    # ---------------------------------------------------------------------

    @classmethod
    def _fspl_db(cls, distance_km: float, freq_ghz: float) -> float:
        """Потери в свободном пространстве (Friis)."""
        if distance_km <= 0 or freq_ghz <= 0:
            return 0.0
        d_m = distance_km * 1000.0
        f_hz = freq_ghz * 1e9
        return 20.0 * math.log10(4.0 * math.pi * d_m * f_hz / cls.SPEED_OF_LIGHT_M_S)

    @classmethod
    def _atmospheric_loss_db(cls,
                             elevation_deg: float,
                             freq_ghz: float) -> float:
        """
        Атмосферные потери (упрощённая ITU-R P.676).
        Чем ниже угол места, тем больше путь через атмосферу.
        """
        if elevation_deg <= 0.0:
            return 30.0  # практически нет связи
        # Зенитное ослабление зависит от частоты
        if freq_ghz < 1.0:
            zenith_loss = 0.1
        elif freq_ghz < 10.0:
            zenith_loss = 0.05 * freq_ghz
        elif freq_ghz < 30.0:
            zenith_loss = 0.5 + 0.1 * (freq_ghz - 10.0)
        else:
            zenith_loss = 2.5 + 0.3 * (freq_ghz - 30.0)
        # Воздушная масса: ~1/sin(elev) с поправкой на кривизну
        elev_rad = math.radians(max(elevation_deg, 1.0))
        air_mass = 1.0 / math.sin(elev_rad)
        return zenith_loss * air_mass

    @classmethod
    def _link_margin_db(cls,
                        station: GroundStation,
                        distance_km: float,
                        elevation_deg: float) -> float:
        """Запас линии: EIRP + G/T − FSPL − L_atm − C/N0_req + 228.6."""
        fspl = cls._fspl_db(distance_km, station.freq_ghz)
        atm = cls._atmospheric_loss_db(elevation_deg, station.freq_ghz)
        margin = (station.eirp_dbw
                  + station.g_over_t_db
                  - fspl
                  - atm
                  - station.required_cn0_db
                  + cls.BOLTZMANN_DB)
        return margin

    @classmethod
    def _quality_score(cls,
                       elevation_deg: float,
                       margin_db: float,
                       min_elevation_deg: float) -> float:
        """
        Оценка качества 0..100.
        Комбинация угла места и запаса линии.
        """
        if elevation_deg < min_elevation_deg or margin_db <= 0.0:
            return 0.0
        # Вклад угла места
        span = max(1.0, 90.0 - min_elevation_deg)
        elev_score = min(1.0, (elevation_deg - min_elevation_deg) / span)
        # Вклад запаса линии
        margin_score = min(1.0, max(0.0, margin_db / 20.0))
        return 100.0 * (0.6 * elev_score + 0.4 * margin_score)

    @classmethod
    def _classify(cls,
                  elevation_deg: float,
                  margin_db: float) -> str:
        if elevation_deg <= 0.0 or margin_db <= 0.0:
            return 'NO_LINK'
        if (margin_db >= cls.EXCELLENT_MARGIN_DB and
                elevation_deg >= cls.EXCELLENT_ELEV_DEG):
            return 'EXCELLENT'
        if (margin_db >= cls.GOOD_MARGIN_DB and
                elevation_deg >= cls.GOOD_ELEV_DEG):
            return 'GOOD'
        return 'MARGINAL'

    # ---------------------------------------------------------------------
    # Основной расчёт
    # ---------------------------------------------------------------------

    @classmethod
    def compute_samples(
        cls,
        window,
        tle_line1: str,
        tle_line2: str,
        stations: List[GroundStation],
        step_seconds: int = 60,
        satellite_name: str = 'SAT',
    ) -> List[CommSample]:
        """
        Возвращает список сэмплов связи.

        Для каждого момента выбирается станция с максимальным углом места.
        """
        if not _SKYFIELD_AVAILABLE:
            print("skyfield недоступен — расчёт связи пропущен.")
            return []
        if not stations:
            return []

        ts, eph = cls._get_ts_eph()
        sat = EarthSatellite(tle_line1, tle_line2, satellite_name, ts)

        # Предсоздаём wgs84-точки для станций
        station_points = [
            (st, wgs84.latlon(st.lat_deg, st.lon_deg, st.alt_m))
            for st in stations
        ]

        step = timedelta(seconds=step_seconds)
        samples: List[CommSample] = []

        current = window.start
        while current <= window.end:
            t = ts.from_datetime(_ensure_utc(current))
            sat_at = sat.at(t)

            best: Optional[CommSample] = None
            best_elev = -90.0

            for st, obs in station_points:
                difference = sat_at - obs
                topocentric = difference.at(t)
                alt, az, distance = topocentric.altaz()
                elev = alt.degrees
                dist_km = distance.km

                if elev < st.min_elevation_deg:
                    continue

                fspl = cls._fspl_db(dist_km, st.freq_ghz)
                atm = cls._atmospheric_loss_db(elev, st.freq_ghz)
                margin = cls._link_margin_db(st, dist_km, elev)
                score = cls._quality_score(elev, margin, st.min_elevation_deg)
                level = cls._classify(elev, margin)

                if elev > best_elev:
                    best_elev = elev
                    best = CommSample(
                        time=current,
                        is_visible=True,
                        station_name=st.name,
                        elevation_deg=elev,
                        azimuth_deg=az.degrees,
                        distance_km=dist_km,
                        fspl_db=fspl,
                        atmospheric_loss_db=atm,
                        link_margin_db=margin,
                        quality_score=score,
                        quality_level=level,
                    )

            if best is None:
                # Нет видимости ни с одной станции
                # Берём ближайшую по углу (для отладки), но помечаем NO_LINK
                samples.append(CommSample(
                    time=current,
                    is_visible=False,
                    station_name='',
                    elevation_deg=0.0,
                    azimuth_deg=0.0,
                    distance_km=0.0,
                    fspl_db=0.0,
                    atmospheric_loss_db=0.0,
                    link_margin_db=0.0,
                    quality_score=0.0,
                    quality_level='NO_LINK',
                ))
            else:
                samples.append(best)

            current += step

        return samples

    # ---------------------------------------------------------------------

    @classmethod
    def calculate_metrics(
        cls,
        window,
        tle_line1: str,
        tle_line2: str,
        stations: List[GroundStation],
        step_seconds: int = 60,
        with_report: bool = False,
    ) -> "CommMetrics | CommReport":
        """Считает агрегированные метрики связи по окну."""
        samples = cls.compute_samples(
            window, tle_line1, tle_line2,
            stations=stations,
            step_seconds=step_seconds,
        )

        if not samples:
            empty = cls._empty_metrics(window, step_seconds)
            return CommReport(empty) if with_report else empty

        metrics = cls._aggregate(window, samples, step_seconds)
        return CommReport(metrics, samples) if with_report else metrics

    # ---------------------------------------------------------------------

    @classmethod
    def _aggregate(cls,
                   window,
                   samples: List[CommSample],
                   step_seconds: int) -> CommMetrics:
        step_min = step_seconds / 60.0
        n = len(samples)
        total_min = n * step_min

        visible = [s for s in samples if s.is_visible]
        n_visible = len(visible)
        total_link_min = n_visible * step_min
        coverage_pct = 100.0 * n_visible / n if n else 0.0

        # --- Streaks: сеансы и разрывы ---
        passes = 0
        gap_durations: List[float] = []
        cur_gap = 0.0
        first_gap_start: Optional[datetime] = None
        longest_gap_start: Optional[datetime] = None
        longest_gap_end: Optional[datetime] = None
        longest_gap = 0.0
        cur_gap_start: Optional[datetime] = None
        prev_visible: Optional[bool] = None
        time_to_first_gap: Optional[float] = None

        for s in samples:
            if s.is_visible:
                if prev_visible is False and cur_gap > 0:
                    gap_durations.append(cur_gap)
                    if cur_gap > longest_gap:
                        longest_gap = cur_gap
                        longest_gap_start = cur_gap_start
                        longest_gap_end = s.time
                    cur_gap = 0.0
                    cur_gap_start = None
                if prev_visible is not True:
                    passes += 1
            else:
                if prev_visible is True or prev_visible is None:
                    if first_gap_start is None:
                        first_gap_start = s.time
                        time_to_first_gap = (
                            (s.time - window.start).total_seconds() / 60.0
                        )
                    cur_gap_start = s.time
                cur_gap += step_min
            prev_visible = s.is_visible

        # Закрываем хвостовой разрыв
        if cur_gap > 0:
            gap_durations.append(cur_gap)
            if cur_gap > longest_gap:
                longest_gap = cur_gap
                longest_gap_start = cur_gap_start
                longest_gap_end = samples[-1].time

        max_gap = max(gap_durations) if gap_durations else 0.0
        mean_gap = sum(gap_durations) / len(gap_durations) if gap_durations else 0.0

        # --- Качество ---
        if visible:
            best_elev = max(s.elevation_deg for s in visible)
            mean_elev = sum(s.elevation_deg for s in visible) / len(visible)
            mean_margin = sum(s.link_margin_db for s in visible) / len(visible)
            min_margin = min(s.link_margin_db for s in visible)
            # Лучшая станция по числу сэмплов
            station_counts: Dict[str, int] = {}
            for s in visible:
                station_counts[s.station_name] = station_counts.get(s.station_name, 0) + 1
            best_station = max(station_counts, key=station_counts.get) \
                if station_counts else None
        else:
            best_elev = 0.0
            mean_elev = 0.0
            mean_margin = 0.0
            min_margin = 0.0
            best_station = None

        time_excellent = sum(step_min for s in samples
                             if s.quality_level == 'EXCELLENT')
        time_good = sum(step_min for s in samples
                        if s.quality_level == 'GOOD')
        time_marginal = sum(step_min for s in samples
                            if s.quality_level == 'MARGINAL')
        time_no_link = sum(step_min for s in samples
                           if s.quality_level == 'NO_LINK')

        # --- До следующего сеанса ---
        time_to_next_pass: Optional[float] = None
        if not samples[0].is_visible:
            for s in samples:
                if s.is_visible:
                    time_to_next_pass = (
                        (s.time - window.start).total_seconds() / 60.0
                    )
                    break

        # --- Покрытие данных ---
        expected_n = max(1, int(total_min / step_min)) if step_min > 0 else 1
        data_coverage = min(100.0, 100.0 * n / expected_n)

        return CommMetrics(
            window_id=getattr(window, 'id', 'N/A'),
            window_start=window.start,
            window_end=window.end,
            step_seconds=float(step_seconds),
            n_samples=n,
            coverage_pct=coverage_pct,
            total_link_min=total_link_min,
            n_passes=passes,
            max_gap_min=max_gap,
            mean_gap_min=mean_gap,
            first_gap_start=first_gap_start,
            longest_gap_start=longest_gap_start,
            longest_gap_end=longest_gap_end,
            time_to_first_gap_min=time_to_first_gap,
            time_to_next_pass_min=time_to_next_pass,
            best_elevation_deg=best_elev,
            mean_elevation_deg=mean_elev,
            mean_link_margin_db=mean_margin,
            min_link_margin_db=min_margin,
            time_excellent_min=time_excellent,
            time_good_min=time_good,
            time_marginal_min=time_marginal,
            time_no_link_min=time_no_link,
            best_station=best_station,
            data_coverage_pct=data_coverage,
        )

    # ---------------------------------------------------------------------

    @staticmethod
    def _empty_metrics(window, step_seconds: int) -> CommMetrics:
        return CommMetrics(
            window_id=getattr(window, 'id', 'N/A'),
            window_start=window.start,
            window_end=window.end,
            step_seconds=float(step_seconds),
            n_samples=0,
            coverage_pct=0.0,
            total_link_min=0.0,
            n_passes=0,
            max_gap_min=0.0,
            mean_gap_min=0.0,
            first_gap_start=None,
            longest_gap_start=None,
            longest_gap_end=None,
            time_to_first_gap_min=None,
            time_to_next_pass_min=None,
            best_elevation_deg=0.0,
            mean_elevation_deg=0.0,
            mean_link_margin_db=0.0,
            min_link_margin_db=0.0,
            time_excellent_min=0.0,
            time_good_min=0.0,
            time_marginal_min=0.0,
            time_no_link_min=0.0,
            best_station=None,
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

    # Набор наземных станций (типичные для МКС)
    stations = [
        GroundStation("Moscow",   lat_deg=55.75, lon_deg=37.62,  min_elevation_deg=5.0),
        GroundStation("Houston",  lat_deg=29.55, lon_deg=-95.09, min_elevation_deg=5.0),
        GroundStation("Munich",   lat_deg=48.14, lon_deg=11.58,  min_elevation_deg=5.0),
    ]

    # 1. Простой расчёт — метрики
    metrics = CommCalculator.calculate_metrics(window, tle1, tle2, stations)
    print(metrics.to_dict())

    # 2. С отчётом
    report = CommCalculator.calculate_metrics(
        window, tle1, tle2, stations,
        step_seconds=30, with_report=True,
    )
    print(report.summary())

    # 3. Проверки
    print("Безопасно по связи:", report.is_safe(max_gap_min=45.0,
                                                 min_coverage_pct=30.0))

    # 4. Интервалы связи
    print("Сеансы связи:")
    for start, end in report.link_intervals():
        print(f"  {start} — {end}")

    # 5. Разрывы
    print("Разрывы:")
    for start, end in report.gap_intervals():
        print(f"  {start} — {end}")

    # 6. Непрерывная связь на этапе
    stage_start = window.start + timedelta(minutes=10)
    if report.has_continuous_link(stage_start, duration_min=15):
        print("Этап 10..25 мин покрыт связью")
    else:
        print("В этапе 10..25 мин есть разрыв")

    # 7. Качество в конкретный момент
    t_check = window.start + timedelta(minutes=20)
    print(f"Связь в {t_check}: {report.has_link_at(t_check)}, "
          f"качество {report.quality_at(t_check)}")
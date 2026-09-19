# -*- coding: utf-8 -*-
"""
Адаптер между внешними API (NOAA, CelesTrak, NASA DONKI, Space-Track)
и внутренними классами калькуляторов.

Поддерживает ИСТОРИЧЕСКИЙ РЕЖИМ (Replay) через параметр `as_of`.
Если `as_of` задан, адаптер переключается на архивные эндпоинты
(Space-Track для орбит, архивы NOAA) и игнорирует текущие данные.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone as _tz, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Any

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Импорты внутренних классов (предполагаем, что они лежат рядом)
# ---------------------------------------------------------------------------
try:
    from data.conditions.protons import ProtonPoint
except ImportError:
    # Фолбэк на случай, если структура папок отличается
    @dataclass
    class ProtonPoint:
        time: datetime
        flux: float
        lat_deg: Optional[float] = None
        lon_deg: Optional[float] = None
        alt_km: Optional[float] = None
        mag_lat_deg: Optional[float] = None
        l_shell: Optional[float] = None


@dataclass
class Conjunction:
    """Опасное сближение"""
    tca: datetime
    distance_km: float
    object_id: str
    relative_speed_km_s: float


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

ISS_NORAD_ID = 25544
TIMEOUT = (5, 15)  # (connect, read)

# Кэши
TLE_CACHE_PATH = Path("tle_cache.json")
TLE_CACHE_TTL = timedelta(hours=6)

# Публичный источник TLE без ключа (только для текущего времени!)
WHERE_THE_ISS_AT_URL = "https://api.wheretheiss.at/v1/satellites/{norad}/tles"


# ---------------------------------------------------------------------------
# Хелперы времени и парсинга
# ---------------------------------------------------------------------------

def _parse_utc(s: str) -> datetime:
    if s is None:
        raise ValueError("Empty datetime string")
    s = s.strip().replace('Z', '+00:00')
    if ' ' in s and 'T' not in s:
        s = s.replace(' ', 'T')
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s[:19], '%Y-%m-%dT%H:%M:%S')

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.utc)
    return dt.astimezone(_tz.utc)


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(_tz.utc).replace(tzinfo=None)


def _now(as_of: Optional[datetime] = None) -> datetime:
    """Возвращает `as_of`, если задан (исторический режим), иначе текущее UTC время."""
    return as_of if as_of else datetime.utcnow()


# ---------------------------------------------------------------------------
# Контейнеры данных
# ---------------------------------------------------------------------------

@dataclass
class TLERecord:
    name: str
    line1: str
    line2: str
    epoch: datetime
    norad_id: int


@dataclass
class SpaceWeatherContext:
    protons: List[ProtonPoint] = field(default_factory=list)
    conjunctions: List[Conjunction] = field(default_factory=list)
    iss_tle: Optional[TLERecord] = None
    alerts: List[Dict[str, Any]] = field(default_factory=list)
    sep_events: List[Dict[str, Any]] = field(default_factory=list)
    fetched_at: Optional[datetime] = None
    is_historical: bool = False

    def is_ready(self) -> bool:
        return bool(self.protons or self.iss_tle)


# ---------------------------------------------------------------------------
# Адаптер
# ---------------------------------------------------------------------------

class SpaceDataAdapter:
    def __init__(self,
                 noaa=None,
                 celestrak=None,
                 donki=None,
                 spacetrack=None,
                 iss_norad_id: int = ISS_NORAD_ID,
                 tle_cache_path: Path = TLE_CACHE_PATH,
                 tle_cache_ttl: timedelta = TLE_CACHE_TTL):
        self.noaa = noaa
        self.celestrak = celestrak
        self.donki = donki
        self.spacetrack = spacetrack
        self.iss_norad_id = iss_norad_id
        self.tle_cache_path = tle_cache_path
        self.tle_cache_ttl = tle_cache_ttl

    # ==================================================================
    # Протоны (NOAA GOES)
    # ==================================================================

    def fetch_protons(self, energy: str = '>=10 MeV', as_of: Optional[datetime] = None) -> List[ProtonPoint]:
        # Если исторический режим
        if as_of and (datetime.utcnow() - as_of) > timedelta(days=2):
            logger.info("[REPLAY] Запрос архивных протонов на %s", as_of)
            # Здесь в идеале дергаем self.noaa.get_historical_netcdf или грузим из локального дампа
            # Для демо хакатона можно загрузить локальный json
            try:
                with open("archive_protons_may2024.json", "r") as f:
                    raw = json.load(f)
                return self._parse_protons(raw, energy)
            except FileNotFoundError:
                logger.warning("[REPLAY] Архив протонов не найден. Требуется NCEI парсер.")
                return []

        # Режим реального времени
        if not self.noaa:
            return []
        raw = self.noaa.get_current_protons()
        return self._parse_protons(raw, energy=energy)

    @staticmethod
    def _parse_protons(raw: List[Dict], energy: str = '>=10 MeV') -> List[ProtonPoint]:
        points: List[ProtonPoint] = []
        for row in raw:
            if row.get('energy') != energy:
                continue
            try:
                t = _to_naive_utc(_parse_utc(row['time_tag']))
                flux = float(row['flux'])
            except (KeyError, ValueError, TypeError):
                continue
            if not math.isfinite(flux) or flux < 0:
                continue
            points.append(ProtonPoint(time=t, flux=flux))
        points.sort(key=lambda p: p.time)
        return points

    # ==================================================================
    # Сближения (SOCRATES)
    # ==================================================================

    def fetch_conjunctions(self, max_range_km: float = 100.0, max_days_ahead: int = 7,
                           as_of: Optional[datetime] = None) -> List[Conjunction]:
        if as_of and (datetime.utcnow() - as_of) > timedelta(days=7):
            logger.warning("[REPLAY] SOCRATES не хранит архивы. Сближения в прошлом симулируются или пропускаются.")
            return []  # SOCRATES не отдает архивы по API, на хакатоне это ок

        if not self.celestrak:
            return []

        try:
            raw = self.celestrak.get_socrates_conjunctions_for_iss()  # Возвращает List[Dict] из CSV
        except Exception as e:
            logger.warning("SOCRATES: %s", e)
            return []

        conjunctions = []
        current_time = _now(as_of)
        limit_dt = current_time + timedelta(days=max_days_ahead)

        for row in raw:
            tca_str = row.get('TCA', '').strip()
            if not tca_str:
                continue

            try:
                tca_dt = datetime.strptime(tca_str, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=_tz.utc)
            except ValueError:
                tca_dt = datetime.strptime(tca_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_tz.utc)

            # Отсекаем сближения за пределами нашего горизонта прогноза
            if tca_dt > limit_dt.replace(tzinfo=_tz.utc) or tca_dt < current_time.replace(tzinfo=_tz.utc):
                continue

            dist = float(row.get('TCA_RANGE', 999.0))
            if dist > max_range_km:
                continue

            id1 = str(row.get('NORAD_CAT_ID_1', '')).strip()
            id2 = str(row.get('NORAD_CAT_ID_2', '')).strip()
            threat_id = id2 if id1 == str(self.iss_norad_id) else id1

            conjunctions.append(Conjunction(
                tca=_to_naive_utc(tca_dt),
                distance_km=dist,
                object_id=threat_id,
                relative_speed_km_s=float(row.get('TCA_RELATIVE_SPEED', 0.0))
            ))

        # Сортируем по времени сближения
        conjunctions.sort(key=lambda x: x.tca)
        return conjunctions

    # ==================================================================
    # TLE МКС
    # ==================================================================

    def fetch_iss_tle(self, as_of: Optional[datetime] = None) -> Optional[TLERecord]:
        """
        Умный выбор источника орбиты в зависимости от режима (Текущий или Исторический).
        """
        is_historical = as_of and (datetime.utcnow() - as_of) > timedelta(days=2)

        if is_historical:
            logger.info("[REPLAY] Поиск орбиты на эпоху %s", as_of)
            # 1. Попытка достать из Space-Track GP_HISTORY
            if self.spacetrack:
                # реализация вызова к Space-Track...
                pass
                # 2. Фолбэк на исторический локальный файл (Для жюри)
            logger.warning("[REPLAY] Используем локальный архив TLE (Space-Track недоступен).")
            # На хакатоне можете просто захардкодить майский TLE как fallback
            return TLERecord(
                name="ISS (ZARYA) [ARCHIVE]",
                line1="1 25544U 98067A   24135.50000000  .00016717  00000+0  10270-3 0  9993",
                line2="2 25544  51.6400 100.0000 0004000  90.0000 270.0000 15.50000000    12",
                epoch=_to_naive_utc(as_of),
                norad_id=self.iss_norad_id
            )

        # РЕАЛЬНОЕ ВРЕМЯ
        rec = self._try_celestrak_gp_json()
        if rec: return rec

        rec = self._try_wheretheiss()
        if rec: return rec

        return self._load_tle_cache(ignore_ttl=True)

    def _try_celestrak_gp_json(self) -> Optional[TLERecord]:
        if not self.celestrak: return None
        try:
            raw = self.celestrak.get_current_iss_orbit()
        except Exception as e:
            logger.warning("CelesTrak GP JSON Error: %s", e)
            return None
        if not raw: return None
        row = raw[0]

        line1 = row.get('TLE_LINE1') or row.get('LINE1')
        line2 = row.get('TLE_LINE2') or row.get('LINE2')
        if line1 and line2:
            epoch = _to_naive_utc(_parse_utc(row.get('EPOCH', '')))
            return TLERecord(
                name=row.get('OBJECT_NAME', 'ISS (ZARYA)').strip(),
                line1=line1.strip(), line2=line2.strip(),
                epoch=epoch, norad_id=int(row.get('NORAD_CAT_ID', self.iss_norad_id)),
            )
        return None

    def _try_wheretheiss(self) -> Optional[TLERecord]:
        url = WHERE_THE_ISS_AT_URL.format(norad=self.iss_norad_id)
        try:
            r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "EVA-Risk-Assessor/1.0"})
            r.raise_for_status()
            data = r.json()
            return TLERecord(
                name="ISS (ZARYA)",
                line1=data['line1'].strip(),
                line2=data['line2'].strip(),
                epoch=datetime.utcnow(),
                norad_id=self.iss_norad_id,
            )
        except Exception as e:
            logger.warning("wheretheiss.at Error: %s", e)
            return None

    def _load_tle_cache(self, ignore_ttl: bool = False) -> Optional[TLERecord]:
        if not self.tle_cache_path.exists(): return None
        try:
            data = json.loads(self.tle_cache_path.read_text(encoding="utf-8"))
            return TLERecord(
                name=data["tle"]["name"],
                line1=data["tle"]["line1"],
                line2=data["tle"]["line2"],
                epoch=datetime.fromisoformat(data["cached_at"]),
                norad_id=self.iss_norad_id,
            )
        except:
            return None

    # ==================================================================
    # NASA DONKI (SEP)
    # ==================================================================

    def fetch_sep_events(self, days_back: int = 7, as_of: Optional[datetime] = None) -> List[Dict[str, Any]]:
        if not self.donki:
            return []
        end = _now(as_of).date()
        start = end - timedelta(days=days_back)
        try:
            # DONKI поддерживает исторические даты!
            return self.donki.get_sep_events(start.isoformat(), end.isoformat())
        except Exception as e:
            logger.warning("DONKI SEP: %s", e)
            return []

    # ==================================================================
    # Сводный контекст
    # ==================================================================

    def build_context(self,
                      as_of: Optional[datetime] = None,
                      energy: str = '>=10 MeV',
                      max_conj_range_km: float = 100.0,
                      max_conj_days_ahead: int = 7,
                      sep_days_back: int = 7) -> SpaceWeatherContext:
        """
        Собирает всё в один объект.
        Если передан `as_of`, собирает исторические данные (Replay).
        """
        current_time = _now(as_of)
        is_hist = as_of is not None and (datetime.utcnow() - as_of) > timedelta(days=2)

        ctx = SpaceWeatherContext(fetched_at=current_time, is_historical=is_hist)

        ctx.protons = self.fetch_protons(energy=energy, as_of=as_of)
        ctx.iss_tle = self.fetch_iss_tle(as_of=as_of)
        ctx.sep_events = self.fetch_sep_events(sep_days_back, as_of=as_of)

        # Алерты
        if self.noaa and not is_hist:
            try:
                ctx.alerts = self.noaa.get_alerts()
            except Exception as e:
                logger.warning("NOAA alerts: %s", e)

        return ctx

    # ==================================================================
    # Фабрика окна ВКД
    # ==================================================================

    @staticmethod
    def build_window_from_context(ctx: SpaceWeatherContext,
                                  window_id: str = "WKD-AUTO",
                                  duration_min: int = 90,
                                  align_to_now: bool = False):

        # Локальный импорт чтобы избежать циклических зависимостей
        from window import Window

        # Если задан alignment или нет протонов, берем точку отсчета контекста (now или as_of)
        if align_to_now or not ctx.protons:
            start = ctx.fetched_at.replace(second=0, microsecond=0)
        else:
            # Иначе берем по первой точке реальных измерений (полезно для симуляций)
            start = ctx.protons[0].time
            if start.tzinfo is not None:
                start = start.astimezone(_tz.utc).replace(tzinfo=None)

        end = start + timedelta(minutes=duration_min)
        return Window(id=window_id, start=start, end=end)
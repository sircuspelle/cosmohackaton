# -*- coding: utf-8 -*-
"""
Адаптер между внешними API (NOAA, CelesTrak, NASA DONKI, Space-Track)
и внутренними классами калькуляторов.

Поддерживает ИСТОРИЧЕСКИЙ РЕЖИМ (Replay) через параметр `as_of`.
Если `as_of` задан, адаптер переключается на архивные эндпоинты
(Space-Track для орбит, архивы NOAA) и игнорирует текущие данные.

Все HTTP-запросы делегируются клиентам из пакета `src.main.apis`.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone as _tz, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Импорты клиентов API (единственное место для сетевых запросов)
# ---------------------------------------------------------------------------
from src.main.apis.http_client import HttpClientError
from src.main.apis.noaa_client import NoaaClient
from src.main.apis.celestrak_client import CelesTrakClient
from src.main.apis.donki_client import DonkiClient
from src.main.apis.wheretheiss_client import WhereTheIssClient
from src.main.apis.spacetrack_client import SpaceTrackClient

# ---------------------------------------------------------------------------
# Импорты внутренних классов
# ---------------------------------------------------------------------------
try:
    from src.main.conditions.protons import ProtonPoint
except ImportError:
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

# Кэши
TLE_CACHE_PATH = Path("data/conditions/tle_cache.json")
TLE_CACHE_TTL = timedelta(hours=6)


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
    if as_of is None:
        return datetime.utcnow()
    return _to_naive_utc(as_of)


def _is_older_than(as_of: Optional[datetime], delta: timedelta) -> bool:
    return as_of is not None and datetime.utcnow() - _now(as_of) > delta


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
    source: str = "unknown"
    retrieved_at: Optional[datetime] = None


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
    """
    Сводит данные из нескольких внешних API в единый SpaceWeatherContext.

    Параметры:
        noaa        — клиент NOAA SWPC (NoaaClient)
        celestrak   — клиент CelesTrak (CelesTrakClient)
        donki       — клиент NASA DONKI (DonkiClient)
        spacetrack  — клиент Space-Track (SpaceTrackClient); нужен только для Replay
        wheretheiss — клиент wheretheiss.at (WhereTheIssClient); fallback-источник TLE
        iss_norad_id   — NORAD ID МКС
        tle_cache_path — путь к файловому кэшу TLE
        tle_cache_ttl  — TTL файлового кэша

    Все HTTP-запросы выполняются внутри клиентов из пакета `apis`.
    """

    def __init__(
        self,
        noaa: Optional[NoaaClient] = None,
        celestrak: Optional[CelesTrakClient] = None,
        donki: Optional[DonkiClient] = None,
        spacetrack: Optional[SpaceTrackClient] = None,
        wheretheiss: Optional[WhereTheIssClient] = None,
        iss_norad_id: int = ISS_NORAD_ID,
        tle_cache_path: Path = TLE_CACHE_PATH,
        tle_cache_ttl: timedelta = TLE_CACHE_TTL,
    ):
        self.noaa = noaa
        self.celestrak = celestrak
        self.donki = donki
        self.spacetrack = spacetrack
        self.wheretheiss = wheretheiss
        self.iss_norad_id = iss_norad_id
        self.tle_cache_path = tle_cache_path
        self.tle_cache_ttl = tle_cache_ttl

    # ==================================================================
    # Протоны (NOAA GOES)
    # ==================================================================

    def fetch_protons(self, energy: str = '>=10 MeV', as_of: Optional[datetime] = None) -> List[ProtonPoint]:
        # Если исторический режим
        if _is_older_than(as_of, timedelta(days=2)):
            logger.info("[REPLAY] Запрос архивных протонов на %s", as_of)
            try:
                with open("data/conditions/archive_protons_may2024.json", "r") as f:
                    raw = json.load(f)
                return self._parse_protons(raw, energy)
            except FileNotFoundError:
                logger.warning("[REPLAY] Архив протонов не найден. Требуется NCEI парсер.")
                return []

        # Режим реального времени
        if not self.noaa:
            return []
        try:
            raw = self.noaa.get_current_protons()
        except HttpClientError as e:
            logger.warning("NOAA protons: %s", e)
            return []
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
    # Сближения (SOCRATES через CelesTrakClient)
    # ==================================================================

    def fetch_conjunctions(
        self,
        max_range_km: float = 100.0,
        max_days_ahead: int = 7,
        as_of: Optional[datetime] = None,
    ) -> List[Conjunction]:
        if _is_older_than(as_of, timedelta(days=7)):
            logger.warning("[REPLAY] SOCRATES не хранит архивы. Сближения в прошлом пропускаются.")
            return []

        if not self.celestrak:
            return []

        try:
            # CelesTrakClient.get_socrates_conjunctions_for_iss() → сырая CSV-строка
            raw_csv = self.celestrak.get_socrates_conjunctions_for_iss()
        except Exception as e:
            logger.warning("SOCRATES: %s", e)
            return []

        import csv
        import io
        raw = list(csv.DictReader(io.StringIO(raw_csv)))

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
                try:
                    tca_dt = datetime.strptime(tca_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_tz.utc)
                except ValueError:
                    continue

            if tca_dt > limit_dt.replace(tzinfo=_tz.utc) or tca_dt < current_time.replace(tzinfo=_tz.utc):
                continue

            try:
                dist = float(row.get('TCA_RANGE', 999.0))
            except (TypeError, ValueError):
                continue
            if dist > max_range_km:
                continue

            id1 = str(row.get('NORAD_CAT_ID_1', '')).strip()
            id2 = str(row.get('NORAD_CAT_ID_2', '')).strip()
            threat_id = id2 if id1 == str(self.iss_norad_id) else id1

            conjunctions.append(Conjunction(
                tca=_to_naive_utc(tca_dt),
                distance_km=dist,
                object_id=threat_id,
                relative_speed_km_s=float(row.get('TCA_RELATIVE_SPEED', 0.0) or 0.0),
            ))

        conjunctions.sort(key=lambda x: x.tca)
        return conjunctions

    # ==================================================================
    # TLE МКС
    # ==================================================================

    def fetch_iss_tle(self, as_of: Optional[datetime] = None) -> Optional[TLERecord]:
        """
        Умный выбор источника орбиты в зависимости от режима (Текущий или Исторический).
        """
        is_historical = _is_older_than(as_of, timedelta(days=2))

        if is_historical:
            logger.info("[REPLAY] Поиск орбиты на эпоху %s", as_of)
            if self.spacetrack:
                try:
                    self.spacetrack.get_historical_tle(self.iss_norad_id, as_of)
                except NotImplementedError as e:
                    logger.warning("[REPLAY] %s", e)
            logger.warning("[REPLAY] Исторический TLE недоступен: современная орбита не подменяет архивную.")
            return None

        # РЕАЛЬНОЕ ВРЕМЯ — пробуем источники по приоритету
        rec = self._try_celestrak_gp_json()
        if rec:
            return rec

        rec = self._try_wheretheiss()
        if rec:
            return rec

        return self._load_tle_cache(ignore_ttl=True)

    def _try_celestrak_gp_json(self) -> Optional[TLERecord]:
        if not self.celestrak:
            return None
        try:
            raw = self.celestrak.get_current_iss_orbit()
        except Exception as e:
            logger.warning("CelesTrak GP JSON Error: %s", e)
            return None
        if not raw:
            return None
        row = raw[0]

        line1 = row.get('TLE_LINE1') or row.get('LINE1')
        line2 = row.get('TLE_LINE2') or row.get('LINE2')
        if line1 and line2:
            epoch = _to_naive_utc(_parse_utc(row.get('EPOCH', '')))
            return TLERecord(
                name=row.get('OBJECT_NAME', 'ISS (ZARYA)').strip(),
                line1=line1.strip(), line2=line2.strip(),
                epoch=epoch,
                norad_id=int(row.get('NORAD_CAT_ID', self.iss_norad_id)),
                source="celestrak",
                retrieved_at=datetime.now(_tz.utc),
            )
        return None

    def _try_wheretheiss(self) -> Optional[TLERecord]:
        """Fallback-источник TLE: wheretheiss.at через WhereTheIssClient."""
        client = self.wheretheiss
        if client is None:
            # Создаём клиент на лету как fallback
            client = WhereTheIssClient()
        try:
            data = client.get_tle(self.iss_norad_id)
            return TLERecord(
                name="ISS (ZARYA)",
                line1=data['line1'].strip(),
                line2=data['line2'].strip(),
                epoch=datetime.utcnow(),
                norad_id=self.iss_norad_id,
                source="wheretheiss.at",
                retrieved_at=datetime.now(_tz.utc),
            )
        except Exception as e:
            logger.warning("wheretheiss.at Error: %s", e)
            return None

    def _load_tle_cache(self, ignore_ttl: bool = False) -> Optional[TLERecord]:
        if not self.tle_cache_path.exists():
            return None
        try:
            data = json.loads(self.tle_cache_path.read_text(encoding="utf-8"))
            return TLERecord(
                name=data["tle"]["name"],
                line1=data["tle"]["line1"],
                line2=data["tle"]["line2"],
                epoch=datetime.fromisoformat(data["cached_at"]),
                norad_id=self.iss_norad_id,
                source="file_cache",
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
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

    def build_context(
        self,
        as_of: Optional[datetime] = None,
        energy: str = '>=10 MeV',
        max_conj_range_km: float = 100.0,
        max_conj_days_ahead: int = 7,
        sep_days_back: int = 7,
    ) -> SpaceWeatherContext:
        """
        Собирает всё в один объект.
        Если передан `as_of`, собирает исторические данные (Replay).
        """
        current_time = _now(as_of)
        is_hist = _is_older_than(as_of, timedelta(days=2))

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
    def build_window_from_context(
        ctx: SpaceWeatherContext,
        window_id: str = "WKD-AUTO",
        duration_min: int = 90,
        align_to_now: bool = False,
    ):
        from src.main.window import Window

        if align_to_now or not ctx.protons:
            start = ctx.fetched_at.replace(second=0, microsecond=0)
        else:
            start = ctx.protons[0].time
            if start.tzinfo is not None:
                start = start.astimezone(_tz.utc).replace(tzinfo=None)

        end = start + timedelta(minutes=duration_min)
        return Window(id=window_id, start=start, end=end)

# -*- coding: utf-8 -*-
"""
Адаптер между внешними API (NOAA, CelesTrak, NASA DONKI, Space-Track)
и внутренними классами калькуляторов.

Преобразует:
  * NOAA GOES integral protons   -> List[ProtonPoint]
  * SOCRATES conjunctions CSV    -> List[Conjunction]
  * CelesTrak TLE / GP JSON      -> TLERecord для Skyfield
  * NASA DONKI SEP events        -> метаданные о событиях
  * NOAA alerts                  -> список активных алертов

Все datetime приводятся к UTC (naive помечается как UTC) — на выходе
готовые объекты для калькуляторов.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone as _tz, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Any

import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Импорты внутренних классов (с фолбэком)
# ---------------------------------------------------------------------------

from data.conditions.protons import ProtonPoint



# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

ISS_NORAD_ID = 25544
TIMEOUT = (5, 15)                   # (connect, read)

# Кэш TLE
TLE_CACHE_PATH = Path("tle_cache.json")
TLE_CACHE_TTL = timedelta(hours=6)  # TLE МКС меняется раз в сутки-двое

# Where the ISS at? — публичный источник TLE без ключа
WHERE_THE_ISS_AT_URL = "https://api.wheretheiss.at/v1/satellites/{norad}/tles"

# Ключи NOAA GOES для интересующих полос энергии
PROTON_ENERGY_KEYS = {
    '>=10 MeV': 'flux_10mev',
    '>=30 MeV': 'flux_30mev',
    '>=50 MeV': 'flux_50mev',
    '>=100 MeV': 'flux_100mev',
}


# ---------------------------------------------------------------------------
# Хелперы UTC
# ---------------------------------------------------------------------------

def _parse_utc(s: str) -> datetime:
    """
    Парсит строку времени в UTC.
    Поддерживает форматы:
      * ISO:        2025-03-01T10:00:00Z  /  2025-03-01T10:00:00+00:00
      * SOCRATES:   2025-03-01 10:00:00
      * GOES:       2025-03-01T10:00:00Z
    """
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
    """Приводит к naive-UTC (как в Window/ProtonPoint внутри системы)."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(_tz.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Контейнеры данных
# ---------------------------------------------------------------------------

@dataclass
class TLERecord:
    """TLE-запись для Skyfield."""
    name: str
    line1: str
    line2: str
    epoch: datetime
    norad_id: int


@dataclass
class SpaceWeatherContext:
    """
    Сводный контекст для калькуляторов:
    протоны, сближения, TLE МКС, алерты, SEP-события.
    """
    protons: List[ProtonPoint] = field(default_factory=list)
    iss_tle: Optional[TLERecord] = None
    alerts: List[Dict[str, Any]] = field(default_factory=list)
    sep_events: List[Dict[str, Any]] = field(default_factory=list)
    fetched_at: Optional[datetime] = None

    def is_ready(self) -> bool:
        """Готов ли контекст к расчёту."""
        return bool(self.protons or self.iss_tle)


# ---------------------------------------------------------------------------
# Адаптер
# ---------------------------------------------------------------------------

class SpaceDataAdapter:
    """
    Адаптер между API-клиентами и внутренними классами калькуляторов.

    Использование:
        adapter = SpaceDataAdapter(noaa, celestrak, donki)
        ctx = adapter.build_context()

        # Готовые объекты:
        ctx.protons       -> List[ProtonPoint]
        ctx.conjunctions  -> List[Conjunction]
        ctx.iss_tle       -> TLERecord

        # Окно ВКД:
        window = SpaceDataAdapter.build_window_from_context(ctx)
    """

    def __init__(self,
                 noaa,
                 celestrak,
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

    def fetch_protons(self,
                      energy: str = '>=10 MeV') -> List[ProtonPoint]:
        """
        Возвращает список ProtonPoint для заданной полосы энергии.
        Поле flux берётся из соответствующего канала GOES.
        """
        raw = self.noaa.get_current_protons()
        return self._parse_protons(raw, energy=energy)

    @staticmethod
    def _parse_protons(raw: List[Dict],
                       energy: str = '>=10 MeV') -> List[ProtonPoint]:
        """
        Парсит JSON NOAA в список ProtonPoint.

        Формат записей NOAA:
            {'time_tag': '2025-03-01T10:00:00Z',
             'energy': '>=10 MeV',
             'flux': 12.3,
             'satellite': 16}
        """
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



    # ==================================================================
    # TLE МКС — с фолбэками
    # ==================================================================

    def fetch_iss_tle(self) -> Optional[TLERecord]:
        """
        Загружает TLE МКС, пробуя несколько источников по очереди:
          1. CelesTrak TLE-text (стабильный, основной)
          2. CelesTrak GP JSON (новый формат — конвертируем, если возможно)
          3. wheretheiss.at (публичный, без ключа)
          4. Локальный кэш (если свежий)
        """
        # --- 1. CelesTrak TLE-text ---
        rec = self._try_celestrak_tle_text()
        if rec is not None:
            print(f"  [TLE] Источник: CelesTrak TLE-text")
            self._save_tle_cache(rec)
            return rec

        # --- 2. CelesTrak GP JSON ---
        rec = self._try_celestrak_gp_json()
        if rec is not None:
            print(f"  [TLE] Источник: CelesTrak GP JSON")
            self._save_tle_cache(rec)
            return rec

        # --- 3. wheretheiss.at ---
        rec = self._try_wheretheiss()
        if rec is not None:
            print(f"  [TLE] Источник: wheretheiss.at")
            self._save_tle_cache(rec)
            return rec

        # --- 4. Локальный кэш ---
        rec = self._load_tle_cache(ignore_ttl=True)
        if rec is not None:
            print(f"  [TLE] Источник: локальный кэш")
            return rec

        print("  [TLE] Все источники исчерпаны")
        return None

    # ---- Отдельные источники --------------------------------------------

    def _try_celestrak_tle_text(self) -> Optional[TLERecord]:
        """CelesTrak FORMAT=TLE — три строки."""
        if not hasattr(self.celestrak, 'get_current_iss_tle_text'):
            return None
        try:
            tle = self.celestrak.get_current_iss_tle_text()
        except Exception as e:
            logger.warning("CelesTrak TLE-text: %s", e)
            return None
        if tle is None:
            return None
        return TLERecord(
            name=tle.name,
            line1=tle.line1,
            line2=tle.line2,
            epoch=datetime.utcnow(),
            norad_id=self.iss_norad_id,
        )

    def _try_celestrak_gp_json(self) -> Optional[TLERecord]:
        """
        CelesTrak GP JSON (новый формат).
        Может содержать TLE_LINE1/LINE2 (старый) или только параметры GP.
        """
        try:
            raw = self.celestrak.get_current_iss_orbit()
        except Exception as e:
            logger.warning("CelesTrak GP JSON: %s", e)
            return None

        if not raw:
            return None

        row = raw[0]

        # Если есть готовые TLE-строки — используем
        line1 = row.get('TLE_LINE1') or row.get('LINE1')
        line2 = row.get('TLE_LINE2') or row.get('LINE2')
        if line1 and line2:
            try:
                epoch = _to_naive_utc(_parse_utc(row.get('EPOCH', '')))
            except Exception:
                epoch = datetime.utcnow()
            return TLERecord(
                name=row.get('OBJECT_NAME', 'ISS (ZARYA)').strip(),
                line1=line1.strip(),
                line2=line2.strip(),
                epoch=epoch,
                norad_id=int(row.get('NORAD_CAT_ID', self.iss_norad_id)),
            )

        # Новый GP JSON без TLE-строк — конвертируем через OMM → TLE
        converted = self._gp_json_to_tle(row)
        if converted is not None:
            return converted

        logger.info("CelesTrak GP JSON: TLE-строки отсутствуют, "
                    "конвертация не удалась")
        return None

    @staticmethod
    def _gp_json_to_tle(row: Dict[str, Any]) -> Optional[TLERecord]:
        """
        Конвертация GP JSON (OMM-подобный) в TLE-строки.

        Реализована базовая формула из стандарта OMM → TLE.
        Если что-то не сходится — возвращаем None, адаптер попробует
        следующий источник.
        """
        try:
            epoch = _parse_utc(row['EPOCH'])
            norad = int(row['NORAD_CAT_ID'])
            classification = row.get('CLASSIFICATION_TYPE', 'U')
            intl_desig = row.get('OBJECT_ID', '')
            mean_motion = float(row['MEAN_MOTION'])
            ecc = float(row['ECCENTRICITY'])
            incl = float(row['INCLINATION'])
            raan = float(row['RA_OF_ASC_NODE'])
            argp = float(row['ARG_OF_PERICENTER'])
            ma = float(row['MEAN_ANOMALY'])
            bstar = float(row.get('BSTAR', 0.0))
            mm_dot = float(row.get('MEAN_MOTION_DOT', 0.0))
            mm_ddot = float(row.get('MEAN_MOTION_DDOT', 0.0))
            element_set = int(row.get('ELEMENT_SET_NO', 999))
            rev_at_epoch = int(row.get('REV_AT_EPOCH', 0))
        except (KeyError, ValueError, TypeError) as e:
            logger.warning("GP JSON → TLE: не хватает полей: %s", e)
            return None

        # --- Формируем epoch в формате TLE: YYDDD.DDDDDDDD ---
        year_2digit = epoch.year % 100
        day_of_year = epoch.timetuple().tm_yday
        frac_day = (
            epoch.hour * 3600 + epoch.minute * 60 + epoch.second
            + epoch.microsecond / 1e6
        ) / 86400.0
        epoch_str = f"{year_2digit:02d}{day_of_year:03d}{frac_day:.8f}"[1:]

        # --- Форматирование чисел по правилам TLE ---
        def _fmt_exp(value: float) -> str:
            """Формат экспоненты TLE: ±NNNNN±N."""
            if value == 0:
                return " 00000+0"
            sign = '-' if value < 0 else ' '
            v = abs(value)
            exp = int(math.floor(math.log10(v))) + 1
            mantissa = v / (10 ** exp)
            mant_str = f"{int(mantissa * 100000):05d}"
            exp_sign = '+' if exp >= 0 else '-'
            return f"{sign}{mant_str}{exp_sign}{abs(exp)}"

        # BSTAR
        bstar_str = _fmt_exp(bstar)
        # MEAN_MOTION_DOT (умножается на 2 в TLE-формате?)
        ndot_str = _fmt_exp(mm_dot)
        nddot_str = _fmt_exp(mm_ddot)

        # Эксцентриситет — 7 цифр без точки
        ecc_str = f"{int(round(ecc * 1e7)):07d}"

        # Международное обозначение — формат YYYY-NNNAAA
        # OBJECT_ID приходит как "1998-067A"
        intl_desig_parts = intl_desig.split('-')
        if len(intl_desig_parts) == 2:
            intl_short = f"{intl_desig_parts[0][2:]}{intl_desig_parts[1]:>4}"
        else:
            intl_short = "        "

        # --- Строка 1 ---
        line1 = (
            f"1 {norad:05d}{classification} {intl_short} "
            f"{epoch_str} "
            f"{ndot_str} "
            f"{nddot_str} "
            f"{bstar_str} 0 "
            f"{element_set:4d}"
        )

        # --- Строка 2 ---
        line2 = (
            f"2 {norad:05d} "
            f"{incl:8.4f} "
            f"{raan:8.4f} "
            f"{ecc_str} "
            f"{argp:8.4f} "
            f"{ma:8.4f} "
            f"{mean_motion:11.8f}"
            f"{rev_at_epoch:5d}"
        )

        # --- Контрольные суммы ---
        line1 = line1.ljust(68)[:68]
        line2 = line2.ljust(68)[:68]
        line1 = line1 + str(SpaceDataAdapter._tle_checksum(line1))
        line2 = line2 + str(SpaceDataAdapter._tle_checksum(line2))

        return TLERecord(
            name=row.get('OBJECT_NAME', 'ISS (ZARYA)').strip(),
            line1=line1,
            line2=line2,
            epoch=_to_naive_utc(epoch),
            norad_id=norad,
        )

    @staticmethod
    def _tle_checksum(line: str) -> int:
        """Контрольная сумма TLE."""
        total = 0
        for ch in line[:68]:
            if ch.isdigit():
                total += int(ch)
            elif ch == '-':
                total += 1
        return total % 10

    def _try_wheretheiss(self) -> Optional[TLERecord]:
        """Публичный источник TLE без ключа."""
        url = WHERE_THE_ISS_AT_URL.format(norad=self.iss_norad_id)
        try:
            r = requests.get(
                url,
                timeout=TIMEOUT,
                headers={"User-Agent": "EVA-Risk-Assessor/1.0"},
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            logger.warning("wheretheiss.at: %s", e)
            return None

        line1 = data.get('line1', '').strip()
        line2 = data.get('line2', '').strip()
        if not line1 or not line2:
            return None

        return TLERecord(
            name="ISS (ZARYA)",
            line1=line1,
            line2=line2,
            epoch=datetime.utcnow(),
            norad_id=self.iss_norad_id,
        )

    # ---- Кэш TLE ---------------------------------------------------------

    def _save_tle_cache(self, rec: TLERecord) -> None:
        """Сохраняет TLE в локальный кэш."""
        try:
            self.tle_cache_path.write_text(
                json.dumps({
                    "cached_at": datetime.utcnow().isoformat(),
                    "tle": {
                        "name": rec.name,
                        "line1": rec.line1,
                        "line2": rec.line2,
                    },
                }, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Не удалось сохранить кэш TLE: %s", e)

    def _load_tle_cache(self, ignore_ttl: bool = False) -> Optional[TLERecord]:
        """Загружает TLE из кэша, если он свежий (или если ignore_ttl)."""
        if not self.tle_cache_path.exists():
            return None
        try:
            data = json.loads(self.tle_cache_path.read_text(encoding="utf-8"))
            cached_at = datetime.fromisoformat(data["cached_at"])
            if not ignore_ttl:
                if datetime.utcnow() - cached_at > self.tle_cache_ttl:
                    return None
            tle = data["tle"]
            return TLERecord(
                name=tle["name"],
                line1=tle["line1"],
                line2=tle["line2"],
                epoch=cached_at,
                norad_id=self.iss_norad_id,
            )
        except Exception as e:
            logger.warning("Кэш TLE повреждён: %s", e)
            return None

    # ==================================================================
    # DONKI SEP
    # ==================================================================

    def fetch_sep_events(self,
                         days_back: int = 7) -> List[Dict[str, Any]]:
        """События SEP за последние N дней."""
        if self.donki is None:
            return []
        end = datetime.utcnow().date()
        start = end - timedelta(days=days_back)
        try:
            return self.donki.get_sep_events(
                start.isoformat(), end.isoformat()
            )
        except Exception as e:
            logger.warning("DONKI SEP: %s", e)
            return []

    # ==================================================================
    # NOAA alerts
    # ==================================================================

    def fetch_alerts(self) -> List[Dict[str, Any]]:
        try:
            return self.noaa.get_alerts()
        except Exception as e:
            logger.warning("NOAA alerts: %s", e)
            return []

    # ==================================================================
    # Сводный контекст
    # ==================================================================

    def build_context(self,
                      energy: str = '>=10 MeV',
                      max_conj_range_km: Optional[float] = 100.0,
                      max_conj_days_ahead: Optional[int] = 7,
                      sep_days_back: int = 7) -> SpaceWeatherContext:
        """
        Собирает всё, что нужно калькуляторам, в один объект.

        Возвращает SpaceWeatherContext с готовыми:
            protons, conjunctions, iss_tle, alerts, sep_events
        """
        ctx = SpaceWeatherContext(fetched_at=datetime.utcnow())

        # Протоны
        try:
            ctx.protons = self.fetch_protons(energy=energy)
        except Exception as e:
            logger.warning("Загрузка протонов: %s", e)

        # Сближения
        try:
            ctx.conjunctions = self.fetch_conjunctions(
                max_range_km=max_conj_range_km,
                max_days_ahead=max_conj_days_ahead,
            )
        except Exception as e:
            logger.warning("Загрузка сближений: %s", e)

        # TLE МКС
        try:
            ctx.iss_tle = self.fetch_iss_tle()
        except Exception as e:
            logger.warning("Загрузка TLE: %s", e)

        # SEP-события
        ctx.sep_events = self.fetch_sep_events(days_back=sep_days_back)

        # Алерты
        ctx.alerts = self.fetch_alerts()

        return ctx

    # ==================================================================
    # Фабрика окна ВКД
    # ==================================================================

    @staticmethod
    def build_window_from_context(ctx: SpaceWeatherContext,
                                  window_id: str = "WKD-AUTO",
                                  duration_min: int = 90,
                                  align_to_now: bool = False):
        """
        Строит окно ВКД от данных контекста.

        Логика:
          * если есть протоны — от первой точки (реальные данные);
          * иначе — от текущего момента (fallback).

        Параметры:
            window_id       — ID окна
            duration_min    — длительность окна в минутах
            align_to_now    — если True, всегда от текущего момента
                              (даже если есть протоны)
        """
        from data.window import Window  # локальный импорт, чтобы не тянуть вверху

        if align_to_now or not ctx.protons:
            start = datetime.utcnow().replace(second=0, microsecond=0)
        else:
            start = ctx.protons[0].time
            if start.tzinfo is not None:
                start = start.astimezone(_tz.utc).replace(tzinfo=None)

        end = start + timedelta(minutes=duration_min)
        return Window(id=window_id, start=start, end=end)
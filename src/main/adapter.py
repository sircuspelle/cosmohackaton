"""
Адаптер между внешними API (NOAA, CelesTrak, NASA DONKI, Space-Track)
и внутренними классами калькуляторов.

Поддерживает ИСТОРИЧЕСКИЙ РЕЖИМ (Replay) через параметр `as_of`.
Если `as_of` задан, адаптер переключается на архивные эндпоинты
(локальная историческая выгрузка CelesTrak, архивы NOAA/GFZ) и игнорирует текущие данные.

Все HTTP-запросы делегируются клиентам из пакета `src.main.apis`.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import timezone as _tz
from pathlib import Path
from typing import Any

from src.main.replay_archive import DEFAULT_ARCHIVE_DIR, START, END, known_at, utc, validate_mode, weather_rows

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Импорты клиентов API (единственное место для сетевых запросов)
# ---------------------------------------------------------------------------
from src.main.apis.celestrak_client import CelesTrakClient
from src.main.apis.donki_client import DonkiClient
from src.main.apis.http_client import HttpClientError
from src.main.apis.noaa_client import NoaaClient
from src.main.apis.spacetrack_client import SpaceTrackClient
from src.main.apis.wheretheiss_client import WhereTheIssClient

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
        lat_deg: float | None = None
        lon_deg: float | None = None
        alt_km: float | None = None
        mag_lat_deg: float | None = None
        l_shell: float | None = None


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
    s = s.strip().replace("Z", "+00:00")
    if " " in s and "T" not in s:
        s = s.replace(" ", "T")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.utc)
    return dt.astimezone(_tz.utc)


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(_tz.utc).replace(tzinfo=None)


def _now(as_of: datetime | None = None) -> datetime:
    """Возвращает `as_of`, если задан (исторический режим), иначе текущее UTC время."""
    if as_of is None:
        return datetime.utcnow()
    return _to_naive_utc(as_of)


def _is_older_than(as_of: datetime | None, delta: timedelta) -> bool:
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
    retrieved_at: datetime | None = None


@dataclass
class SpaceWeatherContext:
    protons: list[ProtonPoint] = field(default_factory=list)
    conjunctions: list[Conjunction] = field(default_factory=list)
    iss_tle: TLERecord | None = None
    alerts: list[dict[str, Any]] = field(default_factory=list)
    sep_events: list[dict[str, Any]] = field(default_factory=list)
    fetched_at: datetime | None = None
    is_historical: bool = False
    kp: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

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
        noaa: NoaaClient | None = None,
        celestrak: CelesTrakClient | None = None,
        donki: DonkiClient | None = None,
        spacetrack: SpaceTrackClient | None = None,
        wheretheiss: WhereTheIssClient | None = None,
        iss_norad_id: int = ISS_NORAD_ID,
        tle_cache_path: Path = TLE_CACHE_PATH,
        tle_cache_ttl: timedelta = TLE_CACHE_TTL,
        archive_dir: Path = DEFAULT_ARCHIVE_DIR,
    ):
        self.noaa = noaa
        self.celestrak = celestrak
        self.donki = donki
        self.spacetrack = spacetrack
        self.wheretheiss = wheretheiss
        self.iss_norad_id = iss_norad_id
        self.tle_cache_path = tle_cache_path
        self.tle_cache_ttl = tle_cache_ttl
        self.archive_dir = Path(archive_dir)

    # ==================================================================
    # Протоны (NOAA GOES)
    # ==================================================================

    def fetch_protons(
        self, energy: str = ">=10 MeV", as_of: datetime | None = None,
        *, replay_mode: str = "as_of", end: datetime | None = None,
    ) -> list[ProtonPoint]:
        if as_of is not None:
            raw = weather_rows(self.archive_dir, 'protons', as_of, replay_mode, end)
            return self._parse_protons(raw, energy)

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
    def _parse_protons(raw: list[dict], energy: str = ">=10 MeV") -> list[ProtonPoint]:
        points: list[ProtonPoint] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            if row.get("energy") != energy:
                continue
            try:
                t = _to_naive_utc(_parse_utc(row["time_tag"]))
                flux = float(row["flux"])
            except (KeyError, ValueError, TypeError):
                continue
            if not math.isfinite(flux) or flux < 0:
                continue
            points.append(ProtonPoint(time=t, flux=flux))
        points.sort(key=lambda p: p.time)
        return points

    def fetch_kp(self, as_of: datetime | None = None, *,
                 replay_mode: str = 'as_of', end: datetime | None = None) -> list[dict]:
        if as_of is not None:
            raw = weather_rows(self.archive_dir, 'kp', as_of, replay_mode, end)
        elif self.noaa:
            try:
                raw = self.noaa.get_kp_index()
            except Exception as exc:
                logger.warning('NOAA Kp: %s', exc)
                return []
        else:
            return []
        if raw and isinstance(raw[0], list):
            raw = [dict(zip(raw[0], row)) for row in raw[1:]]
        result = []
        for row in raw:
            try:
                timestamp = utc(row['time_tag'])
                value = float(row['kp'])
                if math.isfinite(value) and 0 <= value <= 9:
                    result.append({**row, 'time_tag': timestamp.isoformat() + 'Z', 'kp': value})
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(result, key=lambda r: r['time_tag'])

    # ==================================================================
    # Сближения (SOCRATES через CelesTrakClient)
    # ==================================================================

    def fetch_conjunctions(
        self,
        max_range_km: float = 100.0,
        max_days_ahead: int = 7,
        as_of: datetime | None = None,
    ) -> list[Conjunction]:
        if as_of is not None:
            logger.warning(
                "[REPLAY] SOCRATES не хранит архивы. Сближения в прошлом пропускаются."
            )
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
            tca_str = row.get("TCA", "").strip()
            if not tca_str:
                continue

            try:
                tca_dt = datetime.strptime(tca_str, "%Y-%m-%d %H:%M:%S.%f").replace(
                    tzinfo=_tz.utc
                )
            except ValueError:
                try:
                    tca_dt = datetime.strptime(tca_str, "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=_tz.utc
                    )
                except ValueError:
                    continue

            if tca_dt > limit_dt.replace(
                tzinfo=_tz.utc
            ) or tca_dt < current_time.replace(tzinfo=_tz.utc):
                continue

            try:
                dist = float(row.get("TCA_RANGE", 999.0))
            except (TypeError, ValueError):
                continue
            if dist > max_range_km:
                continue

            id1 = str(row.get("NORAD_CAT_ID_1", "")).strip()
            id2 = str(row.get("NORAD_CAT_ID_2", "")).strip()
            threat_id = id2 if id1 == str(self.iss_norad_id) else id1

            conjunctions.append(
                Conjunction(
                    tca=_to_naive_utc(tca_dt),
                    distance_km=dist,
                    object_id=threat_id,
                    relative_speed_km_s=float(
                        row.get("TCA_RELATIVE_SPEED", 0.0) or 0.0
                    ),
                )
            )

        conjunctions.sort(key=lambda x: x.tca)
        return conjunctions

    # ==================================================================
    # TLE МКС
    # ==================================================================

    def fetch_iss_tle(self, as_of: datetime | None = None, *,
                      replay_mode: str = 'as_of',
                      reconstruction_end: datetime | None = None) -> TLERecord | None:
        if as_of is not None:
            validate_mode(replay_mode)
            client = self.celestrak or CelesTrakClient()
            try:
                try:
                    rows = client.get_gp_history(self.iss_norad_id,
                        archive_path=self.archive_dir / 'archive_tle_may_june2024.json')
                except TypeError:
                    rows = client.get_gp_history(self.iss_norad_id, as_of)
            except Exception as exc:
                logger.warning('[REPLAY] CelesTrak archive: %s', exc)
                rows = []
            selected = self._select_historical_tle(
                rows, as_of, replay_mode, 'celestrak_gp_history', reconstruction_end
            )
            if selected:
                return selected
            # Same eligibility rules apply to fallback caches.
            try:
                raw = json.loads(self.tle_cache_path.read_text(encoding='utf-8'))
                rows = raw.get(str(self.iss_norad_id), raw.get('tle', []))
                rows = [rows] if isinstance(rows, dict) else rows
            except (OSError, ValueError, AttributeError):
                rows = []
            return self._select_historical_tle(
                rows, as_of, replay_mode, 'file_cache', reconstruction_end
            )

        # РЕАЛЬНОЕ ВРЕМЯ — пробуем источники по приоритету
        rec = self._try_celestrak_gp_json()
        if rec:
            return rec

        rec = self._try_wheretheiss()
        if rec:
            return rec

        return self._load_tle_cache(ignore_ttl=True)

    def _try_celestrak_gp_json(self) -> TLERecord | None:
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

        line1 = row.get("TLE_LINE1") or row.get("LINE1")
        line2 = row.get("TLE_LINE2") or row.get("LINE2")
        if line1 and line2:
            epoch = _to_naive_utc(_parse_utc(row.get("EPOCH", "")))
            return TLERecord(
                name=row.get("OBJECT_NAME", "ISS (ZARYA)").strip(),
                line1=line1.strip(),
                line2=line2.strip(),
                epoch=epoch,
                norad_id=int(row.get("NORAD_CAT_ID", self.iss_norad_id)),
                source="celestrak",
                retrieved_at=datetime.now(_tz.utc),
            )
        return None

    def _try_wheretheiss(self) -> TLERecord | None:
        """Fallback-источник TLE: wheretheiss.at через WhereTheIssClient."""
        client = self.wheretheiss
        if client is None:
            # Создаём клиент на лету как fallback
            client = WhereTheIssClient()
        try:
            data = client.get_tle(self.iss_norad_id)
            return TLERecord(
                name="ISS (ZARYA)",
                line1=data["line1"].strip(),
                line2=data["line2"].strip(),
                epoch=datetime.utcnow(),
                norad_id=self.iss_norad_id,
                source="wheretheiss.at",
                retrieved_at=datetime.now(_tz.utc),
            )
        except Exception as e:
            logger.warning("wheretheiss.at Error: %s", e)
            return None

    def _tle_from_row(self, row, source):
        line1 = row.get('TLE_LINE1') or row.get('LINE1') or row.get('line1')
        line2 = row.get('TLE_LINE2') or row.get('LINE2') or row.get('line2')
        if not line1 or not line2 or not line1.startswith('1 ') or not line2.startswith('2 '):
            raise ValueError('Missing or invalid TLE lines')
        # Epoch comes from the TLE itself, never from a cache retrieval timestamp.
        year = int(line1[18:20])
        year += 2000 if year < 57 else 1900
        epoch = datetime(year, 1, 1) + timedelta(days=float(line1[20:32]) - 1)
        norad = int(line1[2:7])
        if int(line2[2:7]) != norad:
            raise ValueError('TLE object IDs differ')
        retrieved = row.get('fetched_at') or row.get('cached_at')
        return TLERecord(row.get('OBJECT_NAME', row.get('name', 'ISS (ZARYA)')),
            line1, line2, epoch, norad, source,
            utc(retrieved) if retrieved else None)

    def _select_historical_tle(self, rows, as_of, mode, source, end=None):
        cutoff = utc(as_of)
        upper = utc(end) if end is not None and mode == 'reconstruction' else None
        eligible = []
        for row in rows:
            try:
                record = self._tle_from_row(row, source)
                age = cutoff - record.epoch
                if record.norad_id != self.iss_norad_id or not timedelta(0) <= age <= timedelta(days=1):
                    continue
                if upper is not None and record.epoch > upper:
                    continue
                if mode == 'as_of' and not known_at(row, cutoff):
                    continue
                eligible.append(record)
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                continue
        return max(eligible, key=lambda r: r.epoch) if eligible else None

    def _find_tle_for_epoch(self, norad_id, as_of):
        try:
            raw = json.loads(self.tle_cache_path.read_text())
            rows = raw.get(str(norad_id), raw.get('tle', []))
            rows = [rows] if isinstance(rows, dict) else rows
            return self._select_historical_tle(rows, as_of, 'as_of', 'file_cache')
        except (OSError, ValueError, AttributeError):
            return None

    def _save_tle_to_cache(self, rec):
        self.tle_cache_path.parent.mkdir(parents=True, exist_ok=True)
        raw = (
            json.loads(self.tle_cache_path.read_text())
            if self.tle_cache_path.exists()
            else {}
        )
        rows = raw.get(str(rec.norad_id), [])
        rows = [rows] if isinstance(rows, dict) else rows
        rows = [r for r in rows if r.get("epoch") != rec.epoch.isoformat()]
        rows.append(
            {
                "epoch": rec.epoch.isoformat(),
                "line1": rec.line1,
                "line2": rec.line2,
                "name": rec.name,
                "norad_id": rec.norad_id,
                "source": rec.source,
                "cached_at": datetime.now(_tz.utc).isoformat(),
            }
        )
        raw[str(rec.norad_id)] = sorted(rows, key=lambda r: r["epoch"])[-1000:]
        self.tle_cache_path.write_text(json.dumps(raw, indent=2))

    def _load_tle_cache(self, ignore_ttl: bool = False) -> TLERecord | None:
        if not self.tle_cache_path.exists():
            return None
        try:
            data = json.loads(self.tle_cache_path.read_text(encoding="utf-8"))
            rows = data.get(str(self.iss_norad_id), [])
            rows = [rows] if isinstance(rows, dict) else rows
            records = [self._tle_from_row(row, row.get("source", "file_cache"))
                       for row in rows]
            return max(records, key=lambda record: record.epoch) if records else None
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    # ==================================================================
    # NASA DONKI (SEP)
    # ==================================================================

    def fetch_sep_events(
        self, days_back: int = 7, as_of: datetime | None = None
    ) -> list[dict[str, Any]]:
        if as_of is not None:
            # DONKI archive responses can contain later revisions with no publication evidence.
            return []
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
        as_of: datetime | None = None,
        energy: str = ">=10 MeV",
        max_conj_range_km: float = 100.0,
        max_conj_days_ahead: int = 7,
        sep_days_back: int = 7,
        replay_mode: str = "as_of",
        reconstruction_end: datetime | None = None,
    ) -> SpaceWeatherContext:
        """
        Собирает всё в один объект.
        Если передан `as_of`, собирает исторические данные (Replay).
        """
        current_time = _now(as_of)
        is_hist = as_of is not None
        if is_hist:
            validate_mode(replay_mode)

        ctx = SpaceWeatherContext(fetched_at=current_time, is_historical=is_hist)

        ctx.protons = self.fetch_protons(energy=energy, as_of=as_of, replay_mode=replay_mode, end=reconstruction_end)
        ctx.kp = self.fetch_kp(as_of=as_of, replay_mode=replay_mode, end=reconstruction_end)
        ctx.iss_tle = self.fetch_iss_tle(
            as_of=as_of,
            replay_mode=replay_mode,
            reconstruction_end=reconstruction_end,
        )
        ctx.sep_events = self.fetch_sep_events(sep_days_back, as_of=as_of)

        ctx.conjunctions = self.fetch_conjunctions(max_conj_range_km, max_conj_days_ahead, as_of)
        if is_hist:
            ctx.limitations.append('SOCRATES: исторические прогнозы сближений отсутствуют; риск мусора не равен нулю.')
            ctx.limitations.append('Исторические NOAA alerts и версии DONKI на момент прогноза отсутствуют.')
            if not START <= utc(as_of) < END:
                ctx.limitations.append('Дата вне локального архива 2024-05-01 — 2024-06-30.')
            if replay_mode == 'reconstruction':
                ctx.limitations.append('Реконструкция использует ретроспективные данные; доступность на дату прогноза не доказана.')
            else:
                ctx.limitations.append('Строгий as_of исключает записи без подтверждённого времени доступности и более поздние версии.')
            if not ctx.protons:
                ctx.limitations.append('Нет допустимых протонных измерений в выбранном интервале.')
            if not ctx.kp:
                ctx.limitations.append('Нет допустимых Kp в выбранном интервале.')
            if ctx.iss_tle is None:
                ctx.limitations.append('Нет допустимой исторической TLE не старше суток; нужна выгрузка CelesTrak GP History.')
            if ctx.kp:
                ctx.limitations.append('Kp — планетарный трёхчасовой индекс, не локальный риск или доза на МКС.')
            if ctx.protons:
                ctx.limitations.append('GOES — измерения на геостационарной орбите, не доза на МКС; пропуски не интерполированы.')

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

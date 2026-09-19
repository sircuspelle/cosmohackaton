# -*- coding: utf-8 -*-
"""
Клиент NASA DONKI (Space Weather Database Of Notifications, Knowledge, Information).

Поддерживает все типы событий, используемые в проекте:
  FLR  — Solar Flare
  SEP  — Solar Energetic Particle
  CME  — Coronal Mass Ejection
  GST  — Geomagnetic Storm
  IPS  — Interplanetary Shock
  HSS  — High Speed Stream
  RBE  — Radiation Belt Enhancement
  MPC  — Magnetopause Crossing
  WSAEnlilSimulations — модель прихода CME к Земле

Все URL-ы и базовый путь централизованы здесь.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List
from urllib.parse import urlencode

from src.main.apis.http_client import HttpClient

logger = logging.getLogger(__name__)

_BASE = "https://kauai.ccmc.gsfc.nasa.gov/DONKI/WS/get"

# Карта: короткое имя → часть пути в URL DONKI
_ENDPOINTS: Dict[str, str] = {
    "FLR":                 "FLR",
    "SEP":                 "SEP",
    "CME":                 "CME",
    "GST":                 "GST",
    "IPS":                 "IPS",
    "HSS":                 "HSS",
    "RBE":                 "RBE",
    "MPC":                 "MPC",
    "WSAEnlilSimulations": "WSAEnlilSimulations",
}


def _build_url(kind: str, start_date: str, end_date: str, extra: Dict[str, str] | None = None) -> str:
    params: Dict[str, str] = {"startDate": start_date, "endDate": end_date}
    if extra:
        params.update(extra)
    path = _ENDPOINTS[kind]
    return f"{_BASE}/{path}?{urlencode(params)}"


class DonkiClient:
    """
    HTTP-клиент NASA DONKI.

    Параметры:
        http — экземпляр HttpClient
    """

    def __init__(self, http: HttpClient | None = None) -> None:
        self._http = http or HttpClient()

    # ------------------------------------------------------------------
    # Вспомогательный метод
    # ------------------------------------------------------------------

    def _fetch(self, kind: str, start_date: str, end_date: str,
               extra: Dict[str, str] | None = None) -> List[Any]:
        url = _build_url(kind, start_date, end_date, extra)
        logger.debug("DONKI: fetching %s from %s", kind, url)
        result = self._http.get_json(url)
        # DONKI может вернуть null при пустом диапазоне
        return result if isinstance(result, list) else []

    def get_url(self, kind: str, start_date: str, end_date: str,
                extra: Dict[str, str] | None = None) -> str:
        """Возвращает URL без выполнения запроса (нужен events/service.py для specs)."""
        return _build_url(kind, start_date, end_date, extra)

    # ------------------------------------------------------------------
    # Публичные методы
    # ------------------------------------------------------------------

    def get_sep_events(self, start_date: str, end_date: str) -> List[Any]:
        """Solar Energetic Particle events."""
        return self._fetch("SEP", start_date, end_date)

    def get_flr_events(self, start_date: str, end_date: str) -> List[Any]:
        """Solar Flare events."""
        return self._fetch("FLR", start_date, end_date)

    def get_cme_events(self, start_date: str, end_date: str) -> List[Any]:
        """Coronal Mass Ejection events."""
        return self._fetch("CME", start_date, end_date)

    def get_gst_events(self, start_date: str, end_date: str) -> List[Any]:
        """Geomagnetic Storm events."""
        return self._fetch("GST", start_date, end_date)

    def get_ips_events(self, start_date: str, end_date: str) -> List[Any]:
        """Interplanetary Shock events (только Earth)."""
        return self._fetch("IPS", start_date, end_date, extra={"location": "Earth"})

    def get_hss_events(self, start_date: str, end_date: str) -> List[Any]:
        """High Speed Stream events."""
        return self._fetch("HSS", start_date, end_date)

    def get_rbe_events(self, start_date: str, end_date: str) -> List[Any]:
        """Radiation Belt Enhancement events."""
        return self._fetch("RBE", start_date, end_date)

    def get_mpc_events(self, start_date: str, end_date: str) -> List[Any]:
        """Magnetopause Crossing events."""
        return self._fetch("MPC", start_date, end_date)

    def get_wsa_enlil(self, start_date: str, end_date: str) -> List[Any]:
        """WSA-Enlil Solar Wind Prediction (прогноз прихода CME к Земле)."""
        return self._fetch("WSAEnlilSimulations", start_date, end_date)

    def get_all_kinds(self, start_date: str, end_date: str) -> Dict[str, List[Any]]:
        """
        Загружает все типы событий за указанный диапазон дат.

        Возвращает словарь {kind: [events]}.
        Ошибки для отдельных типов логируются и не прерывают загрузку остальных.
        """
        result: Dict[str, List[Any]] = {}
        for kind in _ENDPOINTS:
            try:
                extra = {"location": "Earth"} if kind == "IPS" else None
                result[kind] = self._fetch(kind, start_date, end_date, extra)
            except Exception as exc:
                logger.warning("DONKI %s: %s", kind, exc)
                result[kind] = []
        return result


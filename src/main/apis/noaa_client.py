# -*- coding: utf-8 -*-
"""
Клиент NOAA Space Weather Prediction Center (SWPC).

Предоставляет доступ к:
  * Потоку протонов GOES (integral-protons-1-day)
  * Рентгеновскому потоку GOES (xrays-1-day)
  * Планетарному Kp-индексу (noaa-planetary-k-index-forecast)
  * Оперативным алертам SWPC (alerts.json)

Все URL-ы централизованы здесь — менять адреса нужно только в этом файле.
"""

from __future__ import annotations

import logging
from typing import Any, List

from src.main.apis.http_client import HttpClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# URL-ы NOAA SWPC
# ---------------------------------------------------------------------------
_BASE = "https://services.swpc.noaa.gov"
URL_PROTONS  = f"{_BASE}/json/goes/primary/integral-protons-1-day.json"
URL_XRAYS    = f"{_BASE}/json/goes/primary/xrays-1-day.json"
URL_KP       = f"{_BASE}/products/noaa-planetary-k-index-forecast.json"
URL_ALERTS   = f"{_BASE}/products/alerts.json"


class NoaaClient:
    """
    HTTP-клиент NOAA SWPC.

    Параметры:
        http — экземпляр HttpClient (если не передан, создаётся с дефолтными настройками)
    """

    def __init__(self, http: HttpClient | None = None) -> None:
        self._http = http or HttpClient()

    # ------------------------------------------------------------------

    def get_current_protons(self) -> List[Any]:
        """
        Текущие данные потока протонов GOES (>=10 MeV, >=50 MeV, >=100 MeV...).

        Возвращает список словарей вида:
            {"time_tag": "...", "flux": ..., "energy": ">=10 MeV", "satellite": 18}
        """
        logger.debug("NOAA: fetching proton flux from %s", URL_PROTONS)
        return self._http.get_json(URL_PROTONS)

    def get_current_xrays(self) -> List[Any]:
        """
        Текущие данные рентгеновского потока GOES.

        Возвращает список словарей вида:
            {"time_tag": "...", "flux": ..., "energy": "0.1-0.8nm", "satellite": 18}
        """
        logger.debug("NOAA: fetching X-ray flux from %s", URL_XRAYS)
        return self._http.get_json(URL_XRAYS)

    def get_kp_index(self) -> List[Any]:
        """
        Прогноз планетарного Kp-индекса.

        Возвращает список (первый элемент — заголовки, далее — строки данных),
        либо список словарей — зависит от версии API.
        """
        logger.debug("NOAA: fetching Kp index from %s", URL_KP)
        return self._http.get_json(URL_KP)

    def get_alerts(self) -> List[Any]:
        """
        Оперативные алерты SWPC (WATCH, WARNING, ALERT, SUMMARY...).

        Возвращает список словарей вида:
            {"product_id": "...", "issue_datetime": "...", "message": "..."}
        """
        logger.debug("NOAA: fetching alerts from %s", URL_ALERTS)
        return self._http.get_json(URL_ALERTS)


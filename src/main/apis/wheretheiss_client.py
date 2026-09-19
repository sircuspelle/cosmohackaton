# -*- coding: utf-8 -*-
"""
Клиент wheretheiss.at — публичный API TLE без аутентификации.

Раньше HTTP-запрос был встроен прямо в SpaceDataAdapter._try_wheretheiss().
Теперь всё сетевое взаимодействие сосредоточено здесь.

Документация: https://wheretheiss.at/w/developer
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from src.main.apis.http_client import HttpClient

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.wheretheiss.at/v1/satellites/{norad_id}/tles"


class WhereTheIssClient:
    """
    Клиент wheretheiss.at для получения TLE по NORAD ID.

    Параметры:
        http — экземпляр HttpClient
    """

    def __init__(self, http: HttpClient | None = None) -> None:
        self._http = http or HttpClient()

    # ------------------------------------------------------------------

    def get_tle(self, norad_id: int) -> Dict[str, Any]:
        """
        Получает TLE для объекта с указанным NORAD ID.

        Возвращает словарь с ключами:
            {
                "name": "...",
                "line1": "1 25544U ...",
                "line2": "2 25544 ..."
            }

        Raises:
            HttpClientError — при сетевых или HTTP-ошибках.
        """
        url = _BASE_URL.format(norad_id=norad_id)
        logger.debug("WhereTheISS: fetching TLE for NORAD %d from %s", norad_id, url)
        data = self._http.get_json(url)
        return data

    def get_iss_tle(self) -> Dict[str, Any]:
        """Получает TLE для МКС (NORAD 25544). Удобный алиас."""
        return self.get_tle(25544)


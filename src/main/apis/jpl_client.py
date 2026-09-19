# -*- coding: utf-8 -*-
"""
Клиент JPL Small-Body Close Approach Data (SSD CAD API).

Предоставляет доступ к данным о сближениях малых тел (астероиды, кометы)
с Землёй. Используется в events/service.py как источник `jpl_cad`.

Документация: https://ssd-api.jpl.nasa.gov/doc/cad.html

ВАЖНО: JPL CAD возвращает сближения с ЗЕМЛЁЙ, а не с МКС.
Это контекстный источник, а не прямая угроза — см. комментарий в events/adapters.py.
Временна́я шкала в данных — TDB (Barycentric Dynamical Time), не UTC.
"""

from __future__ import annotations

import logging
from typing import Any, Dict
from urllib.parse import urlencode

from src.main.apis.http_client import HttpClient

logger = logging.getLogger(__name__)

_BASE_URL = "https://ssd-api.jpl.nasa.gov/cad.api"


class JplClient:
    """
    Клиент JPL SSD Close Approach Data API.

    Параметры:
        http — экземпляр HttpClient
    """

    def __init__(self, http: HttpClient | None = None) -> None:
        self._http = http or HttpClient()

    # ------------------------------------------------------------------

    def get_close_approaches(
        self,
        date_min: str,
        date_max: str,
        body: str = "Earth",
        dist_max: str = "0.05",
    ) -> Dict[str, Any]:
        """
        Запрашивает данные о сближениях малых тел с указанным телом Солнечной системы.

        Параметры:
            date_min  — начало диапазона (формат YYYY-MM-DD)
            date_max  — конец диапазона (формат YYYY-MM-DD)
            body      — тело, с которым проверяется сближение (по умолчанию 'Earth')
            dist_max  — максимальное расстояние в а.е. (по умолчанию 0.05)

        Возвращает словарь с ключами:
            signature  — {"version": "1.5", ...}
            count      — число записей
            fields     — список имён полей
            data       — список строк (каждая — одно сближение)

        Raises:
            HttpClientError — при сетевых или HTTP-ошибках.
        """
        params = {
            "date-min": date_min,
            "date-max": date_max,
            "body": body,
            "dist-max": dist_max,
        }
        url = f"{_BASE_URL}?{urlencode(params)}"
        logger.debug("JPL CAD: fetching close approaches from %s", url)
        return self._http.get_json(url)

    def build_url(
        self,
        date_min: str,
        date_max: str,
        body: str = "Earth",
        dist_max: str = "0.05",
    ) -> str:
        """
        Возвращает URL без выполнения запроса.
        Используется events/service.py для формирования specs.
        """
        params = {
            "date-min": date_min,
            "date-max": date_max,
            "body": body,
            "dist-max": dist_max,
        }
        return f"{_BASE_URL}?{urlencode(params)}"


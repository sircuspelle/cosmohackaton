# -*- coding: utf-8 -*-
"""
Клиент Space-Track.org (www.space-track.org).

Используется для получения исторических TLE (GP_HISTORY)
в режиме Replay в SpaceDataAdapter.

Аутентификация через cookies (логин/пароль), поэтому клиент
поддерживает сессию. Требует наличия аккаунта на space-track.org.

ВАЖНО: В текущей реализации проекта (хакатон) этот клиент является
заглушкой — исторические TLE не реализованы. Структура сохранена
для будущей интеграции.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.main.apis.http_client import HttpClient

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.space-track.org"
_LOGIN_URL = f"{_BASE_URL}/ajaxauth/login"
_QUERY_URL = f"{_BASE_URL}/basicspacedata/query"


class SpaceTrackClient:
    """
    Клиент Space-Track.org.

    Параметры:
        username  — логин Space-Track (обязателен для реальных запросов)
        password  — пароль Space-Track
        http      — экземпляр HttpClient (для базовых запросов)

    Примечание:
        Space-Track использует cookie-сессию, которая не поддерживается
        базовым HttpClient. Для полной интеграции нужен requests.Session
        или http.cookiejar. Текущая реализация — скелет.
    """

    def __init__(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        http: HttpClient | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._http = http or HttpClient()
        self._session_cookies: Optional[str] = None

    # ------------------------------------------------------------------

    def get_current_tle(self, norad_id: int) -> List[Dict[str, Any]]:
        """
        Получает текущий GP (TLE) для объекта по NORAD ID.

        Возвращает список с одним словарём, содержащим поля:
            OBJECT_NAME, NORAD_CAT_ID, EPOCH, TLE_LINE1, TLE_LINE2, ...
        """
        logger.warning(
            "SpaceTrackClient: текущие TLE следует получать из CelesTrak (GP JSON). "
            "Space-Track нужен только для исторических данных."
        )
        return self._gp_query(norad_id, limit=1)

    def get_historical_tle(
        self,
        norad_id: int,
        epoch: datetime,
        tolerance_days: int = 3,
    ) -> List[Dict[str, Any]]:
        """
        Возвращает список TLE из GP_HISTORY вокруг указанной эпохи.

        Параметры:
            norad_id       — NORAD ID объекта
            epoch          — целевая эпоха (naive UTC или aware)
            tolerance_days — окно поиска ±N дней вокруг эпохи

        Raises:
            NotImplementedError — если нет credentials (заглушка для хакатона).
        """
        if not self._username or not self._password:
            raise NotImplementedError(
                "Space-Track credentials не заданы. "
                "Для исторических TLE необходим аккаунт на space-track.org."
            )

        from datetime import timedelta
        naive_epoch = epoch.replace(tzinfo=None) if epoch.tzinfo else epoch
        date_from = (naive_epoch - timedelta(days=tolerance_days)).strftime("%Y-%m-%d")
        date_to = (naive_epoch + timedelta(days=tolerance_days)).strftime("%Y-%m-%d")

        logger.info(
            "SpaceTrackClient: запрос GP_HISTORY NORAD=%d эпоха %s..%s",
            norad_id, date_from, date_to,
        )
        # TODO: реализовать полноценную cookie-сессию через requests.Session
        raise NotImplementedError(
            "Space-Track GP_HISTORY: полная реализация требует cookie-сессии. "
            "Используйте CelesTrakClient для текущих TLE."
        )

    # ------------------------------------------------------------------
    # Внутренние методы (скелет)
    # ------------------------------------------------------------------

    def _gp_query(self, norad_id: int, limit: int = 1) -> List[Dict[str, Any]]:
        """
        Базовый запрос к GP endpoint Space-Track.
        Требует активной сессии (_session_cookies).
        """
        if not self._session_cookies:
            raise RuntimeError(
                "SpaceTrackClient: сессия не установлена. "
                "Вызовите _login() или используйте CelesTrakClient."
            )
        url = (
            f"{_QUERY_URL}/class/gp/NORAD_CAT_ID/{norad_id}"
            f"/orderby/EPOCH%20desc/limit/{limit}/format/json"
        )
        logger.debug("SpaceTrack: querying %s", url)
        # Реальный запрос потребует заголовка Cookie: _session_cookies
        raise NotImplementedError("SpaceTrackClient._gp_query: cookie-сессия не реализована.")


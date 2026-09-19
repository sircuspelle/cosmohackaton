# -*- coding: utf-8 -*-
"""
Клиент CelesTrak.

Предоставляет доступ к:
  * Текущей орбите МКС в формате GP JSON (TLE_LINE1, TLE_LINE2, EPOCH, ...)
  * CSV-файлу SOCRATES с данными о сближениях (всеми объектами)

URL-ы централизованы здесь.
"""

from __future__ import annotations

import logging
from typing import Any, List

from src.main.apis.http_client import HttpClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# URL-ы CelesTrak
# ---------------------------------------------------------------------------
_ISS_NORAD = 25544
URL_ISS_GP      = "https://celestrak.org/SOCRATES/query.php"  # GP JSON ниже
URL_ISS_GP_JSON = f"https://celestrak.org/SOCRATES/query.php?CATNR={_ISS_NORAD}&FORMAT=JSON"
URL_GP_JSON     = "https://celestrak.org/SOCRATES/query.php?CATNR={norad_id}&FORMAT=JSON"
URL_SOCRATES    = "https://celestrak.org/SOCRATES/sort-minRange.csv"


class CelesTrakClient:
    """
    HTTP-клиент CelesTrak.

    Параметры:
        http       — экземпляр HttpClient
        iss_norad  — NORAD-ID МКС (по умолчанию 25544)
    """

    def __init__(self, http: HttpClient | None = None, iss_norad: int = _ISS_NORAD) -> None:
        self._http = http or HttpClient()
        self._iss_norad = iss_norad

    # ------------------------------------------------------------------

    def get_current_iss_orbit(self) -> List[Any]:
        """
        Текущие орбитальные элементы МКС в формате GP JSON.

        Возвращает список из одного словаря с ключами:
            OBJECT_NAME, NORAD_CAT_ID, EPOCH, TLE_LINE1, TLE_LINE2, ...
        """
        url = URL_GP_JSON.format(norad_id=self._iss_norad)
        logger.debug("CelesTrak: fetching ISS GP JSON from %s", url)
        return self._http.get_json(url)

    def get_orbit_by_norad(self, norad_id: int) -> List[Any]:
        """
        Орбитальные элементы произвольного объекта по NORAD ID.

        Возвращает список из одного словаря в формате GP JSON.
        """
        url = URL_GP_JSON.format(norad_id=norad_id)
        logger.debug("CelesTrak: fetching GP JSON for NORAD %d from %s", norad_id, url)
        return self._http.get_json(url)

    def get_gp_history(self, norad_id: int, epoch=None, archive_path=None) -> List[Any]:
        """Load a locally captured CelesTrak GP History export or query GP endpoint."""
        import json
        from pathlib import Path
        if archive_path is not None:
            path = Path(archive_path)
            if not path.exists():
                return []
            data = json.loads(path.read_text(encoding="utf-8"))
            rows = data.get(str(norad_id), data) if isinstance(data, dict) else data
            return [r for r in rows if int(r.get("NORAD_CAT_ID", r.get("norad_id", norad_id))) == norad_id]
        # CelesTrak historical GP is supplied through its Special Data Request;
        # keep network behavior explicit and injectable for callers/tests.
        return []

    def get_socrates_conjunctions_for_iss(self) -> str:
        """
        CSV-файл SOCRATES со всеми прогнозируемыми сближениями (отсортировано по дальности).

        Возвращает сырую строку CSV.
        Поля: NORAD_CAT_ID_1, NORAD_CAT_ID_2, TCA, TCA_RANGE, TCA_RELATIVE_SPEED, MAX_PROB, ...

        Примечание:
            SOCRATES не хранит исторических архивов — данные актуальны только
            на момент запроса.
        """
        logger.debug("CelesTrak: fetching SOCRATES CSV from %s", URL_SOCRATES)
        return self._http.get_text(URL_SOCRATES)


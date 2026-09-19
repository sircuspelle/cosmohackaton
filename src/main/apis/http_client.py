# -*- coding: utf-8 -*-
"""
Базовый HTTP-клиент.

Все остальные клиенты в пакете apis используют этот класс для
выполнения сетевых запросов. Инкапсулирует:
  * Единый User-Agent для всего проекта
  * Retry с экспоненциальным back-off
  * Таймаут (connect + read)
  * Поддержку requests (если установлен) и urllib (fallback)
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from typing import Any, Optional, Union
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _requests = None
    _HAS_REQUESTS = False

USER_AGENT = "EVA-Risk-Assessor/1.0"
DEFAULT_TIMEOUT = (5, 15)   # (connect, read) в секундах
DEFAULT_RETRIES = 1
DEFAULT_MAX_BYTES = 25_000_000
_RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


class HttpClientError(IOError):
    """Ошибка HTTP-запроса (сетевая или протокольная)."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class HttpClient:
    """
    Базовый HTTP-клиент с retry и поддержкой requests/urllib.

    Параметры:
        timeout       — кортеж (connect_sec, read_sec) или единое число
        retries       — число повторных попыток при retryable-ошибках
        max_bytes     — максимальный размер ответа в байтах
        user_agent    — значение заголовка User-Agent
    """

    def __init__(
        self,
        timeout: Union[tuple, float] = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.max_bytes = max_bytes
        self.user_agent = user_agent

    # ------------------------------------------------------------------
    # Публичные методы
    # ------------------------------------------------------------------

    def get_text(self, url: str, headers: Optional[dict] = None) -> str:
        """
        GET-запрос; возвращает тело ответа как строку (UTF-8).

        Raises:
            HttpClientError — при сетевых ошибках или HTTP 4xx/5xx.
        """
        return self._request(url, extra_headers=headers or {})

    def get_json(self, url: str, headers: Optional[dict] = None) -> Any:
        """
        GET-запрос; парсит JSON и возвращает Python-объект.

        Raises:
            HttpClientError — при сетевых ошибках или HTTP 4xx/5xx.
            json.JSONDecodeError — если ответ не является валидным JSON.
        """
        text = self.get_text(url, headers=headers)
        return json.loads(text)

    # ------------------------------------------------------------------
    # Внутренняя реализация
    # ------------------------------------------------------------------

    def _request(self, url: str, extra_headers: dict) -> str:
        all_headers = {"User-Agent": self.user_agent, "Accept": "application/json,text/csv,text/plain,*/*"}
        all_headers.update(extra_headers)

        last_error: Optional[str] = None
        for attempt in range(self.retries + 1):
            if attempt > 0:
                sleep_time = min(2 ** (attempt - 1), 4)
                logger.debug("Retry %d/%d for %s (sleep %.1fs)", attempt, self.retries, url, sleep_time)
                time.sleep(sleep_time)

            try:
                return self._do_request(url, all_headers)
            except HttpClientError as exc:
                last_error = str(exc)
                if exc.status_code is not None and exc.status_code not in _RETRYABLE_HTTP_CODES:
                    raise
                logger.warning("HTTP error on attempt %d: %s", attempt + 1, exc)

        raise HttpClientError(f"All {self.retries + 1} attempts failed for {url}: {last_error}")

    def _do_request(self, url: str, headers: dict) -> str:
        if _HAS_REQUESTS:
            return self._do_request_requests(url, headers)
        return self._do_request_urllib(url, headers)

    def _do_request_requests(self, url: str, headers: dict) -> str:
        try:
            connect_t, read_t = self.timeout if isinstance(self.timeout, tuple) else (self.timeout, self.timeout)
            resp = _requests.get(url, headers=headers, timeout=(connect_t, read_t), stream=True)
            resp.raise_for_status()
            content = resp.raw.read(self.max_bytes + 1)
            if len(content) > self.max_bytes:
                raise HttpClientError(f"Response exceeds {self.max_bytes} bytes: {url}")
            if content[:2] == b'\x1f\x8b':
                content = gzip.decompress(content)
            return content.decode("utf-8-sig")
        except _requests.exceptions.HTTPError as exc:
            raise HttpClientError(str(exc), status_code=exc.response.status_code if exc.response else None) from exc
        except _requests.exceptions.RequestException as exc:
            raise HttpClientError(str(exc)) from exc

    def _do_request_urllib(self, url: str, headers: dict) -> str:
        timeout_val = self.timeout[1] if isinstance(self.timeout, tuple) else self.timeout
        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=timeout_val) as response:
                raw = response.read(self.max_bytes + 1)
                if len(raw) > self.max_bytes:
                    raise HttpClientError(f"Response exceeds {self.max_bytes} bytes: {url}")
                if raw[:2] == b'\x1f\x8b':
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8-sig")
        except HTTPError as exc:
            raise HttpClientError(str(exc), status_code=exc.code) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise HttpClientError(str(exc)) from exc


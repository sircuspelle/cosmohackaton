# -*- coding: utf-8 -*-
"""
Пакет apis — единственное место в проекте, где происходят
HTTP-запросы к внешним API и чтение внешних данных.

Экспортируемые клиенты:
  * HttpClient        — базовый HTTP-клиент (retry, timeout, User-Agent)
  * NoaaClient        — NOAA SWPC (протоны, рентген, Kp, алерты)
  * CelesTrakClient   — CelesTrak (TLE МКС, SOCRATES CSV)
  * DonkiClient       — NASA DONKI (SEP, FLR, CME, GST, IPS, HSS, RBE, MPC, WSA-Enlil)
  * WhereTheIssClient — wheretheiss.at (TLE по NORAD ID)
  * SpaceTrackClient  — Space-Track.org (исторические TLE)
  * JplClient         — JPL SSD (Close Approach Data)
"""

from src.main.apis.http_client import HttpClient
from src.main.apis.noaa_client import NoaaClient
from src.main.apis.celestrak_client import CelesTrakClient
from src.main.apis.donki_client import DonkiClient
from src.main.apis.wheretheiss_client import WhereTheIssClient
from src.main.apis.spacetrack_client import SpaceTrackClient
from src.main.apis.jpl_client import JplClient

__all__ = [
    "HttpClient",
    "NoaaClient",
    "CelesTrakClient",
    "DonkiClient",
    "WhereTheIssClient",
    "SpaceTrackClient",
    "JplClient",
]


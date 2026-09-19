from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class Window:
    """Окно проведения ВКД."""

    id: str
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if not self.id or not self.id.strip():
            raise ValueError("window id must not be empty")

        self.start = self._utc_naive(self.start)
        self.end = self._utc_naive(self.end)

        if self.end <= self.start:
            raise ValueError("window end must be after start")

        if self.duration < timedelta(hours=1):
            raise ValueError("EVA duration must be at least 1 hour")

        if self.duration > timedelta(hours=8):
            raise ValueError("EVA duration must not exceed 8 hours")

    @staticmethod
    def _utc_naive(value: datetime) -> datetime:
        """
        Приводит дату к UTC без tzinfo.

        Внутри расчётов проекта используются naive UTC datetime,
        поэтому timezone удаляется после преобразования.
        """
        if not isinstance(value, datetime):
            raise TypeError("window boundaries must be datetime values")

        if value.tzinfo is None:
            return value

        return value.astimezone(timezone.utc).replace(tzinfo=None)

    @property
    def duration(self) -> timedelta:
        """Продолжительность окна."""
        return self.end - self.start

    @property
    def duration_minutes(self) -> float:
        """Продолжительность окна в минутах."""
        return self.duration.total_seconds() / 60.0

    def contains(self, moment: datetime) -> bool:
        """Проверяет, входит ли момент во временное окно."""
        moment = self._utc_naive(moment)
        return self.start <= moment <= self.end

    def overlap(
        self,
        start: datetime,
        end: datetime,
    ) -> timedelta:
        """Возвращает пересечение окна с другим интервалом."""
        other_start = self._utc_naive(start)
        other_end = self._utc_naive(end)

        left = max(self.start, other_start)
        right = min(self.end, other_end)

        return max(timedelta(0), right - left)

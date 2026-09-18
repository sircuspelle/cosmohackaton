from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

@dataclass
class Window:
    """Окно проведения ВКД."""
    id: str
    start: datetime
    end: datetime

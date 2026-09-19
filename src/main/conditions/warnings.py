# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional, Dict, Any


@dataclass(frozen=True)
class WarningAssessment:
    """Детальный отчет по предупреждению или фактору риска для ВКД."""
    factor_name: str                  # Название фактора (например, "debris", "protons")
    eva_impact_value: str             # Значение / влияние для ВКД
    source: str                       # Источник данных
    published_at: Optional[datetime]  # Время публикации исходных данных
    model_or_rule: str                # Примененная модель или правило
    confidence_justification: str     # Обоснование уверенности / покрытие данных
    risk_score: Optional[float]       # Числовой риск (0-100)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if self.published_at:
            d["published_at"] = self.published_at.isoformat()
        return d

    def format_for_operator(self) -> str:
        """Форматированный вывод для оператора ЦУП."""
        pub_time = self.published_at.strftime('%Y-%m-%d %H:%M UTC') if self.published_at else 'Н/Д'
        return (
            f"⚠️ **Фактор:** {self.factor_name.upper()}\n"
            f"  • **Значение для ВКД:** {self.eva_impact_value}\n"
            f"  • **Источник:** {self.source}\n"
            f"  • **Время публикации:** {pub_time}\n"
            f"  • **Модель / Правило:** {self.model_or_rule}\n"
            f"  • **Обоснование уверенности:** {self.confidence_justification}\n"
            f"  • **Оценка риска:** {self.risk_score if self.risk_score is not None else 'Н/Д'} / 100"
        )
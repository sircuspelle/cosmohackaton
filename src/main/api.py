from datetime import datetime
from typing import List, Optional, Dict
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.main.window import Window
from src.main.conditions.thermal import ThermalCalculator
from src.main.conditions.debris import DebrisCalculator
from src.main.conditions.illumination import IlluminationCalculator
from data.conditions.proton import TrajectoryAwareProtonCalculator, ProtonPoint
from src.main.window_comparator import WindowComparator

app = FastAPI(
    title="ISS EVA Window Decision Support API",
    description="API для комплексной оценки и ранжирования окон внекорабельной деятельности (ВКД)",
    version="1.0.0"
)


# --- Схемы запросов (Pydantic models) ---

class WindowModel(BaseModel):
    id: str
    start: datetime
    end: datetime


class EvaluationRequest(BaseModel):
    windows: List[WindowModel]
    tle_line1: str
    tle_line2: str
    weights: Optional[Dict[str, float]] = None
    # Опционально можно передать сырые точки протонов или мусора, если есть
    shielding_g_cm2: Optional[float] = 30.0


# --- Эндпоинты ---

@app.post("/compare-windows")
def compare_windows(payload: EvaluationRequest):
    """
    Принимает список кандидатных окон и TLE-данные станции,
    прогоняет все калькуляторы (тепло, мусор, свет, протоны)
    и возвращает отранжированный результат через WindowComparator.
    """
    if len(payload.windows) < 2:
        raise HTTPException(status_code=400, detail="Нужно передать как минимум 2 окна для сравнения.")

    try:
        # Превращаем Pydantic-модели в объекты Window, которые ждут калькуляторы
        windows = [Window(id=w.id, start=w.start, end=w.end) for w in payload.windows]

        factor_results = {}

        for w in windows:
            # 1. Расчет термального режима
            thermal_res = ThermalCalculator.calculate_metrics(
                window=w,
                tle_line1=payload.tle_line1,
                tle_line2=payload.tle_line2
            )

            # 2. Расчет космического мусора (передаем пустой список, если нет внешних данных сближений)
            debris_res = DebrisCalculator.calculate_metrics(
                window=w,
                conjunctions=[]
            )

            # 3. Расчет освещенности
            illum_res = IlluminationCalculator.calculate_metrics(
                window=w,
                tle_line1=payload.tle_line1,
                tle_line2=payload.tle_line2
            )

            # 4. Расчет протонных метрик (передаем пустой ряд, если нет телеметрии)
            proton_res = TrajectoryAwareProtonCalculator.calculate_proton_metrics(
                window=w,
                data_series=[],
                shielding_g_cm2=payload.shielding_g_cm2
            )

            # Собираем все факторы для конкретного окна
            factor_results[w.id] = {
                "thermal": thermal_res,
                "debris": debris_res,
                "illumination": illum_res,
                "protons": proton_res,
            }

        # 5. Сравниваем окна через наш умный компаратор
        comparison = WindowComparator.compare(
            windows=windows,
            factor_results=factor_results,
            weights=payload.weights
        )

        return comparison.to_dict()

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Ошибка при расчете: {str.__(e) if hasattr(e, '__') else str(e)}")


@app.get("/health")
def health_check():
    return {"status": "ok", "message": "EVA Decision Support API работает штатно"}
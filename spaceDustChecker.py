import json
from pathlib import Path

import pandas as pd
import requests

ISS_NORAD_ID = 25544
MAX_RESULTS = 25

SOCRATES_CSV_URL = "https://celestrak.org/SOCRATES/sort-minRange.csv"
SOCRATES_INFO_URL = "https://celestrak.org/SOCRATES/jsonDir.php"

CACHE_FILE = Path("socrates_raw.csv")
CACHE_META_FILE = Path("socrates_cache.json")


def get_latest_file_info():
    """
    Получаем информацию о текущем файле SOCRATES:
    имя, размер, время обновления.
    """
    response = requests.get(
        SOCRATES_INFO_URL,
        timeout=30,
        headers={"User-Agent": "ISS-Risk-Monitor/1.0"},
    )

    response.raise_for_status()

    files = response.json()

    for file_info in files:
        if file_info["FILE_NAME"] == "sort-minRange.csv":
            return file_info

    raise RuntimeError("Файл sort-minRange.csv не найден")


def download_socrates_data():
    """
    Скачивает CSV только если CelesTrak обновил данные.
    """

    file_info = get_latest_file_info()
    remote_mtime = file_info["FILE_MTIME"]

    # Проверяем локальный кеш
    if CACHE_FILE.exists() and CACHE_META_FILE.exists():
        with open(CACHE_META_FILE, "r", encoding="utf-8") as f:
            cache_meta = json.load(f)

        if cache_meta.get("FILE_MTIME") == remote_mtime:
            print("Данные CelesTrak не изменились.")
            print("Используем локальный кеш.")

            return CACHE_FILE

    # print("Обнаружены новые данные SOCRATES.")
    # print(f"Последнее обновление: {remote_mtime}")
    # print("Скачиваю CSV...")

    response = requests.get(
        SOCRATES_CSV_URL,
        timeout=120,
        headers={"User-Agent": "ISS-Risk-Monitor/1.0"},
    )

    response.raise_for_status()

    CACHE_FILE.write_bytes(response.content)

    with open(CACHE_META_FILE, "w", encoding="utf-8") as f:
        json.dump(file_info, f, indent=4, ensure_ascii=False)

    # print("CSV скачан.")

    return CACHE_FILE


def get_iss_conjunctions(csv_file):
    """
    Находит все сближения, где один из объектов — МКС.
    """

    df = pd.read_csv(csv_file)

    # МКС может быть либо первым, либо вторым объектом
    iss_mask = (df["NORAD_CAT_ID_1"] == ISS_NORAD_ID) | (
        df["NORAD_CAT_ID_2"] == ISS_NORAD_ID
    )

    df = df[iss_mask].copy()

    # Сортируем по минимальному расстоянию
    df = df.sort_values("TCA_RANGE")

    # Берём первые MAX_RESULTS
    df = df.head(MAX_RESULTS)

    results = []

    for _, row in df.iterrows():
        # Определяем, какой объект является МКС,
        # а какой — встречным объектом
        if row["NORAD_CAT_ID_1"] == ISS_NORAD_ID:
            iss_id = row["NORAD_CAT_ID_1"]
            iss_name = row["OBJECT_NAME_1"]
            iss_dse = row["DSE_1"]

            other_id = row["NORAD_CAT_ID_2"]
            other_name = row["OBJECT_NAME_2"]
            other_dse = row["DSE_2"]

        else:
            iss_id = row["NORAD_CAT_ID_2"]
            iss_name = row["OBJECT_NAME_2"]
            iss_dse = row["DSE_2"]

            other_id = row["NORAD_CAT_ID_1"]
            other_name = row["OBJECT_NAME_1"]
            other_dse = row["DSE_1"]

        results.append(
            {
                "iss_norad_id": int(iss_id),
                "iss_name": iss_name,
                "iss_days_since_epoch": float(iss_dse),
                "object_norad_id": int(other_id),
                "object_name": other_name,
                "object_days_since_epoch": float(other_dse),
                "tca_utc": row["TCA"],
                "min_range_km": float(row["TCA_RANGE"]),
                "relative_speed_km_s": float(row["TCA_RELATIVE_SPEED"]),
                "max_probability": float(row["MAX_PROB"]),
                "dilution_threshold_km": float(row["DILUTION"]),
            }
        )

    return pd.DataFrame(results)

    # def save_data(df):
    df.to_csv(
        "iss_conjunctions.csv",
        index=False,
        encoding="utf-8",
    )

    df.to_json(
        "iss_conjunctions.json",
        orient="records",
        indent=4,
        force_ascii=False,
    )

    print()
    print("Сохранено:")
    print("  iss_conjunctions.csv")
    print("  iss_conjunctions.json")


def main():
    csv_file = download_socrates_data()

    df = get_iss_conjunctions(csv_file)

    print()
    print("Ближайшие объекты к МКС:")
    print()

    print(
        df[
            [
                "object_norad_id",
                "object_name",
                "tca_utc",
                "min_range_km",
                "relative_speed_km_s",
                "max_probability",
            ]
        ].to_string(index=False)
    )

    # save_data(df)


if __name__ == "__main__":
    main()

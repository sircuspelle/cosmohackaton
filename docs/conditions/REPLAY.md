# Исторический replay (май–июнь 2024)

`SpaceDataAdapter.build_context(as_of=..., replay_mode="as_of")` читает только локальные JSON-снимки в `data/conditions/`:

- `archive_tle_may_june2024.json` — TLE ISS, выгруженные из CelesTrak GP History/Special Data Request. Поля `available_at`/`published_at` обязательны для строгого `as_of`.
- `archive_protons_may_june2024.json` — GOES >=10 MeV. Источник архива: NOAA/NCEI GOES SEM; `available_at` фиксирует публикацию.
- `archive_kp_may_june2024.json` — планетарный Kp из GFZ web-service. Kp учитывается после окончания трёхчасового интервала (`end_time`) и `available_at`.

Поддержаны два режима:

- `as_of` — исторически честный replay: записи без доказанного времени доступности и записи после отсечки отбрасываются.
- `reconstruction` — восстановление по наблюдаемому времени; это ретроспективный анализ, и доступность данных в момент прогноза не утверждается.

Ограничения: локальный диапазон 2024-05-01—2024-06-30; SOCRATES не хранит исторические прогнозы сближений; Kp — планетарный proxy; GOES измеряет геостационарную среду, а не дозу на МКС; пропуски не считаются нулевым риском.

Источники: [CelesTrak historical GP request](https://celestrak.org/NORAD/archives/request.php), [NOAA GOES SEM archive](https://www.ncei.noaa.gov/data/goes-space-environment-monitor/access/), [GFZ Kp API](https://kp.gfz.de/en/data).

# EVA Risk Assessor

Сервис оценивает риски выхода в открытый космос и сравнивает возможные окна
работы экипажа. Он объединяет космическую погоду, протонный поток GOES,
планетарный индекс Kp, орбитальную информацию МКС и прогнозы сближений.

## Возможности

Для заданного начала, длительности сеанса и периода поиска сервис получает и
нормализует данные источников, строит окна одинаковой длительности, оценивает
радиационные, геомагнитные и орбитальные факторы, показывает покрытие и
ограничения, а затем выбирает окно с минимальным подтверждённым риском.

Пустой источник не считается безопасным: результат помечается как
`insufficient_data`, `unavailable` или `archive_unavailable`.

## Запуск

```bash
pip install -r docs/conditions/requirements.txt
python -m src.main.app demo --output demo_result.json
```

Оценка сохраняется в JSON вместе с параметрами запроса, источниками,
событиями, покрытиями факторов, ограничениями и рекомендацией:

```bash
python -m src.main.app assess \
  --start 2024-05-10T12:00:00Z \
  --duration-hours 6 \
  --search-hours 6 \
  --mode reconstruction \
  --output result.json
```

## Исторический replay

Локальный replay поддерживает период с 1 мая по 30 июня 2024 года. Архивы
находятся в `data/conditions/`: GP/TLE МКС из исторической выгрузки CelesTrak,
GOES proton flux `>=10 MeV` и трёхчасовой планетарный Kp из GFZ.

Строгий `as_of` пропускает только сведения с явным `available_at`,
`published_at` или `fetched_at`, не превышающим отсечку:

```bash
python -m src.main.app conditions \
  --start 2024-05-10T12:00:00Z \
  --mode as_of \
  --as-of 2024-05-10T11:00:00Z \
  --replay-mode as_of \
  --output conditions.json
```

Ретроспективная реконструкция выбирает наблюдения в интервале
`[as_of, reconstruction_end]`:

```bash
python -m src.main.app conditions \
  --start 2024-05-10T12:00:00Z \
  --mode reconstruction \
  --replay-mode reconstruction \
  --reconstruction-end 2024-05-10T18:00:00Z \
  --output reconstruction.json
```

`reconstruction` показывает, что было измерено в интервале задним числом;
это не утверждение о доступности данных для оператора в момент `as_of`.
Ограничения записываются в поле `limitations`: SOCRATES не хранит
исторические прогнозы сближений, Kp является планетарным proxy, а GOES не
заменяет расчёт дозы на МКС.

## Проверка

```bash
python -m pytest src/test/ -v
```

Тесты проверяют сохранение результатов, честность `as_of`, границы архивного
периода, reconstruction-интервал, доступность источников, схему TLE-кэша и
обработку нехватки данных.

## Фронтенд

Запустите локальный сервер из корня репозитория:

```bash
python -m http.server 8080
```

Откройте [http://localhost:8080/frontend/](http://localhost:8080/frontend/).
Панель автоматически читает `result.json`, показывает рекомендацию, сравнение
окон, факторы, источники и ограничения. Большие списки событий не выводятся
целиком в первом экране: пользователь видит агрегированные показатели и
статус качества данных.

Чтобы кнопка «Оценить окна» обращалась к API, запустите сервис параллельно:

```bash
python -m src.main.app serve --port 8000
```

## Источники

- [CelesTrak GP historical request](https://celestrak.org/NORAD/archives/request.php)
- [NOAA GOES Space Environment Monitor](https://www.ncei.noaa.gov/data/goes-space-environment-monitor/access/)
- [GFZ Kp data and API](https://kp.gfz.de/en/data)

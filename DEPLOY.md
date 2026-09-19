# DEPLOY.md

---

## Часть 1. Гайд по деплою

### 1.1 Требования к серверу

| Параметр | Минимум | Рекомендовано |
|----------|---------|---------------|
| CPU | 1 vCPU | 2 vCPU |
| RAM | 1 GB | 2 GB |
| Диск | 10 GB SSD | 20 GB SSD |
| OS | Ubuntu 22.04 | Ubuntu 24.04 |
| Сеть | Порт 80 | Порт 80, 443 |

### 1.2 Установка Docker

Подключаемся к серверу:

```bash
ssh root@ВАШ_IP
```

Устанавливаем Docker одной командой:

```bash
curl -fsSL https://get.docker.com | sh
```

Добавляем текущего пользователя в группу docker (чтобы не писать sudo):

```bash
usermod -aG docker $USER
newgrp docker
```

Проверяем:

```bash
docker --version
docker compose version
```

### 1.3 Деплой

Скачиваем и запускаем deploy.sh:

```bash
curl -sL https://raw.githubusercontent.com/sircuspelle/cosmohackaton/main/deploy.sh -o deploy.sh
chmod +x deploy.sh
./deploy.sh deploy
```

Скрипт выполнит всё автоматически:
- Клонирует репозиторий в /opt/cosmohackaton
- Соберёт Docker-образ
- Запустит сервер и nginx
- Проверит healthcheck

После завершения сервис будет доступен на порту 80:

```
http://ВАШ_IP/health
```

### 1.4 Управление сервисами

Все команды через deploy.sh:

| Команда | Действие |
|---------|----------|
| `./deploy.sh deploy` | Первичный деплой |
| `./deploy.sh update` | Pull + пересборка |
| `./deploy.sh stop` | Остановка |
| `./deploy.sh restart` | Перезапуск |
| `./deploy.sh status` | Статус контейнеров и healthcheck |
| `./deploy.sh logs` | Логи всех сервисов |
| `./deploy.sh logs server` | Логи только Python-сервера |
| `./deploy.sh logs nginx` | Логи только nginx |
| `./deploy.sh clean` | Полная очистка (контейнеры + volume) |

### 1.5 Автозапуск при перезагрузке сервера

Контейнеры настроены на restart: unless-stopped. Проверяем:

```bash
docker inspect eva-server --format '{{.HostConfig.RestartPolicy.Name}}'
```

Ожидаемый вывод: unless-stopped

Отключение автозапуска:

```bash
docker update --restart=no eva-server eva-nginx
```

Включение автозапуска обратно:

```bash
docker update --restart=unless-stopped eva-server eva-nginx
```

### 1.6 Настройка домена и SSL (опционально)

Ставим Certbot для Let's Encrypt:

```bash
apt install -y certbot
```

Генерируем сертификат (выполнять при остановленном nginx):

```bash
docker compose stop nginx
certbot certonly --standalone -d your-domain.com -d www.your-domain.com
```

Копируем сертификаты в проект:

```bash
mkdir -p nginx/ssl
cp /etc/letsencrypt/live/your-domain.com/fullchain.pem nginx/ssl/cert.pem
cp /etc/letsencrypt/live/your-domain.com/privkey.pem nginx/ssl/key.pem
```

Добавляем SSL в docker-compose.yml — в сервисе nginx добавляем порт 443 и volume:

```yaml
  nginx:
    image: nginx:alpine
    container_name: eva-nginx
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx/default.conf:/etc/nginx/conf.d/default.conf:ro
      - ./nginx/ssl:/etc/nginx/ssl:ro
    depends_on:
      server:
        condition: service_healthy
```

Обновляем конфиг nginx для SSL — файл nginx/default.conf:

```nginx
server {
    listen 80;
    server_name your-domain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name your-domain.com;

    ssl_certificate     /etc/nginx/ssl/cert.pem;
    ssl_certificate_key /etc/nginx/ssl/key.pem;

    location / {
        proxy_pass http://server:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```

Перезапускаем:

```bash
docker compose up -d
```

Автопродление сертификата — добавляем крон:

```bash
echo "0 3 * * * certbot renew --quiet && cp /etc/letsencrypt/live/your-domain.com/fullchain.pem /opt/cosmohackaton/nginx/ssl/cert.pem && cp /etc/letsencrypt/live/your-domain.com/privkey.pem /opt/cosmohackaton/nginx/ssl/key.pem && docker compose restart nginx" | crontab -
```

### 1.7 Отладка

Проверяем, что контейнер запущен и слушает порт:

```bash
docker exec eva-server python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/health').read().decode())"
```

Входим внутрь контейнера для отладки:

```bash
docker exec -it eva-server bash
```

Проверяем установленные пакеты внутри контейнера:

```bash
docker exec eva-server pip list
```

Проверяем, что SQLite доступна:

```bash
docker exec eva-server python -c "import sqlite3; print(sqlite3.connect('/app/data/events/eva.sqlite3').execute('SELECT 1').fetchone())"
```

Просмотр сетей Docker Compose:

```bash
docker network ls
docker network inspect cosmohackaton_default
```

---

## Часть 2. Технологический стек и технологии

### 2.1 Python 3.12

Основной язык проекта. Используется Python 3.12 в Docker-образе python:3.12-slim.

Ключевые особенности использования в проекте:

- Встроенный HTTP-сервер (http.server.ThreadingHTTPServer) — не требует сторонних зависимостей для запуска API
- sqlite3 — стандартная библиотека для работы с SQLite без установки драйверов
- urllib.request — fallback HTTP-клиент, если requests не установлен
- concurrent.futures.ThreadPoolExecutor — параллельная загрузка данных из внешних API
- argparse — CLI-интерфейс для ручных прогонов

Импорт модулей проекта:

```python
from src.main.core import assess, iso, now
from src.main.events.service import config_load, run
from src.main.events.storage import Store
```

### 2.2 SQLite

Файловая embedded-база данных. Не требует отдельного сервера или контейнера.

Файл: data/events/eva.sqlite3

Таблицы:

```sql
CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    url TEXT,
    fetched TEXT,
    body TEXT,
    headers TEXT
);

CREATE INDEX IF NOT EXISTS snapshot_url_time ON snapshots(url, fetched);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created TEXT,
    result TEXT
);
```

Транзакции в коде (из storage.py):

```python
db.execute('PRAGMA journal_mode=WAL')
db.execute('INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?)',
           (digest, url, fetched, body, headers))
db.execute('INSERT OR IGNORE INTO runs VALUES(?,?,?)',
           (rid, iso(now()), raw))
```

WAL-режим включён для конкурентного чтения/записи без блокировок.

### 2.3 Docker

Используется multi-service архитектура:

| Компонент | Образ | Назначение |
|-----------|-------|------------|
| server | python:3.12-slim | Python API-сервер |
| nginx | nginx:alpine | Реверс-прокси, балансировка |

### 2.4 Dockerfile

```dockerfile
FROM python:3.12-slim AS server

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY config/ config/

RUN mkdir -p data/events data/conditions

EXPOSE 8000

CMD ["python", "-m", "src.main.app", "--config", "config/events/config.json", "serve", "--host", "0.0.0.0", "--port", "8000"]
```

Пояснения:

- python:3.12-slim — минимальный образ (~150 MB вместо ~900 MB полного)
- pip install --no-cache-dir — не кэширует pip-пакеты в образе
- CMD запускает сервер на 0.0.0.0:8000 — доступен извне контейнера

### 2.5 Docker Compose

```yaml
services:
  server:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: eva-server
    restart: unless-stopped
    expose:
      - "8000"
    volumes:
      - eva-data:/app/data
    environment:
      - TZ=UTC
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s

  nginx:
    image: nginx:alpine
    container_name: eva-nginx
    restart: unless-stopped
    ports:
      - "80:80"
    volumes:
      - ./nginx/default.conf:/etc/nginx/conf.d/default.conf:ro
    depends_on:
      server:
        condition: service_healthy

volumes:
  eva-data:
    driver: local
```

Ключевые решения:

- expose vs ports: сервер использует expose (доступен только внутри сети Docker), nginx использует ports (доступен снаружи)
- healthcheck: Python stdlib urllib проверяет /health каждые 30 секунд
- depends_on с condition: service_healthy: nginx не стартует пока сервер не будет готов
- restart: unless-stopped: автоперезапуск после ребута, но не после ручной остановки
- TZ=UTC: единый часовой пояс для всех вычислений
- Named volume eva-data: данные SQLite переживают пересоздание контейнеров

### 2.6 Nginx

Реверс-прокси для маршрутизации трафика:

```nginx
server {
    listen 80;
    server_name _;

    location / {
        proxy_pass http://server:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```

Все запросы на порт 80 проксируются на Python-сервер внутри Docker-сети по имени сервиса server:8000.

### 2.7 Python-зависимости

requirements.txt:

```
requests>=2.31
skyfield>=1.49
numpy>=1.24
```

| Пакет | Назначение в проекте |
|-------|---------------------|
| requests | HTTP-клиент для запросов к внешним API (NOAA, CelesTrak, NASA DONKI, JPL, Space-Track) |
| skyfield | Вычисление орбит, освещённости, теплового режима, связи МКС |
| numpy | Числовые вычисления, используемые skyfield для орбитальных расчётов |

requests является опциональным — http_client.py поддерживает fallback на urllib.request из стандартной библиотеки.

### 2.8 Внешние API-источники данных

| Источник | Клиент | Данные | TTL кэш |
|----------|--------|--------|---------|
| NOAA SWPC | noaa_client.py | Kp-индекс, протоны, рентген, алерты | 300 сек |
| NASA DONKI | donki_client.py | CME, SEP, геомагнитные бури | 1800 сек |
| CelesTrak SOCRATES | celestrak_client.py | Сближения с объектами космического мусора | 28800 сек |
| JPL CAD | jpl_client.py | Близкие астероиды | 86400 сек |
| Space-Track | spacetrack_client.py | TLE-орбиты МКС (для Replay) | - |
| wheretheiss.at | wheretheiss_client.py | TLE-орбиты (fallback) | - |

### 2.9 Архитектура приложения

Структура модулей:

```
src/main/
  app.py               — CLI и HTTP-сервер (точка входа)
  core.py              — чистые расчёты окон и событий
  adapter.py           — адаптер между внешними API и калькуляторами
  window.py            — модель окна ВКД
  window_comparator.py — компаратор окон
  demo.py              — синтетические данные для тестирования
  utils.py             — утилиты
  events/
    service.py         — оркестрация сбора данных и оценки
    storage.py         — SQLite-хранилище (кэш +runs)
    adapters.py        — парсинг данных от каждого источника
  apis/
    http_client.py     — базовый HTTP-клиент с retry
    noaa_client.py     — NOAA SWPC
    donki_client.py    — NASA DONKI
    celestrak_client.py — CelesTrak
    jpl_client.py      — JPL
    spacetrack_client.py — Space-Track
    wheretheiss_client.py — wheretheiss.at
  conditions/
    protons.py         — расчёт протонного потока
    illumination.py    — расчёт освещённости
    thermal.py         — расчёт теплового режима
    communication.py   — расчёт связи
    debris.py          — расчёт мусора
```

Поток данных:

1. app.py принимает запрос (CLI или HTTP POST /assess)
2. events/service.py формирует список URL-ов для загрузки
3. events/storage.py проверяет кэш SQLite, загружает при необходимости
4. events/adapters.py парсит ответы каждого источника в единый формат событий
5. core.pyassess() вычисляет окна, факторы, рекомендации
6. Результат возвращается как JSON и сохраняется в SQLite (runs)

### 2.10 .dockerignore

```
.git
.gitignore
.idea
__pycache__
**/__pycache__
*.pyc
*.pyo
docs/
src/test/
result.json
```

Исключает из Docker build context:

- .git — история весит много, не нужна в образе
- __pycache__, *.pyc — байткод Python
- docs/ — документация, не нужна для запуска
- src/test/ — тесты, не нужны в продакшн-образе
- result.json — выходной файл прошлых прогонов

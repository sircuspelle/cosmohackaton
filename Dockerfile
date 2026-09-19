FROM python:3.12-slim AS server

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY config/ config/

RUN mkdir -p data/events data/conditions

EXPOSE 8000

CMD ["python", "-m", "src.main.app", "--config", "config/events/config.json", "serve", "--host", "0.0.0.0", "--port", "8000"]

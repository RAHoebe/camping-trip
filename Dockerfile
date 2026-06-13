FROM python:3.13.2-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_APP=app.py \
    DATA_FOLDER=/app/data \
    GRAPHHOPPER_API_KEY_FILE=/run/secrets/graphhopper.key \
    GUNICORN_LOG_LEVEL=warning

COPY requirements_docker.txt .
RUN pip install --no-cache-dir -r requirements_docker.txt

COPY . .
RUN mkdir -p /app/data /app/data/uploads

EXPOSE 8034

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8034/')" || exit 1

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:8034 --workers 1 --threads 8 --timeout 120 --access-logfile /dev/null --error-logfile - --log-level ${GUNICORN_LOG_LEVEL} app:app"]

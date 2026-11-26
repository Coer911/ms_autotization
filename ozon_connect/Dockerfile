# syntax=docker/dockerfile:1
# Отдельный образ Ozon Connect. Корневой Dockerfile хаба не использует этот файл.
# DOCKER_BUILDKIT=1 docker build -t ozon-connect -f ozon_connect/Dockerfile --build-context hub=hub ozon_connect
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    TZ=Europe/Moscow \
    OZON_DRY_RUN=1 \
    OZON_FBS_ENABLED=0 \
    OZON_FBO_ENABLED=0 \
    OZON_DATABASE_URL=sqlite+aiosqlite:////data/ozon.sqlite3

WORKDIR /app
COPY requirements-ozon.txt /tmp/requirements-ozon.txt
RUN pip install --no-cache-dir -r /tmp/requirements-ozon.txt \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin ozon \
    && mkdir -p /data \
    && chown ozon:ozon /data

COPY --from=hub __init__.py /app/hub/__init__.py
COPY --from=hub moysklad /app/hub/moysklad
COPY *.py /app/ozon_connect/

USER ozon
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()"]
CMD ["python", "-m", "ozon_connect", "serve"]

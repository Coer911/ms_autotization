# Отдельный образ Ozon Connect. Корневой Dockerfile хаба не использует этот файл.
# Сборка: docker build -t ozon-connect -f ozon_connect/Dockerfile ozon_connect
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
COPY __init__.py __main__.py server.py settings.py errors.py ./ozon_connect/

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin ozon \
    && mkdir -p /data \
    && chown ozon:ozon /data
USER ozon

VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()"]
CMD ["python", "-m", "ozon_connect", "serve"]

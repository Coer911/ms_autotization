"""HTTP для проверки контейнера. Других маршрутов нет."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from ozon_connect.settings import Settings


def ready_body(settings: Settings) -> dict:
    return {
        "status": "ready",
        "service": "ozon-connect",
        "dry_run": settings.dry_run,
        "fbs_enabled": settings.fbs_enabled,
        "fbo_enabled": settings.fbo_enabled,
        "external_writes": "blocked",
        "catalog_import": "off",
        "database": "not_opened",
        "accounts": [account.id for account in settings.accounts],
    }


def make_server(settings: Settings, host: str, port: int) -> ThreadingHTTPServer:
    handler = _handler(settings)
    return ThreadingHTTPServer((host, port), handler)


def serve(settings: Settings, host: str | None = None) -> None:
    httpd = make_server(settings, host or settings.host, settings.port)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def _handler(settings: Settings):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/healthz":
                self._json(200, {"status": "ok", "service": "ozon-connect"})
                return
            if path == "/readyz":
                self._json(200, ready_body(settings))
                return
            self._json(404, {"error": {"code": "NOT_FOUND", "message": "нет такого пути"}})

        def do_POST(self) -> None:
            self._reject_write()

        def do_PUT(self) -> None:
            self._reject_write()

        def do_PATCH(self) -> None:
            self._reject_write()

        def do_DELETE(self) -> None:
            self._reject_write()

        def _reject_write(self) -> None:
            # Тело не читаем. Хвост длиннее буфера и chunked иначе остаются
            # на сокете, и следующий запрос на keep-alive читается из них.
            self._json(405, {
                "error": {
                    "code": "WRITE_DISABLED",
                    "message": "Ozon Connect не принимает Push и не пишет во внешние API",
                },
            }, close=True)

        def _json(self, status: int, payload: dict, close: bool = False) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            if close:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, fmt: str, *args) -> None:
            return

    return Handler

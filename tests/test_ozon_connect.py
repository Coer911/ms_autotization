"""Контейнер Ozon Connect: окружение, health, отказ от записи. Сеть наружу не нужна."""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from ozon_connect.errors import OzonError
from ozon_connect.server import make_server, ready_body
from ozon_connect.settings import load_settings, sqlite_path


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OZON_DRY_RUN", "OZON_FBS_ENABLED", "OZON_FBO_ENABLED", "OZON_DATABASE_URL",
                 "OZON_MOYSKLAD_TOKEN", "OZON_MOYSKLAD_RPS", "OZON_ACCOUNTS", "PORT"):
        monkeypatch.delenv(name, raising=False)


def test_sqlite_url_is_only_a_path():
    assert sqlite_path("sqlite+aiosqlite:////data/ozon.sqlite3").as_posix() == "/data/ozon.sqlite3"


def test_defaults_block_writes(monkeypatch):
    _clear(monkeypatch)
    settings = load_settings()
    assert settings.dry_run is True
    assert settings.fbs_enabled is False
    assert settings.fbo_enabled is False
    assert settings.external_writes_allowed is False
    assert settings.accounts == ()


def test_dry_run_off_still_blocks_external_writes(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OZON_DRY_RUN", "0")
    assert load_settings().external_writes_allowed is False


def test_account_secret_required(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("OZON_ACCOUNTS", "shop")
    with pytest.raises(OzonError) as caught:
        load_settings()
    assert caught.value.code == "ACCOUNT_SECRET"


def test_ready_hides_secrets_and_does_not_create_database(tmp_path, monkeypatch):
    _clear(monkeypatch)
    database = tmp_path / "ozon.sqlite3"
    monkeypatch.setenv("OZON_DATABASE_URL", "sqlite+aiosqlite:///" + database.as_posix())
    monkeypatch.setenv("OZON_MOYSKLAD_TOKEN", "secret-token")
    monkeypatch.setenv("OZON_ACCOUNTS", "shop")
    monkeypatch.setenv("OZON_ACCOUNT_SHOP_CLIENT_ID", "client-id")
    monkeypatch.setenv("OZON_ACCOUNT_SHOP_API_KEY", "api-key")
    settings = load_settings()
    body = ready_body(settings)
    text = json.dumps(body)
    assert "secret-token" not in text
    assert "api-key" not in text
    assert "client-id" not in text
    assert body["database"] == "not_opened"
    assert body["external_writes"] == "blocked"
    assert body["catalog_import"] == "off"

    httpd = make_server(settings, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/healthz", timeout=2) as response:
            assert response.status == 200
            assert json.loads(response.read())["status"] == "ok"
        with urllib.request.urlopen(base + "/readyz", timeout=2) as response:
            assert json.loads(response.read())["accounts"] == ["shop"]
        request = urllib.request.Request(base + "/hooks/ozon", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=2)
        assert caught.value.code == 405
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert not database.exists()


def _start(monkeypatch: pytest.MonkeyPatch):
    _clear(monkeypatch)
    settings = load_settings()
    httpd = make_server(settings, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, f"127.0.0.1:{httpd.server_address[1]}"


def _read_response(sock: socket.socket) -> tuple[int, dict[str, str], bytes]:
    sock.settimeout(2)
    raw = b""
    while b"\r\n\r\n" not in raw:
        piece = sock.recv(4096)
        assert piece
        raw += piece
    head, _, rest = raw.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        name, value = line.split(":", 1)
        headers[name.lower()] = value.strip()
    need = int(headers.get("content-length", "0"))
    while len(rest) < need:
        piece = sock.recv(4096)
        assert piece
        rest += piece
    return status, headers, rest[:need]


def test_large_post_closes_connection_before_next_healthz(monkeypatch):
    httpd, _thread, address = _start(monkeypatch)
    host, port = address.split(":")
    poison = b"GET /poison HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    body = b"x" * 65_536 + poison
    pipelined = (
        b"POST /hooks/ozon HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: keep-alive\r\n"
        b"\r\n"
        + body
        + b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
    )
    try:
        with socket.create_connection((host, int(port)), timeout=2) as sock:
            sock.sendall(pipelined)
            status, headers, payload = _read_response(sock)
            assert status == 405
            assert headers["connection"] == "close"
            assert json.loads(payload)["error"]["code"] == "WRITE_DISABLED"
            sock.settimeout(2)
            extra = sock.recv(4096)
            assert extra == b""
        with socket.create_connection((host, int(port)), timeout=2) as sock:
            sock.sendall(b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
            status, _headers, payload = _read_response(sock)
            assert status == 200
            assert json.loads(payload)["status"] == "ok"
    finally:
        httpd.shutdown()
        httpd.server_close()

"""Контейнер Ozon Connect: окружение, health, отказ от записи. Сеть наружу не нужна."""

from __future__ import annotations

import json
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

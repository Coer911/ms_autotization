"""Импорт FBS: чтение Ozon, сопоставление артикула, одна строка на отправление."""
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest
from conftest import fresh_storage, settings_for

from hub.automations import ConfigError, build
from hub.moysklad import MoySklad
from hub.ozon.client import OzonClient, OzonContractError, parse_fbs_list
from hub.ozon.fbs_import import import_fbs
from hub.ozon.ids import operation_key
from hub.ozon.redact import redact_posting
from hub.ozon.statuses import FILTER_STATUSES, OBJECT_STATUSES, SUBSTATUSES
from hub.settings import load_config
from hub.storage.ozon import count_operations, get_posting, list_lines, save_posting

KEY = "super-secret-key"
WHEN = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


def posting(**extra) -> dict:
    body = {
        "posting_number": "05708065-0029-1",
        "order_id": 680420041,
        "order_number": "05708065-0029",
        "status": "awaiting_deliver",
        "substatus": "posting_awaiting_passport_data",
        "tpl_integration_type": "ozon",
        "in_process_at": "2022-05-13T07:07:32Z",
        "shipment_date": "2022-05-13T10:00:00Z",
        "delivering_date": None,
        "is_express": False,
        "parent_posting_number": "",
        "delivery_method": {"id": 10, "warehouse_id": 77, "warehouse": "Склад Нахабино",
                            "name": "курьер"},
        "cancellation": {"cancel_reason_id": 0, "cancel_reason": "не хранить",
                         "cancellation_type": "", "cancelled_after_ship": False},
        "customer": {"name": "Тест Заказчик", "phone": "79000000000",
                     "address": {"address_tail": "ул. Секретная, 1"}},
        "addressee": {"name": "Тест Получатель", "phone": "79000000001"},
        "financial_data": {"products": [{"payout": "PAYOUT-99"}]},
        "products": [{
            "offer_id": "205953", "sku": 358924380, "quantity": 2,
            "price": "1390.000000", "currency_code": "RUB", "name": "Конструктор",
        }],
    }
    body.update(extra)
    return body


class OzonScript:
    def __init__(self, pages, status=200, text=""):
        self.pages = pages
        self.status = status
        self.text = text
        self.bodies: list[dict] = []
        self.paths: list[str] = []
        self.headers: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.paths.append(request.url.path)
            self.headers.append(dict(request.headers))
            self.bodies.append(json.loads(request.content.decode()))
            if self.status != 200:
                return httpx.Response(self.status, text=self.text or KEY)
            return httpx.Response(200, json=self.pages[len(self.bodies) - 1])
        return httpx.MockTransport(handler)


def page(postings, cursor="", has_next=False) -> dict:
    return {"postings": postings, "cursor": cursor, "has_next": has_next}


def catalog(products: dict[str, list[dict]], variants: dict[str, list[dict]] | None = None):
    variants = variants or {}
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method != "GET":
            return httpx.Response(500, json={"errors": [{"error": "запись запрещена", "code": 500}]})
        article = ""
        query = request.url.query.decode() if isinstance(request.url.query, bytes) else request.url.query
        raw = parse_qs(query).get("filter", [""])[0]
        if raw.startswith("article="):
            article = raw.removeprefix("article=")
        if request.url.path.endswith("/entity/product"):
            rows = products.get(article, [])
        elif request.url.path.endswith("/entity/variant"):
            rows = variants.get(article, [])
        else:
            return httpx.Response(404, json={"errors": [{"error": request.url.path, "code": 404}]})
        return httpx.Response(200, json={"rows": rows[:2]})

    return httpx.MockTransport(handler), calls


async def run_import(pages, products=None, *, ms=True, days=2):
    script = OzonScript(pages)
    transport, calls = catalog(products or {"205953": [{"id": "prod-1", "article": "205953"}]})
    storage = await fresh_storage()
    ozon = OzonClient("12345", KEY, transport=script.transport())
    catalog_ms = MoySklad("test-token", transport=transport) if ms else None
    try:
        stats = await import_fbs(storage, ozon, catalog_ms, lookback_days=days, moment=WHEN)
        return storage, script, stats, calls
    except Exception:
        await storage.close()
        raise
    finally:
        await ozon.aclose()
        if catalog_ms is not None:
            await catalog_ms.close()


def flat(row: dict, lines: list[dict]) -> str:
    return json.dumps({"row": row, "lines": lines}, ensure_ascii=False)


def test_operation_key_includes_cabinet_scheme_and_optional_position():
    assert operation_key("12345", "fbs", "05708065-0029-1", "import") == (
        "12345|fbs|05708065-0029-1|import")
    assert operation_key("12345", "fbo", "05708065-0029-1", "return", "ret-9") == (
        "12345|fbo|05708065-0029-1|return|ret-9")
    with pytest.raises(ValueError):
        operation_key("12345", "fbs", "a|b", "import")
    with pytest.raises(ValueError):
        operation_key("12345", "dbs", "1", "import")


def test_redact_drops_buyer_and_payout():
    snapshot = redact_posting(posting())
    assert snapshot.posting_number == "05708065-0029-1"
    assert snapshot.warehouse_id == "77"
    assert snapshot.lines[0].offer_id == "205953"
    assert snapshot.lines[0].price == "1390.000000"
    assert snapshot.lines[0].line_key == "0000"
    dumped = json.dumps(snapshot.__dict__, ensure_ascii=False, default=lambda item: item.__dict__)
    assert "Тест Заказчик" not in dumped
    assert "79000000000" not in dumped
    assert "PAYOUT-99" not in dumped
    assert "Секретная" not in dumped


def test_v3_envelope_is_not_treated_as_empty_v4():
    with pytest.raises(OzonContractError):
        parse_fbs_list({"result": {"postings": [posting()], "has_next": False}})


def test_real_config_leaves_ozon_off_and_logistics_on():
    from pathlib import Path
    cfg = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    assert cfg["automations"]["ozon_fbs_import"]["enabled"] is False
    autos = build(cfg["automations"])
    names = {item.name for item in autos}
    assert "ozon_fbs_import" not in names
    assert {"logistics_sheet", "logistics_publish"} <= names


def test_enabled_import_requires_keys(monkeypatch):
    monkeypatch.delenv("OZON_CLIENT_ID", raising=False)
    monkeypatch.delenv("OZON_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        build({"ozon_fbs_import": {"every_minutes": 15, "lookback_days": 7, "scheme": "fbs"}})
    monkeypatch.setenv("OZON_CLIENT_ID", "12345")
    monkeypatch.setenv("OZON_API_KEY", KEY)
    auto = build({"ozon_fbs_import": {"every_minutes": 15, "lookback_days": 7, "scheme": "fbs"}})[0]
    assert auto.slot(datetime(2026, 11, 26, 12, 7, tzinfo=UTC)).endswith(":48")


async def test_import_matches_article_and_stores_no_personal_data():
    storage, script, stats, calls = await run_import([page([posting()])])
    try:
        assert stats.inserted == 1 and stats.matched == 1 and stats.seen == 1
        assert script.paths == ["/v4/posting/fbs/list"]
        body = script.bodies[0]
        assert body["sort_dir"] == "ASC" and "offset" not in body and "dir" not in body
        assert body["with"] == {
            "analytics_data": False, "barcodes": False, "financial_data": False, "legal_info": False}
        assert body["filter"]["since"] == "2026-11-24T12:00:00Z"
        assert script.headers[0]["client-id"] == "12345"
        row = await get_posting(storage, "12345", "fbs", "05708065-0029-1")
        lines = await list_lines(storage, "12345", "fbs", "05708065-0029-1")
        assert row["status"] == "awaiting_deliver"
        assert row["warehouse"] == "Склад Нахабино"
        assert lines[0]["ms_id"] == "prod-1" and lines[0]["match_status"] == "matched"
        text = flat(row, lines)
        for secret in ("Тест Заказчик", "79000000000", "79000000001", "PAYOUT-99",
                       "Секретная", KEY, "не хранить"):
            assert secret not in text
        assert all(method == "GET" for method, _ in calls)
        assert await count_operations(storage) == 1
    finally:
        await storage.close()


async def test_second_import_does_not_duplicate_and_status_change_updates():
    first, second = posting(), posting(status="delivered", substatus="posting_delivered")
    storage, _, stats, _ = await run_import([page([first])])
    try:
        again = await import_fbs(
            storage, OzonClient("12345", KEY, transport=OzonScript([page([first])]).transport()),
            None, lookback_days=2, moment=WHEN)
        assert again.unchanged == 1 and again.inserted == 0
        assert await count_operations(storage) == 1
        changed = await import_fbs(
            storage, OzonClient("12345", KEY, transport=OzonScript([page([second])]).transport()),
            None, lookback_days=2, moment=WHEN)
        assert changed.updated == 1
        row = await get_posting(storage, "12345", "fbs", "05708065-0029-1")
        assert row["status"] == "delivered"
        assert await count_operations(storage) == 1
        assert stats.inserted == 1
    finally:
        await storage.close()


async def test_removed_line_does_not_stay_and_ambiguous_article_is_not_guessed():
    two = posting(products=[
        {"offer_id": "205953", "sku": 1, "quantity": 1, "price": "10.00", "currency_code": "RUB"},
        {"offer_id": "СИРОП-700", "sku": 2, "quantity": 1, "price": "20.00", "currency_code": "RUB"},
    ])
    one = posting(products=[
        {"offer_id": "205953", "sku": 1, "quantity": 1, "price": "10.00", "currency_code": "RUB"},
    ])
    products = {
        "205953": [{"id": "prod-1"}, {"id": "prod-2"}],
        "СИРОП-700": [{"id": "prod-3"}],
    }
    storage, _, stats, _ = await run_import([page([two])], products)
    try:
        assert stats.ambiguous == 1 and stats.matched == 1
        assert len(await list_lines(storage, "12345", "fbs", "05708065-0029-1")) == 2
        await import_fbs(
            storage, OzonClient("12345", KEY, transport=OzonScript([page([one])]).transport()),
            MoySklad("test-token", transport=catalog(products)[0]),
            lookback_days=2, moment=WHEN)
        lines = await list_lines(storage, "12345", "fbs", "05708065-0029-1")
        assert [line["offer_id"] for line in lines] == ["205953"]
    finally:
        await storage.close()


async def test_unsafe_offer_and_missing_token_do_not_call_catalog():
    raw = posting(products=[{"offer_id": "a;b=c", "sku": 1, "quantity": 1, "price": "1", "currency_code": "RUB"}])
    storage, _, stats, calls = await run_import([page([raw])], {})
    try:
        assert stats.skipped == 1 and calls == []
    finally:
        await storage.close()
    storage, _, stats, _ = await run_import([page([posting()])], ms=False)
    try:
        lines = await list_lines(storage, "12345", "fbs", "05708065-0029-1")
        assert stats.no_catalog == 1 and lines[0]["match_status"] == "no_catalog"
    finally:
        await storage.close()


async def test_cursor_advances_and_stuck_cursor_stops():
    storage, script, stats, _ = await run_import([
        page([posting()], cursor="next", has_next=True),
        page([posting(posting_number="05708065-0030-1")], cursor="", has_next=False),
    ])
    try:
        assert stats.seen == 2 and stats.inserted == 2
        assert script.bodies[1]["cursor"] == "next"
        assert await count_operations(storage) == 2
    finally:
        await storage.close()
    storage = await fresh_storage()
    try:
        with pytest.raises(OzonContractError):
            await import_fbs(
                storage,
                OzonClient("12345", KEY, transport=OzonScript([
                    page([posting()], cursor="same", has_next=True),
                    page([posting()], cursor="same", has_next=True),
                ]).transport()),
                None, lookback_days=2, moment=WHEN)
    finally:
        await storage.close()


async def test_error_text_does_not_contain_api_key():
    script = OzonScript([], status=500, text=f"отказ {KEY}")
    client = OzonClient("12345", KEY, transport=script.transport())
    try:
        with pytest.raises(Exception) as caught:
            await client.list_fbs_page("2026-11-25T00:00:00Z", "2026-11-26T00:00:00Z")
    finally:
        await client.aclose()
    assert KEY not in str(caught.value)


async def test_parallel_saves_keep_one_posting():
    import asyncio

    from hub.ozon.match import Match
    storage = await fresh_storage()
    snapshot = redact_posting(posting())
    try:
        await asyncio.gather(*[
            save_posting(storage, "12345", "fbs", snapshot, [Match("matched", "product", "prod-1")])
            for _ in range(8)])
        assert await get_posting(storage, "12345", "fbs", "05708065-0029-1") is not None
        assert await count_operations(storage) == 1
        assert len(await list_lines(storage, "12345", "fbs", "05708065-0029-1")) == 1
    finally:
        await storage.close()


def test_status_map_mentions_every_documented_code():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "docs/ozon/STATUS_MAP.md").read_text(encoding="utf-8")
    for code in (*FILTER_STATUSES, *OBJECT_STATUSES, *SUBSTATUSES):
        assert f"`{code}`" in text


def test_settings_do_not_require_ozon_keys():
    settings = settings_for({})
    assert settings.ozon_client_id == "" and settings.ozon_api_key == ""

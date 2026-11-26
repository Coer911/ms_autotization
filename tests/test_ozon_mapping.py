"""Сверка артикулов Ozon Connect на фикстурах. Живые API не вызываются."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from hub.moysklad.client import MoySklad
from ozon_connect.errors import OzonError
from ozon_connect.importer import execute
from ozon_connect.mapping import MsCard, OzonCard, Sku, classify_ms, reconcile
from ozon_connect.ms_read import read_moysklad
from ozon_connect.ozon_read import fetch_ozon
from ozon_connect.settings import Account, Settings


def _settings(path, *accounts) -> Settings:
    return Settings(
        database_path=path,
        dry_run=True,
        fbs_enabled=False,
        fbo_enabled=False,
        moysklad_token="test-token",
        moysklad_rps=None,
        accounts=accounts or (Account("shop", "client", "key"),),
        port=8080,
    )


def _clock():
    step = {"n": 0}

    def clock() -> str:
        step["n"] += 1
        return f"2026-11-26T16:00:{step['n']:02d}Z"

    return clock


def _ozon(offer: str, product: str = "10", sku: str = "9", archived: bool = False) -> OzonCard:
    skus = (Sku(sku, "supplier_1", "EXPRESS"),) if sku else ()
    return OzonCard(offer, product, skus, archived)


def _ms(ms_id: str, article: str, kind: str = "product", archived: bool = False) -> MsCard:
    return MsCard(ms_id, kind, article, archived)


def _run(tmp_path, ozon, cards, accounts=None, fetch_ozon_cards=None, fetch_ms=None):
    settings = _settings(tmp_path / "ozon.sqlite3", *(accounts or ()))

    def default_ozon(account):
        return ozon, []

    def default_ms():
        return cards, []

    if fetch_ozon_cards is None:
        fetch_ozon_cards = default_ozon
    if fetch_ms is None:
        fetch_ms = default_ms
    report, code = execute(
        settings,
        clock=_clock(),
        fetch_ms=fetch_ms,
        fetch_ozon_cards=fetch_ozon_cards,
    )
    return settings, report, code


def test_exact_match_keeps_zeros_case_and_spaces(tmp_path):
    cards = [
        _ms("p-1", "001"),
        _ms("p-2", "1"),
        _ms("p-3", "Ab"),
        _ms("p-4", " ab"),
    ]
    ozon = [_ozon("001", "11"), _ozon("ab", "12"), _ozon("Ab", "13"), _ozon(" ab", "14", sku="")]
    _settings_path, report, code = _run(tmp_path, ozon, cards)
    assert code == 0
    shop = report["accounts"][0]
    assert shop["fresh"] is True
    assert shop["matched"] == 3
    assert shop["missing"] == 1
    from ozon_connect.store import Store
    store = Store(_settings_path.database_path)
    try:
        links = store.load_mappings("shop")
        assert links["001"].ms_id == "p-1"
        assert links["001"].status == "matched"
        assert links["ab"].status == "missing"
        assert links["Ab"].ms_id == "p-3"
        assert links[" ab"].ms_id == "p-4"
        assert store.conn.execute(
            "SELECT offer_id FROM mappings WHERE offer_id = 'ab' AND account_id = 'shop'"
        ).fetchone()[0] == "ab"
        assert store.conn.execute(
            "SELECT offer_id FROM mappings WHERE offer_id = 'AB'"
        ).fetchone() is None
    finally:
        store.close()


def test_duplicates_variants_bundles_and_archive_are_not_auto_picked(tmp_path):
    cards = [
        _ms("v-1", "SAME", "variant"),
        _ms("v-2", "SAME", "variant"),
        _ms("b-1", "BUNDLE", "bundle"),
        _ms("old", "ARCH", "product", archived=True),
        _ms("live", "MIX", "product"),
        _ms("gone", "MIX", "variant", archived=True),
    ]
    ozon = [
        _ozon("SAME"),
        _ozon("BUNDLE"),
        _ozon("ARCH"),
        _ozon("MIX"),
        _ozon("HIDDEN", archived=True),
    ]
    _, report, code = _run(tmp_path, ozon, cards)
    assert code == 0
    shop = report["accounts"][0]
    assert shop["ambiguous"] == 1
    assert shop["matched"] == 2
    assert shop["archived"] == 2
    problems = {item["offer_id"]: item for item in shop["problems"]}
    assert problems["SAME"]["reason"] == "ambiguous"
    assert problems["ARCH"]["reason"] == "moysklad_archived"
    assert problems["HIDDEN"]["reason"] == "ozon_archived"
    from ozon_connect.store import Store
    store = Store(tmp_path / "ozon.sqlite3")
    try:
        links = store.load_mappings("shop")
        assert links["SAME"].ms_id is None
        assert links["SAME"].candidates == (("v-1", "variant"), ("v-2", "variant"))
        assert links["BUNDLE"].ms_id == "b-1"
        assert links["BUNDLE"].ms_type == "bundle"
        assert links["MIX"].ms_id == "live"
        assert links["ARCH"].ms_id is None
        assert links["HIDDEN"].ms_id is None
    finally:
        store.close()


def test_service_and_empty_keys_stay_out_of_mappings():
    service, audit = classify_ms("service", {"id": "s-1", "code": "ABC", "archived": False})
    assert service is None
    assert audit.kind == "service"
    empty, empty_audit = classify_ms("product", {"id": "p-1", "article": "", "code": "ABC"})
    assert empty is None
    assert empty_audit.kind == "empty_article"
    numeric, numeric_audit = classify_ms("variant", {"id": "v-1", "article": 1, "meta": {"type": "variant"}})
    assert numeric is None
    assert numeric_audit.kind == "empty_article"
    links = reconcile(
        {},
        {"ABC": [_ozon("ABC")]},
        [],
        "2026-11-26T16:00:00Z",
    )
    assert links[0].status == "missing"
    assert links[0].ms_id is None


def test_reimport_is_idempotent_and_conflict_keeps_uuid(tmp_path):
    first = [_ozon("A", "10", "9")]
    cards = [_ms("uuid-1", "A")]
    settings, report, code = _run(tmp_path, first, cards)
    assert code == 0 and report["accounts"][0]["matched"] == 1
    from ozon_connect.store import Store
    store = Store(settings.database_path)
    created = store.load_mappings("shop")["A"].created_at
    store.close()

    changed = [_ozon("A", "99", "8")]
    moved = [_ms("uuid-1", "OTHER"), _ms("uuid-2", "A")]
    _, second, code = _run(tmp_path, changed, moved)
    assert code == 0
    store = Store(settings.database_path)
    try:
        link = store.load_mappings("shop")["A"]
        assert link.status == "conflict"
        assert link.reason == "offer_id_reused"
        assert link.ms_id == "uuid-1"
        assert link.ozon_product_id == "10"
        assert link.ozon_skus == (Sku("9", "supplier_1", "EXPRESS"),)
        assert link.created_at == created
        assert store.conn.execute("SELECT COUNT(*) FROM mappings").fetchone()[0] == 1
    finally:
        store.close()

    same = [_ozon("A", "10", "9")]
    _, third, code = _run(tmp_path, same, cards)
    assert code == 0
    store = Store(settings.database_path)
    try:
        again = store.load_mappings("shop")["A"]
        assert again.status == "matched"
        assert again.ms_id == "uuid-1"
        assert again.created_at == created
        assert store.conn.execute("SELECT COUNT(*) FROM mappings").fetchone()[0] == 1
    finally:
        store.close()


def test_article_and_sku_changes_do_not_rebind(tmp_path):
    settings, _, code = _run(tmp_path, [_ozon("A", "10", "9")], [_ms("uuid-1", "A")])
    assert code == 0
    _, report, code = _run(
        tmp_path,
        [_ozon("A", "10", "9")],
        [_ms("uuid-1", "RENAMED"), _ms("uuid-2", "A")],
    )
    assert code == 0
    assert report["accounts"][0]["conflict"] == 1
    from ozon_connect.store import Store
    store = Store(settings.database_path)
    link = store.load_mappings("shop")["A"]
    store.close()
    assert link.ms_id == "uuid-1"
    assert link.reason == "article_changed"

    settings, _, _ = _run(tmp_path, [_ozon("B", "10", "9")], [_ms("uuid-b", "B")])
    _, report, code = _run(tmp_path, [_ozon("B", "10", "77")], [_ms("uuid-b", "B")])
    assert code == 0
    assert report["accounts"][0]["problems"][0]["reason"] == "stable_id_changed"
    store = Store(settings.database_path)
    kept = store.load_mappings("shop")["B"]
    store.close()
    assert kept.ms_id == "uuid-b"
    assert kept.ozon_skus[0].sku == "9"


def test_new_duplicate_does_not_replace_confirmed_uuid(tmp_path):
    settings, _, code = _run(tmp_path, [_ozon("A")], [_ms("uuid-1", "A")])
    assert code == 0
    _, report, code = _run(
        tmp_path,
        [_ozon("A")],
        [_ms("uuid-1", "A"), _ms("uuid-2", "A", "variant")],
    )
    assert code == 0
    assert report["accounts"][0]["conflict"] == 1
    from ozon_connect.store import Store
    store = Store(settings.database_path)
    try:
        link = store.load_mappings("shop")["A"]
        assert link.ms_id == "uuid-1"
        assert link.reason == "ambiguous"
        assert link.candidates == ()
    finally:
        store.close()


def test_same_offer_id_is_isolated_per_account(tmp_path):
    accounts = (Account("one", "c1", "k1"), Account("two", "c2", "k2"))
    cards = [_ms("shared", "A")]

    def ozon_for(account):
        product = "1" if account.id == "one" else "2"
        return ([_ozon("A", product)], [])

    settings = _settings(tmp_path / "ozon.sqlite3", *accounts)
    report, code = execute(
        settings,
        clock=_clock(),
        fetch_ms=lambda: (cards, []),
        fetch_ozon_cards=ozon_for,
    )
    assert code == 0
    assert [item["account_id"] for item in report["accounts"]] == ["one", "two"]
    assert all(item["matched"] == 1 for item in report["accounts"])
    from ozon_connect.store import Store
    store = Store(settings.database_path)
    try:
        assert store.load_mappings("one")["A"].ozon_product_id == "1"
        assert store.load_mappings("two")["A"].ozon_product_id == "2"
        assert store.load_mappings("one")["A"].ms_id == store.load_mappings("two")["A"].ms_id == "shared"
    finally:
        store.close()


def test_last_page_failure_keeps_previous_map(tmp_path):
    settings, report, code = _run(tmp_path, [_ozon("A")], [_ms("uuid-1", "A")])
    assert code == 0 and report["accounts"][0]["matched"] == 1

    def broken(account):
        raise OzonError("обрыв последней страницы", "PAGE")

    report, code = execute(
        settings,
        clock=_clock(),
        fetch_ms=lambda: ([_ms("uuid-1", "A")], []),
        fetch_ozon_cards=broken,
    )
    assert code == 1
    shop = report["accounts"][0]
    assert shop["fresh"] is False
    assert shop["reason"] == "incomplete_fetch"
    assert shop["matched"] == 1
    assert shop["missing"] == 0
    from ozon_connect.store import Store
    store = Store(settings.database_path)
    try:
        assert store.load_mappings("shop")["A"].status == "matched"
        assert store.load_mappings("shop")["A"].ms_id == "uuid-1"
    finally:
        store.close()


def test_ozon_list_includes_archive_and_zero_stock_and_stops_on_last_page():
    calls = []

    def post(path, body):
        calls.append((path, body))
        if path == "/v3/product/list":
            visibility = body["filter"]["visibility"]
            if visibility == "ALL" and body["last_id"] == "":
                return {"result": {
                    "items": [{
                        "product_id": 5,
                        "offer_id": "001",
                        "archived": False,
                        "has_fbo_stocks": False,
                        "has_fbs_stocks": False,
                    }],
                    "total": 2,
                    "last_id": "page-2",
                }}
            if visibility == "ALL":
                return {"result": {
                    "items": [{"product_id": 6, "offer_id": "", "archived": False}],
                    "total": 2,
                    "last_id": "page-2",
                }}
            return {"result": {"items": [], "total": 0, "last_id": ""}}
        assert path == "/v3/product/info/list"
        assert list(body) == ["offer_id"]
        return {"items": [{
            "id": 5,
            "offer_id": "001",
            "sku": 9,
            "sources": [{"sku": 9, "source": "supplier_1", "shipment_type": "EXPRESS"}],
        }]}

    cards, audits = fetch_ozon(post)
    assert [card.offer_id for card in cards] == ["001"]
    assert cards[0].product_id == "5"
    assert cards[0].skus == (Sku("9", "supplier_1", "EXPRESS"),)
    assert [(item.source, item.source_id, item.kind) for item in audits] == [("ozon", "6", "empty_offer_id")]
    assert calls[0][1]["filter"]["visibility"] == "ALL"
    assert calls[1][1]["last_id"] == "page-2"
    assert any(call[1].get("filter", {}).get("visibility") == "ARCHIVED" for call in calls)


def test_ozon_last_page_error_does_not_return_partial_catalog():
    def post(path, body):
        if path == "/v3/product/info/list":
            raise AssertionError("карточки не запрашиваются после обрыва списка")
        if body["last_id"] == "":
            return {"result": {
                "items": [{"product_id": 1, "offer_id": "A", "archived": False}],
                "total": 2,
                "last_id": "last",
            }}
        raise OzonError("обрыв", "PAGE")

    with pytest.raises(OzonError) as caught:
        fetch_ozon(post)
    assert caught.value.code == "PAGE"


def test_moysklad_read_is_get_only_and_keeps_services_out():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        assert request.method == "GET"
        assert request.url.params["filter"] == "archived=true;archived=false"
        assert request.url.params["limit"] == "1000"
        entity = request.url.path.rstrip("/").rsplit("/", 1)[-1]
        if entity == "product":
            rows = [{"id": "p1", "article": "A", "archived": False, "meta": {"type": "product"}}]
        elif entity == "service":
            rows = [{"id": "s1", "code": "A", "archived": False, "meta": {"type": "service"}}]
        else:
            rows = []
        return httpx.Response(200, json={"rows": rows, "meta": {"size": len(rows)}})

    async def load():
        transport = httpx.MockTransport(handler)
        async with MoySklad("test-token", transport=transport, retries=1) as ms:
            return await read_moysklad(ms)

    cards, audits = asyncio.run(load())
    assert seen and set(seen) == {"GET"}
    assert [card.ms_id for card in cards] == ["p1"]
    assert [item.kind for item in audits] == ["service"]
    assert reconcile({}, {"A": [_ozon("A")]}, cards, "2026-11-26T16:00:00Z")[0].ms_id == "p1"


def test_empty_offer_and_service_are_in_the_report_not_the_map(tmp_path):
    def ozon_for(account):
        return fetch_ozon(_catalog)

    def _catalog(path, body):
        if path == "/v3/product/list":
            visibility = body["filter"]["visibility"]
            if visibility == "ALL" and body["last_id"] == "":
                return {"result": {
                    "items": [
                        {"product_id": 1, "offer_id": "A", "archived": False, "has_fbo_stocks": False},
                        {"product_id": 2, "offer_id": "", "archived": False},
                    ],
                    "total": 2,
                    "last_id": "",
                }}
            return {"result": {"items": [], "total": 0, "last_id": ""}}
        return {"items": [{"id": 1, "offer_id": "A", "sources": []}]}

    settings = _settings(tmp_path / "ozon.sqlite3")
    report, code = execute(
        settings,
        clock=_clock(),
        fetch_ms=lambda: (
            [_ms("p1", "A")],
            [classify_ms("service", {"id": "s1", "code": "A"})[1]],
        ),
        fetch_ozon_cards=ozon_for,
    )
    assert code == 0
    shop = report["accounts"][0]
    assert shop["matched"] == 1
    assert shop["diagnostics"]["empty_offer_id"] == 1
    assert shop["diagnostics"]["service"] == 1
    assert shop["unkeyed"][0]["source_id"] in {"2", "s1"}
    assert "client" not in str(shop)
    assert "key" not in str(shop["unkeyed"])

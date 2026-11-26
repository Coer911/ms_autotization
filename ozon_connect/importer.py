"""Явный импорт каталогов. Публикует карту кабинета только после полного обхода."""

from __future__ import annotations

import asyncio
import json
import sys

from ozon_connect.errors import OzonError
from ozon_connect.mapping import reconcile, utc_now
from ozon_connect.ms_read import fetch_moysklad
from ozon_connect.ozon_read import fetch_ozon, group_cards, post_json
from ozon_connect.settings import Settings
from ozon_connect.store import Store


def run_import(settings: Settings, account: str | None = None) -> int:
    report, code = execute(settings, account=account)
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return code


def execute(
    settings: Settings,
    account: str | None = None,
    *,
    clock=utc_now,
    fetch_ms=None,
    fetch_ozon_cards=None,
) -> tuple[dict, int]:
    chosen = [item for item in settings.accounts if account is None or item.id == account]
    if account is not None and not chosen:
        raise OzonError(f"кабинет {account} не задан", "BAD_ACCOUNT")
    if not chosen:
        return {"accounts": []}, 0
    if not settings.moysklad_token:
        raise OzonError("OZON_MOYSKLAD_TOKEN пуст", "MOYSKLAD_TOKEN")
    store = Store(settings.database_path)
    try:
        return _execute(store, settings, chosen, clock, fetch_ms, fetch_ozon_cards)
    finally:
        store.close()


def _execute(store, settings, accounts, clock, fetch_ms, fetch_ozon_cards) -> tuple[dict, int]:
    started = {item.id: store.begin_run(item.id, clock()) for item in accounts}
    load_ms = fetch_ms or (
        lambda: asyncio.run(fetch_moysklad(settings.moysklad_token, settings.moysklad_rps))
    )
    try:
        cards, ms_audit = load_ms()
    except Exception as error:
        code = _failure_code(error)
        for item in accounts:
            store.fail_run(started[item.id], clock(), code)
        return _report(store, [item.id for item in accounts]), 1
    failed = False
    for item in accounts:
        try:
            if fetch_ozon_cards is None:
                ozon_cards, ozon_audit = fetch_ozon(
                    lambda path, body, item=item: post_json(item.client_id, item.api_key, path, body),
                )
            else:
                ozon_cards, ozon_audit = fetch_ozon_cards(item)
            now = clock()
            links = reconcile(store.load_mappings(item.id), group_cards(ozon_cards), cards, now)
            store.publish(item.id, started[item.id], links, [*ozon_audit, *ms_audit], now)
        except Exception as error:
            failed = True
            store.fail_run(started[item.id], clock(), _failure_code(error))
    ids = [item.id for item in accounts]
    return _report(store, ids), (1 if failed else 0)


def _failure_code(error: Exception) -> str:
    if isinstance(error, OzonError):
        return error.code
    status = getattr(error, "status", None)
    if isinstance(status, int):
        return f"MOYSKLAD_{status}"
    return "FETCH"


def _report(store: Store, account_ids: list[str]) -> dict:
    return {"accounts": [_account_report(store, account_id) for account_id in account_ids]}


def _account_report(store: Store, account_id: str) -> dict:
    run = store.latest_run(account_id)
    links = list(store.load_mappings(account_id).values())
    audits = store.load_audit(account_id)
    counts = {status: 0 for status in ("matched", "ambiguous", "missing", "archived", "conflict")}
    for link in links:
        if link.status in counts:
            counts[link.status] += 1
    diagnostics = {"empty_offer_id": 0, "service": 0, "empty_article": 0}
    unkeyed = []
    for item in audits:
        if item.kind in diagnostics:
            diagnostics[item.kind] += 1
        unkeyed.append({
            "source": item.source,
            "source_id": item.source_id,
            "kind": item.kind,
            "entity_type": item.entity_type,
        })
    fresh = bool(run and run["status"] == "complete")
    return {
        "account_id": account_id,
        "fresh": fresh,
        "run_status": None if run is None else run["status"],
        "reason": None if run is None or fresh else run["reason"],
        "totals": len(links),
        "matched": counts["matched"],
        "ambiguous": counts["ambiguous"],
        "missing": counts["missing"],
        "archived": counts["archived"],
        "conflict": counts["conflict"],
        "diagnostics": diagnostics,
        "problems": [
            {"offer_id": link.offer_id, "status": link.status, "reason": link.reason}
            for link in links
            if link.status != "matched"
        ],
        "unkeyed": unkeyed,
    }

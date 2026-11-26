"""Чтение каталога Ozon. Пишущих методов Seller API здесь нет.

Поля списка и карточки взяты из ProductAPI_GetProductList и
ProductAPI_GetProductInfoList. shipment_type не считается схемой FBS/FBO.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from ozon_connect.errors import OzonError
from ozon_connect.mapping import Audit, OzonCard, Sku

BASE = "https://api-seller.ozon.ru"
_LIST = "/v3/product/list"
_INFO = "/v3/product/info/list"
_VISIBILITIES = ("ALL", "ARCHIVED")
_PAGE = 1000


def fetch_ozon(post) -> tuple[list[OzonCard], list[Audit]]:
    """post(path, body) -> dict. Секреты кабинета сюда не передаются."""
    raw: list[dict] = []
    for visibility in _VISIBILITIES:
        raw.extend(_pages(post, visibility))
    audits: list[Audit] = []
    keyed: list[dict] = []
    for item in raw:
        offer = item.get("offer_id")
        product_id = _text_id(item.get("product_id"))
        if not isinstance(offer, str) or offer == "":
            audits.append(Audit("ozon", product_id or "", "empty_offer_id", None))
            continue
        keyed.append(item)
    info = _info(post, [item["offer_id"] for item in keyed])
    grouped: dict[str, list[OzonCard]] = {}
    for item in keyed:
        card = _card(item, info.get(item["offer_id"]))
        grouped.setdefault(card.offer_id, []).append(card)
    cards = [card for offer_id in sorted(grouped) for card in grouped[offer_id]]
    return cards, audits


def group_cards(cards: list[OzonCard]) -> dict[str, list[OzonCard]]:
    grouped: dict[str, list[OzonCard]] = {}
    for card in cards:
        grouped.setdefault(card.offer_id, []).append(card)
    return grouped


def post_json(client_id: str, api_key: str, path: str, body: dict) -> dict:
    if path not in {_LIST, _INFO}:
        raise OzonError("Ozon Connect читает только список и карточку товара", "READ_ONLY")
    payload = json.dumps(body).encode()
    request = urllib.request.Request(
        BASE + path,
        data=payload,
        headers={
            "Client-Id": client_id,
            "Api-Key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        raise OzonError(f"Ozon ответил {error.code}", f"HTTP_{error.code}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise OzonError("Ozon не ответил каталогом", "TRANSPORT") from error


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise OzonError(f"Ozon редирект {code}", "REDIRECT")


def _pages(post, visibility: str) -> list[dict]:
    last_id = ""
    seen: set[str] = set()
    items: list[dict] = []
    while True:
        payload = post(_LIST, {"filter": {"visibility": visibility}, "last_id": last_id, "limit": _PAGE})
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict) or not isinstance(result.get("items"), list):
            raise OzonError("Ozon list: нет result.items", "BAD_RESPONSE")
        total = result.get("total")
        if isinstance(total, bool) or not isinstance(total, int):
            raise OzonError("Ozon list: нет total", "BAD_RESPONSE")
        page = result["items"]
        items.extend(page)
        if not page or len(items) >= total:
            return items
        new_last = result.get("last_id") or ""
        if not isinstance(new_last, str) or not new_last or new_last == last_id or new_last in seen:
            raise OzonError("Ozon list: страница оборвалась", "PAGE")
        seen.add(new_last)
        last_id = new_last


def _info(post, offer_ids: list[str]) -> dict[str, dict]:
    unique: list[str] = []
    seen: set[str] = set()
    for offer_id in offer_ids:
        if offer_id not in seen:
            seen.add(offer_id)
            unique.append(offer_id)
    found: dict[str, dict] = {}
    for offset in range(0, len(unique), _PAGE):
        chunk = unique[offset:offset + _PAGE]
        payload = post(_INFO, {"offer_id": chunk})
        rows = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise OzonError("Ozon info: нет items", "BAD_RESPONSE")
        for row in rows:
            offer_id = row.get("offer_id") if isinstance(row, dict) else None
            if not isinstance(offer_id, str):
                continue
            if offer_id in found and found[offer_id] != row:
                found[offer_id] = {**found[offer_id], "_disagree": True}
            else:
                found[offer_id] = row
    return found


def _card(item: dict, info: dict | None) -> OzonCard:
    offer_id = item["offer_id"]
    list_id = _text_id(item.get("product_id"))
    info_id = _text_id(info.get("id")) if info else None
    disagree = bool(info and info.get("_disagree"))
    if list_id and info_id and list_id != info_id:
        disagree = True
    archived = bool(item.get("archived"))
    if info:
        archived = archived or bool(info.get("is_archived")) or bool(info.get("is_autoarchived"))
    return OzonCard(
        offer_id,
        info_id or list_id,
        _skus(info) if info else (),
        archived,
        disagree,
    )


def _skus(info: dict) -> tuple[Sku, ...]:
    found: list[Sku] = []
    seen: set[tuple[str, str, str]] = set()
    for source in info.get("sources") or []:
        if not isinstance(source, dict):
            continue
        sku = _sku_text(source.get("sku"))
        if sku is None:
            continue
        item = Sku(
            sku,
            source.get("source") if isinstance(source.get("source"), str) else "",
            source.get("shipment_type") if isinstance(source.get("shipment_type"), str) else "",
        )
        key = (item.sku, item.source, item.shipment_type)
        if key not in seen:
            seen.add(key)
            found.append(item)
    top = _sku_text(info.get("sku"))
    if top is not None and not any(item.sku == top for item in found):
        found.append(Sku(top, "", ""))
    return tuple(sorted(found, key=lambda item: (item.sku, item.source, item.shipment_type)))


def _text_id(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value != "":
        return value
    return None


def _sku_text(value: object) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    return None

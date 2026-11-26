"""Сопоставить offer_id отправления с артикулом товара или модификации МойСклад.

Только чтение: GET entity/product и GET entity/variant, фильтр ``article=``.
Несколько карточек с одним артикулом не угадываем.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..moysklad.client import MoySklad, entity_id

# Запятая, точка с запятой и равно ломают язык фильтра МойСклад.
_OFFER = re.compile(r"^[\w.+/() -]{1,255}$", re.UNICODE)


@dataclass(frozen=True)
class Match:
    status: str
    ms_type: str = ""
    ms_id: str = ""


def _row_id(row: dict) -> str:
    if isinstance(row.get("id"), str) and row["id"]:
        return row["id"]
    href = ((row.get("meta") or {}).get("href")) if isinstance(row.get("meta"), dict) else ""
    if isinstance(href, str) and href:
        return entity_id(href)
    raise ValueError("МойСклад вернул карточку без id")


async def _by_article(ms: MoySklad, type_: str, offer_id: str) -> list[dict]:
    return await ms.find(type_, f"article={offer_id}")


async def match_offer(ms: MoySklad | None, offer_id: str, cache: dict[str, Match]) -> Match:
    if ms is None:
        return Match("no_catalog")
    if offer_id in cache:
        return cache[offer_id]
    if not offer_id or _OFFER.fullmatch(offer_id) is None:
        found = Match("skipped")
    else:
        products = await _by_article(ms, "product", offer_id)
        variants = await _by_article(ms, "variant", offer_id)
        if len(products) > 1 or len(variants) > 1 or (products and variants):
            found = Match("ambiguous")
        elif len(products) == 1:
            found = Match("matched", "product", _row_id(products[0]))
        elif len(variants) == 1:
            found = Match("matched", "variant", _row_id(variants[0]))
        else:
            found = Match("unmatched")
    cache[offer_id] = found
    return found

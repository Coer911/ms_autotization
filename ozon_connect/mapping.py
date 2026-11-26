"""Сверка offer_id и article. Сеть и SQLite здесь не открываются.

Сравнение строк точное: без trim, casefold и перевода в число.
Архивная карточка не выбирается. Услуга по code не сопоставляется.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

STATUSES = (
    "matched",
    "ambiguous",
    "missing",
    "archived",
    "service",
    "empty_article",
    "conflict",
)


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Sku:
    sku: str
    source: str
    shipment_type: str


@dataclass(frozen=True)
class OzonCard:
    offer_id: str
    product_id: str | None
    skus: tuple[Sku, ...]
    archived: bool
    ids_disagree: bool = False


@dataclass(frozen=True)
class MsCard:
    ms_id: str
    ms_type: str
    article: str
    archived: bool


@dataclass(frozen=True)
class Audit:
    source: str
    source_id: str
    kind: str
    entity_type: str | None


@dataclass(frozen=True)
class Link:
    offer_id: str
    ozon_product_id: str | None
    ozon_skus: tuple[Sku, ...]
    ms_id: str | None
    ms_type: str | None
    ms_article: str | None
    status: str
    reason: str | None
    candidates: tuple[tuple[str, str], ...]
    created_at: str
    last_verified_at: str


def skus_blob(skus: tuple[Sku, ...]) -> str | None:
    if not skus:
        return None
    payload = [
        {"shipment_type": item.shipment_type, "sku": item.sku, "source": item.source}
        for item in skus
    ]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def skus_from_blob(raw: str | None) -> tuple[Sku, ...]:
    if not raw:
        return ()
    return tuple(
        Sku(item["sku"], item.get("source") or "", item.get("shipment_type") or "")
        for item in json.loads(raw)
    )


def candidates_blob(candidates: tuple[tuple[str, str], ...]) -> str | None:
    if not candidates:
        return None
    payload = [{"ms_id": ms_id, "ms_type": ms_type} for ms_id, ms_type in candidates]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def candidates_from_blob(raw: str | None) -> tuple[tuple[str, str], ...]:
    if not raw:
        return ()
    return tuple((item["ms_id"], item["ms_type"]) for item in json.loads(raw))


def classify_ms(entity: str, row: dict) -> tuple[MsCard | None, Audit | None]:
    """entity/service подтверждён как услуга. code не читается.

    Товар, модификация и комплект без строкового article в сверку не входят.
    """
    meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
    declared = meta.get("type") if isinstance(meta.get("type"), str) else ""
    kind = declared if declared in {"product", "variant", "bundle", "service"} else entity
    ms_id = row.get("id")
    if not isinstance(ms_id, str) or ms_id == "":
        return None, None
    if kind == "service" or entity == "service":
        return None, Audit("moysklad", ms_id, "service", "service")
    article = row["article"] if "article" in row else None
    if not isinstance(article, str) or article == "":
        return None, Audit("moysklad", ms_id, "empty_article", kind)
    return MsCard(ms_id, kind, article, bool(row.get("archived"))), None


def reconcile(
    previous: dict[str, Link],
    groups: dict[str, list[OzonCard]],
    cards: list[MsCard],
    now: str,
) -> list[Link]:
    active: dict[str, list[MsCard]] = {}
    archived: dict[str, list[MsCard]] = {}
    by_id: dict[str, MsCard] = {}
    for card in cards:
        by_id[card.ms_id] = card
        bucket = archived if card.archived else active
        bucket.setdefault(card.article, []).append(card)
    return [
        _decide(
            offer_id,
            groups[offer_id],
            _by_id(active.get(offer_id, [])),
            _by_id(archived.get(offer_id, [])),
            by_id,
            previous.get(offer_id),
            now,
        )
        for offer_id in sorted(groups)
    ]


def _by_id(cards: list[MsCard]) -> list[MsCard]:
    return sorted(cards, key=lambda card: (card.ms_id, card.ms_type))


def _link(
    offer_id: str,
    *,
    product_id: str | None,
    skus: tuple[Sku, ...],
    ms_id: str | None,
    ms_type: str | None,
    ms_article: str | None,
    status: str,
    reason: str | None,
    candidates: tuple[tuple[str, str], ...] = (),
    created_at: str,
    verified_at: str,
) -> Link:
    if status != "ambiguous":
        candidates = ()
    if status == "ambiguous" or status not in {"matched", "conflict", "archived"}:
        ms_id = ms_type = ms_article = None
    return Link(
        offer_id, product_id, skus, ms_id, ms_type, ms_article,
        status, reason, candidates, created_at, verified_at,
    )


def _decide(
    offer_id: str,
    group: list[OzonCard],
    active: list[MsCard],
    archived: list[MsCard],
    by_id: dict[str, MsCard],
    previous: Link | None,
    now: str,
) -> Link:
    created = previous.created_at if previous else now
    product_ids = {card.product_id for card in group}
    sku_sets = {card.skus for card in group}
    disagree = len(product_ids) > 1 or len(sku_sets) > 1 or any(card.ids_disagree for card in group)
    product_id = next(iter(product_ids)) if len(product_ids) == 1 else None
    skus = next(iter(sku_sets)) if len(sku_sets) == 1 else ()
    ozon_archived = any(card.archived for card in group)
    if previous and previous.ms_id:
        return _frozen(
            previous, product_id, skus, ozon_archived, disagree, active, by_id, created, now,
        )
    if disagree:
        return _link(
            offer_id, product_id=None, skus=(), ms_id=None, ms_type=None, ms_article=None,
            status="conflict", reason="offer_id_reused", created_at=created, verified_at=now,
        )
    if ozon_archived:
        return _link(
            offer_id, product_id=product_id, skus=skus, ms_id=None, ms_type=None, ms_article=None,
            status="archived", reason="ozon_archived", created_at=created, verified_at=now,
        )
    if len(active) == 1:
        one = active[0]
        return _link(
            offer_id, product_id=product_id, skus=skus, ms_id=one.ms_id, ms_type=one.ms_type,
            ms_article=one.article, status="matched", reason=None,
            created_at=created, verified_at=now,
        )
    if len(active) > 1:
        return _link(
            offer_id, product_id=product_id, skus=skus, ms_id=None, ms_type=None, ms_article=None,
            status="ambiguous", reason="ambiguous",
            candidates=tuple((card.ms_id, card.ms_type) for card in active),
            created_at=created, verified_at=now,
        )
    if archived:
        return _link(
            offer_id, product_id=product_id, skus=skus, ms_id=None, ms_type=None, ms_article=None,
            status="archived", reason="moysklad_archived", created_at=created, verified_at=now,
        )
    return _link(
        offer_id, product_id=product_id, skus=skus, ms_id=None, ms_type=None, ms_article=None,
        status="missing", reason=None, created_at=created, verified_at=now,
    )


def _frozen(
    frozen: Link,
    product_id: str | None,
    skus: tuple[Sku, ...],
    ozon_archived: bool,
    disagree: bool,
    active: list[MsCard],
    by_id: dict[str, MsCard],
    created: str,
    now: str,
) -> Link:
    """Подтверждённый UUID не подменяется. Расхождение фиксируется как conflict."""
    live = by_id.get(frozen.ms_id or "")
    unique = active[0] if len(active) == 1 else None
    product_changed = (
        frozen.ozon_product_id is not None
        and product_id is not None
        and product_id != frozen.ozon_product_id
    )
    article_moved = live is not None and live.article != frozen.offer_id
    other_ms = unique is not None and unique.ms_id != frozen.ms_id
    if disagree or product_changed:
        reason: str | None = "offer_id_reused"
    elif article_moved or other_ms:
        reason = "article_changed"
    elif len(active) > 1:
        reason = "ambiguous"
    elif skus != frozen.ozon_skus:
        reason = "stable_id_changed"
    elif ozon_archived:
        reason = "ozon_archived"
    elif live is not None and live.archived:
        reason = "moysklad_archived"
    elif live is None or unique is None:
        reason = "article_changed"
    else:
        reason = None
    if reason is None:
        status = "matched"
    elif reason in {"ozon_archived", "moysklad_archived"}:
        status = "archived"
    else:
        status = "conflict"
    return _link(
        frozen.offer_id,
        product_id=frozen.ozon_product_id,
        skus=frozen.ozon_skus,
        ms_id=frozen.ms_id,
        ms_type=frozen.ms_type,
        ms_article=frozen.ms_article,
        status=status,
        reason=reason,
        created_at=created,
        verified_at=now,
    )

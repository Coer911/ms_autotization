"""Чтение ассортимента МойСклад через hub.moysklad. Запись не вызывается.

Фильтр archived=true;archived=false — _general.md, оба состояния.
article у товара, модификации и комплекта. У услуги поля article нет.
Отчёт остатков не используется: он пропускает нулевой остаток.
"""

from __future__ import annotations

from ozon_connect.mapping import Audit, MsCard, classify_ms

# Оба состояния архива, как в _general.md. Без фильтра архив в выборку не входит.
ARCHIVED_BOTH = "archived=true;archived=false"
ENTITIES = ("product", "variant", "bundle", "service")


async def read_moysklad(ms) -> tuple[list[MsCard], list[Audit]]:
    cards: list[MsCard] = []
    audits: list[Audit] = []
    for entity in ENTITIES:
        async for row in ms.iterate(f"entity/{entity}", limit=1000, filter=ARCHIVED_BOTH):
            card, audit = classify_ms(entity, row)
            if card:
                cards.append(card)
            if audit:
                audits.append(audit)
    return cards, audits


async def fetch_moysklad(token: str, rps: int | None) -> tuple[list[MsCard], list[Audit]]:
    from hub.moysklad.client import MoySklad

    async with MoySklad(token, rps=rps) as ms:
        return await read_moysklad(ms)

"""Текст сообщения → поля заказа. Без сети и без МойСклад.

Нужны контрагент и строки «название — количество» (или «2 кг название»).
Свободный текст без этой формы сюда не додумывается: бот просит прислать её заново.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_QTY = r"\d+(?:[.,]\d+)?"
_UNIT = r"(?:кг|гр|шт|штук|упак|уп|мл|пачка|пачки|пач|л|г)"
_QTY_FIRST = re.compile(
    rf"^(?P<qty>{_QTY})(?:\s*{_UNIT})?(?:\s*[xх×*])?\s+(?P<name>.+)$",
    re.IGNORECASE,
)
_NAME_FIRST = re.compile(
    rf"^(?P<name>.+?)\s*[—–\-xх×*]\s*(?P<qty>{_QTY})\s*{_UNIT}?$",
    re.IGNORECASE,
)
_NAME_UNIT = re.compile(
    rf"^(?P<name>.+?)\s+(?P<qty>{_QTY})\s*{_UNIT}$",
    re.IGNORECASE,
)
_PARTY = re.compile(r"^(?:контрагент|покупатель|клиент)\s*[:—–\-]\s*(?P<name>.+)$", re.IGNORECASE)
_COMMENT = re.compile(r"^(?:комментарий|примечание)\s*[:—–\-]\s*(?P<text>.*)$", re.IGNORECASE)
_MAX_LINES = 30


@dataclass
class ItemLine:
    name: str
    quantity: float


@dataclass
class ParsedOrder:
    counterparty: str = ""
    lines: list[ItemLine] = field(default_factory=list)
    comment: str = ""
    missing: list[str] = field(default_factory=list)

    @property
    def filled(self) -> bool:
        return bool(self.counterparty and self.lines and not self.missing)


def _quantity(text: str) -> float:
    return float(text.replace(",", "."))


def _position(line: str) -> ItemLine | None:
    for pattern in (_QTY_FIRST, _NAME_FIRST, _NAME_UNIT):
        match = pattern.match(line)
        if not match:
            continue
        name = match.group("name").strip(" .")
        quantity = _quantity(match.group("qty"))
        if name and quantity > 0:
            return ItemLine(name, quantity)
    return None


def parse_order_text(text: str) -> ParsedOrder:
    """Контрагент, позиции и комментарий. Неполная форма — в missing, заказ из неё не создаётся."""
    raw = (text or "").replace("\u00a0", " ").replace("\u202f", " ").strip()
    party = ""
    extra_party = False
    comments: list[str] = []
    items: list[ItemLine] = []
    rest: list[str] = []
    missing: list[str] = []
    for line in raw.splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        if comment := _COMMENT.match(line):
            note = comment.group("text").strip()
            if note:
                comments.append(note)
            continue
        if labeled := _PARTY.match(line):
            name = labeled.group("name").strip()
            if party:
                extra_party = True
            elif name:
                party = name
            continue
        if item := _position(line):
            items.append(item)
            continue
        rest.append(line)
    # первая строка без подписи — контрагент, только если ниже уже есть позиции
    if not party and rest and items:
        party = rest.pop(0)
    if extra_party:
        missing.append("в тексте два контрагента, оставьте одного")
    if len(items) > _MAX_LINES:
        missing.append(f"слишком много позиций, пришлите не больше {_MAX_LINES}")
        items = items[:_MAX_LINES]
    if items:
        comments.extend(rest)
    else:
        for line in rest:
            shown = line if len(line) <= 80 else line[:80] + "…"
            missing.append(f"не разобрал строку «{shown}»")
    if not party:
        missing.append("не вижу контрагента")
    if not items:
        missing.append("не вижу позиций")
    return ParsedOrder(party, items, "; ".join(comments), missing)

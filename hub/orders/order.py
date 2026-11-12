"""Поиск карточек и создание заказа покупателя. Единственная запись бота очереди.

Ищет так же, как справочный бот: юрлицо через Tools.org, контрагент фильтром name~,
товар через report/stock/all и entity/product. Пишет только MoySklad.post на
/entity/customerorder (docs/moysklad-api/documents/_customerOrder.md,
«Создать Заказ покупателя»): organization, agent, позиции с ценой в копейках.

HUB_DRY_RUN post не вызывает. Ключ dry: не мешает потом создать тот же заказ.
Повтор пары «чат-источник + message id» второй заказ не создаёт.
Это не автоматизация хаба: её запускает сообщение в очереди, не событие МойСклад.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..bot.tools import ToolError, Tools
from ..moysklad import MoySklad, meta_of
from .parse import ParsedOrder

log = logging.getLogger("hub.orders")
_DESCRIPTION = 4096
_ASSORTMENT = ("product", "variant", "bundle", "service")
_OPTIONS = 8


class OrderError(Exception):
    """Текст можно показать в чат. code остаётся в логе."""

    def __init__(self, user_message: str, *, code: str = "order_error"):
        super().__init__(user_message)
        self.user_message = user_message
        self.code = code


@dataclass(frozen=True)
class Source:
    chat_id: str
    message_id: str
    username: str = ""
    title: str = ""

    def link(self) -> str:
        if self.username and self.message_id:
            return f"https://t.me/{self.username}/{self.message_id}"
        if self.chat_id.startswith("-100") and self.chat_id[4:].isdigit() and self.message_id:
            return f"https://t.me/c/{self.chat_id[4:]}/{self.message_id}"
        return ""


@dataclass
class ProductHit:
    name: str
    href: str
    type: str
    price: float


@dataclass
class ResolvedLine:
    query: str
    quantity: float
    product: ProductHit | None = None
    options: list[ProductHit] = field(default_factory=list)


@dataclass
class Resolved:
    counterparty_query: str
    counterparty: dict | None
    counterparty_options: list[dict]
    lines: list[ResolvedLine]
    comment: str
    organization: dict | None
    org_error: str

    def complete(self) -> bool:
        return bool(
            self.organization and self.counterparty and self.lines
            and all(line.product for line in self.lines) and not self.org_error)


@dataclass
class Created:
    name: str = ""
    dry_run: bool = False
    duplicate: bool = False
    body: dict = field(default_factory=dict)


def source_from(msg: dict) -> Source:
    """Чат-источник и message id. У канала они есть в forward_origin.

    Если Telegram их не прислал, источник — само сообщение в очереди:
    повторное «да» не создаст второй заказ.
    """
    origin = msg.get("forward_origin") or {}
    kind = origin.get("type")
    if kind == "channel":
        chat = origin.get("chat") or {}
        mid = origin.get("message_id")
        if chat.get("id") is not None and mid is not None:
            return Source(str(chat["id"]), str(mid), str(chat.get("username") or ""),
                          str(chat.get("title") or ""))
    if kind == "chat":
        chat = origin.get("sender_chat") or {}
        mid = origin.get("message_id")
        if chat.get("id") is not None and mid is not None:
            return Source(str(chat["id"]), str(mid), str(chat.get("username") or ""),
                          str(chat.get("title") or ""))
    legacy = msg.get("forward_from_chat") or {}
    legacy_mid = msg.get("forward_from_message_id")
    if legacy.get("id") is not None and legacy_mid is not None:
        return Source(str(legacy["id"]), str(legacy_mid), str(legacy.get("username") or ""),
                      str(legacy.get("title") or ""))
    chat = msg.get("chat") or {}
    return Source(str(chat.get("id", "")), str(msg.get("message_id", "")))


def _choose(rows: list, query: str, name_of):
    query = query.strip()
    if not rows or not query:
        return None, []
    exact = [row for row in rows if name_of(row).casefold() == query.casefold()]
    if len(exact) == 1:
        return exact[0], []
    if len(rows) == 1:
        return rows[0], []
    return None, rows[:_OPTIONS]


def _kind(href: str, meta_type: str) -> str:
    if meta_type in _ASSORTMENT:
        return meta_type
    for kind in _ASSORTMENT:
        if f"/{kind}/" in href:
            return kind
    return ""


def _href(row: dict) -> str:
    return (row.get("meta") or {}).get("href", "").split("?")[0]


class Lookup:
    def __init__(self, ms: MoySklad, organization: str):
        self.ms = ms
        self.tools = Tools(ms)
        self.organization_name = organization.strip()
        self._org: dict | None = None
        self._org_error = ""
        self._org_loaded = False

    async def organization(self) -> tuple[dict | None, str]:
        if self._org_loaded:
            return self._org, self._org_error
        self._org_loaded = True
        if not self.organization_name:
            self._org_error = "Не задана организация (HUB_ORDER_ORGANIZATION)."
            return None, self._org_error
        try:
            self._org = await self.tools.org(self.organization_name)
        except ToolError as e:
            self._org_error = str(e)
        return self._org, self._org_error

    async def find_products(self, search: str) -> list[ProductHit]:
        search = search.strip()
        if not search:
            return []
        found: dict[str, ProductHit] = {}
        stock = await self.ms.get("report/stock/all", filter=f"search={search};stockMode=all", limit=100)
        for row in stock.get("rows") or []:
            hit = self._hit(row, price=float(row.get("salePrice") or 0))
            if hit:
                found[hit.href] = hit
        products = await self.ms.get("entity/product", search=search, filter="archived=false", limit=100)
        for row in products.get("rows") or []:
            if row.get("archived"):
                found.pop(_href(row), None)
                continue
            prices = row.get("salePrices") or []
            price = float(prices[0].get("value") or 0) if prices else 0.0
            hit = self._hit(row, price=price)
            if not hit:
                continue
            prev = found.get(hit.href)
            if prev is None:
                found[hit.href] = hit
            elif hit.price:
                prev.price = hit.price
        rows = list(found.values())
        words = search.casefold().split()
        strict = [row for row in rows if all(word in row.name.casefold() for word in words)]
        return strict or rows

    def _hit(self, row: dict, *, price: float) -> ProductHit | None:
        href = _href(row)
        kind = _kind(href, (row.get("meta") or {}).get("type", ""))
        name = (row.get("name") or "").strip()
        if not href or not kind or not name:
            return None
        return ProductHit(name, href, kind, price)

    async def resolve(self, parsed: ParsedOrder) -> Resolved:
        org, org_error = await self.organization()
        counterparty, options = None, []
        if parsed.counterparty:
            rows = await self.tools.counterparties(parsed.counterparty)
            rows = [row for row in rows if parsed.counterparty.casefold() in row.get("name", "").casefold()]
            counterparty, options = _choose(rows, parsed.counterparty, lambda row: row.get("name", ""))
        lines = []
        for item in parsed.lines:
            hits = await self.find_products(item.name)
            product, product_options = _choose(hits, item.name, lambda hit: hit.name)
            lines.append(ResolvedLine(item.name, item.quantity, product, product_options))
        return Resolved(parsed.counterparty, counterparty, options, lines, parsed.comment, org, org_error)


def description(comment: str, source: Source) -> str:
    link = source.link()
    origin = f"Источник: {link}" if link else "Источник:"
    tail = f"{origin}\nчат {source.chat_id}, сообщение {source.message_id}"
    text = f"{comment}\n{tail}".strip() if comment else tail
    return text[:_DESCRIPTION]


def build_body(resolved: Resolved, source: Source) -> dict:
    if not resolved.complete():
        raise OrderError("В заказе нет организации, контрагента или позиций.", code="not_ready")
    return {
        "organization": meta_of(resolved.organization),
        "agent": meta_of(resolved.counterparty),
        "description": description(resolved.comment, source),
        "positions": [
            {
                "quantity": line.quantity,
                "price": float(line.product.price),
                "assortment": {"meta": {
                    "href": line.product.href,
                    "type": line.product.type,
                    "mediaType": "application/json",
                }},
            }
            for line in resolved.lines
            if line.product is not None
        ],
    }


class OrderCreator:
    def __init__(self, ms: MoySklad, *, dry_run: bool, storage=None):
        self.ms = ms
        self.dry_run = dry_run
        self.storage = storage
        self.journal: list[dict] = []
        self._memory: dict[str, str] = {}

    def key(self, source: Source) -> str:
        base = f"order-queue:{source.chat_id}:{source.message_id}"
        return f"dry:{base}" if self.dry_run else base

    async def find(self, source: Source) -> str | None:
        if not source.chat_id or not source.message_id:
            return None
        key = self.key(source)
        if key in self._memory:
            return self._memory[key]
        if self.storage is None:
            return None
        value = await self.storage.get(key)
        if value is not None:
            self._memory[key] = value
        return value

    async def _remember(self, key: str, value: str) -> None:
        self._memory[key] = value
        if self.storage is None:
            return
        try:
            await self.storage.set(key, value)
        except Exception:
            log.exception("не записал ключ заказа %s", key)

    async def create(self, source: Source, resolved: Resolved) -> Created:
        if not source.chat_id or not source.message_id:
            raise OrderError("Не вижу номер сообщения, заказ не создаю.", code="no_source")
        body = build_body(resolved, source)
        known = await self.find(source)
        if known is not None:
            return Created(name="" if known == "dry" else known, dry_run=known == "dry",
                           duplicate=True, body=body)
        who = resolved.counterparty["name"] if resolved.counterparty else ""
        positions = "; ".join(
            f"{line.product.name} × {line.quantity}" for line in resolved.lines if line.product)
        self.journal.append({"dry_run": self.dry_run, "source": self.key(source), "body": body})
        if self.dry_run:
            log.info("dry-run, заказ не создан, чат %s сообщение %s, %s: %s",
                     source.chat_id, source.message_id, who, positions)
            await self._remember(self.key(source), "dry")
            return Created(dry_run=True, body=body)
        doc = await self.ms.post("entity/customerorder", body)
        name = str(doc.get("name") or "")
        log.info("создан заказ %s, чат %s сообщение %s, %s: %s",
                 name, source.chat_id, source.message_id, who, positions)
        await self._remember(self.key(source), name)
        return Created(name=name, body=body)

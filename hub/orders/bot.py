"""Чат-очередь: long polling, черновик и слово «да».

Слушает только HUB_ORDER_CHAT. Чужой чат получает одну строку с chat id:
текст оттуда не разбирается. Голос и фото заказом не становятся.
Создание заказа — hub/orders/order.py, и только после «да».
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

import httpx

from ..moysklad import MoySkladError
from .order import Created, Lookup, OrderCreator, OrderError, Resolved, Source, source_from
from .parse import parse_order_text

log = logging.getLogger("hub.orders")
API = "https://api.telegram.org"
LIMIT = 4000
_YES = re.compile(r"^(?:да|подтверждаю)[.!]?$", re.IGNORECASE)
_NO = re.compile(r"^(?:нет|отмена|отменить)[.!]?$", re.IGNORECASE)
_NUMBER = re.compile(r"^\d{1,2}$")
_SERVICE = ("new_chat_members", "left_chat_member", "new_chat_member", "group_chat_created",
            "supergroup_chat_created", "channel_chat_created")

HELP = """Я собираю заказ покупателя из этого чата. После «да» создаю его в МойСклад.

Перешлите сюда текст или напишите так:
ООО «Кофейня»
Бразилия Сантос — 2 кг
Эфиопия — 1

Пришлю черновик. «да» — создать заказ, «нет» — отменить.
/cancel — сбросить черновик.

В пробном режиме (HUB_DRY_RUN) номер не появится: в МойСклад ничего не пишется.
Голос, фото и стикер не читаю: пришлите текст. Публичные чаты я не смотрю."""


def chunks(text: str, limit: int = LIMIT) -> list[str]:
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out + ([text] if text else [])


def money(kop: float) -> str:
    rub = (kop or 0) / 100
    if abs(rub - round(rub)) < 0.001:
        body = f"{int(round(rub)):,}".replace(",", " ")
    else:
        body = f"{rub:,.2f}".replace(",", " ").replace(".", ",")
    return f"{body} ₽"


def qty_text(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.3f}".rstrip("0").rstrip(".").replace(".", ",")


def _gap(resolved: Resolved):
    if not resolved.counterparty and resolved.counterparty_options:
        return "counterparty", resolved.counterparty_options
    for index, line in enumerate(resolved.lines):
        if line.product is None and line.options:
            return f"line:{index}", line.options
    return None


def render(resolved: Resolved, source: Source, *, dry_run: bool) -> str:
    lines = ["Черновик заказа"]
    if dry_run:
        lines.append("Пробный режим: в МойСклад ничего не запишется, номер не появится.")
    if resolved.organization:
        lines.append(f"Организация: {resolved.organization['name']}")
    elif resolved.org_error:
        lines.append(resolved.org_error)
    gap = _gap(resolved)
    if resolved.counterparty and not (gap and gap[0] == "counterparty"):
        lines.append(f"Контрагент: {resolved.counterparty['name']}")
    elif gap and gap[0] == "counterparty":
        lines.append("Несколько контрагентов. Ответьте номером:")
        lines.extend(f"{i}. {row['name']}" for i, row in enumerate(gap[1], 1))
        return "\n".join(lines)
    elif resolved.counterparty_query:
        lines.append(f"Контрагент «{resolved.counterparty_query}» не найден.")
    else:
        lines.append("Не вижу контрагента.")
        if not resolved.lines:
            lines.append("Напишите так:")
            lines.append("ООО «Кофейня»")
            lines.append("Бразилия Сантос — 2 кг")
            return "\n".join(lines)
    for index, line in enumerate(resolved.lines):
        if gap and gap[0] == f"line:{index}":
            lines.append(f"«{line.query}» подходит к нескольким. Ответьте номером:")
            lines.extend(f"{i}. {hit.name}" for i, hit in enumerate(gap[1], 1))
            return "\n".join(lines)
        if line.product:
            price = money(line.product.price)
            if not line.product.price:
                price += ", в карточке нет цены"
            lines.append(f"{line.product.name} — {qty_text(line.quantity)} — {price}")
        else:
            lines.append(f"Товар «{line.query}» не найден.")
    if resolved.comment:
        lines.append(f"Комментарий: {resolved.comment}")
    if link := source.link():
        lines.append(f"Источник: {link}")
    lines.append(f"чат {source.chat_id}, сообщение {source.message_id}")
    if resolved.complete():
        lines.append("Ответьте «да», чтобы создать заказ. «нет» — отменить.")
    else:
        lines.append("Заказ пока не создаю.")
    return "\n".join(lines)


def _already(known: str) -> str:
    if known == "dry":
        return "По этому сообщению пробный прогон уже был. Заказ не создан, номера нет."
    if known:
        return f"Этот заказ уже есть: {known}. Второй раз не создаю."
    return "Этот заказ уже создавался. Второй раз не создаю."


def _created(result: Created) -> str:
    if result.duplicate:
        return _already("dry" if result.dry_run else result.name)
    if result.dry_run:
        return "Пробный прогон. Заказ не создан, номер не присвоен."
    if result.name:
        return f"Заказ {result.name} создан."
    return "Заказ создан."


@dataclass
class _Pending:
    source: Source
    resolved: Resolved


class OrderSession:
    """Один черновик на очередь. «Да» создаёт заказ, только если поля уже однозначны."""

    def __init__(self, lookup: Lookup, creator: OrderCreator):
        self.lookup = lookup
        self.creator = creator
        self.pending: _Pending | None = None
        self._lock = asyncio.Lock()

    async def cancel(self) -> None:
        async with self._lock:
            self.pending = None

    async def handle_message(self, msg: dict) -> str:
        text = (msg.get("text") or "").strip()
        async with self._lock:
            if _NO.match(text):
                self.pending = None
                return "Черновик сброшен."
            if _YES.match(text):
                return await self._confirm()
            if self.pending is not None:
                chosen = self._choose(text)
                if chosen is not None:
                    return chosen
            return await self._draft(msg, text)

    def _choose(self, text: str) -> str | None:
        if self.pending is None:
            return None
        gap = _gap(self.pending.resolved)
        if gap is None:
            return None
        kind, options = gap
        option = None
        if _NUMBER.match(text):
            number = int(text)
            if not 1 <= number <= len(options):
                return f"Нужен номер от 1 до {len(options)}."
            option = options[number - 1]
        else:
            names = [row["name"] if isinstance(row, dict) else row.name for row in options]
            exact = [i for i, name in enumerate(names) if name.casefold() == text.casefold()]
            if len(exact) == 1:
                option = options[exact[0]]
            elif len(text) >= 3:
                part = [i for i, name in enumerate(names) if text.casefold() in name.casefold()]
                if len(part) == 1:
                    option = options[part[0]]
        if option is None:
            return None
        resolved = self.pending.resolved
        if kind == "counterparty":
            resolved.counterparty = option
            resolved.counterparty_options = []
            resolved.counterparty_query = option["name"]
        else:
            line = resolved.lines[int(kind.split(":")[1])]
            line.product = option
            line.options = []
        return self._view()

    async def _draft(self, msg: dict, text: str) -> str:
        parsed = parse_order_text(text)
        source = source_from(msg)
        known = await self.creator.find(source)
        if known is not None:
            return _already(known)
        try:
            resolved = await self.lookup.resolve(parsed)
        except MoySkladError as e:
            log.warning("заказ: МойСклад %s: %s", e.status, e.message)
            return "МойСклад не ответил. Повторите сообщение чуть позже."
        replaced = self.pending is not None
        self.pending = _Pending(source, resolved)
        view = self._view()
        return "Предыдущий черновик заменён.\n" + view if replaced else view

    async def _confirm(self) -> str:
        if self.pending is None:
            return "Нет черновика. Перешлите текст заказа."
        if not self.pending.resolved.complete():
            return self._view()
        try:
            result = await self.creator.create(self.pending.source, self.pending.resolved)
        except OrderError as e:
            log.warning("заказ не создан (%s): %s", e.code, e.user_message)
            return e.user_message
        except MoySkladError as e:
            log.warning("заказ: МойСклад %s: %s", e.status, e.message)
            return "МойСклад не записал заказ. Черновик на месте, ответьте «да» ещё раз."
        self.pending = None
        return _created(result)

    def _view(self) -> str:
        if self.pending is None:
            return "Нет черновика. Перешлите текст заказа."
        return render(self.pending.resolved, self.pending.source, dry_run=self.creator.dry_run)


class OrderQueueBot:
    def __init__(self, token: str, chat_id: str, session: OrderSession, *,
                 http: httpx.AsyncClient | None = None, poll_timeout: int = 50):
        self.session = session
        self.chat_id = str(chat_id)
        self.poll_timeout = poll_timeout
        self.http = http or httpx.AsyncClient(timeout=poll_timeout + 15)
        self._url = f"{API}/bot{token}/"
        self._offset = 0
        self._tasks: set[asyncio.Task] = set()

    async def close(self) -> None:
        await self.http.aclose()

    async def api(self, method: str, **data) -> dict:
        response = await self.http.post(self._url + method, json=data)
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        if response.status_code != 200 or not body.get("ok"):
            raise RuntimeError(f"Telegram {method}: {response.status_code} {body.get('description', '')}")
        return body["result"]

    async def send(self, chat: str, text: str) -> None:
        for part in chunks(text, LIMIT):
            await self.api("sendMessage", chat_id=chat, text=part, disable_web_page_preview=True)

    async def poll_once(self) -> int:
        updates = await self.api("getUpdates", offset=self._offset, timeout=self.poll_timeout,
                                 allowed_updates=["message"])
        for update in updates:
            self._offset = max(self._offset, update["update_id"] + 1)
            msg = update.get("message")
            if not msg:
                continue
            task = asyncio.create_task(self.handle(msg))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return len(updates)

    async def run(self) -> None:
        me = await self.api("getMe")
        log.info("бот очереди @%s слушает чат %s", me.get("username"), self.chat_id)
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("бот очереди getUpdates: %s — повторю через 10 с", e)
                await asyncio.sleep(10)

    async def handle(self, msg: dict) -> None:
        chat = str((msg.get("chat") or {}).get("id", ""))
        try:
            if chat != self.chat_id:
                if chat:
                    log.warning("бот очереди: чужой чат %s", chat)
                    await self.send(
                        chat,
                        "Нет доступа. Этот бот слушает только чат-очередь. "
                        f"Ваш chat id: {chat} — его ставят в HUB_ORDER_CHAT.")
                return
            if any(msg.get(key) for key in _SERVICE):
                return
            text = (msg.get("text") or "").strip()
            if not text:
                await self.send(chat, "Пришлите текст.")
                return
            command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
            if command in ("/start", "/help"):
                await self.send(chat, HELP)
                return
            if command in ("/new", "/cancel"):
                await self.session.cancel()
                await self.send(chat, "Черновик сброшен.")
                return
            if command:
                await self.send(chat, "Такой команды нет. /help — как оформить заказ.")
                return
            await self.send(chat, await self.session.handle_message(msg))
        except Exception:
            log.exception("бот очереди: не удалось ответить в чат %s", chat)
            if chat:
                try:
                    await self.send(chat, "Не получилось обработать сообщение. Попробуйте ещё раз или /cancel.")
                except Exception:
                    log.exception("бот очереди: не удалось отправить ошибку")


def make_order_bot(settings, ms, storage=None, *, http: httpx.AsyncClient | None = None) -> OrderQueueBot:
    lookup = Lookup(ms, settings.order_organization)
    creator = OrderCreator(ms, dry_run=settings.dry_run, storage=storage)
    return OrderQueueBot(settings.order_bot_token, settings.order_chat,
                         OrderSession(lookup, creator), http=http)

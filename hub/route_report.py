"""Маршрутный лист за месяц по статусу: только чтение МойСклад, файл XLSX локально.

В отличие от logistics_sheet здесь не нужен переход статуса и вебхуки: берутся все
заказы покупателя с датой (moment) в месяце, которые сейчас в нужном статусе.
Ничего не пишет ни в МойСклад, ни в базу хаба, ни на Диск.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date

from .automations.logistics_sheet import delivery_address
from .entities import Order, from_api
from .moysklad import MoySklad

ALL = "all"
MONTHS = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
          "сентябрь", "октябрь", "ноябрь", "декабрь")
CANCELLED = frozenset({"отменён", "отменен", "отмена"})


def _norm(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def parse_month(text: str) -> date:
    """«2026-11» или «11.2026» → первое число месяца."""
    text = text.strip()
    for sep, order in (("-", (0, 1)), (".", (1, 0))):
        if sep in text:
            parts = text.split(sep)
            if len(parts) == 2 and all(p.isdigit() for p in parts):
                year, month = int(parts[order[0]]), int(parts[order[1]])
                if 1 <= month <= 12:
                    return date(year, month, 1)
    raise ValueError(f"месяц «{text}»: нужно ГГГГ-ММ, например 2026-11")


def month_filter(first: date) -> str:
    last = monthrange(first.year, first.month)[1]
    return (f"moment>={first.isoformat()} 00:00:00;"
            f"moment<={first.replace(day=last).isoformat()} 23:59:59")


def month_title(first: date) -> str:
    return f"{MONTHS[first.month - 1]} {first.year}"


def _day(moment: str) -> str:
    """«2026-11-20 10:00:00.000» → «20.11.2026»; пусто — пусто."""
    text = (moment or "")[:10]
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return f"{text[8:10]}.{text[5:7]}.{text[:4]}"
    return text


def _cancelled(order: Order) -> bool:
    if _norm(order.state) in CANCELLED:
        return True
    return ((order.raw or {}).get("state") or {}).get("stateType") == "Unsuccessful"


async def _phone(ms: MoySklad, order: Order, cache: dict[str, str], phone_field: str) -> str:
    if phone_field:
        value = order.attr(phone_field)
        if value is not None and str(value).strip():
            return str(value).strip()
    own = (order.agent.phone or "").strip()
    if own:
        return own
    agent_id = order.agent.id
    if not agent_id:
        return ""
    if agent_id not in cache:
        cache[agent_id] = ""
        async for row in ms.iterate(f"entity/counterparty/{agent_id}/contactpersons", limit=100):
            phone = str(row.get("phone") or "").strip()
            if phone:
                cache[agent_id] = phone
                break
    return cache[agent_id]


async def collect(ms: MoySklad, first: date, state: str, *, hours_field: str = "",
                  phone_field: str = "", with_cancelled: bool = False) -> list[dict]:
    """Заказы месяца в статусе state (или все при state='all'), по дате заказа."""
    want = _norm(state)
    phones: dict[str, str] = {}
    out: list[dict] = []
    async for row in ms.list("customerorder", filter=month_filter(first),
                             expand="agent,state", limit=100):
        order = from_api("customerorder", row)
        if want != ALL and _norm(order.state) != want:
            continue
        if want == ALL and not with_cancelled and _cancelled(order):
            continue
        hours = order.attr(hours_field) if hours_field else None
        out.append({
            "id": order.id,
            "name": order.name,
            "moment": order.moment,
            "date": order.date,
            "delivery": _day(order.delivery_planned),
            "counterparty": order.agent.name,
            "address": delivery_address(order),
            "phone": await _phone(ms, order, phones, phone_field),
            "hours": "" if hours is None else str(hours).strip(),
            "sum": order.sum,
            "state": order.state,
            "comment": (order.description or "").strip(),
        })
    out.sort(key=lambda o: (o["moment"], o["name"]))
    return out

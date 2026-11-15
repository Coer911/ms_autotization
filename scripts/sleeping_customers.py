"""Спящие покупатели: кто заказывал раньше, но не заказал в выбранном месяце.

Только читает МойСклад (заказы покупателей и контрагентов), ничего не пишет.

    MOYSKLAD_TOKEN=... python3 scripts/sleeping_customers.py 2026-09 2026-11 --top 20

Для каждого месяца: контрагенты, у которых есть заказы за 6 месяцев до него
(или после — в соседних месяцах окна), но нет ни одного заказа в самом месяце.
Сортировка — по сумме заказов за окно, отменённые заказы не считаются.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.moysklad.client import MoySklad, entity_id  # noqa: E402


def month_bounds(ym: str) -> tuple[date, date]:
    y, m = map(int, ym.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), m % 12 + 1, 1)
    return start, end


def shift(d: date, months: int) -> date:
    idx = d.year * 12 + d.month - 1 + months
    return date(idx // 12, idx % 12 + 1, 1)


async def load_orders(ms: MoySklad, since: date, until: date) -> list[dict]:
    last = date.fromordinal(until.toordinal() - 1)
    flt = f"moment>={since:%Y-%m-%d} 00:00:00;moment<={last:%Y-%m-%d} 23:59:59"
    return [o async for o in ms.iterate("entity/customerorder", filter=flt,
                                        expand="agent,state", limit=100)]


def cancelled(order: dict) -> bool:
    return "отмен" in ((order.get("state") or {}).get("name") or "").lower()


def analyse(orders: list[dict], month: str, history: int = 6) -> list[dict]:
    m_start, m_end = month_bounds(month)
    w_start = shift(m_start, -history)
    stats: dict[str, dict] = {}
    ordered_in_month: set[str] = set()
    for o in orders:
        if cancelled(o) or not o.get("agent"):
            continue
        day = date.fromisoformat(o["moment"][:10])
        aid = entity_id(o["agent"]["meta"]["href"])
        if m_start <= day < m_end:
            ordered_in_month.add(aid)
            continue
        if not (w_start <= day < m_start):
            continue
        a = o["agent"]
        s = stats.setdefault(aid, {"id": aid, "name": a.get("name", aid), "phone": a.get("phone", ""),
                                   "email": a.get("email", ""), "sum": 0, "orders": 0, "last": day,
                                   "months": set()})
        s["sum"] += o.get("sum", 0)
        s["orders"] += 1
        s["last"] = max(s["last"], day)
        s["months"].add(day.strftime("%Y-%m"))
    rows = [s for aid, s in stats.items() if aid not in ordered_in_month]
    rows.sort(key=lambda s: s["sum"], reverse=True)
    return rows


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("months", nargs="+", help="месяцы ГГГГ-ММ")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--history", type=int, default=6, help="сколько месяцев до месяца смотреть")
    p.add_argument("--csv", help="куда сохранить CSV")
    args = p.parse_args()

    starts = [month_bounds(m) for m in args.months]
    since = shift(min(s for s, _ in starts), -args.history)
    until = max(e for _, e in starts)
    async with MoySklad(os.environ.get("MOYSKLAD_TOKEN", "")) as ms:
        orders = await load_orders(ms, since, until)
    print(f"Заказов покупателей {since}…{until}: {len(orders)}\n")

    out = []
    for month in args.months:
        rows = analyse(orders, month, args.history)
        print(f"## Не заказывали в {month} — всего {len(rows)}, топ {args.top}\n")
        for i, s in enumerate(rows[: args.top], 1):
            contact = ", ".join(x for x in (s["phone"], s["email"]) if x) or "нет контакта"
            print(f"{i}. {s['name']} — {s['sum'] / 100:,.0f} ₽ за {s['orders']} зак., "
                  f"последний {s['last']:%d.%m}, {contact}".replace(",", " ", 1))
            out.append({"месяц": month, "место": i, "контрагент": s["name"],
                        "сумма_руб": round(s["sum"] / 100), "заказов": s["orders"],
                        "последний_заказ": s["last"].isoformat(), "телефон": s["phone"],
                        "email": s["email"], "id": s["id"]})
        print()
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(out[0]) if out else ["месяц"], delimiter=";")
            w.writeheader()
            w.writerows(out)
        print(f"CSV: {args.csv}")


if __name__ == "__main__":
    asyncio.run(main())

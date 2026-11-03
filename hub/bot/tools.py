"""Что бот умеет спрашивать у МойСклад. Только чтение — записи в МойСклад здесь нет.

Каждая функция — один инструмент для модели: понятное описание, JSON-схема параметров,
ответ — компактный JSON (рубли, а не копейки; короткие списки с «ещё N»). Считается здесь,
в коде, а не моделью: модель выбирает инструмент и пересказывает результат.

Правила API, на которые опирается расчёт (проверено на живом аккаунте):
  * report/profit/* — суммы в копейках; фильтр counterparty понимает только «=»,
    поэтому «все, кроме X» считается вычитанием отчёта по X из общего;
  * report/stock/all — price это себестоимость единицы, salePrice — цена продажи;
  * расход сырья виден только в report/turnover/byoperations (техоперации, отгрузки,
    списания), отчёт прибыльности его не показывает.
"""
from __future__ import annotations

import asyncio
import calendar
import json
import math
import re
from collections import defaultdict
from datetime import date, timedelta

from ..moysklad import MoySklad, MoySkladError

ENTITY = "entity/"
OUTFLOW = ("demand", "retaildemand", "processing", "loss", "productionstagecompletion")
MAX_ROWS = 40
MAX_REORDER_ITEMS = 80


def rub(kop: float) -> float:
    return round((kop or 0) / 100, 2)


def num(x: float) -> float:
    return round(x or 0, 3)


def clip(rows: list, n: int = MAX_ROWS) -> dict:
    return {"rows": rows[:n], "more": max(0, len(rows) - n)}


def parse_day(text: str) -> date:
    return date.fromisoformat(text.strip()[:10])


def month_starts(today: date, months: int) -> tuple[date, date]:
    """Последние `months` полных месяцев перед текущим: (первый день, последний день)."""
    first_this = today.replace(day=1)
    end = first_this - timedelta(days=1)
    y, m = first_this.year, first_this.month - months
    while m <= 0:
        y, m = y - 1, m + 12
    return date(y, m, 1), end


class ToolError(Exception):
    """Ошибка в параметрах — уходит модели как is_error, чтобы она поправилась."""


class Tools:
    def __init__(self, ms: MoySklad, *, internal: list[str] = (), payment_field: str = "Оплата",
                 today=date.today):
        self.ms = ms
        self.internal = [s.lower() for s in internal]   # перемещения под видом покупателя (Озон)
        self.payment_field = payment_field
        self.today = today

    def href(self, kind: str, id_: str) -> str:
        return f"{self.ms.base}/{ENTITY}{kind}/{id_}"

    # ── справочники ──────────────────────────────────────────────────────────

    async def organizations(self) -> list[dict]:
        return await self.ms.cached("bot:orgs", lambda: self._rows(ENTITY + "organization"))

    async def _rows(self, path: str, **params) -> list[dict]:
        return [r async for r in self.ms.iterate(path, limit=1000, **params)]

    async def org(self, name: str) -> dict | None:
        if not name:
            return None
        orgs = await self.organizations()
        want = name.lower().replace(".", " ").split()
        hits = [o for o in orgs if all(w in o["name"].lower().replace(".", " ") for w in want)]
        if not hits:
            raise ToolError(f"организация «{name}» не найдена; есть: "
                            + "; ".join(o["name"] for o in orgs))
        if len(hits) > 1:
            exact = [o for o in hits if o["name"].lower() == name.lower()]
            if not exact:     # «Краснояров Данил» — основное ИП, а не «… (ОЗОН БАНК)»
                exact = [o for o in hits if "(" not in o["name"]]
            if len(exact) != 1:
                raise ToolError(f"«{name}» подходит к нескольким: " + "; ".join(o["name"] for o in hits))
            hits = exact
        return hits[0]

    async def counterparties(self, name: str) -> list[dict]:
        return (await self.ms.get(ENTITY + "counterparty", filter=f"name~{name}", limit=100))["rows"]

    def is_internal(self, name: str) -> bool:
        low = name.lower()
        return any(s in low for s in self.internal)

    # ── остатки ──────────────────────────────────────────────────────────────

    async def items(self, search: str, store: str = "") -> list[dict]:
        """Позиции под поиск с остатком. Отчёт остатков не отдаёт позиции с нулевым
        остатком даже при stockMode=all, поэтому их добираем из карточек товаров."""
        flt = f"search={search};stockMode=all"
        if store:
            stores = (await self.ms.get(ENTITY + "store", search=store, limit=10))["rows"]
            if not stores:
                raise ToolError(f"склад «{store}» не найден")
            flt += f";store={self.href('store', stores[0]['id'])}"
        out = {}
        for r in await self._rows("report/stock/all", filter=flt):
            href = r["meta"]["href"].split("?")[0]
            out[href] = {"name": r["name"], "article": r.get("article"), "href": href,
                         "stock": num(r.get("stock")), "reserve": num(r.get("reserve")),
                         "in_transit": num(r.get("inTransit")),
                         "uom": (r.get("uom") or {}).get("name", ""), "cost": rub(r.get("price")),
                         "sale_price": rub(r.get("salePrice"))}
        products = (await self.ms.get(ENTITY + "product", search=search, filter="archived=false",
                                      limit=100))["rows"]
        for p in products:
            href = p["meta"]["href"].split("?")[0]
            if href not in out:
                out[href] = {"name": p["name"], "article": p.get("article"), "href": href,
                             "stock": 0, "reserve": 0, "in_transit": 0,
                             "uom": "", "cost": rub((p.get("buyPrice") or {}).get("value")),
                             "sale_price": rub(((p.get("salePrices") or [{}])[0]).get("value"))}
        return sorted(out.values(), key=lambda r: r["name"])

    async def stock(self, search: str, store: str = "", only_positive: bool = False) -> dict:
        rows = [{k: v for k, v in r.items() if k != "href"} for r in await self.items(search, store)
                if not only_positive or r["stock"] > 0]
        return {"search": search, "count": len(rows), **clip(rows, 80)}

    # ── продажи ──────────────────────────────────────────────────────────────

    async def _profit(self, kind: str, a: date, b: date, flt: str) -> list[dict]:
        params = {"momentFrom": f"{a} 00:00:00", "momentTo": f"{b} 23:59:59"}
        if flt:
            params["filter"] = flt
        return await self._rows(f"report/profit/{kind}", **params)

    async def sales(self, date_from: str, date_to: str, organization: str = "",
                    search: str = "", exclude: list[str] | None = None,
                    include_internal: bool = False, group_by: str = "product") -> dict:
        a, b = parse_day(date_from), parse_day(date_to)
        if b < a:
            raise ToolError("date_to раньше date_from")
        if group_by == "counterparty" and search:
            raise ToolError("group_by=counterparty вместе с search не поддерживается")
        org = await self.org(organization)
        base = f"organization={self.href('organization', org['id'])}" if org else ""
        excl = [x.lower() for x in (exclude or [])]
        if not include_internal:
            excl += self.internal
        by_cp = await self._profit("bycounterparty", a, b, base)
        drop = [r for r in by_cp if any(x in r["counterparty"]["name"].lower() for x in excl)]
        keep = [r for r in by_cp if r not in drop]
        fields = ("sellQuantity", "sellSum", "sellCostSum", "returnQuantity", "returnSum", "profit")
        if group_by == "counterparty":
            rows = [{"name": r["counterparty"]["name"], **{f: r.get(f, 0) for f in fields}} for r in keep]
        else:
            # по товарам: «все, кроме X» = общий отчёт минус отчёты по каждому X
            total = await self._profit("byproduct", a, b, base)
            minus: dict[str, dict] = defaultdict(lambda: defaultdict(float))
            for r in drop:
                flt = ";".join(filter(None, [base, f"counterparty={r['counterparty']['meta']['href'].split('?')[0]}"]))
                for x in await self._profit("byproduct", a, b, flt):
                    for f in fields:
                        minus[x["assortment"]["meta"]["href"]][f] += x.get(f, 0)
            rows = []
            for x in total:
                m = minus.get(x["assortment"]["meta"]["href"], {})
                rows.append({"name": x["assortment"]["name"], **{f: x.get(f, 0) - m.get(f, 0) for f in fields}})
            if search:
                words = search.lower().split()
                rows = [r for r in rows if all(w in r["name"].lower() for w in words)]
        rows = [r for r in rows if abs(r["sellSum"]) > 0.5 or abs(r["returnSum"]) > 0.5
                or abs(r["profit"]) > 0.5]
        tot = {f: sum(r[f] for r in rows) for f in fields}
        revenue = tot["sellSum"] - tot["returnSum"]
        out = [{"name": r["name"], "qty": num(r["sellQuantity"]), "revenue": rub(r["sellSum"]),
                "cost": rub(r["sellCostSum"]), "returns": rub(r["returnSum"]), "profit": rub(r["profit"])}
               for r in sorted(rows, key=lambda r: -r["sellSum"])]
        return {"period": f"{a}..{b}", "organization": org["name"] if org else "все",
                "excluded_counterparties": sorted({r["counterparty"]["name"] for r in drop}),
                "excluded_revenue": rub(sum(r["sellSum"] for r in drop)),
                "total": {"revenue": rub(tot["sellSum"]), "cost": rub(tot["sellCostSum"]),
                          "returns": rub(tot["returnSum"]), "profit": rub(tot["profit"]),
                          "margin_pct": round(tot["profit"] / revenue * 100, 1) if revenue else None,
                          "qty": num(tot["sellQuantity"])},
                "by": group_by, **clip(out)}

    async def sales_by_month(self, search: str, months: int = 6, organization: str = "") -> dict:
        """Помесячно по товарам, чьё название содержит все слова search."""
        today = self.today()
        months = max(1, min(int(months), 24))
        y, m = today.year, today.month
        out = []
        for _ in range(months):
            a = date(y, m, 1)
            b = date(y, m, calendar.monthrange(y, m)[1])
            r = await self.sales(str(a), str(min(b, today)), organization, search)
            out.append({"month": a.strftime("%Y-%m"), "qty": r["total"]["qty"],
                        "revenue": r["total"]["revenue"], "profit": r["total"]["profit"],
                        "items": [{"name": x["name"], "qty": x["qty"]} for x in r["rows"]]})
            y, m = (y, m - 1) if m > 1 else (y - 1, 12)
        out.reverse()
        return {"search": search, "months": out}

    # ── заказ поставщику ─────────────────────────────────────────────────────

    async def outflow(self, href: str, a: date, b: date) -> float:
        kind = href.rstrip("/").split("/")[-2]
        if kind not in ("product", "variant"):        # комплекты и услуги на складе не лежат
            return 0.0
        total = 0.0
        async for o in self.ms.iterate("report/turnover/byoperations", limit=1000,
                                       filter=f"{kind}={href}",
                                       momentFrom=f"{a} 00:00:00", momentTo=f"{b} 23:59:59"):
            if o["quantity"] < 0 and o["operation"]["meta"]["type"] in OUTFLOW:
                total += -o["quantity"]
        return total

    async def reorder(self, search: str, days: int = 30, months: int = 3,
                      min_qty: float = 0, round_to: float = 1, store: str = "") -> dict:
        """Сколько докупить, чтобы хватило на days дней по среднему расходу за months полных месяцев."""
        days = max(1, min(int(days), 365))
        months = max(1, min(int(months), 12))
        a, b = month_starts(self.today(), months)
        span = (b - a).days + 1
        items = await self.items(search, store)
        if len(items) > MAX_REORDER_ITEMS:
            raise ToolError(f"под «{search}» {len(items)} позиций — уточните поиск "
                            f"(не больше {MAX_REORDER_ITEMS})")
        sem = asyncio.Semaphore(3)

        async def one(it):
            async with sem:
                used = await self.outflow(it["href"], a, b)
            per_day = used / span
            have = it["stock"] + it["in_transit"]
            need = per_day * days - have
            qty = 0.0
            if need > 0 and per_day > 0:
                step = round_to or 1
                qty = max(math.ceil(need / step - 1e-9) * step, min_qty)
            return {"name": it["name"], "stock": it["stock"], "in_transit": it["in_transit"],
                    "used_per_month": num(per_day * 30.5),
                    "days_left": round(have / per_day) if per_day else None,
                    "order": num(qty), "cost": it["cost"], "sum": round(qty * it["cost"], 2)}

        rows = await asyncio.gather(*(one(it) for it in items))
        to_order = sorted([r for r in rows if r["order"] > 0], key=lambda r: (r["days_left"] or 0))
        rest = [r for r in rows if r["order"] <= 0 and r["used_per_month"] > 0]
        return {"search": search, "days": days, "history": f"{a}..{b}",
                "order": to_order, "order_total_qty": num(sum(r["order"] for r in to_order)),
                "order_total_sum": round(sum(r["sum"] for r in to_order), 2),
                "enough": [{"name": r["name"], "stock": r["stock"], "days_left": r["days_left"]}
                           for r in sorted(rest, key=lambda r: r["days_left"] or 0)][:MAX_ROWS],
                "unused": sorted(r["name"] for r in rows if r["used_per_month"] == 0 and r["stock"] > 0)}

    async def supplier_prices(self, supplier: str, months: int = 12, search: str = "") -> dict:
        """Что и почём брали у поставщика: последняя цена и сколько раз."""
        cps = await self.counterparties(supplier)
        if not cps:
            raise ToolError(f"поставщик «{supplier}» не найден")
        if len(cps) > 1:
            exact = [c for c in cps if c["name"].lower() == supplier.lower()]
            if len(exact) != 1:
                raise ToolError("уточните поставщика: " + "; ".join(c["name"] for c in cps[:10]))
            cps = exact
        cp = cps[0]
        since = self.today() - timedelta(days=31 * max(1, min(int(months), 36)))
        supplies = await self._rows(ENTITY + "supply", filter=f"agent={self.href('counterparty', cp['id'])};"
                                    f"moment>={since} 00:00:00", order="moment,asc")
        items: dict[str, dict] = {}
        for s in supplies:
            for p in await self._rows(f"{ENTITY}supply/{s['id']}/positions", expand="assortment"):
                name = p["assortment"]["name"]
                if search and not all(w in name.lower() for w in search.lower().split()):
                    continue
                it = items.setdefault(name, {"name": name, "times": 0, "qty": 0.0})
                it.update(last_date=s["moment"][:10], last_price=rub(p["price"]))
                it["times"] += 1
                it["qty"] = num(it["qty"] + p["quantity"])
        rows = sorted(items.values(), key=lambda r: r["name"])
        return {"supplier": cp["name"], "since": str(since), "supplies": len(supplies),
                "supplies_sum": rub(sum(s.get("sum", 0) for s in supplies)), **clip(rows, 80)}

    async def tech_card(self, search: str) -> dict:
        plans = (await self.ms.get(ENTITY + "processingplan", search=search, limit=20))["rows"]
        out = []
        for p in plans[:6]:
            mats = await self._rows(f"{ENTITY}processingplan/{p['id']}/materials", expand="assortment")
            prods = await self._rows(f"{ENTITY}processingplan/{p['id']}/products", expand="assortment")
            out.append({"name": p["name"],
                        "makes": [{"name": x["assortment"]["name"], "qty": x["quantity"]} for x in prods],
                        "materials": [{"name": x["assortment"]["name"], "qty": x["quantity"]} for x in mats]})
        return {"search": search, "found": len(plans), "cards": out}

    # ── долги покупателей ────────────────────────────────────────────────────

    def term_days(self, term: str | None) -> int | None:
        if not term:
            return None
        if "предоплат" in term.lower():
            return 0
        m = re.search(r"(\d+)", term)
        return int(m.group(1)) if m else None

    def term_of(self, obj: dict | None) -> str | None:
        for a in (obj or {}).get("attributes", []):
            if a.get("name") == self.payment_field:
                v = a.get("value")
                return v.get("name") if isinstance(v, dict) else v
        return None

    async def unpaid(self, date_from: str, organization: str = "", counterparty: str = "",
                     default_term_days: int | None = None, only_overdue: bool = False) -> dict:
        today = self.today()
        a = parse_day(date_from)
        flt = [f"moment>={a} 00:00:00", "applicable=true"]
        org = await self.org(organization)
        if org:
            flt.append(f"organization={self.href('organization', org['id'])}")
        if counterparty:
            cps = await self.counterparties(counterparty)
            if not cps:
                raise ToolError(f"контрагент «{counterparty}» не найден")
            if len(cps) > 1:
                raise ToolError("уточните контрагента: " + "; ".join(c["name"] for c in cps[:10]))
            flt.append(f"agent={self.href('counterparty', cps[0]['id'])}")
        demands = await self._rows(ENTITY + "demand", filter=";".join(flt), expand="agent")
        orders: dict[str, dict] = {}
        rows = []
        for d in demands:
            debt = d["sum"] - d.get("payedSum", 0)
            if debt < 50 or self.is_internal(d["agent"]["name"]):     # копейки недоплаты не долг
                continue
            term, src = self.term_of(d), "отгрузка"
            if not term and d.get("customerOrder"):
                h = d["customerOrder"]["meta"]["href"]
                if h not in orders:
                    try:
                        orders[h] = await self.ms.get(h)
                    except MoySkladError:
                        orders[h] = {}
                term, src = self.term_of(orders[h]), "заказ"
            if not term:
                term, src = self.term_of(d["agent"]), "контрагент"
            days = self.term_days(term)
            if days is None and default_term_days is not None:
                days, src = int(default_term_days), "по умолчанию"
            shipped = parse_day(d["moment"])
            due = shipped + timedelta(days=days) if days is not None else None
            late = (today - due).days if due else None
            if only_overdue and not (late and late > 0):
                continue
            rows.append({"number": d["name"], "date": str(shipped), "counterparty": d["agent"]["name"],
                         "agent_id": d["agent"]["id"], "sum": rub(d["sum"]), "paid": rub(d.get("payedSum")),
                         "debt": rub(debt), "term": term, "term_from": src if term or due else None,
                         "due": str(due) if due else None, "overdue_days": late,
                         "link": d["meta"].get("uuidHref", "")})
        by: dict[str, list] = defaultdict(list)
        for r in rows:
            by[r["agent_id"]].append(r)
        summary = []
        for aid, rs in by.items():
            try:
                bal = rub((await self.ms.get(f"report/counterparty/{aid}")).get("balance", 0))
            except MoySkladError:
                bal = None
            debt = round(sum(r["debt"] for r in rs), 2)
            summary.append({"counterparty": rs[0]["counterparty"], "shipments": len(rs), "debt": debt,
                            "balance": bal,
                            # баланс в плюс — деньги пришли, но платёж не привязан к отгрузке
                            "real_debt": round(max(0.0, min(debt, -bal)), 2) if bal is not None else debt,
                            "oldest": min(r["date"] for r in rs)})
        summary.sort(key=lambda s: -s["real_debt"])
        for r in rows:
            r.pop("agent_id")
        rows.sort(key=lambda r: (-(r["overdue_days"] or -10**6), r["date"]))
        return {"since": str(a), "today": str(today), "organization": org["name"] if org else "все",
                "shipments": len(rows), "debt_by_shipments": round(sum(r["debt"] for r in rows), 2),
                "real_debt": round(sum(s["real_debt"] for s in summary), 2),
                "no_term": sum(1 for r in rows if r["due"] is None),
                "by_counterparty": summary[:MAX_ROWS], **clip(rows, 60)}

    # ── карточка товара ──────────────────────────────────────────────────────

    async def product(self, search: str) -> dict:
        rows = await self._rows("report/stock/all", filter=f"search={search};stockMode=all")
        out = []
        for r in rows[:15]:
            href = r["meta"]["href"].split("?")[0]
            info = {"name": r["name"], "article": r.get("article"), "code": r.get("code"),
                    "stock": num(r.get("stock")), "cost_avg": rub(r.get("price")),
                    "sale_price": rub(r.get("salePrice"))}
            if "/product/" in href:
                try:
                    p = await self.ms.get(href)
                    info["buy_price"] = rub((p.get("buyPrice") or {}).get("value"))
                    info["archived"] = p.get("archived", False)
                except MoySkladError:
                    pass
            out.append(info)
        return {"search": search, "found": len(rows), "rows": out}


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


STR, INT, BOOL = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
DATE = {"type": "string", "description": "ГГГГ-ММ-ДД"}
ORG = {"type": "string", "description": "часть названия организации-продавца, например «Краснояров Д»; пусто — все"}

SPECS = [
    ("stock", "Остатки товаров и сырья по поиску (название, артикул, код): остаток, резерв, "
     "в пути, себестоимость единицы, цена продажи.",
     _schema({"search": STR, "store": {"type": "string", "description": "часть названия склада; пусто — все"},
              "only_positive": BOOL}, ["search"])),
    ("sales", "Продажи (отчёт прибыльности) за период: выручка, себестоимость, возвраты, валовая прибыль, "
     "маржа и строки по товарам или по покупателям. Внутренние перемещения (например, на Озон) "
     "исключены, пока include_internal=false. exclude — части названий покупателей, которых не считать.",
     _schema({"date_from": DATE, "date_to": DATE, "organization": ORG,
              "search": {"type": "string", "description": "слова из названия товара; пусто — все товары"},
              "exclude": {"type": "array", "items": STR}, "include_internal": BOOL,
              "group_by": {"type": "string", "enum": ["product", "counterparty"]}},
             ["date_from", "date_to"])),
    ("sales_by_month", "Оборот товара по месяцам (штуки, выручка, прибыль), считая текущий месяц.",
     _schema({"search": STR, "months": INT, "organization": ORG}, ["search"])),
    ("reorder", "Заказ поставщику: сколько докупить по каждой позиции под поиск, чтобы хватило на days дней "
     "по среднему расходу (продажи + техоперации + списания) за months полных месяцев. "
     "Для сырья и готовых товаров. round_to — кратность упаковки, min_qty — минимум, если заказывать.",
     _schema({"search": STR, "days": INT, "months": INT, "min_qty": {"type": "number"},
              "round_to": {"type": "number"}, "store": STR}, ["search"])),
    ("supplier_prices", "Что и почём покупали у поставщика (по приёмкам): последняя цена, дата, сколько раз.",
     _schema({"supplier": STR, "months": INT, "search": STR}, ["supplier"])),
    ("tech_card", "Техкарта (тех. операция): из какого сырья и в каком количестве делается продукт.",
     _schema({"search": STR}, ["search"])),
    ("unpaid", "Неоплаченные покупателями отгрузки с даты: долг по каждой, срок оплаты из доп. поля "
     "(отгрузка → заказ → контрагент), просрочка, ссылка в МойСклад, сводка по контрагентам с "
     "балансом взаиморасчётов (real_debt учитывает непривязанные оплаты).",
     _schema({"date_from": DATE, "organization": ORG, "counterparty": STR,
              "default_term_days": {"type": "integer", "description": "срок, если поле оплаты пустое"},
              "only_overdue": BOOL}, ["date_from"])),
    ("product", "Карточка товара: артикул, код, остаток, средняя себестоимость, закупочная цена "
     "из карточки, цена продажи.",
     _schema({"search": STR}, ["search"])),
]


def definitions() -> list[dict]:
    return [{"name": n, "description": d, "input_schema": s} for n, d, s in SPECS]


async def call(tools: Tools, name: str, args: dict) -> str:
    specs = {n: s for n, _, s in SPECS}
    if name not in specs:
        raise ToolError(f"нет инструмента {name}")
    if not isinstance(args, dict):
        raise ToolError("параметры — JSON-объект")
    allowed = specs[name]["properties"]
    unknown = set(args) - set(allowed)
    missing = set(specs[name]["required"]) - set(args)
    if unknown or missing:
        raise ToolError(f"{name}: лишние параметры {sorted(unknown)}, не хватает {sorted(missing)}")
    result = await getattr(tools, name)(**args)
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

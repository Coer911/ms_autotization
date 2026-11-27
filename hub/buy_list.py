"""Список к закупке по двум складам. Только чтение: заказ поставщику не создаётся.

Расход за месяц — поле outcome отчёта «Обороты» (report/turnover/all, docs/moysklad-api
reports/_report_turnover.md) по складу за 31 день. Документы перемещения (type=move)
вычитаются: это не потребление. Остаток — поле stock расширенного отчёта «Остатки»
(report/stock/all, stockMode=all) на сейчас. groupBy=variant, потому что на складе сырья
остатки лежат и по товарам, и по модификациям, а groupBy=product склеивает модификации
в товар и не стыкуется со строкой остатка.

Заказать = max(0, расход_в_день × 15 − остаток). Сиропы HERBARISTA 700 мл округляются
вверх до целой бутылки. Кордиалы 500 мл не входят. В таблицу попадают только позиции,
которые надо заказать.

quantityMode у отчёта остатков по умолчанию nonEmpty (reports/_report_stock.md):
нулевое «доступно» не приходит. Тогда распроданный вариант 700 мл пропадает из списка,
а полностью зарезервированный считается как остаток 0 и завышает заказ. Поэтому оба
запроса остатков идут с quantityMode=all, а строка оборотов ещё сверяется с группой
productFolder, если остатка по этому href нет.
"""
from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

WINDOW_DAYS = 31
COVER_DAYS = 15
RAW_STORE_MARK = "ТОЛЬКО СЫРЬЯ"
FG_STORE_MARK = "ГОТОВАЯ ПРОДУКЦИЯ"
SYRUP_FOLDER = "HERBARISTA 700 ML"
MSK = ZoneInfo("Europe/Moscow")


class BuyListError(Exception):
    """Склад или группа не нашлись однозначно. Повторять запрос бессмысленно."""


@dataclass(frozen=True)
class Line:
    name: str
    monthly: float
    stock: float
    order: float
    unit: str


@dataclass(frozen=True)
class Block:
    title: str
    store: str
    start: date
    end: date
    lines: tuple[Line, ...]
    covered: int
    whole: bool
    note: str


def norm(value: object) -> str:
    return " ".join(str(value or "").casefold().replace("ё", "е").split())


def window(today: date) -> tuple[date, date]:
    """31 день включительно, последний день — today."""
    return today - timedelta(days=WINDOW_DAYS - 1), today


def is_cordial_500(name: str) -> bool:
    low = norm(name)
    return "500" in low and ("кордиал" in low or "cordial" in low)


def is_syrup_700(path_name: str = "", folder_name: str = "") -> bool:
    """Группа HERBARISTA 700 ML. «CORDIALS 700» — другой сегмент пути и не подходит."""
    if norm(folder_name) == norm(SYRUP_FOLDER):
        return True
    return norm(SYRUP_FOLDER) in [norm(part) for part in str(path_name or "").split("/")]


def order_qty(monthly: float, stock: float, *, whole: bool) -> float:
    """max(0, расход_в_день × 15 − остаток). Сиропы — вверх до целой бутылки.

    Сырьё короче 0,0005 единицы после округления до тысячных не заказываем:
    в таблице это всё равно 0, а пыль вроде остатка −0,0001 кг не список закупки.
    """
    need = (monthly / WINDOW_DAYS) * COVER_DAYS - stock
    if need <= 1e-6:
        return 0.0
    if whole:
        return float(math.ceil(need - 1e-9))
    rounded = round(need, 3)
    return rounded if rounded > 0 else 0.0


def _group_name(obj: dict) -> str:
    """Имя группы: в остатках это folder, в оборотах — productFolder, иначе pathName."""
    folder = obj.get("folder") or obj.get("productFolder") or {}
    if isinstance(folder, dict) and folder.get("name"):
        return str(folder["name"])
    return str(obj.get("pathName") or "")


def _href(row: dict) -> str:
    meta = row.get("meta") or {}
    return str(meta.get("href") or "").split("?")[0]


def _outcome(row: dict) -> float:
    return float((row.get("outcome") or {}).get("quantity") or 0)


def _sort_key(line: Line) -> tuple:
    """Сначала то, чего хватит на меньше дней. При равенстве — больший расход выше.

    Минусовой остаток без расхода — это 0 дней, а не «минус бесконечность»: иначе
    пыль вроде −0,025 кг оказывается выше позиции, которая реально кончается.
    """
    daily = line.monthly / WINDOW_DAYS
    if daily <= 1e-12:
        days = 0.0 if line.stock < 0 else math.inf
    else:
        days = line.stock / daily
    return (days, -line.monthly, norm(line.name))


def assemble(stock_rows: list[dict], turnover_rows: list[dict], move_rows: list[dict], *,
             whole: bool, allow) -> tuple[tuple[Line, ...], int]:
    """Склеить остаток и расход по href. allow(href, name, folder) отсекает чужие позиции."""
    monthly: dict[str, float] = defaultdict(float)
    names: dict[str, str] = {}
    units: dict[str, str] = {}
    stocks: dict[str, float] = {}

    def take_name(href: str, name: str, unit: str) -> None:
        if name:
            names[href] = name
        if unit:
            units[href] = unit

    for row in turnover_rows:
        assortment = row.get("assortment") or {}
        href = _href(assortment)
        folder = _group_name(assortment)
        if not href or not allow(href, assortment.get("name") or "", folder):
            continue
        monthly[href] += _outcome(row)
        unit = ((assortment.get("uom") or {}).get("name")) or ""
        take_name(href, assortment.get("name") or "", unit)
    for row in move_rows:
        assortment = row.get("assortment") or {}
        href = _href(assortment)
        if href not in monthly:
            continue
        monthly[href] = max(0.0, monthly[href] - _outcome(row))
    for row in stock_rows:
        href = _href(row)
        folder = ((row.get("folder") or {}).get("name")) or ""
        if not href or not allow(href, row.get("name") or "", folder):
            continue
        stocks[href] = float(row.get("stock") or 0)
        unit = ((row.get("uom") or {}).get("name")) or ""
        take_name(href, row.get("name") or "", unit)

    lines: list[Line] = []
    covered = 0
    for href, name in names.items():
        used = monthly.get(href, 0.0)
        have = stocks.get(href, 0.0)
        qty = order_qty(used, have, whole=whole)
        if qty <= 0:
            if used > 0:
                covered += 1
            continue
        unit = units.get(href) or ("шт" if whole else "")
        lines.append(Line(name=name, monthly=used, stock=have, order=qty, unit=unit))
    lines.sort(key=_sort_key)
    return tuple(lines), covered


def syrup_hrefs(products: list[dict], stock_rows: list[dict]) -> set[str]:
    """Товары группы HERBARISTA 700 ML, без кордиалов 500 мл.

    Остатки добирают модификации: у них в отчёте есть folder, а в entity/product их нет.
    """
    hrefs: set[str] = set()
    for product in products:
        path = product.get("pathName") or ""
        name = product.get("name") or ""
        if is_syrup_700(path) and not is_cordial_500(name):
            href = _href(product)
            if href:
                hrefs.add(href)
    for row in stock_rows:
        folder = ((row.get("folder") or {}).get("name")) or ""
        name = row.get("name") or ""
        if is_syrup_700(folder_name=folder) and not is_cordial_500(name):
            href = _href(row)
            if href:
                hrefs.add(href)
    return hrefs


def _fmt(value: float, unit: str, *, whole: bool = False) -> str:
    if whole:
        text = str(int(value))
    else:
        text = f"{value:.3f}".rstrip("0").rstrip(".").replace(".", ",")
        if text in {"", "-0"}:
            text = "0"
    return f"{text} {unit}".strip() if unit else text


def _cell(text: str) -> str:
    return str(text).replace("|", "/")


def render_block(block: Block) -> str:
    start = block.start.strftime("%d.%m.%Y")
    end = block.end.strftime("%d.%m.%Y")
    lines = [
        f"{block.title} — {block.store}",
        (f"Окно расхода: {start}–{end} ({WINDOW_DAYS} день). "
         "Перемещения между складами не входят в расход."),
        f"Заказать = max(0, расход в день × {COVER_DAYS} − остаток). Покрытие {COVER_DAYS} дней.",
    ]
    if block.note:
        lines.append(block.note)
    lines.append(f"С расходом и запасом на {COVER_DAYS} дней: {block.covered}. "
                 "В таблице только то, что надо заказать.")
    lines.append("")
    lines.append("| Название | Расход за месяц | Остаток | Заказать |")
    lines.append("| --- | --- | --- | --- |")
    if not block.lines:
        lines.append("| — | — | — | — |")
    for line in block.lines:
        lines.append(
            f"| {_cell(line.name)} | {_fmt(line.monthly, line.unit)} | "
            f"{_fmt(line.stock, line.unit)} | {_fmt(line.order, line.unit, whole=block.whole)} |"
        )
    return "\n".join(lines)


def render(raw: Block, syrup: Block) -> str:
    text = (
        "Список к закупке. Заказ поставщику не создаётся.\n\n"
        + render_block(raw)
        + "\n\n"
        + render_block(syrup)
        + "\n"
    )
    return text


def pick_store(stores: list[dict], mark: str) -> dict:
    hits = [row for row in stores if mark.casefold() in (row.get("name") or "").casefold()]
    if len(hits) != 1:
        names = "; ".join(row.get("name") or "" for row in stores) or "пусто"
        raise BuyListError(f"склад «{mark}» найден {len(hits)} раз. Склады: {names}")
    return hits[0]


def pick_folder(folders: list[dict]) -> dict:
    hits = [row for row in folders if norm(row.get("name")) == norm(SYRUP_FOLDER)]
    if len(hits) != 1:
        raise BuyListError(f"группа «{SYRUP_FOLDER}» найдена {len(hits)} раз")
    return hits[0]


async def _rows(ms, path: str, **params) -> list[dict]:
    return [row async for row in ms.iterate(path, limit=1000, **params)]


async def collect(ms, today: date | None = None) -> tuple[Block, Block]:
    """Прочитать два склада и собрать блоки. Пишет в МойСклад ничего: только iterate/GET."""
    today = today or datetime.now(MSK).date()
    start, end = window(today)
    moment = {"momentFrom": f"{start} 00:00:00", "momentTo": f"{end} 23:59:59"}
    stores, folders = await asyncio.gather(
        _rows(ms, "entity/store"),
        _rows(ms, "entity/productfolder"),
    )
    raw_store = pick_store(stores, RAW_STORE_MARK)
    fg_store = pick_store(stores, FG_STORE_MARK)
    folder = pick_folder(folders)
    raw_href = raw_store["meta"]["href"]
    fg_href = fg_store["meta"]["href"]
    folder_href = folder["meta"]["href"]
    products, raw_stock, raw_turn, raw_move, fg_stock, fg_turn, fg_move = await asyncio.gather(
        _rows(ms, "entity/product", filter=f"pathName~{SYRUP_FOLDER}"),
        _rows(ms, "report/stock/all",
              filter=f"store={raw_href};stockMode=all;quantityMode=all"),
        _rows(ms, "report/turnover/all", filter=f"store={raw_href}", groupBy="variant", **moment),
        _rows(ms, "report/turnover/all", filter=f"store={raw_href};type=move",
              groupBy="variant", **moment),
        _rows(ms, "report/stock/all",
              filter=f"store={fg_href};stockMode=all;quantityMode=all;productFolder={folder_href}"),
        _rows(ms, "report/turnover/all", filter=f"store={fg_href}", groupBy="variant", **moment),
        _rows(ms, "report/turnover/all", filter=f"store={fg_href};type=move",
              groupBy="variant", **moment),
    )
    hrefs = syrup_hrefs(products, fg_stock)

    def raw_allow(_href: str, name: str, _folder: str) -> bool:
        return not is_cordial_500(name)

    def syrup_allow(href: str, name: str, folder: str) -> bool:
        if is_cordial_500(name):
            return False
        return href in hrefs or is_syrup_700(folder, folder)

    raw_lines, raw_covered = assemble(
        raw_stock, raw_turn, raw_move, whole=False, allow=raw_allow)
    syrup_lines, syrup_covered = assemble(
        fg_stock, fg_turn, fg_move, whole=True, allow=syrup_allow)
    raw = Block(
        "Склад сырья", raw_store["name"], start, end, raw_lines, raw_covered, False, "",
    )
    syrup = Block(
        "Сиропы HERBARISTA 700 мл", fg_store["name"], start, end, syrup_lines, syrup_covered,
        True, "Кордиалы 500 мл не входят. Заказ округлён вверх до целой бутылки.",
    )
    return raw, syrup

"""Список закупки по техкартам чая из XLSX. Заказ поставщику не создаёт.

Файл локальный: МойСклад не читается и не пишется. Две вёрстки листа:

* таблица: колонки «компонент» (сырьё, наименование) и «количество»
  (норма, кг, %); техкарта — колонка, название над таблицей или имя вкладки;
* матрица: первая колонка — компонент, остальные столбцы — техкарты.

Строка «Выход, кг» задаёт партию для процентов, пока не встретится новое
название техкарты. Доли без выхода остаются долями и в массу не переводятся.
`--batches` умножает уже посчитанную массу. Оформление заказа — только после
отдельного явного подтверждения; этот модуль документ не создаёт.
"""
from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from io import BytesIO

from openpyxl import load_workbook

# Основа + падеж («компоненты», «техкарты»), но не другое слово («весенний»)
# и не название техкарты через пробел («купаж жасмин»).
_ENDINGS = (
    "иями", "ями", "ами", "иях", "ях", "ах", "иям", "ям", "ам", "ием",
    "ого", "ему", "ыми", "ими", "ов", "ев", "ей", "ой", "ою", "ею", "ом", "ем",
    "ая", "яя", "ое", "ее", "ые", "ие", "ия", "ии", "ий", "ый", "ую", "юю",
    "а", "я", "ы", "и", "у", "ю", "е", "о", "",
)
_UNIT_QTY = {"кг", "г", "гр", "мг", "мл", "л", "шт"}
_TOTAL = {"итого", "всего", "total"}
_OUTPUT_WORD = {"выход", "партия", "объем"}


class OrderNotPlaced(Exception):
    """Заказ поставщику не создаётся, пока нет отдельного явного подтверждения."""


ORDER_REFUSAL = (
    "Заказ поставщику не создан: нужно отдельное явное подтверждение. "
    "Команда только считает позиции и количество."
)


@dataclass(frozen=True)
class BuyLine:
    name: str
    unit: str
    quantity: float
    absolute: bool
    cards: tuple[str, ...]


def norm(value: object) -> str:
    text = str(value or "").casefold().replace("ё", "е").replace("\xa0", " ")
    return " ".join(text.split())


def parse_qty(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    text = text.rstrip("%")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _lead(title: str, key: str) -> bool:
    """Метка колонки: целиком или с хвостом после пробела/знака («норма расхода»)."""
    if title == key:
        return True
    return len(title) > len(key) and title.startswith(key) and title[len(key)] in " ,.:;()"


def _form(title: str, stem: str, *, phrase: bool = False) -> bool:
    """Основа и окончание. phrase — можно пояснение после пробела, но не чужое слово."""
    if not title.startswith(stem):
        return False
    rest = title[len(stem):]
    for ending in _ENDINGS:
        if not rest.startswith(ending):
            continue
        tail = rest[len(ending):]
        if tail == "":
            return True
        # Скобка в названии («Купаж (жасмин)») — это техкарта, не шапка.
        # «Вес (кг)» проходит отдельно: у веса phrase=True.
        if phrase and tail[0] in " ,.:;()":
            return True
        if not phrase and tail[0] in ",.:;":
            return True
    return False


def _unit_header(title: str) -> bool:
    compact = title.replace(" ", "")
    if compact in {"ед", "ед."}:
        return True
    if compact.startswith("ед.") and compact[3:].startswith("изм"):
        return True
    return _form(title, "единиц", phrase=True)


def _role(title: str) -> str | None:
    if not title:
        return None
    if title in {"%", "процент"} or _form(title, "процент", phrase=True) or _lead(title, "доля"):
        return "percent"
    if any(_lead(title, key) for key in (
        "наименование техкарты", "название техкарты", "наименование продукта",
        "название продукта", "готовая продукция",
    )) or _form(title, "готов") or any(
        _form(title, stem) for stem in ("техкарт", "купаж", "рецепт", "продукт")
    ):
        return "card"
    if any(_lead(title, key) for key in (
        "количество", "кол-во", "колво", "норма", "расход", "масса", "qty",
    )) or _form(title, "количеств", phrase=True) or _form(title, "вес", phrase=True) or title in _UNIT_QTY:
        return "qty"
    if _unit_header(title):
        return "unit"
    if any(_lead(title, key) for key in (
        "наименование сырья", "название сырья", "наименование", "название",
    )) or _form(title, "наименован", phrase=True) or _form(title, "назван", phrase=True) or any(
        _form(title, stem, phrase=True) for stem in ("компонент", "ингредиент", "позиц")
    ) or any(_form(title, stem) for stem in ("сырье", "материал")):
        return "component"
    return None


def _header(row: tuple) -> dict | None:
    # Число в строке — это уже данные, а не шапка. Иначе «Сырьё | 5 | кг»
    # выглядит как новая таблица: имя похоже на колонку, «кг» — на количество.
    if any(parse_qty(cell) is not None and norm(cell) not in _UNIT_QTY for cell in row):
        return None
    roles: list[tuple[int, str, str]] = []
    for i, cell in enumerate(row):
        title = norm(cell)
        role = _role(title)
        if role:
            roles.append((i, role, title))
    if not any(role == "component" for _, role, _ in roles):
        return None
    if any(role in {"qty", "percent"} for _, role, _ in roles):
        return {"kind": "long", "roles": roles}
    cards = [(i, str(cell).strip()) for i, cell in enumerate(row)
             if str(cell or "").strip() and _role(norm(cell)) is None]
    comp = next(i for i, role, _ in roles if role == "component")
    if cards:
        return {"kind": "matrix", "component": comp, "cards": cards,
                "unit": next((i for i, role, _ in roles if role == "unit"), None)}
    return None


def _is_total(name: object) -> bool:
    title = norm(name)
    if not title:
        return False
    head = title.split(",", 1)[0].strip()
    return head.split(" ", 1)[0] in _TOTAL


def _is_output_label(label: str) -> bool:
    head = label.split(",", 1)[0].strip()
    if head == "на партию" or head.startswith("на партию "):
        return True
    return head.split(" ", 1)[0] in _OUTPUT_WORD


def _output(row: tuple) -> tuple[float, str] | None:
    texts = [(i, norm(cell)) for i, cell in enumerate(row) if norm(cell)]
    if not texts:
        return None
    index, label = texts[0]
    if not _is_output_label(label):
        return None
    unit = label.split(",", 1)[1].strip() if "," in label else ""
    if unit not in _UNIT_QTY:
        unit = ""
    for cell in list(row[index + 1:]) + [label]:
        qty = parse_qty(cell)
        if qty is not None:
            return qty, unit
    found = re.search(r"(\d+(?:[.,]\d+)?)\s*(кг|г|гр|мг|мл|л|шт)?", label)
    if not found:
        return None
    return float(found.group(1).replace(",", ".")), (found.group(2) or unit)


def _title(row: tuple) -> str | None:
    filled = [cell for cell in row if str(cell or "").strip()]
    if len(filled) != 1:
        return None
    text = str(filled[0]).strip()
    title = norm(text)
    if _role(title) or _output(row) or parse_qty(text) is not None or _is_total(text):
        return None
    if len(title) < 2:
        return None
    return text


def _label(sheet: str, card: str) -> str:
    return card if norm(card) == norm(sheet) else f"{sheet}: {card}"


def _sheet_rows(ws) -> list[tuple]:
    return [tuple(cell.value for cell in row) for row in ws.iter_rows()]


def _take_long(row: tuple, spec: dict, sheet: str, card: str, output: tuple[float, str] | None,
               lines: list[dict]) -> str:
    found = {role: i for i, role, _ in spec["roles"]}
    raw_card = row[found["card"]] if "card" in found and found["card"] < len(row) else None
    if str(raw_card or "").strip():
        card = str(raw_card).strip()
    comp_i = found["component"]
    name = row[comp_i] if comp_i < len(row) else None
    if not str(name or "").strip() or _is_total(name) or _is_output_label(norm(name)):
        return card
    unit = ""
    if "unit" in found and found["unit"] < len(row) and row[found["unit"]] is not None:
        unit = str(row[found["unit"]]).strip()
    qty = parse_qty(row[found["qty"]]) if "qty" in found and found["qty"] < len(row) else None
    absolute = qty is not None
    if not absolute and "percent" in found and found["percent"] < len(row):
        percent = parse_qty(row[found["percent"]])
        if percent is None:
            return card
        if output:
            qty = percent / 100 * output[0]
            unit = unit or output[1]
            absolute = True
        else:
            qty, unit, absolute = percent, "%", False
    if qty is None or qty == 0:
        return card
    if qty < 0:
        raise ValueError(f"отрицательное количество у «{name}»")
    if "qty" in found and not unit:
        header = next(title for _, role, title in spec["roles"] if role == "qty")
        if header in _UNIT_QTY:
            unit = header
    lines.append({"sheet": sheet, "card": card, "name": " ".join(str(name).split()),
                  "quantity": qty, "unit": unit, "absolute": absolute})
    return card


def _parse_sheet(ws) -> list[dict]:
    rows = _sheet_rows(ws)
    lines: list[dict] = []
    card = ws.title
    output: tuple[float, str] | None = None
    i = 0
    while i < len(rows):
        row = rows[i]
        spec = _header(row)
        if spec is None:
            title = _title(row)
            if title:
                card, output = title, None
            else:
                found = _output(row)
                if found:
                    output = found
            i += 1
            continue
        i += 1
        if spec["kind"] == "long":
            while i < len(rows) and _header(rows[i]) is None and _title(rows[i]) is None:
                if not any(str(cell or "").strip() for cell in rows[i]):
                    i += 1
                    break
                produced = _output(rows[i])
                if produced:
                    output = produced
                    i += 1
                    continue
                card = _take_long(rows[i], spec, ws.title, card, output, lines)
                i += 1
            continue
        while i < len(rows) and _header(rows[i]) is None and _title(rows[i]) is None:
            data = rows[i]
            name = data[spec["component"]] if spec["component"] < len(data) else None
            if not str(name or "").strip():
                i += 1
                break
            if _is_total(name) or _output(data):
                i += 1
                continue
            unit = ""
            if spec["unit"] is not None and spec["unit"] < len(data) and data[spec["unit"]] is not None:
                unit = str(data[spec["unit"]]).strip()
            for col, card_name in spec["cards"]:
                qty = parse_qty(data[col] if col < len(data) else None)
                if qty is None or qty == 0:
                    continue
                if qty < 0:
                    raise ValueError(f"отрицательное количество у «{name}»")
                lines.append({"sheet": ws.title, "card": card_name,
                              "name": " ".join(str(name).split()),
                              "quantity": qty, "unit": unit, "absolute": True})
            i += 1
    return lines


def parse_tech_cards(data: bytes) -> list[dict]:
    book = load_workbook(BytesIO(data), data_only=False)
    lines: list[dict] = []
    for ws in book.worksheets:
        lines.extend(_parse_sheet(ws))
    if not lines:
        names = ", ".join(ws.title for ws in book.worksheets) or "нет вкладок"
        raise ValueError(
            "в файле нет таблицы техкарты. Нужна строка заголовков: "
            "компонент (сырьё, наименование) и количество (норма, кг, %) "
            "или матрица, где первая колонка — компонент, а остальные — техкарты. "
            f"Вкладки: {names}"
        )
    return lines


def positions(data: bytes, *, card: str = "", batches: float = 1) -> list[BuyLine]:
    if batches <= 0:
        raise ValueError("партий должно быть больше нуля")
    needle = norm(card)
    grouped: OrderedDict[tuple, dict] = OrderedDict()
    for line in parse_tech_cards(data):
        label = _label(line["sheet"], line["card"])
        if needle and needle not in norm(label) and needle not in norm(line["sheet"]):
            continue
        qty = line["quantity"] * batches if line["absolute"] else line["quantity"]
        key = (norm(line["name"]), norm(line["unit"]), line["absolute"])
        item = grouped.get(key)
        if item is None:
            grouped[key] = {"name": line["name"], "unit": line["unit"], "quantity": qty,
                            "absolute": line["absolute"], "cards": [label]}
        else:
            item["quantity"] += qty
            if label not in item["cards"]:
                item["cards"].append(label)
    rows = [BuyLine(item["name"], item["unit"], round(item["quantity"], 6), item["absolute"],
                    tuple(item["cards"]))
            for item in grouped.values()]
    rows.sort(key=lambda row: (not row.absolute, norm(row.name), norm(row.unit)))
    return rows


def fmt_qty(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".").replace(".", ",")


def render(rows: list[BuyLine], *, batches: float) -> str:
    absolute = [row for row in rows if row.absolute]
    shares = [row for row in rows if not row.absolute]
    out = ["К закупке по техкартам чая. Заказ поставщику не создан.", ""]
    if batches != 1:
        out.append(f"Партий каждой техкарты: {fmt_qty(batches)}")
        out.append("")
    out.extend(_table(absolute or shares))
    if absolute and shares:
        out.extend(["", "Доли без нормы выхода, в массу не переводил:", ""])
        out.extend(_table(shares))
    elif shares and not absolute:
        out.extend(["", "Это доли: нормы выхода нет, в массу не переводил."])
    if batches != 1 and shares:
        out.append("Доли на число партий не умножал.")
    out.extend(["", "Заказ поставщику появится после отдельного явного подтверждения. "
                "Сейчас напечатан только список."])
    return "\n".join(out)


def _table(rows: list[BuyLine]) -> list[str]:
    if not rows:
        return ["Позиций нет."]
    head = ("позиция", "ед", "количество", "техкарты")
    body = [(row.name, row.unit or "—", fmt_qty(row.quantity), _cards(row.cards)) for row in rows]
    widths = [max(len(head[i]), *(len(line[i]) for line in body)) for i in range(4)]

    def show(line: tuple[str, str, str, str]) -> str:
        return "  ".join(part.ljust(widths[i]) for i, part in enumerate(line)).rstrip()
    return [show(head), show(tuple("-" * n for n in widths)), *(show(line) for line in body),
            "", f"Позиций: {len(rows)}."]


def _cards(cards: tuple[str, ...]) -> str:
    text = "; ".join(cards)
    if len(text) <= 80:
        return text
    return f"{len(cards)} техкарт"


def report(data: bytes, *, card: str = "", batches: float = 1, place_order: bool = False) -> str:
    if place_order:
        raise OrderNotPlaced(ORDER_REFUSAL)
    return render(positions(data, card=card, batches=batches), batches=batches)

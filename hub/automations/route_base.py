"""База контрагентов логиста («Маршрутный лист.xlsx», вкладка «Адреса скорая почта…»).

Из базы в маршрутный лист данные переносятся дословно: адрес, телефон(ы), часы, места,
комментарий. Ничего не причёсывается. Заказ сопоставляется с базой по названию
контрагента; если строк несколько (точки одного юрлица) — по совпадению адреса заказа.
Не нашли или неоднозначно — строка берётся из МойСклад, базу не угадываем.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO

from openpyxl import load_workbook

DEFAULT_SHEET = "Адреса скорая почта"
_LEGAL = {"ооо", "ип", "ао", "пао", "зао", "оао"}
_QUOTES = str.maketrans({c: " " for c in "\"'«»“”„()"})
_COLUMNS = {
    "address": ("адрес",),
    "counterparty": ("контрагент",),
    "phone": ("телефон",),
    "hours": ("часы",),
    "places": ("кол-мест", "кол-во мест", "мест"),
    "comment": ("комментарий",),
}


@dataclass
class BaseRow:
    counterparty: str
    address: object
    phone: object
    hours: object
    places: object
    comment: object


def norm(text: object) -> str:
    value = str(text or "").casefold().replace("ё", "е").replace("\xa0", " ").translate(_QUOTES)
    return " ".join(value.split())


def _core(name: str) -> str:
    return " ".join(w for w in norm(name).split() if w not in _LEGAL)


def _address_tokens(text: object) -> set[str]:
    words = re.findall(r"[0-9]+|[а-яa-z]{3,}", norm(text))
    stop = {"россия", "санкт", "петербург", "улица", "наб", "проспект", "пр-кт", "литера",
            "корп", "дом", "город"}
    return {w for w in words if w not in stop}


def parse_base(data: bytes, sheet_prefix: str = DEFAULT_SHEET) -> list[BaseRow]:
    book = load_workbook(BytesIO(data), read_only=True, data_only=True)
    want = norm(sheet_prefix)
    sheet = next((ws for ws in book.worksheets if norm(ws.title).startswith(want)), None)
    if sheet is None:
        raise ValueError(f"в базе нет вкладки «{sheet_prefix}…»")
    rows = sheet.iter_rows(values_only=True)
    header = [norm(v) for v in next(rows, ())]
    index: dict[str, int] = {}
    for key, names in _COLUMNS.items():
        for i, title in enumerate(header):
            if any(title.startswith(n) for n in names) and i not in index.values():
                index[key] = i
                break
    if "counterparty" not in index or "address" not in index:
        raise ValueError("в базе нет колонок «Адрес» и «Контрагент»")
    out = []
    for raw in rows:
        def cell(key, raw=raw):
            i = index.get(key)
            return raw[i] if i is not None and i < len(raw) else None
        name = cell("counterparty")
        if not str(name or "").strip() or not str(cell("address") or "").strip():
            continue
        out.append(BaseRow(str(name), cell("address"), cell("phone"), cell("hours"),
                           cell("places"), cell("comment")))
    return out


def match(base: list[BaseRow], counterparty: str, address: str) -> BaseRow | None:
    """Строка базы для заказа или None. Неоднозначность → None (не угадываем)."""
    agent = _core(counterparty)
    if len(agent) < 2:
        return None
    found = []
    for row in base:
        core = _core(row.counterparty)
        if len(core) < 2:
            continue
        if core == agent or _contains(agent, core) or _contains(core, agent):
            found.append(row)
    if len(found) <= 1:
        return found[0] if found else None
    tokens = _address_tokens(address)
    scored = sorted(((len(tokens & _address_tokens(r.address)), i) for i, r in enumerate(found)),
                    reverse=True)
    best, second = scored[0], scored[1]
    if best[0] > second[0]:
        return found[best[1]]
    return None


def _contains(longer: str, shorter: str) -> bool:
    """Целые слова: «ривьера» входит в «ривьера морская», «22» не входит в «122»."""
    return f" {shorter} " in f" {longer} "


def window(hours: object) -> tuple[int, int]:
    """«Доставка С/ПО»: с 12 до 18, начало позже — если в базе приёмка позже 12."""
    start, end = 12, 18
    nums = [int(n) for n in re.findall(r"(?<![\d:.])(\d{1,2})(?:[:.]\d\d)?(?!\d)", str(hours or ""))]
    nums = [n for n in nums if n < 24]
    if nums:
        start = max(12, nums[0]) if nums[0] < 18 else 12
        if len(nums) > 1 and start < nums[1] < 18:
            end = nums[1]
    return start, end


def places(value: object) -> int:
    try:
        number = int(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        return 1
    return number if number > 0 else 1

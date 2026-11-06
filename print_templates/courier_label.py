"""Термостикер 120×75 мм для курьера — шаблон печатной формы заказа покупателя.

    pip install xlwt
    python print_templates/courier_label.py          # → print_templates/courier_label_120x75.xls

Готовый .xls загружается в МойСклад руками (через API шаблоны не загружаются — см.
docs/moysklad-api/workbook/_workbook_print_templates.md): заказ покупателя → Печать →
Настроить… → добавить шаблон. Подробно — print_templates/README.md.

Разметка — как в стандартных шаблонах МойСклад (download-template/order.xls,
invoice-stamp-sign.xls): ${…} — выражения jXLS. В заказе покупателя
o.sourceAgentRequisite — покупатель, o.targetAgentRequisite — ваша организация.

Набор полей сверен с 800 заказами (17.08–04.11.2026): адрес доставки есть в 61 %, ещё 28 % —
только в карточке покупателя; комментарий к адресу (вход, часы, контакт) — 29 %; комментарий
заказа — 28 % (у заказов с сайта там способ доставки, адрес ПВЗ и получатель); телефон
в карточке — 85 %. Доп. поля «Тип доставки» и «Оплата» заполнены меньше чем в 1 % — их нет.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import xlwt
from xlwt import Worksheet

WIDTH_MM, HEIGHT_MM = 120, 75
MARGIN_X_MM, MARGIN_Y_MM = 3, 2

# Доп. поле заказа (строка): телефон получателя, если он не тот, что в карточке покупателя.
ATTR_PHONE = "Номер телефона"
PHONE_ATTR = f'formatter.findAttribute(o, "{ATTR_PHONE}").valueString'

# Что подставляет МойСклад. Ключи — места на стикере.
FIELDS = {
    "number": "ЗАКАЗ № ${o.name}",
    "date": 'от ${formatter.format("%1$td.%1$tm.%1$tY", formatter.getExcelDate(o.moment))}',
    "recipient": "${o.sourceAgentRequisite.agent.name}",
    # Телефон из доп. поля заказа, если оно заполнено, иначе — из карточки покупателя.
    "phone": "${formatter.printIfElse(empty(" + PHONE_ATTR + "), "
    "o.sourceAgentRequisite.agent.contact.phones, " + PHONE_ATTR + ")}",
    # Адрес доставки заказа, если пусто — фактический адрес из карточки покупателя.
    "address": "${formatter.printIfElse(empty(o.addressFull), "
    "o.sourceAgentRequisite.actualAddress, o.addressFull)}",
    "address_note": "${o.address.comment}",
    "comment": "${o.description}",
    "sum": "${o.sum.sumInCurrency / 100.0}",
    "sender": "Отправитель: ${o.targetAgentRequisite.agent.name}, "
    "тел. ${o.targetAgentRequisite.agent.contact.phones}",
}

# Сетка: 4 колонки и 8 строк, в сумме 114 × 71 мм — стикер минус поля.
# Строк на поле — по тем же 800 заказам: столько, чтобы текст влез целиком почти всегда.
COLS_MM = (21, 47, 19, 27)
ROWS_MM = (
    9.0,  # 0 номер и дата
    6.5,  # 1 получатель: 1 строка — 99 % имён; длиннее — шрифт ужмётся
    7.0,  # 2 телефон
    9.5,  # 3 адрес: 2 строки — 99 %
    12.0,  # 4 комментарий к адресу: 3 строки — 91 %
    17.5,  # 5 комментарий заказа: 5 строк — 94 %
    5.5,  # 6 сумма и места
    4.0,  # 7 отправитель
)


def style(size: float, *, bold: bool = False, horiz: str = "left", vert: str = "center",
           wrap: bool = True, shrink: bool = False, top: str = "", box: str = "",
           num: str = "") -> xlwt.XFStyle:
    """Стиль ячейки: шрифт Arial, выравнивание; top — линия сверху, box — рамка со всех сторон."""
    spec = (f"font: name Arial, height {round(size * 20)}{', bold on' if bold else ''}; "
            f"align: horiz {horiz}, vert {vert}{', wrap on' if wrap else ''}"
            f"{', shrink_to_fit on' if shrink else ''}")
    if box:
        spec += f"; borders: left {box}, right {box}, top {top or box}, bottom {box}"
    elif top:
        spec += f"; borders: top {top}"
    return xlwt.easyxf(spec, num_format_str=num or "General")


def mm_to_colwidth(mm: float) -> int:
    # Ширина колонки — в 1/256 ширины цифры шрифта по умолчанию (Arial 10 ≈ 7 px при 96 dpi).
    return round(mm / 25.4 * 96 / 7 * 256)


def mm_to_twips(mm: float) -> int:
    return round(mm / 25.4 * 72 * 20)


def pls_record(width_mm: float, height_mm: float) -> bytes:
    """PLS (0x004D) с DEVMODEW: размер бумаги пользователя, как его пишет Excel с драйвером термопринтера.

    BIFF8 не умеет хранить произвольный размер в PAGESETUP, только номер из списка.
    Свой размер живёт в настройках принтера — эту запись читают не все конвертеры.
    """
    devmode = bytearray(220)  # DEVMODEW: dmDeviceName (64 байта) пустой — подойдёт любой принтер
    # DM_ORIENTATION | DM_PAPERSIZE | DM_PAPERLENGTH | DM_PAPERWIDTH | DM_SCALE | DM_COPIES
    struct.pack_into("<HHHHI", devmode, 64, 0x0401, 0, 220, 0, 0x1 | 0x2 | 0x4 | 0x8 | 0x10 | 0x100)
    # dmOrientation=portrait, dmPaperSize=DMPAPER_USER, длина и ширина в 0,1 мм, масштаб 100 %, 1 копия
    struct.pack_into("<hhhhhh", devmode, 76, 1, 256, round(height_mm * 10), round(width_mm * 10), 100, 1)
    body = b"\x00\x00" + bytes(devmode)
    return struct.pack("<HH", 0x004D, len(body)) + body


HEADER, FOOTER, SETUP = 0x0014, 0x0015, 0x00A1


def fix_page_block(sheet: Worksheet.Worksheet, pls: bytes = b"") -> None:
    """Правит блок настроек страницы листа по [MS-XLS].

    PLS (если дан) встаёт перед SETUP. Пустые HEADER/FOOTER пишутся без тела: «нет колонтитула»,
    а не строка нулевой длины, как их пишет xlwt.
    """
    original = sheet._Worksheet__print_settings_rec

    def patched() -> bytes:
        data, out, pos = original(), [], 0
        while pos < len(data):
            rt, cb = struct.unpack_from("<HH", data, pos)
            record = data[pos:pos + 4 + cb]
            pos += 4 + cb
            if rt in (HEADER, FOOTER):
                record = struct.pack("<HH", rt, 0)
            elif rt == SETUP and pls:
                out.append(pls)
            out.append(record)
        return b"".join(out)

    sheet._Worksheet__print_settings_rec = patched


def build(values: dict[str, str] | None = None) -> xlwt.Workbook:
    """Собирает стикер. values подменяет выражения МойСклад — для предпросмотра на примере заказа."""
    v = {**FIELDS, **(values or {})}
    wb = xlwt.Workbook(encoding="utf-8")
    # COUNTRY: без неё POI в МойСклад падает на цикле jXLS («CountryRecord not found»
    # при сдвиге строк) — шаблон с таблицей печатается как «Некорректный шаблон».
    wb.country_code = 7
    ws = wb.add_sheet("Стикер")

    for i, mm in enumerate(COLS_MM):
        ws.col(i).width = mm_to_colwidth(mm)
    for i, mm in enumerate(ROWS_MM):
        row = ws.row(i)
        row.height_mismatch = True
        row.height = mm_to_twips(mm)

    label = style(7.5, vert="top")
    sep = "medium"

    ws.write_merge(0, 0, 0, 1, v["number"], style(15, bold=True, wrap=False, shrink=True))
    ws.write_merge(0, 0, 2, 3, v["date"], style(10, horiz="right", wrap=False))

    ws.write(1, 0, "Получатель:", style(7.5, top=sep))
    ws.write_merge(1, 1, 1, 3, v["recipient"], style(12, bold=True, wrap=False, shrink=True, top=sep))

    ws.write(2, 0, "Телефон:", style(7.5))
    ws.write_merge(2, 2, 1, 3, v["phone"], style(14, bold=True, wrap=False, shrink=True))

    ws.write(3, 0, "Адрес:", label)
    ws.write_merge(3, 3, 1, 3, v["address"], style(10.5, vert="top"))

    ws.write(4, 0, "К адресу:", label)
    ws.write_merge(4, 4, 1, 3, v["address_note"], style(8.5, bold=True, vert="top"))

    ws.write(5, 0, "Комментарий:", style(7.5, vert="top", top="thin"))
    ws.write_merge(5, 5, 1, 3, v["comment"], style(8, vert="top", top="thin"))

    ws.write(6, 0, "Сумма:", style(7.5, top=sep))
    sum_style = style(11, bold=True, wrap=False, top=sep, num='#,##0.00" руб."')
    try:
        ws.write(6, 1, float(v["sum"]), sum_style)
    except ValueError:
        ws.write(6, 1, v["sum"], sum_style)
    ws.write(6, 2, "Мест:", style(7.5, top=sep))
    ws.write(6, 3, "________", style(10, top=sep))

    ws.write_merge(7, 7, 0, 3, v["sender"], style(6.5, wrap=False, shrink=True, top="thin"))

    # Страница: без колонтитулов, поля по краям, всё на один лист.
    ws.header_str = b""  # xlwt на Python 3 принимает здесь только bytes
    ws.footer_str = b""
    ws.header_margin = ws.footer_margin = 0
    ws.left_margin = ws.right_margin = MARGIN_X_MM / 25.4
    ws.top_margin = ws.bottom_margin = MARGIN_Y_MM / 25.4
    ws.portrait = True  # ширина и высота заданы самим размером бумаги: 120 по ширине, 75 по высоте
    ws.paper_size_code = 256  # DMPAPER_USER — размер берётся из PLS
    ws.fit_num_pages = 1
    ws.fit_width_to_pages = ws.fit_height_to_pages = 1
    ws.show_grid = False
    fix_page_block(ws, pls_record(WIDTH_MM, HEIGHT_MM))
    return wb


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("courier_label_120x75.xls")
    build().save(str(out))
    print(out)


if __name__ == "__main__":
    main()

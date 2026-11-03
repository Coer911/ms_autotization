"""Термостикер 120×75 мм для курьера — шаблон печатной формы заказа покупателя.

    pip install xlwt
    python print_templates/courier_label.py          # → print_templates/courier_label_120x75.xls

Готовый .xls загружается в МойСклад руками (через API шаблоны не загружаются — см.
docs/moysklad-api/workbook/_workbook_print_templates.md): Продажи → Заказы покупателей →
открыть заказ → Печать → Настроить… → Загрузить шаблон. Подробно — print_templates/README.md.

Разметка — как в стандартных шаблонах МойСклад (download-template/order.xls,
invoice-stamp-sign.xls): ${…} — выражения jXLS. В заказе покупателя
o.sourceAgentRequisite — покупатель, o.targetAgentRequisite — ваша организация.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import xlwt
from xlwt import Worksheet

WIDTH_MM, HEIGHT_MM = 120, 75
MARGIN_X_MM, MARGIN_Y_MM = 3, 2

# Доп. поля заказа покупателя — имена ровно как в МойСклад (Настройки → Заказы покупателей).
ATTR_DELIVERY = "Тип доставки"  # справочник
ATTR_PAYMENT = "Оплата"  # справочник
ATTR_PHONE = "Номер телефона"  # строка


def attr(name: str, suffix: str) -> str:
    return f'formatter.findAttribute(o, "{name}").{suffix}'


PHONE_ATTR = attr(ATTR_PHONE, "valueString")

# Что подставляет МойСклад. Ключи — места на стикере.
FIELDS = {
    "number": "ЗАКАЗ № ${o.name}",
    "date": 'от ${formatter.format("%1$td.%1$tm.%1$tY", formatter.getExcelDate(o.moment))}',
    "delivery": "${" + attr(ATTR_DELIVERY, "entityValue.name") + "}",
    "recipient": "${o.sourceAgentRequisite.agent.name}",
    # Телефон из доп. поля заказа, если оно заполнено, иначе — из карточки покупателя.
    "phone": "${formatter.printIfElse(empty(" + PHONE_ATTR + "), "
    "o.sourceAgentRequisite.agent.contact.phones, " + PHONE_ATTR + ")}",
    "address": "${o.addressFull}",
    "comment": "${o.description}",
    "sum": "${o.sum.sumInCurrency / 100.0}",
    "payment": "${" + attr(ATTR_PAYMENT, "entityValue.name") + "}",
    "positions": "${o.positions.size()}",
    "sender": "Отправитель: ${o.targetAgentRequisite.agent.name}, "
    "тел. ${o.targetAgentRequisite.agent.contact.phones}",
}

# Сетка: 4 колонки и 9 строк, в сумме 114 × 71 мм — стикер минус поля.
COLS_MM = (21, 47, 19, 27)
ROWS_MM = (
    9.0,  # 0 номер и дата
    5.5,  # 1 тип доставки
    10.5,  # 2 получатель (2 строки)
    7.0,  # 3 телефон
    14.0,  # 4 адрес (3 строки)
    9.5,  # 5 комментарий (3 строки мелко)
    6.5,  # 6 сумма и оплата
    5.0,  # 7 мест и позиций
    4.0,  # 8 отправитель
)


def _style(size: float, *, bold: bool = False, horiz: str = "left", vert: str = "center",
           wrap: bool = True, top: str = "", num: str = "") -> xlwt.XFStyle:
    spec = (f"font: name Arial, height {round(size * 20)}{', bold on' if bold else ''}; "
            f"align: horiz {horiz}, vert {vert}{', wrap on' if wrap else ''}")
    if top:
        spec += f"; borders: top {top}"
    return xlwt.easyxf(spec, num_format_str=num or "General")


def _mm_to_colwidth(mm: float) -> int:
    # Ширина колонки — в 1/256 ширины цифры шрифта по умолчанию (Arial 10 ≈ 7 px при 96 dpi).
    return round(mm / 25.4 * 96 / 7 * 256)


def _mm_to_twips(mm: float) -> int:
    return round(mm / 25.4 * 72 * 20)


def _pls_record(width_mm: float, height_mm: float) -> bytes:
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


def _with_pls(sheet: Worksheet.Worksheet, pls: bytes) -> None:
    """Правит блок настроек страницы листа по [MS-XLS].

    PLS встаёт перед SETUP. Пустые HEADER/FOOTER пишутся без тела: «нет колонтитула»,
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
            elif rt == SETUP:
                out.append(pls)
            out.append(record)
        return b"".join(out)

    sheet._Worksheet__print_settings_rec = patched


def build(values: dict[str, str] | None = None) -> xlwt.Workbook:
    """Собирает стикер. values подменяет выражения МойСклад — для предпросмотра на примере заказа."""
    v = {**FIELDS, **(values or {})}
    wb = xlwt.Workbook(encoding="utf-8")
    ws = wb.add_sheet("Стикер")

    for i, mm in enumerate(COLS_MM):
        ws.col(i).width = _mm_to_colwidth(mm)
    for i, mm in enumerate(ROWS_MM):
        row = ws.row(i)
        row.height_mismatch = True
        row.height = _mm_to_twips(mm)

    label = _style(7.5, vert="top")
    label_mid = _style(7.5)
    sep = "medium"

    ws.write_merge(0, 0, 0, 1, v["number"], _style(15, bold=True, wrap=False))
    ws.write_merge(0, 0, 2, 3, v["date"], _style(10, horiz="right", wrap=False))

    ws.write(1, 0, "Доставка:", label_mid)
    ws.write_merge(1, 1, 1, 3, v["delivery"], _style(9.5, bold=True))

    ws.write(2, 0, "Получатель:", _style(7.5, vert="top", top=sep))
    ws.write_merge(2, 2, 1, 3, v["recipient"], _style(12, bold=True, vert="top", top=sep))

    ws.write(3, 0, "Телефон:", label_mid)
    ws.write_merge(3, 3, 1, 3, v["phone"], _style(14, bold=True))

    ws.write(4, 0, "Адрес:", label)
    ws.write_merge(4, 4, 1, 3, v["address"], _style(10.5, vert="top"))

    ws.write(5, 0, "Комментарий:", label)
    ws.write_merge(5, 5, 1, 3, v["comment"], _style(8, vert="top"))

    ws.write(6, 0, "Сумма:", _style(7.5, top=sep))
    sum_style = _style(12, bold=True, wrap=False, top=sep, num='#,##0.00" руб."')
    try:
        ws.write(6, 1, float(v["sum"]), sum_style)
    except ValueError:
        ws.write(6, 1, v["sum"], sum_style)
    ws.write(6, 2, "Оплата:", _style(7.5, top=sep))
    ws.write(6, 3, v["payment"], _style(8.5, bold=True, top=sep))

    ws.write(7, 0, "Мест:", label_mid)
    ws.write(7, 1, "________", _style(10))
    ws.write(7, 2, "Позиций:", label_mid)
    ws.write(7, 3, v["positions"], _style(10, wrap=False))

    ws.write_merge(8, 8, 0, 3, v["sender"], _style(6.5, wrap=False, top="thin"))

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
    _with_pls(ws, _pls_record(WIDTH_MM, HEIGHT_MM))
    return wb


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("courier_label_120x75.xls")
    build().save(str(out))
    print(out)


if __name__ == "__main__":
    main()

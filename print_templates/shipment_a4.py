"""Лист A4 на коробку: всё, что на стикере курьера, плюс состав заказа и логотип. Шаблон для «Отгрузок».

    pip install openpyxl pillow        # и LibreOffice (soffice) для перевода в .xls
    python print_templates/shipment_a4.py            # → print_templates/shipment_a4.xls

Загружается в МойСклад руками: отгрузка → Печать → Настроить… → добавить шаблон.
Подробно — print_templates/README.md.

Собирается в openpyxl с логотипом и переводится в .xls через LibreOffice — как договор
(contract_supply.py), который уже напечатан в МойСклад с логотипом без ошибок.

В отгрузке стороны наоборот, чем в заказе: o.sourceAgentRequisite — ваша организация,
o.targetAgentRequisite — покупатель (стандартный УПД МойСклад: «Продавец» — source,
«Покупатель» — target). Позиции отгрузки — o.shipments, как в стандартном «Товарном чеке»
(download-template/demand_check.xls).

Сверено с 600 отгрузками (04.09–05.11.2026): номер отгрузки никогда не совпадает с номером
заказа (02998 и 1139 (2)6), поэтому крупно — номер заказа. Комментарий в отгрузку почти
не копируется (1 % против 25 % у заказов) — берётся из заказа. Адрес доставки есть в 87 %
отгрузок, ещё 4 % — из карточки покупателя. Позиций: медиана 3, 99 % — до 15, на первый
лист помещается около 18.
"""

from __future__ import annotations

import sys
from pathlib import Path

from contract_supply import LOGO, to_xls
from openpyxl import Workbook
from openpyxl.drawing.image import Image as XlImage
from openpyxl.styles import Alignment, Border, Font, Side

OUT = Path(__file__).with_name("shipment_a4.xls")
FONT = "Arial"
MARGIN_MM = 10
LOGO_MM = 45  # ширина логотипа

BUYER = "o.targetAgentRequisite"
SELLER = "o.sourceAgentRequisite"
ORDER = "o.customerOrder"  # заказ покупателя, из которого сделана отгрузка

FIELDS = {
    "number": "ЗАКАЗ № ${" + ORDER + ".name}",
    "shipment": 'Отгрузка № ${o.name} от ${formatter.format("%1$td.%1$tm.%1$tY", '
    "formatter.getExcelDate(o.moment))}",
    "recipient": "${" + BUYER + ".agent.name}",
    "phone": "${" + BUYER + ".agent.contact.phones}",
    # Адрес доставки отгрузки, если пусто — фактический адрес из карточки покупателя.
    "address": "${formatter.printIfElse(empty(o.addressFull), " + BUYER + ".actualAddress, o.addressFull)}",
    "address_note": "${o.address.comment}",
    # Комментарий отгрузки, если пусто — комментарий заказа.
    "comment": "${formatter.printIfElse(empty(o.description), " + ORDER + ".description, o.description)}",
    "sum": "${formatter.allAmount(o) / 100.00}",
    "count": "Всего позиций: ${o.getPositions().size()}",
    "sender": "Отправитель: ${" + SELLER + ".agent.name}, тел. ${" + SELLER + ".agent.contact.phones}",
}

# Сетка A4 без полей: 190 мм. Верх — блок курьера (колонки A:B — подписи, C:F — значения),
# низ — таблица: № | Наименование (B:C) | Кол-во | Ед. | ✓
COLS_MM = (10, 24, 96, 22, 18, 20)
HEAD_ROWS_MM = (
    13.0,  # 0 логотип, номер и дата отгрузки
    13.0,  # 1 номер заказа
    12.0,  # 2 получатель
    11.0,  # 3 телефон
    18.0,  # 4 адрес: 3 строки
    17.0,  # 5 комментарий к адресу: 3 строки
    24.0,  # 6 комментарий: 5 строк
    10.0,  # 7 сумма и места
    6.0,  # 8 отправитель
    5.0,  # 9 отступ
    8.0,  # 10 «Состав заказа» и число позиций
    7.0,  # 11 шапка таблицы
)
TAG_ROW = len(HEAD_ROWS_MM)  # строка <jx:forEach> (с 0); jXLS её удаляет
ITEM_ROW_MM = 6.5

THIN, MEDIUM = Side(style="thin"), Side(style="medium")


def _pt(mm: float) -> float:
    return round(mm * 72 / 25.4, 1)


def _cell(ws, r: int, c0: int, c1: int, value, size: float, *, bold: bool = False, horiz: str = "left",
          vert: str = "center", wrap: bool = True, shrink: bool = False, top: Side | None = None,
          box: bool = False, num: str = "") -> None:
    """Значение в строку r (с 0), колонки c0..c1 (с 0) объединяются; top — линия сверху, box — рамка."""
    cell = ws.cell(r + 1, c0 + 1, value)
    cell.font = Font(name=FONT, size=size, bold=bold)
    cell.alignment = Alignment(horizontal=horiz, vertical=vert, wrap_text=wrap and not shrink,
                               shrink_to_fit=shrink)
    if num:
        cell.number_format = num
    if top or box:
        for c in range(c0, c1 + 1):
            ws.cell(r + 1, c + 1).border = (Border(left=THIN, right=THIN, top=top or THIN, bottom=THIN)
                                            if box else Border(top=top))
    if c1 > c0:
        ws.merge_cells(start_row=r + 1, start_column=c0 + 1, end_row=r + 1, end_column=c1 + 1)


def build(values: dict[str, str] | None = None, items: list[tuple] | None = None) -> Workbook:
    """Собирает лист. values и items (название, кол-во, ед.) — для предпросмотра на реальной отгрузке:
    тогда вместо цикла jXLS пишутся готовые строки, как их развернёт МойСклад."""
    v = {**FIELDS, **(values or {})}
    wb = Workbook()
    normal = next(st for st in wb._named_styles if st.name == "Normal")
    normal.font = Font(name=FONT, size=10)  # ширина колонок в «символах» — по шрифту книги
    ws = wb.active
    ws.title = "Отгрузка"
    for i, mm in enumerate(COLS_MM):
        ws.column_dimensions["ABCDEF"[i]].width = round((mm / 25.4 * 96 - 5) / 7, 2)
    for i, mm in enumerate(HEAD_ROWS_MM):
        ws.row_dimensions[i + 1].height = _pt(mm)

    logo = XlImage(str(LOGO))
    logo.width = round(LOGO_MM / 25.4 * 96)
    logo.height = round(logo.width * 239 / 1142)
    ws.add_image(logo, "A1")
    _cell(ws, 0, 3, 5, v["shipment"], 9, horiz="right", vert="top")

    _cell(ws, 1, 0, 5, v["number"], 26, bold=True, wrap=False, shrink=True)

    _cell(ws, 2, 0, 1, "Получатель:", 10, top=MEDIUM)
    _cell(ws, 2, 2, 5, v["recipient"], 20, bold=True, shrink=True, top=MEDIUM)
    _cell(ws, 3, 0, 1, "Телефон:", 10)
    _cell(ws, 3, 2, 5, v["phone"], 22, bold=True, shrink=True)
    _cell(ws, 4, 0, 1, "Адрес:", 10, vert="top")
    _cell(ws, 4, 2, 5, v["address"], 15, vert="top")
    _cell(ws, 5, 0, 1, "К адресу:", 10, vert="top")
    _cell(ws, 5, 2, 5, v["address_note"], 12, bold=True, vert="top")
    _cell(ws, 6, 0, 1, "Комментарий:", 10, vert="top", top=THIN)
    _cell(ws, 6, 2, 5, v["comment"], 11, vert="top", top=THIN)

    _cell(ws, 7, 0, 1, "Сумма:", 10, top=MEDIUM)
    try:
        sum_value = float(v["sum"])
    except ValueError:
        sum_value = v["sum"]
    _cell(ws, 7, 2, 2, sum_value, 16, bold=True, wrap=False, top=MEDIUM, num='#,##0.00" руб."')
    _cell(ws, 7, 3, 3, "Мест:", 10, horiz="right", top=MEDIUM)
    _cell(ws, 7, 4, 5, "________", 14, top=MEDIUM)
    _cell(ws, 8, 0, 5, v["sender"], 9, shrink=True, top=THIN)

    _cell(ws, 10, 0, 2, "СОСТАВ ЗАКАЗА", 13, bold=True, wrap=False)
    _cell(ws, 10, 3, 5, v["count"], 10, horiz="right", wrap=False)
    for c0, c1, title in ((0, 0, "№"), (1, 2, "Наименование"), (3, 3, "Кол-во"), (4, 4, "Ед."), (5, 5, "✓")):
        _cell(ws, 11, c0, c1, title, 10, bold=True, horiz="center", box=True)

    def item(r: int, n, title, q, u) -> None:
        ws.row_dimensions[r + 1].height = _pt(ITEM_ROW_MM)
        _cell(ws, r, 0, 0, n, 10, horiz="center", box=True)
        _cell(ws, r, 1, 2, title, 10, shrink=True, box=True)
        _cell(ws, r, 3, 3, q, 11, bold=True, horiz="center", box=True)
        _cell(ws, r, 4, 4, u, 10, horiz="center", box=True)
        _cell(ws, r, 5, 5, "", 10, box=True)

    if items is None:
        # Цикл jXLS, как в download-template/demand_check.xls: номер строки — формулой
        # $[ROW() - ROW(A<строка тега>)], строки с тегами МойСклад удаляет при печати.
        ws.cell(TAG_ROW + 1, 1, '<jx:forEach items="${o.shipments}" var="position">')
        item(TAG_ROW + 1, f"$[ROW() - ROW(A{TAG_ROW})]", "${position.printName}",
             "${position.quantity}", "${position.consignment.good.uom.name}")
        ws.cell(TAG_ROW + 3, 1, "</jx:forEach>")
    else:
        for i, (title, q, u) in enumerate(items):
            item(TAG_ROW + i, i + 1, title, q, u)

    # Страница: A4 книжная, без колонтитулов, по ширине — один лист, по высоте — сколько нужно.
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.orientation = "portrait"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    m = ws.page_margins
    m.left = m.right = m.top = m.bottom = MARGIN_MM / 25.4
    m.header = m.footer = 0
    ws.sheet_view.showGridLines = False
    return wb


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT
    print(to_xls(build(), out))


if __name__ == "__main__":
    main()

"""Сборочный лист A4 для склада — шаблон для «Заказов покупателей». Оформление как у shipment_a4.

    pip install openpyxl pillow        # и LibreOffice (soffice) для перевода в .xls
    python print_templates/picking_list.py           # → print_templates/picking_list.xls

Печатается из заказа на статусе «ПОДТВЕРЖДЕН / ОЖИДАЕТ СБОРКИ», когда отгрузки ещё нет.
Без цен и телефона: номер заказа, покупатель, адрес, комментарий крупно (в нём помол,
пробники, «замотать»), позиции с клеткой «Собрано», внизу — кто собрал, кто проверил, мест.

Выражения — из стандартного шаблона МойСклад «Заказ» (download-template/order.xls):
в заказе o.sourceAgentRequisite — покупатель, позиции — o.positions, единица —
position.good.uom.name. Адрес и комментарий к адресу — как в стикере courier_label,
он уже печатался на заказах без ошибок.
"""

from __future__ import annotations

import sys
from pathlib import Path

from contract_supply import LOGO, to_xls
from openpyxl import Workbook
from openpyxl.drawing.image import Image as XlImage
from openpyxl.styles import Font
from shipment_a4 import COLS_MM, FONT, ITEM_ROW_MM, LOGO_MM, MARGIN_MM, MEDIUM, THIN, _cell, _pt

OUT = Path(__file__).with_name("picking_list.xls")
BUYER = "o.sourceAgentRequisite"

FIELDS = {
    "title": 'Сборочный лист · заказ от ${formatter.format("%1$td.%1$tm.%1$tY", '
    "formatter.getExcelDate(o.moment))}",
    "number": "ЗАКАЗ № ${o.name}",
    "buyer": "${" + BUYER + ".agent.name}",
    # Адрес доставки заказа, если пусто — фактический адрес из карточки покупателя.
    "address": "${formatter.printIfElse(empty(o.addressFull), " + BUYER + ".actualAddress, o.addressFull)}",
    "address_note": "${o.address.comment}",
    "comment": "${o.description}",
    # Вызов метода, а не o.positions.size(): ссылку на коллекцию вне цикла jXLS понимает как
    # «размножить строку по позициям» — и таблица разъезжается. Так же в стандартном «Товарном чеке».
    "count": "Всего позиций: ${o.getPositions().size()}",
}

# Та же сетка 190 мм, что у shipment_a4: A:B — подписи, C:F — значения; таблица
# № | Наименование (B:C) | Кол-во | Ед. | ✓ Собрано
HEAD_ROWS_MM = (
    13.0,  # 0 логотип, «Сборочный лист · заказ от …»
    13.0,  # 1 номер заказа
    10.0,  # 2 покупатель
    12.0,  # 3 адрес: 2 строки
    12.0,  # 4 комментарий к адресу: 3 строки
    30.0,  # 5 комментарий: 5 строк крупно
    5.0,  # 6 отступ
    8.0,  # 7 «Состав заказа» и число позиций
    7.0,  # 8 шапка таблицы
)
TAG_ROW = len(HEAD_ROWS_MM)  # строка <jx:forEach> (с 0); jXLS её удаляет


def build(values: dict[str, str] | None = None, items: list[tuple] | None = None) -> Workbook:
    """Собирает лист. values и items (название, кол-во, ед.) — для предпросмотра на реальном заказе:
    тогда вместо цикла jXLS пишутся готовые строки, как их развернёт МойСклад."""
    v = {**FIELDS, **(values or {})}
    wb = Workbook()
    normal = next(st for st in wb._named_styles if st.name == "Normal")
    normal.font = Font(name=FONT, size=10)  # ширина колонок в «символах» — по шрифту книги
    ws = wb.active
    ws.title = "Сборка"
    for i, mm in enumerate(COLS_MM):
        ws.column_dimensions["ABCDEF"[i]].width = round((mm / 25.4 * 96 - 5) / 7, 2)
    for i, mm in enumerate(HEAD_ROWS_MM):
        ws.row_dimensions[i + 1].height = _pt(mm)

    logo = XlImage(str(LOGO))
    logo.width = round(LOGO_MM / 25.4 * 96)
    logo.height = round(logo.width * 239 / 1142)
    ws.add_image(logo, "A1")
    _cell(ws, 0, 3, 5, v["title"], 9, horiz="right", vert="top")

    _cell(ws, 1, 0, 5, v["number"], 26, bold=True, wrap=False, shrink=True)
    _cell(ws, 2, 0, 1, "Покупатель:", 10, top=MEDIUM)
    _cell(ws, 2, 2, 5, v["buyer"], 16, bold=True, shrink=True, top=MEDIUM)
    _cell(ws, 3, 0, 1, "Адрес:", 10, vert="top")
    _cell(ws, 3, 2, 5, v["address"], 12, vert="top")
    _cell(ws, 4, 0, 1, "К адресу:", 10, vert="top")
    _cell(ws, 4, 2, 5, v["address_note"], 11, bold=True, vert="top")
    _cell(ws, 5, 0, 1, "Комментарий:", 10, vert="top", top=THIN)
    _cell(ws, 5, 2, 5, v["comment"], 14, bold=True, vert="top", top=THIN)

    _cell(ws, 7, 0, 2, "СОСТАВ ЗАКАЗА", 13, bold=True, wrap=False, top=MEDIUM)
    _cell(ws, 7, 3, 5, v["count"], 10, horiz="right", wrap=False, top=MEDIUM)
    for c0, c1, title in ((0, 0, "№"), (1, 2, "Наименование"), (3, 3, "Кол-во"), (4, 4, "Ед."),
                          (5, 5, "✓ Собрано")):
        # «✓ Собрано» — 8 пт: шире колонки текст вылезает, и LibreOffice ужимает весь лист
        _cell(ws, 8, c0, c1, title, 8 if c0 == 5 else 10, bold=True, horiz="center", box=True, wrap=False)

    def item(r: int, n, title, q, u) -> None:
        ws.row_dimensions[r + 1].height = _pt(ITEM_ROW_MM + 1)
        _cell(ws, r, 0, 0, n, 10, horiz="center", box=True)
        _cell(ws, r, 1, 2, title, 11, shrink=True, box=True)
        _cell(ws, r, 3, 3, q, 13, bold=True, horiz="center", box=True)
        _cell(ws, r, 4, 4, u, 10, horiz="center", box=True)
        _cell(ws, r, 5, 5, "", 10, box=True)

    if items is None:
        # Цикл jXLS, как в download-template/order.xls: номер строки — формулой
        # $[ROW() - ROW(A<строка тега>)], строки с тегами МойСклад удаляет при печати.
        ws.cell(TAG_ROW + 1, 1, '<jx:forEach items="${o.positions}" var="position">')
        item(TAG_ROW + 1, f"$[ROW() - ROW(A{TAG_ROW})]", "${position.printName}",
             "${position.quantity}", "${position.good.uom.name}")
        ws.cell(TAG_ROW + 3, 1, "</jx:forEach>")
        after = TAG_ROW + 3  # первая строка (с 0) после закрывающего тега
    else:
        for i, (title, q, u) in enumerate(items):
            item(TAG_ROW + i, i + 1, title, q, u)
        after = TAG_ROW + len(items)

    # Под таблицей — подписи склада; jXLS сдвигает эти строки вниз вместе с таблицей.
    for r, left, right in ((after + 1, "Собрал: ______________________", "Мест: ________"),
                           (after + 2, "Проверил: ____________________", "Дата, время: _______")):
        ws.row_dimensions[r + 1].height = _pt(12)
        _cell(ws, r, 0, 2, left, 12, vert="bottom")
        _cell(ws, r, 3, 5, right, 12, vert="bottom")

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

"""Лист A4 на коробку: всё, что на стикере курьера, плюс состав заказа. Шаблон для «Отгрузок».

    pip install xlwt
    python print_templates/shipment_a4.py            # → print_templates/shipment_a4.xls

Загружается в МойСклад руками: отгрузка → Печать → Настроить… → добавить шаблон.
Подробно — print_templates/README.md.

В отгрузке стороны наоборот, чем в заказе: o.sourceAgentRequisite — ваша организация,
o.targetAgentRequisite — покупатель (стандартный УПД МойСклад: «Продавец» — source,
«Покупатель» — target). Позиции отгрузки — o.shipments, как в стандартном «Товарном чеке»
(download-template/demand_check.xls).

Сверено с 600 отгрузками (04.09–05.11.2026): номер отгрузки никогда не совпадает с номером
заказа (02998 и 1139 (2)6), поэтому крупно — номер заказа. Комментарий в отгрузку почти
не копируется (1 % против 25 % у заказов) — берётся из заказа. Адрес доставки есть в 87 %
отгрузок, ещё 4 % — из карточки покупателя. Позиций: медиана 3, 99 % — до 15, на первый
лист помещается около 20.
"""

from __future__ import annotations

import sys
from pathlib import Path

import xlwt
from courier_label import fix_page_block, mm_to_colwidth, mm_to_twips, style

MARGIN_MM = 10

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
    13.0,  # 0 номер заказа, номер и дата отгрузки
    12.0,  # 1 получатель
    11.0,  # 2 телефон
    18.0,  # 3 адрес: 3 строки
    17.0,  # 4 комментарий к адресу: 3 строки
    24.0,  # 5 комментарий: 5 строк
    10.0,  # 6 сумма и места
    6.0,  # 7 отправитель
    5.0,  # 8 отступ
    8.0,  # 9 «Состав заказа» и число позиций
    7.0,  # 10 шапка таблицы
)
TAG_ROW = len(HEAD_ROWS_MM)  # строка <jx:forEach>; jXLS её удаляет
ITEM_ROW_MM = 6.5


def build(values: dict[str, str] | None = None, items: list[tuple[str, str, str]] | None = None) -> xlwt.Workbook:
    """Собирает лист. values и items (название, кол-во, ед.) — для предпросмотра на реальной отгрузке:
    тогда вместо цикла jXLS пишутся готовые строки, как их развернёт МойСклад."""
    v = {**FIELDS, **(values or {})}
    wb = xlwt.Workbook(encoding="utf-8")
    # COUNTRY: без неё POI в МойСклад падает на цикле jXLS («CountryRecord not found»
    # при сдвиге строк) — шаблон с таблицей печатается как «Некорректный шаблон».
    wb.country_code = 7
    ws = wb.add_sheet("Отгрузка")

    for i, mm in enumerate(COLS_MM):
        ws.col(i).width = mm_to_colwidth(mm)
    for i, mm in enumerate(HEAD_ROWS_MM):
        row = ws.row(i)
        row.height_mismatch = True
        row.height = mm_to_twips(mm)

    sep = "medium"
    label = style(10, vert="top")

    ws.write_merge(0, 0, 0, 2, v["number"], style(26, bold=True, wrap=False, shrink=True))
    ws.write_merge(0, 0, 3, 5, v["shipment"], style(9, horiz="right"))

    ws.write_merge(1, 1, 0, 1, "Получатель:", style(10, top=sep))
    ws.write_merge(1, 1, 2, 5, v["recipient"], style(20, bold=True, wrap=False, shrink=True, top=sep))

    ws.write_merge(2, 2, 0, 1, "Телефон:", style(10))
    ws.write_merge(2, 2, 2, 5, v["phone"], style(22, bold=True, wrap=False, shrink=True))

    ws.write_merge(3, 3, 0, 1, "Адрес:", label)
    ws.write_merge(3, 3, 2, 5, v["address"], style(15, vert="top"))

    ws.write_merge(4, 4, 0, 1, "К адресу:", label)
    ws.write_merge(4, 4, 2, 5, v["address_note"], style(12, bold=True, vert="top"))

    ws.write_merge(5, 5, 0, 1, "Комментарий:", style(10, vert="top", top="thin"))
    ws.write_merge(5, 5, 2, 5, v["comment"], style(11, vert="top", top="thin"))

    ws.write_merge(6, 6, 0, 1, "Сумма:", style(10, top=sep))
    sum_style = style(16, bold=True, wrap=False, top=sep, num='#,##0.00" руб."')
    try:
        ws.write(6, 2, float(v["sum"]), sum_style)
    except ValueError:
        ws.write(6, 2, v["sum"], sum_style)
    ws.write(6, 3, "Мест:", style(10, horiz="right", top=sep))
    ws.write_merge(6, 6, 4, 5, "________", style(14, top=sep))

    ws.write_merge(7, 7, 0, 5, v["sender"], style(9, wrap=False, shrink=True, top="thin"))

    ws.write_merge(9, 9, 0, 2, "СОСТАВ ЗАКАЗА", style(13, bold=True, wrap=False))
    ws.write_merge(9, 9, 3, 5, v["count"], style(10, horiz="right", wrap=False))

    head = style(10, bold=True, horiz="center", box="thin")
    ws.write(10, 0, "№", head)
    ws.write_merge(10, 10, 1, 2, "Наименование", head)
    ws.write(10, 3, "Кол-во", head)
    ws.write(10, 4, "Ед.", head)
    ws.write(10, 5, "✓", head)

    num = style(10, horiz="center", box="thin")
    name = style(10, wrap=False, shrink=True, box="thin")
    qty = style(11, bold=True, horiz="center", box="thin")
    uom = style(10, horiz="center", box="thin")
    check = style(10, box="thin")

    def item_row(r: int, n: int | str, title: str, q: float | str, u: str) -> None:
        row = ws.row(r)
        row.height_mismatch = True
        row.height = mm_to_twips(ITEM_ROW_MM)
        ws.write(r, 0, n, num)
        ws.write_merge(r, r, 1, 2, title, name)
        ws.write(r, 3, q, qty)
        ws.write(r, 4, u, uom)
        ws.write(r, 5, "", check)

    if items is None:
        # Цикл jXLS, как в download-template/demand_check.xls: номер строки — формулой
        # $[ROW() - ROW(A<строка тега>)], строки с тегами МойСклад удаляет при печати.
        ws.write(TAG_ROW, 0, '<jx:forEach items="${o.shipments}" var="position">')
        item_row(TAG_ROW + 1, f"$[ROW() - ROW(A{TAG_ROW})]", "${position.printName}",
                 "${position.quantity}", "${position.consignment.good.uom.name}")
        ws.write(TAG_ROW + 2, 0, "</jx:forEach>")
    else:
        for i, (title, q, u) in enumerate(items):
            item_row(TAG_ROW + i, i + 1, title, q, u)

    # Страница: A4 книжная, без колонтитулов, по ширине — один лист, по высоте — сколько нужно.
    ws.header_str = b""  # xlwt на Python 3 принимает здесь только bytes
    ws.footer_str = b""
    ws.header_margin = ws.footer_margin = 0
    ws.left_margin = ws.right_margin = ws.top_margin = ws.bottom_margin = MARGIN_MM / 25.4
    ws.portrait = True
    ws.paper_size_code = 9  # A4
    ws.fit_num_pages = 1
    ws.fit_width_to_pages = 1
    ws.fit_height_to_pages = 0
    ws.show_grid = False
    fix_page_block(ws)
    return wb


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("shipment_a4.xls")
    build().save(str(out))
    print(out)


if __name__ == "__main__":
    main()

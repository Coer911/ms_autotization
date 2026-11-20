"""Неизменяемый XLSX маршрутного листа. В Диск этот модуль не ходит."""
from __future__ import annotations

from datetime import datetime
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.page import PageMargins

FORMAT_VERSION = "2"
# Заголовки как в файле логиста «ДД.ММ.ГГГГ.xlsx», вкладка МЛ — байт в байт, с висячими пробелами.
HEADERS = (
    "Дата доставка",
    "Адрес отправки ",
    "комментарий отправителя",
    "Доставка С",
    "Доставка ПО",
    "Адрес доставка",
    "Офис/квартира отправки",
    "Покупатель",
    "контактный телефон",
    "Наличными",
    "Цена доставки ",
    "Кол-во мест",
    "Комментарий покупателя",
)
SENDER_ADDRESS = "ул. Курляндская, д. 49"
SENDER_NOTE = "Остановитесь у арки, контактный номер склада 89213892435"
_WIDTHS = (10.13, 21.63, 21.5, 9, 11.75, 26.38, 9, 26.13, 16.75, 10.13, 13, 9.25, 38.5)
_FIXED = datetime(2000, 1, 1)
_FONT = Font(name="Arial", size=10)
_THIN = Border(
    left=Side(style="thin", color="BFBFBF"),
    right=Side(style="thin", color="BFBFBF"),
    top=Side(style="thin", color="BFBFBF"),
    bottom=Side(style="thin", color="BFBFBF"),
)


def build_route_xlsx(day_text: str, orders: list[dict], publication_id: str) -> bytes:
    """Вкладка МЛ в формате логиста. Один набор строк и id публикации — одни и те же байты.

    Строка заказа: counterparty, address, phone, comment, start, end, places (и id).
    """
    day = datetime.strptime(day_text, "%d.%m.%Y")
    wb = Workbook()
    wb.properties.creator = "moysklad-hub"
    wb.properties.lastModifiedBy = "moysklad-hub"
    wb.properties.created = _FIXED
    wb.properties.modified = _FIXED
    sheet = wb.active
    sheet.title = "МЛ"
    _page(sheet, day_text, len(orders))
    wrap = Alignment(wrap_text=True, vertical="top")
    for col, title in enumerate(HEADERS, 1):
        cell = sheet.cell(1, col, title)
        cell.font = _FONT
        cell.alignment = wrap
        cell.border = _THIN
    for index, width in enumerate(_WIDTHS, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.print_title_rows = "1:1"
    for row, order in enumerate(orders, 2):
        values = [
            day,
            SENDER_ADDRESS,
            SENDER_NOTE,
            int(order.get("start") or 12),
            int(order.get("end") or 18),
            order.get("address") or "",
            None,
            order.get("counterparty") or "",
            order.get("phone") or "",
            None,
            None,
            int(order.get("places") or 1),
            order.get("comment") or "",
        ]
        for col, value in enumerate(values, 1):
            cell = sheet.cell(row, col, value)
            cell.font = _FONT
            cell.alignment = wrap
            cell.border = _THIN
        sheet.cell(row, 1).number_format = "dd.mm.yyyy"
    last = max(1, 1 + len(orders))
    sheet.print_area = f"A1:M{last}"
    hidden = wb.create_sheet("_hub")
    hidden.sheet_state = "hidden"
    hidden.append(("order_id", "route_date", "format_version", "publication_id", "source"))
    for order in orders:
        hidden.append((order["id"], day_text, FORMAT_VERSION, publication_id,
                       order.get("source") or ""))
    out = BytesIO()
    wb.save(out)
    return out.getvalue()


MONTH_HEADERS = (
    "№",
    "Заказ",
    "Дата заказа",
    "Плановая отгрузка",
    "Контрагент",
    "Адрес доставки",
    "Телефон",
    "Часы приёмки",
    "Сумма, ₽",
    "Статус",
    "Комментарий логисту",
    "Выполнено",
)
_MONTH_WIDTHS = (5, 11, 12, 13, 28, 42, 17, 14, 12, 22, 30, 12)


def build_month_xlsx(title: str, orders: list[dict]) -> bytes:
    """Маршрутный лист за период со статусом каждого заказа (python -m hub route-sheet)."""
    wb = Workbook()
    wb.properties.creator = "moysklad-hub"
    sheet = wb.active
    sheet.title = "Маршрут"
    _page(sheet, title, len(orders))
    header_font = Font(bold=True, name="Calibri", size=11)
    center = Alignment(wrap_text=True, vertical="center", horizontal="center")
    left = Alignment(wrap_text=True, vertical="center", horizontal="left")
    last_col = get_column_letter(len(MONTH_HEADERS))
    for col, name in enumerate(MONTH_HEADERS, 1):
        cell = sheet.cell(1, col, name)
        cell.font = header_font
        cell.alignment = center
        cell.border = _THIN
    for index, width in enumerate(_MONTH_WIDTHS, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.row_dimensions[1].height = 30
    sheet.freeze_panes = "A2"
    last = max(1, 1 + len(orders))
    sheet.auto_filter.ref = f"A1:{last_col}{last}"
    sheet.print_title_rows = "1:1"
    for number, order in enumerate(orders, 1):
        row = number + 1
        values = [
            number,
            order.get("name") or "",
            order.get("date") or "",
            order.get("delivery") or "",
            order.get("counterparty") or "",
            order.get("address") or "",
            order.get("phone") or "",
            str(order.get("hours") or ""),
            order.get("sum") or 0,
            order.get("state") or "",
            order.get("comment") or "",
            "",
        ]
        for col, value in enumerate(values, 1):
            cell = sheet.cell(row, col, value)
            cell.alignment = left
            cell.border = _THIN
            if col in (2, 8):
                cell.number_format = "@"
            if col == 9:
                cell.number_format = "#,##0.00"
        sheet.row_dimensions[row].height = 32
    if orders:
        total = len(orders) + 2
        sheet.cell(total, 8, "Итого").font = header_font
        cell = sheet.cell(total, 9, f"=SUM(I2:I{last})")
        cell.font = header_font
        cell.number_format = "#,##0.00"
        last = total
    sheet.print_area = f"A1:{last_col}{last}"
    hidden = wb.create_sheet("_hub")
    hidden.sheet_state = "hidden"
    hidden.append(("order_id", "name", "state"))
    for order in orders:
        hidden.append((order["id"], order.get("name") or "", order.get("state") or ""))
    out = BytesIO()
    wb.save(out)
    return out.getvalue()


def _page(sheet, day_text: str, count: int) -> None:
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.page_setup.horizontalCentered = True
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_margins = PageMargins(left=0.4, right=0.4, top=0.7, bottom=0.6, header=0.28, footer=0.28)
    sheet.oddHeader.left.text = f"Маршрутный лист — {day_text}"
    sheet.oddHeader.right.text = f"заказов: {count}"
    sheet.oddFooter.right.text = "Стр. &P из &N"
    sheet.page_setup.scale = 100
    sheet.sheet_view.showGridLines = False
    sheet.print_options.horizontalCentered = True
    sheet.sheet_properties.tabColor = "1F4E79"

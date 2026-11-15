"""Неизменяемый XLSX маршрутного листа. В Диск этот модуль не ходит."""
from __future__ import annotations

from datetime import datetime
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.page import PageMargins

FORMAT_VERSION = "1"
HEADERS = (
    "№",
    "Контрагент",
    "Адрес доставки",
    "Телефон",
    "Часы приёмки",
    "Комментарий логисту",
    "Выполнено",
)
_WIDTHS = (6, 28, 44, 18, 18, 32, 14)
_FIXED = datetime(2000, 1, 1)
_THIN = Border(
    left=Side(style="thin", color="BFBFBF"),
    right=Side(style="thin", color="BFBFBF"),
    top=Side(style="thin", color="BFBFBF"),
    bottom=Side(style="thin", color="BFBFBF"),
)


def build_route_xlsx(day_text: str, orders: list[dict], publication_id: str) -> bytes:
    """Один и тот же набор заказов и id публикации даёт одни и те же байты."""
    wb = Workbook()
    wb.properties.creator = "moysklad-hub"
    wb.properties.lastModifiedBy = "moysklad-hub"
    wb.properties.created = _FIXED
    wb.properties.modified = _FIXED
    sheet = wb.active
    sheet.title = "Маршрут"
    _page(sheet, day_text, len(orders))
    header_font = Font(bold=True, name="Calibri", size=11)
    center = Alignment(wrap_text=True, vertical="center", horizontal="center")
    left = Alignment(wrap_text=True, vertical="center", horizontal="left")
    for col, title in enumerate(HEADERS, 1):
        cell = sheet.cell(1, col, title)
        cell.font = header_font
        cell.alignment = center
        cell.border = _THIN
    for index, width in enumerate(_WIDTHS, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.row_dimensions[1].height = 30
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:G{max(1, 1 + len(orders))}"
    sheet.print_title_rows = "1:1"
    for number, order in enumerate(orders, 1):
        row = number + 1
        hours = str(order.get("hours") or "")
        values = [
            number,
            order.get("counterparty") or "",
            order.get("address") or "",
            order.get("phone") or "",
            hours,
            order.get("comment") or "",
            "",
        ]
        for col, value in enumerate(values, 1):
            cell = sheet.cell(row, col, value)
            cell.alignment = left
            cell.border = _THIN
            if col == 5:
                cell.number_format = "@"
                cell.value = hours
        sheet.row_dimensions[row].height = 32
    last = max(1, 1 + len(orders))
    sheet.print_area = f"A1:G{last}"
    hidden = wb.create_sheet("_hub")
    hidden.sheet_state = "hidden"
    hidden.append(("order_id", "route_date", "format_version", "publication_id"))
    for order in orders:
        hidden.append((order["id"], day_text, FORMAT_VERSION, publication_id))
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

"""Договор поставки товаров — печатная форма МойСклад для раздела «Договоры» (CRM → Договоры).

    pip install openpyxl pillow        # и LibreOffice (soffice) для перевода в .xls
    python print_templates/contract_supply.py   # → print_templates/contract_supply.xls

Текст договора — в contract_supply.txt (правится как обычный текст), логотип —
assets/cultura_logo.png. Шаблоны договоров в МойСклад — .xls: и стандартный «Договор»
(GET /download-template/contract.xls), и ваш «Договор ПК для ООО без отсрочки.xls».
Лист собирается в openpyxl (он умеет картинки) и переводится в .xls через LibreOffice.

Поля — как в стандартном contract.xls МойСклад: o.name, o.moment, o.agent.* — покупатель,
o.agent.requisite.* — его реквизиты и счёт, o.agent.contact.* — телефон и e-mail.
Фактический адрес o.agent.requisite.actualAddress — из справки МойСклад «Формулы для шаблона
договора». ФИО директора покупателя и срока оплаты в МойСклад нет — на листе пропуски.
Данные поставщика — текстом из договора (в карточке ИП в МойСклад адрес и телефон другие).

Высоты строк и разрывы страниц считаются здесь же по метрикам шрифта: в .xls строки
не растягиваются под текст сами, а раздел 11 не должен разорваться между листами.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XlImage
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.worksheet.pagebreak import Break
from PIL import ImageFont

HERE = Path(__file__).parent
TEXT = HERE / "contract_supply.txt"
LOGO = HERE / "assets" / "cultura_logo.png"
OUT = HERE / "contract_supply.xls"

FONT_DIR = Path("/usr/share/fonts/truetype/liberation")  # Liberation Sans = метрики Arial
FONT = "Arial"
SIZE = 10

# A4: поля и колонки в мм. A — номера пунктов, B:D — текст; в реквизитах A:B — подпись строки.
MARGINS_MM = {"left": 20, "right": 10, "top": 12, "bottom": 18, "footer": 8}
COLS_MM = (13, 37, 65, 65)
PAGE_MM = 297 - MARGINS_MM["top"] - MARGINS_MM["bottom"]

IS_IP = 'o.agent.getCompanyType().name().equals("INPR")'
BUYER_TITLE = ("formatter.printIfElse(empty(o.agent.requisite.legalTitle), o.agent.name, "
               "o.agent.requisite.legalTitle)")
BLANK = "____________________"

FIELDS = {
    "НОМЕР": "${o.name}",
    "ДАТА": '«${formatter.format("%1$td", o.moment)}» ${formatter.format("%1$tB %1$tY", o.moment)} г.',
    # ООО — «в лице Генерального директора …», ИП — от своего имени.
    "ПОКУПАТЕЛЬ": "${" + BUYER_TITLE + "}${formatter.printIfElse(" + IS_IP
    + ', ", именуемый в дальнейшем «Покупатель»", ", именуемое в дальнейшем «Покупатель», '
    "в лице Генерального директора " + BLANK + ', действующего на основании Устава")}',
    "ОТСРОЧКА": "______",  # у каждого клиента своя; поле в МойСклад — после решения
}

ACC = "o.agent.requisite.account"
# (подпись, покупатель — выражение МойСклад, поставщик — текст, строк под значение)
REQUISITES = [
    ("Наименование компании", "${" + BUYER_TITLE + "}", "ИП Краснояров Данил Сергеевич", 3),
    ("Юр. адрес", "${o.agent.requisite.legalAddress}",
     "192148, Санкт-Петербург, ул. Седова, д. 23, кв. 3", 4),
    ("Факт. адрес", "${o.agent.requisite.actualAddress}", "198035, Санкт-Петербург, ул. Курляндская, д. 49", 4),
    ("Номер лицензии, соответствующий номеру записи в реестре", "", "—", 3),
    ("Оператор ЭДО", "", "Диадок", 1),
    ("ИНН / КПП", "${o.agent.requisite.INN}${formatter.printIf(!empty(o.agent.requisite.KPP), "
     '" / " + o.agent.requisite.KPP)}', "781143363491", 1),
    ("КПП ОП", "", "—", 1),
    ("ОГРН / ОГРНИП", "${formatter.printIfElse(!empty(o.agent.requisite.KPP), o.agent.requisite.ogrn, "
     "o.agent.requisite.ogrnip)}", "ОГРНИП 315784700227318", 1),
    ("Р/с", "${" + ACC + ".accountNumber}", "40802810100000029308", 1),
    ("Банк", "${" + ACC + ".bankName}", "АО «Т-Банк»", 2),
    ("Корр. счёт", "${" + ACC + ".correspondentAccount}", "30101810145250000974", 1),
    ("БИК", "${" + ACC + ".BIC}", "044525974", 1),
    ("Контактный телефон представителя", "${o.agent.contact.phones}", "+7 (921) 891-78-84", 2),
    ("E-mail", "${o.agent.contact.email}", "hello@cultura-coffee.ru", 1),
]
SIGN_POSITION = ('${formatter.printIfElse(' + IS_IP
                 + ', "Индивидуальный предприниматель", "Генеральный директор")}')


@dataclass
class Row:
    height_pt: float
    cells: list = field(default_factory=list)  # (col_from, col_to, value, font, alignment, border)
    keep_with_next: bool = False
    image: bool = False


def _font(size: float = SIZE, bold: bool = False) -> Font:
    return Font(name=FONT, size=size, bold=bold)


_measure_cache: dict = {}


def _text_lines(text: str, width_mm: float, size: float = SIZE, bold: bool = False) -> int:
    """Сколько строк займёт текст в ячейке шириной width_mm (перенос по словам, как в Excel)."""
    key = (size, bold)
    if key not in _measure_cache:
        name = "LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf"
        _measure_cache[key] = ImageFont.truetype(str(FONT_DIR / name), size=size * 10)
    font = _measure_cache[key]
    limit = (width_mm * 72 / 25.4 - 5) * 10  # минус отступы ячейки
    lines = 0
    for paragraph in text.split("\n"):
        line, count = "", 1
        for word in paragraph.split(" "):
            probe = f"{line} {word}".strip()
            if font.getlength(probe) <= limit:
                line = probe
            else:
                count, line = count + 1, word
        lines += count
    return lines


def _height(lines: int, size: float = SIZE, extra: float = 3) -> float:
    return lines * size * 1.27 + extra  # 12,7 pt на строку 10 pt — как у Excel


def _width(c0: int, c1: int) -> float:
    return sum(COLS_MM[c0:c1 + 1])


def _fill(text: str, values: dict) -> str:
    return re.sub(r"{{(\w+)}}", lambda m: values[m.group(1)], text)


def _sample_len(text: str) -> str:
    """Для расчёта высоты строки с полями: подставляет длинные примерные значения."""
    return re.sub(r"\$\{[^}]*}", "Х" * 12, text).replace(
        "Х" * 12, 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ТЕХНОЛОГИЯ ДВИЖЕНИЯ"', 1)


def _rows(values: dict, buyer: dict | None) -> list[Row]:
    just = Alignment(horizontal="justify", vertical="top", wrap_text=True)
    left = Alignment(horizontal="left", vertical="top", wrap_text=True)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    right = Alignment(horizontal="right", vertical="top", wrap_text=True)
    thin = Side(style="thin")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)

    rows = [Row(_mm_pt(15), image=True)]
    for raw in TEXT.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line or line.startswith("# "):
            continue
        if line.startswith("!TITLE "):
            text = _fill(line[7:], values)
            rows.append(Row(_height(1, 12, 8), [(0, 3, text, _font(12, True), center, None)]))
        elif line.startswith("!CITYDATE "):
            city, date = _fill(line[10:], values).split("|")
            rows.append(Row(_height(1, extra=10), [(0, 1, city, _font(), left, None),
                                                    (2, 3, date, _font(), right, None)]))
        elif line == "!REQUISITES":
            rows += _requisites(values, buyer, box, left, center)
        elif line.startswith("## "):
            text = line[3:]
            rows.append(Row(_height(_text_lines(text, _width(0, 3), SIZE, True), extra=10),
                            [(0, 3, text, _font(bold=True), center, None)], keep_with_next=True))
        elif m := re.match(r"^(\d+(?:\.\d+)+\.)\s+(.*)$", line):
            num, text = m.groups()
            text = _fill(text, values)
            n = _text_lines(_sample_len(text), _width(1, 3))
            rows.append(Row(_height(n), [(0, 0, num, _font(), left, None), (1, 3, text, _font(), just, None)]))
        elif line.startswith("- "):
            text = "– " + line[2:]
            n = _text_lines(text, _width(1, 3))
            rows.append(Row(_height(n), [(1, 3, text, _font(), just, None)]))
        else:
            text = _fill(line, values)
            n = _text_lines(_sample_len(text), _width(0, 3))
            rows.append(Row(_height(n), [(0, 3, text, _font(), just, None)]))
    return rows


def _requisites(values: dict, buyer: dict | None, box: Border, left: Alignment, center: Alignment) -> list[Row]:
    head = _font(bold=True)
    out = [Row(_height(1), [(0, 1, "", head, center, box), (2, 2, "Покупатель", head, center, box),
                            (3, 3, "Поставщик", head, center, box)], keep_with_next=True)]
    for label, expr, supplier, lines in REQUISITES:
        value = (buyer or {}).get(label, expr)
        n = max(lines, _text_lines(label, _width(0, 1), 9), _text_lines(supplier, _width(3, 3)))
        out.append(Row(_height(n, extra=2), [(0, 1, label, _font(9), left, box), (2, 2, value, _font(), left, box),
                                             (3, 3, supplier, _font(), left, box)], keep_with_next=True))
    position = (buyer or {}).get("Должность", SIGN_POSITION)
    out.append(Row(_height(1, extra=8), [(2, 2, position, _font(bold=True), left, None),
                                         (3, 3, "Индивидуальный предприниматель", _font(bold=True), left, None)],
                   keep_with_next=True))
    out.append(Row(_height(1, extra=14), [(2, 2, "______________ / ______________ /", _font(), left, None),
                                          (3, 3, "______________ / Краснояров Д.С. /", _font(), left, None)],
                   keep_with_next=True))
    out.append(Row(_height(1, extra=4), [(2, 2, "М.П.", _font(), left, None), (3, 3, "М.П.", _font(), left, None)],
                   keep_with_next=True))
    return out


def _mm_pt(mm: float) -> float:
    return mm * 72 / 25.4


def _page_breaks(rows: list[Row]) -> list[int]:
    """Номера строк (с 1), перед которыми начинается новый лист. Блок из строк с keep_with_next
    переносится целиком; страница берётся на 4 % меньше, чтобы автоматический разрыв не опередил."""
    page, used, breaks, i = _mm_pt(PAGE_MM) * 0.96, 0.0, [], 0
    while i < len(rows):
        j = i
        while j < len(rows) - 1 and rows[j].keep_with_next:
            j += 1
        block = sum(r.height_pt for r in rows[i:j + 1])
        if used and used + block > page and block <= page:
            breaks.append(i)  # строка i (с 0) → разрыв после строки i в нумерации с 1
            used = 0.0
        for k in range(i, j + 1):
            if used + rows[k].height_pt > page:
                breaks.append(k)
                used = 0.0
            used += rows[k].height_pt
        i = j + 1
    return breaks


def build(values: dict | None = None, buyer: dict | None = None) -> Workbook:
    """Собирает лист. values/buyer подменяют поля МойСклад — для предпросмотра на реальном договоре."""
    v = {**FIELDS, **(values or {})}
    rows = _rows(v, buyer)
    wb = Workbook()
    normal = next(st for st in wb._named_styles if st.name == "Normal")
    normal.font = _font()  # ширина колонок в «символах» считается по шрифту книги — пусть это Arial 10
    ws = wb.active
    ws.title = "Договор"
    for i, mm in enumerate(COLS_MM):
        ws.column_dimensions["ABCD"[i]].width = round((mm / 25.4 * 96 - 5) / 7, 2)
    for r, row in enumerate(rows, start=1):
        ws.row_dimensions[r].height = round(row.height_pt, 1)
        for c0, c1, value, font, align, border in row.cells:
            cell = ws.cell(r, c0 + 1, value)
            cell.font, cell.alignment = font, align
            if border:
                for c in range(c0, c1 + 1):
                    ws.cell(r, c + 1).border = border
            if c1 > c0:
                ws.merge_cells(start_row=r, start_column=c0 + 1, end_row=r, end_column=c1 + 1)
    logo = XlImage(str(LOGO))
    logo.width = round(55 / 25.4 * 96)  # 55 мм
    logo.height = round(logo.width * 239 / 1142)
    ws.add_image(logo, "A1")
    for b in _page_breaks(rows):
        ws.row_breaks.append(Break(id=b))  # разрыв после строки b (нумерация с 1) = перед строкой b+1

    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.orientation = "portrait"
    # По ширине — один лист: конвертеры считают ширину колонок по-разному (LibreOffice шире Excel),
    # лишнее ужимается масштабом, а не уходит на отдельные страницы. По высоте — сколько нужно.
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    m = ws.page_margins
    m.left, m.right = MARGINS_MM["left"] / 25.4, MARGINS_MM["right"] / 25.4
    m.top, m.bottom = MARGINS_MM["top"] / 25.4, MARGINS_MM["bottom"] / 25.4
    m.header, m.footer = 0, MARGINS_MM["footer"] / 25.4
    ws.oddFooter.left.text = "Покупатель ____________________"
    ws.oddFooter.center.text = "Поставщик ____________________"
    ws.oddFooter.right.text = "Стр. &P из &N"
    for part in (ws.oddFooter.left, ws.oddFooter.center, ws.oddFooter.right):
        part.size, part.font = 8, FONT
    ws.sheet_view.showGridLines = False
    ws.print_area = f"A1:D{len(rows)}"
    return wb


def to_xls(wb: Workbook, out: Path) -> Path:
    """Сохраняет книгу в .xls через LibreOffice (BIFF8 с картинкой — как шаблоны МойСклад)."""
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise SystemExit("нужен LibreOffice (soffice) для перевода в .xls")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / (out.stem + ".xlsx")
        wb.save(src)
        subprocess.run([soffice, "--headless", "--convert-to", "xls", "--outdir", tmp, str(src)],
                       check=True, capture_output=True)
        shutil.move(Path(tmp) / (out.stem + ".xls"), out)
    return out


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT
    print(to_xls(build(), out))


if __name__ == "__main__":
    main()

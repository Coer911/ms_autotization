"""tea-buy: позиции и количество по техкартам чая, без заказа поставщику."""
from io import BytesIO

import pytest
from openpyxl import Workbook

from hub import __main__ as cli
from hub.tea_buy import OrderNotPlaced, parse_tech_cards, positions, report


def _xlsx(builder) -> bytes:
    book = Workbook()
    builder(book)
    buf = BytesIO()
    book.save(buf)
    return buf.getvalue()


def _long(book: Workbook) -> None:
    ws = book.active
    ws.title = "Купажирование"
    ws.append(["Жасминовый"])
    ws.append(["Выход, кг", 10])
    ws.append(["Компонент", "Количество", "Ед"])
    ws.append(["Сенча", 7, "кг"])
    ws.append(["Жасминовый цвет", "0,2", "кг"])
    ws.append([])
    ws.append(["Эрл Грей"])
    ws.append(["Выход, кг", 10])
    ws.append(["Компонент", "%"])
    ws.append(["Сенча", 60])
    ws.append(["Бергамот", 4])
    aroma = book.create_sheet("Ароматизация")
    aroma.append(["Техкарта", "Сырьё", "Норма", "Ед. изм."])
    aroma.append(["Жасминовый", "Ароматизатор жасмин", 15, "г"])
    aroma.append([None, "Спирт", 5, "г"])


def _matrix(book: Workbook) -> None:
    ws = book.active
    ws.title = "Купажи"
    ws.append(["Компонент", "Ед", "Жасминовый", "Эрл Грей"])
    ws.append(["Сенча", "кг", 7, 6])
    ws.append(["Жасминовый цвет", "кг", 0.2, None])


def test_long_sheet_sums_components_and_converts_percent():
    rows = positions(_xlsx(_long))
    by_name = {(row.name, row.unit): row for row in rows}
    assert by_name[("Сенча", "кг")].quantity == 13
    assert by_name[("Жасминовый цвет", "кг")].quantity == 0.2
    assert by_name[("Бергамот", "кг")].quantity == 0.4
    assert by_name[("Ароматизатор жасмин", "г")].quantity == 15
    assert by_name[("Спирт", "г")].quantity == 5
    assert "Купажирование: Жасминовый" in by_name[("Сенча", "кг")].cards
    assert "Купажирование: Эрл Грей" in by_name[("Сенча", "кг")].cards


def test_matrix_and_card_filter_and_batches():
    data = _xlsx(_matrix)
    rows = positions(data, card="жасмин", batches=2)
    assert [(row.name, row.quantity, row.unit) for row in rows] == [
        ("Жасминовый цвет", 0.4, "кг"),
        ("Сенча", 14, "кг"),
    ]


def test_percent_without_output_stays_a_share():
    def build(book: Workbook) -> None:
        ws = book.active
        ws.title = "Ароматизация"
        ws.append(["Компонент", "%"])
        ws.append(["Бергамот", 3])

    rows = positions(_xlsx(build), batches=4)
    assert rows[0].quantity == 3 and rows[0].unit == "%" and rows[0].absolute is False


def test_negative_quantity_and_empty_book():
    def negative(book: Workbook) -> None:
        ws = book.active
        ws.append(["Компонент", "кг"])
        ws.append(["Сенча", -1])

    with pytest.raises(ValueError, match="отрицательное"):
        parse_tech_cards(_xlsx(negative))

    def empty(book: Workbook) -> None:
        book.active.title = "Описание"
        book.active.append(["это не таблица"])

    with pytest.raises(ValueError, match="нет таблицы"):
        parse_tech_cards(_xlsx(empty))


def test_report_does_not_place_order():
    data = _xlsx(_matrix)
    text = report(data)
    assert "Заказ поставщику не создан" in text
    assert "Сенча" in text and "13" in text
    with pytest.raises(OrderNotPlaced):
        report(data, place_order=True)


def test_cli_lists_positions_without_token(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MOYSKLAD_TOKEN", raising=False)
    path = tmp_path / "Техкарты_ЧАЙ.xlsx"
    path.write_bytes(_xlsx(_long))
    assert cli.main(["tea-buy", str(path), "--card", "ароматизация"]) == 0
    out = capsys.readouterr().out
    assert "Ароматизатор жасмин" in out
    assert "Сенча" not in out
    assert "только список" in out

    assert cli.main(["tea-buy", str(path), "--place-order"]) == 2
    assert "не создан" in capsys.readouterr().out

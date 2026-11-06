"""Шаблоны печати для загрузки в МойСклад (print_templates/*.xls)."""
from pathlib import Path

import pytest

TEMPLATES = sorted((Path(__file__).resolve().parent.parent / "print_templates").glob("*.xls"))


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_template_has_country_record(path):
    """Без записи COUNTRY POI в МойСклад падает на цикле jXLS («CountryRecord not found»),
    и печать отвечает «Некорректный шаблон печатной формы» — так было с shipment_a4.xls."""
    assert b"\x8c\x00\x04\x00" in path.read_bytes(), "xlwt: wb.country_code = 7"


def test_templates_found():
    assert {p.name for p in TEMPLATES} >= {"courier_label_120x75.xls", "shipment_a4.xls", "contract_supply.xls"}

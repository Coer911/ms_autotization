"""route-sheet: заказы месяца по статусу → XLSX, только чтение МойСклад."""
from datetime import date
from io import BytesIO

import pytest
from conftest import settings_for
from openpyxl import load_workbook

from hub import __main__ as cli
from hub.automations.route_xlsx import MONTH_HEADERS, build_month_xlsx
from hub.moysklad import MoySklad
from hub.route_report import collect, month_filter, parse_month

TARGET = "Подтверждён"


def _order(fake, number, state, moment, address="СПб, Невский 1", phone=""):
    agent = fake.add_counterparty(name=f"Клиент {number}")
    agent["phone"] = phone
    d = fake.add_order(number, state, agent=agent)
    d["moment"] = moment
    d["shipmentAddress"] = address
    d["deliveryPlannedMoment"] = moment.replace("-01 ", "-02 ") if "-01 " in moment else ""
    return d


@pytest.fixture
async def ms(fake):
    client = MoySklad("test-token", transport=fake.transport())
    yield client
    await client.close()


def test_parse_month():
    assert parse_month("2026-11") == date(2026, 11, 1)
    assert parse_month("11.2026") == date(2026, 11, 1)
    with pytest.raises(ValueError):
        parse_month("ноябрь")
    assert month_filter(date(2026, 11, 1)) == \
        "moment>=2026-11-01 00:00:00;moment<=2026-11-30 23:59:59"


async def test_collect_only_month_and_state(fake, ms):
    _order(fake, "0002", TARGET, "2026-11-20 09:00:00.000", phone="+7 900")
    _order(fake, "0001", TARGET, "2026-11-01 10:00:00.000")
    _order(fake, "0003", "Новый", "2026-11-05 10:00:00.000")
    _order(fake, "0004", TARGET, "2026-10-31 23:00:00.000")
    _order(fake, "0005", TARGET, "2026-12-01 00:00:01.000")
    rows = await collect(ms, date(2026, 11, 1), "подтверждён")
    assert [r["name"] for r in rows] == ["0001", "0002"]
    assert rows[0]["delivery"] == "02.11.2026"
    assert rows[1]["phone"] == "+7 900" and rows[1]["state"] == TARGET
    assert all(method == "GET" for method, _ in fake.calls)


async def test_collect_all_states_drops_cancelled(fake, ms):
    _order(fake, "0001", TARGET, "2026-11-03 10:00:00.000")
    _order(fake, "0002", "Отменён", "2026-11-04 10:00:00.000")
    _order(fake, "0003", "Собран", "2026-11-05 10:00:00.000")
    rows = await collect(ms, date(2026, 11, 1), "all")
    assert [r["name"] for r in rows] == ["0001", "0003"]
    rows = await collect(ms, date(2026, 11, 1), "all", with_cancelled=True)
    assert len(rows) == 3


def test_month_xlsx_has_status_and_total():
    orders = [{"id": "a", "name": "0001", "date": "01.11.2026", "delivery": "", "counterparty": "К",
               "address": "СПб", "phone": "", "hours": "10-18", "sum": 1500.5,
               "state": TARGET, "comment": ""}]
    wb = load_workbook(BytesIO(build_month_xlsx("ноябрь 2026 · Подтверждён", orders)))
    sheet = wb["Маршрут"]
    assert tuple(c.value for c in sheet[1]) == MONTH_HEADERS
    assert sheet.cell(2, 10).value == TARGET
    assert sheet.cell(3, 9).value == "=SUM(I2:I2)"
    assert wb["_hub"].sheet_state == "hidden"


async def test_cli_writes_file(fake, monkeypatch, tmp_path, capsys):
    _order(fake, "0001", TARGET, "2026-11-03 10:00:00.000")
    _order(fake, "0002", TARGET, "2026-11-04 10:00:00.000", address="")
    monkeypatch.setattr(cli, "client", lambda s: MoySklad("test-token", transport=fake.transport()))
    out = tmp_path / "лист.xlsx"
    code = await cli.cmd_route_sheet(settings_for({}), "2026-11", TARGET, str(out), False)
    text = capsys.readouterr().out
    assert code == 0, text
    assert "заказов 2" in text and "без адреса доставки: 1" in text
    assert load_workbook(out)["Маршрут"].max_row == 4


async def test_cli_unknown_state_fails(fake, monkeypatch, tmp_path):
    from hub.moysklad import FieldError
    monkeypatch.setattr(cli, "client", lambda s: MoySklad("test-token", transport=fake.transport()))
    with pytest.raises(FieldError):
        await cli.cmd_route_sheet(settings_for({}), "2026-11", "Нет такого", str(tmp_path / "x"), False)

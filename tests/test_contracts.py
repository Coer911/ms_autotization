"""Номера договоров: «ПК» → «ПК-ДД/ММ/ГГ-NN» (python -m hub contracts number)."""
import pytest

from hub import contracts as c
from hub.__main__ import cmd_contracts
from hub.moysklad import MoySklad


@pytest.fixture
async def ms(fake):
    async with MoySklad("test-token", transport=fake.transport()) as client:
        yield client


def test_bare_series():
    assert c.bare_series("ПК") == "ПК"
    assert c.bare_series(" пк- ") == "ПК"
    assert c.bare_series("АОПК") == "АОПК"
    assert c.bare_series("АО") == "АО"
    for full in ("ПК-09/04/26-01", "ПК-14/01/26", "ПК 1", "", "Договор"):
        assert c.bare_series(full) is None


def test_next_name_counts_existing_numbers_of_that_date_and_series():
    names = ["ПК-09/04/26-01", "ПК-09/04/26-02", "ПТ-09/04/26-05", "ПК-10/04/26-07", "пк-09/04/26-04"]
    assert c.next_name(names, "ПК", "09/04/26") == "ПК-09/04/26-05"
    assert c.next_name(names, "ПТ", "09/04/26") == "ПТ-09/04/26-06"
    assert c.next_name(names, "ПК", "11/04/26") == "ПК-11/04/26-01"
    # старый номер без «-NN» занимает 01
    assert c.next_name(["ПК-14/01/26"], "ПК", "14/01/26") == "ПК-14/01/26-02"
    # «АО» не путается с «АОПК»
    assert c.next_name(["АОПК-09/04/26-01"], "АО", "09/04/26") == "АО-09/04/26-01"


def test_plan_numbers_several_bare_contracts_of_one_day_in_order():
    rows = [
        {"id": "b", "name": "ПК", "moment": "2026-11-06 00:00:00.000", "agent": {"name": "ООО Б"}},
        {"id": "a", "name": "пк", "moment": "2026-11-06 00:00:00.000", "agent": {"name": "ООО А"}},
        {"id": "x", "name": "ПК-06/11/26-01", "moment": "2026-11-06 00:00:00.000"},
        {"id": "y", "name": "ПК-05/11/26-01", "moment": "2026-11-05 00:00:00.000"},
        {"id": "z", "name": "ДАО", "moment": "2026-11-05 00:00:00.000"},
    ]
    got = [(r.id, r.new) for r in c.plan(rows, since="2026-11-01")]
    assert got == [("z", "ДАО-05/11/26-01"), ("a", "ПК-06/11/26-02"), ("b", "ПК-06/11/26-03")]
    assert len({new for _, new in got} | {"ПК-06/11/26-01", "ПК-05/11/26-01"}) == 5


async def test_apply_renames_only_bare_contracts(fake, ms):
    old = fake.add_contract("ПК-06/11/26-01")
    new = fake.add_contract("ПК")
    other = fake.add_contract("ZR26-78460")
    todo = c.plan(await c.load(ms), since="2026-11-01")
    assert [(r.id, r.new) for r in todo] == [(new["id"], "ПК-06/11/26-02")]
    await c.apply(ms, todo)
    assert fake.db["contract"][new["id"]]["name"] == "ПК-06/11/26-02"
    assert fake.db["contract"][old["id"]]["name"] == "ПК-06/11/26-01"
    assert fake.db["contract"][other["id"]]["name"] == "ZR26-78460"
    assert not fake.violations


async def test_cli_without_apply_writes_nothing(fake, monkeypatch, capsys):
    from hub import __main__ as cli
    from hub.settings import Settings
    bare = fake.add_contract("ПК", moment="2026-04-09 00:00:00.000")
    monkeypatch.setattr(cli, "client", lambda s: MoySklad("test-token", transport=fake.transport()))
    s = Settings(ms_token="test-token")
    assert await cmd_contracts(s, apply=False, since="2026-04-01") == 0
    assert "→ ПК-09/04/26-01" in capsys.readouterr().out
    assert fake.db["contract"][bare["id"]]["name"] == "ПК"
    assert not [call for call in fake.calls if call[0] == "PUT"]
    assert await cmd_contracts(s, apply=True, since="2026-04-01") == 0
    assert fake.db["contract"][bare["id"]]["name"] == "ПК-09/04/26-01"


def test_old_bare_contracts_are_left_alone():
    rows = [{"id": "old", "name": "ПТ", "moment": "2023-07-12 00:00:00.000"},
            {"id": "new", "name": "ПТ", "moment": "2026-11-06 00:00:00.000"}]
    assert [r.id for r in c.plan(rows, since="2026-10-07")] == ["new"]
    assert [x["id"] for x in c.stale(rows, "2026-10-07")] == ["old"]


async def test_cli_skips_old_contracts_by_default(fake, monkeypatch, capsys):
    from hub import __main__ as cli
    from hub.settings import Settings
    old = fake.add_contract("ПТ", moment="2023-07-12 00:00:00.000")
    monkeypatch.setattr(cli, "client", lambda s: MoySklad("test-token", transport=fake.transport()))
    assert await cmd_contracts(Settings(ms_token="test-token"), apply=True) == 0
    out = capsys.readouterr().out
    assert "не трогаю" in out and "договоров с номером из одной серии" in out
    assert fake.db["contract"][old["id"]]["name"] == "ПТ"

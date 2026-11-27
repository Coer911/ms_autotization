"""buy-list: две таблицы на 15 дней, без заказа поставщику."""
from datetime import date

import pytest

from hub import __main__ as cli
from hub.buy_list import (
    BuyListError,
    assemble,
    collect,
    is_cordial_500,
    is_syrup_700,
    order_qty,
    pick_store,
    render,
    syrup_hrefs,
)

TODAY = date(2026, 11, 27)
RAW = "https://example.test/entity/store/raw"
FG = "https://example.test/entity/store/fg"
FOLDER = "https://example.test/entity/productfolder/syrup700"
PATH = "1 CCR/2 СИРОПЫ > HERBARISTA/HERBARISTA 700 ML"


def _meta(href: str, type_: str = "product") -> dict:
    return {"meta": {"href": href, "type": type_}}


def _stock(href: str, name: str, stock: float, unit: str = "кг", folder: str = "") -> dict:
    row = {**_meta(href), "name": name, "stock": stock, "uom": {"name": unit}}
    if folder:
        row["folder"] = {"name": folder}
    return row


def _turn(href: str, name: str, qty: float, unit: str = "кг") -> dict:
    return {
        "assortment": {**_meta(href), "name": name, "uom": {"name": unit}},
        "outcome": {"quantity": qty},
    }


def test_order_formula_and_bottle_rounding():
    assert order_qty(31, 10, whole=False) == 5
    assert order_qty(31, 20, whole=False) == 0
    assert order_qty(0, -1.2, whole=False) == 1.2
    assert order_qty(0, -0.0001, whole=False) == 0
    assert order_qty(31, 14.2, whole=True) == 1
    assert order_qty(62, 20, whole=True) == 10
    assert order_qty(0, -1.2, whole=True) == 2
    assert order_qty(31, 15, whole=True) == 0


def test_cordial_500_is_not_a_700_syrup():
    assert is_cordial_500("Кордиал вишня 500 мл")
    assert is_cordial_500("HERBARISTA Cordial 500 ml")
    assert not is_cordial_500("Сироп Вишня HERBARISTA, 700 мл")
    assert is_syrup_700(PATH)
    assert is_syrup_700(folder_name="HERBARISTA 700 ML")
    assert not is_syrup_700("…/HERBARISTA CORDIALS 700 МЛ")
    assert not is_syrup_700(folder_name="HERBARISTA 250 ML")


def test_assemble_subtracts_moves_and_sorts_by_cover():
    stock = [
        _stock("p/a", "Валериана", 0, "кг"),
        _stock("p/b", "Мята", 10, "кг"),
        _stock("p/c", "Сенча", 40, "кг"),
        _stock("p/z", "Перец", -0.025, "кг"),
    ]
    turnover = [
        _turn("p/a", "Валериана", 31),
        _turn("p/b", "Мята", 31),
        _turn("p/c", "Сенча", 31),
        _turn("p/d", "Кордиал вишня 500 мл", 31, "шт"),
    ]
    moves = [_turn("p/b", "Мята", 10)]
    lines, covered = assemble(
        stock, turnover, moves, whole=False,
        allow=lambda _href, name, _folder: not is_cordial_500(name),
    )
    assert [(line.name, line.monthly, line.order) for line in lines] == [
        ("Валериана", 31, 15),
        ("Перец", 0, 0.025),
        ("Мята", 21, 0.161),
    ]
    assert covered == 1
    assert all("Кордиал" not in line.name for line in lines)


def test_syrup_hrefs_drop_cordial_and_keep_variant_from_stock():
    products = [
        {**_meta("p/vanilla"), "name": "Сироп Ваниль HERBARISTA, 700 мл", "pathName": PATH},
        {**_meta("p/cordial"), "name": "Кордиал манго 500 мл", "pathName": PATH},
        {**_meta("p/other"), "name": "Сироп 250", "pathName": "…/HERBARISTA 250 ML"},
    ]
    stock = [_stock("v/bubble", "Сироп Бабл гам HERBARISTA, 700 мл", 0, "шт", "HERBARISTA 700 ML")]
    hrefs = syrup_hrefs(products, stock)
    assert hrefs == {"p/vanilla", "v/bubble"}


def test_render_puts_raw_store_before_syrups():
    from hub.buy_list import Block, Line

    start = date(2026, 10, 28)
    raw = Block("Склад сырья", "К 49 (СКЛАД ТОЛЬКО СЫРЬЯ!)", start, TODAY,
                (Line("Валериана", 31, 0, 15, "кг"),), 2, False, "")
    syrup = Block(
        "Сиропы HERBARISTA 700 мл", "К 49 (СКЛАД ГОТОВАЯ ПРОДУКЦИЯ)", start, TODAY,
        (Line("Сироп Ваниль HERBARISTA, 700 мл", 25.1, 3, 10, "шт"),), 4, True,
        "Кордиалы 500 мл не входят. Заказ округлён вверх до целой бутылки.",
    )
    text = render(raw, syrup)
    assert text.index("Склад сырья") < text.index("Сиропы HERBARISTA 700 мл")
    assert text.count("| Название | Расход за месяц | Остаток | Заказать |") == 2
    assert "| Валериана | 31 кг | 0 кг | 15 кг |" in text
    assert "| Сироп Ваниль HERBARISTA, 700 мл | 25,1 шт | 3 шт | 10 шт |" in text
    assert "Кордиалы 500 мл не входят" in text
    assert "Заказ поставщику не создаётся" in text
    assert "28.10.2026–27.11.2026" in text


def test_pick_store_requires_one_match():
    stores = [{"name": "К 49 (СКЛАД ТОЛЬКО СЫРЬЯ!)"}, {"name": "Другой"}]
    assert pick_store(stores, "ТОЛЬКО СЫРЬЯ")["name"].startswith("К 49")
    with pytest.raises(BuyListError):
        pick_store(stores, "нет такого")


class _FakeMS:
    def __init__(self):
        self.seen: list[tuple[str, dict]] = []
        self.writes: list[str] = []

    async def iterate(self, path, limit=1000, **params):
        self.seen.append((path, params))
        filt = params.get("filter") or ""
        for row in self._page(path, filt, params.get("groupBy")):
            yield row

    async def post(self, *args, **kwargs):
        self.writes.append("post")
        raise AssertionError("buy-list не пишет в МойСклад")

    def _page(self, path: str, filt: str, group_by: str | None) -> list[dict]:
        if path == "entity/store":
            return [
                {"name": "К 49 (СКЛАД ТОЛЬКО СЫРЬЯ!)", "meta": {"href": RAW}},
                {"name": "К 49 (СКЛАД ГОТОВАЯ ПРОДУКЦИЯ)", "meta": {"href": FG}},
                {"name": "БОЛЬШЕ КОФЕ (ОБЖАРКА)", "meta": {"href": "https://example.test/entity/store/roast"}},
            ]
        if path == "entity/productfolder":
            return [
                {"name": "HERBARISTA 700 ML", "meta": {"href": FOLDER}},
                {"name": "HERBARISTA CORDIALS 700 МЛ", "meta": {"href": "https://example.test/folder/cordial"}},
            ]
        if path == "entity/product":
            return [
                {**_meta("p/vanilla"), "name": "Сироп Ваниль HERBARISTA, 700 мл", "pathName": PATH},
                {**_meta("p/cordial"), "name": "Кордиал вишня 500 мл", "pathName": PATH},
            ]
        if path == "report/stock/all" and RAW in filt:
            return [_stock("p/mint", "Мята", 1, "кг")]
        if path == "report/stock/all" and FOLDER in filt:
            return [_stock("p/vanilla", "Сироп Ваниль HERBARISTA, 700 мл", 3, "шт", "HERBARISTA 700 ML")]
        if path == "report/turnover/all" and "type=move" in filt:
            assert group_by == "variant"
            return []
        if path == "report/turnover/all" and RAW in filt:
            assert group_by == "variant"
            assert params_moment(self)
            return [_turn("p/mint", "Мята", 62)]
        if path == "report/turnover/all" and FG in filt:
            assert group_by == "variant"
            return [
                _turn("p/vanilla", "Сироп Ваниль HERBARISTA, 700 мл", 31, "шт"),
                _turn("p/cordial", "Кордиал вишня 500 мл", 31, "шт"),
                _turn("p/drip", "Дрип", 100, "шт"),
            ]
        raise AssertionError(path + " " + filt)


def params_moment(fake: _FakeMS) -> bool:
    moment = [params for path, params in fake.seen if path == "report/turnover/all"][-1]
    return moment["momentFrom"] == "2026-10-28 00:00:00" and moment["momentTo"] == "2026-11-27 23:59:59"


async def test_collect_reads_two_stores_and_does_not_write(monkeypatch):
    ms = _FakeMS()
    raw, syrup = await collect(ms, TODAY)
    assert ms.writes == []
    assert raw.store.startswith("К 49") and "СЫРЬЯ" in raw.store
    assert raw.lines[0].name == "Мята"
    assert raw.lines[0].order == order_qty(62, 1, whole=False)
    assert [line.name for line in syrup.lines] == ["Сироп Ваниль HERBARISTA, 700 мл"]
    assert syrup.lines[0].order == 12  # 15 − 3, уже целая бутылка
    assert syrup.whole is True
    paths = [path for path, _params in ms.seen]
    assert "entity/purchaseorder" not in paths
    assert paths.count("report/turnover/all") == 4


def test_cli_prints_table_without_calling_moysklad(monkeypatch, capsys):
    monkeypatch.setenv("MOYSKLAD_TOKEN", "test-token")
    async def fake_collect(_ms, today=None):
        from hub.buy_list import Block, Line
        start = date(2026, 10, 28)
        raw = Block("Склад сырья", "К 49 (СКЛАД ТОЛЬКО СЫРЬЯ!)", start, TODAY,
                    (Line("Мята", 10, 1, 3.839, "кг"),), 0, False, "")
        syrup = Block("Сиропы HERBARISTA 700 мл", "К 49 (СКЛАД ГОТОВАЯ ПРОДУКЦИЯ)", start, TODAY,
                      (), 1, True, "Кордиалы 500 мл не входят. Заказ округлён вверх до целой бутылки.")
        return raw, syrup

    monkeypatch.setattr("hub.buy_list.collect", fake_collect)
    monkeypatch.setattr(cli, "client", lambda _s: _NoClose())
    assert cli.main(["buy-list"]) == 0
    out = capsys.readouterr().out
    assert out.index("Склад сырья") < out.index("Сиропы HERBARISTA 700 мл")
    assert "| Название | Расход за месяц | Остаток | Заказать |" in out
    assert "Мята" in out


class _NoClose:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


def test_cli_reports_missing_store(monkeypatch, capsys):
    monkeypatch.setenv("MOYSKLAD_TOKEN", "test-token")
    async def broken(_ms, today=None):
        raise BuyListError("склад «ТОЛЬКО СЫРЬЯ» найден 0 раз")

    monkeypatch.setattr("hub.buy_list.collect", broken)
    monkeypatch.setattr(cli, "client", lambda _s: _NoClose())
    assert cli.main(["buy-list"]) == 1
    assert "ТОЛЬКО СЫРЬЯ" in capsys.readouterr().out

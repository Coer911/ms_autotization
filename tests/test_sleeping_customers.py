import asyncio
import importlib.util
from pathlib import Path

from hub.moysklad.client import MoySklad
from tests.fake_moysklad import FakeMoySklad

spec = importlib.util.spec_from_file_location(
    "sleeping", Path(__file__).parent.parent / "scripts" / "sleeping_customers.py")
sleeping = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sleeping)


def test_sleeping_customers_by_month():
    fake = FakeMoySklad()
    big = fake.add_counterparty("Большой")
    small = fake.add_counterparty("Малый")
    loyal = fake.add_counterparty("Постоянный")
    gone = fake.add_counterparty("Отменивший")

    def order(n, agent, moment, sum_kop, state="Новый"):
        fake.add_order(n, state=state, agent=agent, sum_kop=sum_kop)["moment"] = moment

    order("1", big, "2026-08-10 10:00:00.000", 900_000)
    order("2", big, "2026-11-03 10:00:00.000", 100_000)    # был в ноябре, не был в сентябре
    order("3", small, "2026-10-01 10:00:00.000", 50_000)   # был в октябре, не был в ноябре
    order("4", loyal, "2026-09-05 10:00:00.000", 10_000)
    order("5", loyal, "2026-11-05 10:00:00.000", 10_000)
    order("6", gone, "2026-10-02 10:00:00.000", 999_999, state="Отменён")
    order("7", small, "2027-01-01 10:00:00.000", 1)         # вне выборки

    async def run():
        async with MoySklad("test-token", transport=fake.transport()) as ms:
            return await sleeping.load_orders(ms, sleeping.date(2026, 3, 1), sleeping.date(2026, 12, 1))

    orders = asyncio.run(run())
    assert len(orders) == 6
    nov = sleeping.analyse(orders, "2026-11")
    sep = sleeping.analyse(orders, "2026-09")
    assert [r["name"] for r in nov] == ["Малый"]
    assert [r["name"] for r in sep] == ["Большой"]
    assert sep[0]["sum"] == 900_000 and nov[0]["sum"] == 50_000

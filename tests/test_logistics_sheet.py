"""Маршрутный лист: переход статуса, дата, идемпотентность. Google подменён памятью."""
import asyncio
import json
from datetime import date
from zoneinfo import ZoneInfo

import pytest
from conftest import webhook

from hub.automations import ConfigError, build, event_types
from hub.automations.logistics_sheet import (
    delivery_address,
    parse_transition,
    route_date,
)
from hub.channels.route_sheet import file_title
from hub.entities import Order, from_api
from hub.events import Event
from hub.storage import runs

MSK = ZoneInfo("Europe/Moscow")
TARGET = "ПОДТВЕРЖДЕН / ОЖИДАЕТ СБОРКИ"
# Четверг, не «сейчас»: сегодня в этих тестах суббота, и «следующий рабочий от now()»
# тоже понедельник. Пятница 13.11 от четверга 12.11 таким способом не получается.
TRANSITION = "2026-11-12 18:00:00.000"
ROUTE = date(2026, 11, 13)
ROUTE_TEXT = "13.11.2026"


def _target(fake):
    states = fake.states["customerorder"]
    found = next((s for s in states if s["name"] == TARGET), None)
    if found:
        return found
    from fake_moysklad import meta
    found = {**meta("state", f"st-customerorder-{len(states)}", "customerorder/metadata/states"),
             "id": f"st-customerorder-{len(states)}", "name": TARGET,
             "stateType": "Regular", "color": 1}
    states.append(found)
    return found


def _params(**extra):
    base = {"uses": "logistics_sheet", "folder_id": "folder-1", "template_file_id": "template-1",
            "hours_field": "Часы приемки"}
    base.update(extra)
    return {"routes": base}


def _prepare(fake, number="1001", state="Новый"):
    _target(fake)
    order = fake.add_order(number, state=state)
    order["shipmentAddress"] = "Тестовая улица, 1"
    order["description"] = "позвонить утром"
    order["agent"]["name"] = "ООО «Ромашка»"
    order["agent"]["phone"] = "+7 900 111-22-33"
    order["agent"]["legalAddress"] = "Юридический адрес, не писать"
    order["agent"]["actualAddress"] = "Фактический адрес, не писать"
    order["attributes"].append({"name": "Часы приемки", "value": "10-14"})
    return order


async def _see(hub, fake, order):
    await webhook(hub, fake, "customerorder", order["id"], f"see-{order['id']}")
    await hub.tick()


async def _move(hub, fake, order, state=TARGET, updated=TRANSITION, rid="go", fields=None):
    fake.set_state("customerorder", order["id"], state)
    if updated:
        fake.db["customerorder"][order["id"]]["updated"] = updated
    await webhook(hub, fake, "customerorder", order["id"], rid, fields=fields)
    return await hub.tick()


def _rows(hub):
    return [row for item in hub.channels.book.files.values() for row in item["rows"].values()]


def _only(hub):
    assert len(hub.channels.book.files) == 1
    return next(iter(hub.channels.book.files.values()))


# ── календарь ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("moment", "expect"), [
    ("2026-11-16 09:00:00.000", date(2026, 11, 17)),  # пн → вт
    ("2026-11-17 09:00:00.000", date(2026, 11, 18)),  # вт → ср
    ("2026-11-18 09:00:00.000", date(2026, 11, 19)),  # ср → чт
    ("2026-11-19 09:00:00.000", date(2026, 11, 20)),  # чт → пт
    ("2026-11-13 23:59:00.000", date(2026, 11, 16)),  # пт → пн
    ("2026-11-14 12:00:00.000", date(2026, 11, 16)),  # сб → пн
    ("2026-11-15 12:00:00.000", date(2026, 11, 16)),  # вс → пн
    ("2026-01-31 18:00:00.000", date(2026, 2, 2)),    # конец января, суббота → пн
    ("2026-03-31 18:00:00.000", date(2026, 4, 1)),    # вторник 31.03 → среда 01.04
    ("2026-04-30 18:00:00.000", date(2026, 5, 1)),    # конец апреля, четверг → пятница
    ("2026-02-28 18:00:00.000", date(2026, 3, 2)),    # конец февраля, суббота → пн
    ("2026-05-31 18:00:00.000", date(2026, 6, 1)),    # конец мая, воскресенье → пн
    ("2026-08-31 18:00:00.000", date(2026, 9, 1)),    # конец августа, понедельник → вт
    ("2026-12-31 18:00:00.000", date(2027, 1, 1)),    # конец года, четверг → пятница
    ("2027-12-31 18:00:00.000", date(2028, 1, 3)),    # конец года, пятница → пн
])
def test_route_date_week_month_year(moment, expect):
    assert route_date(parse_transition(moment)) == expect


def test_route_date_respects_timezone_of_timestamp():
    """Наивная строка МойСклад — уже Москва. Тот же миг с Z — другие сутки в Москве."""
    naive = route_date(parse_transition("2026-11-15 21:30:00.000"))
    as_utc = route_date(parse_transition("2026-11-15T21:30:00Z"))
    as_offset = route_date(parse_transition("2026-11-15T21:30:00+00:00"))
    as_moscow = route_date(parse_transition("2026-11-16T00:30:00+03:00"))
    assert naive == date(2026, 11, 16)          # воскресенье → понедельник
    assert as_utc == date(2026, 11, 17)         # уже понедельник по Москве → вторник
    assert as_offset == as_utc == as_moscow
    assert parse_transition("2026-11-15 21:30:00").tzinfo == MSK


def test_delivery_address_ignores_legal_and_actual():
    empty = Order(id="1", shipment_address="", raw={
        "shipmentAddress": "",
        "agent": {"legalAddress": "Юридический адрес", "actualAddress": "Фактический адрес"},
    })
    assert delivery_address(empty) == ""
    structured = Order(id="2", shipment_address="", raw={
        "shipmentAddress": "",
        "shipmentAddressFull": {"street": "Тестовая", "house": "2"},
        "agent": {"legalAddress": "Юридический адрес"},
    })
    assert delivery_address(structured) == "Тестовая, 2"
    assert "Юридический" not in delivery_address(structured)


# ── включение ────────────────────────────────────────────────────────────────

def test_disabled_does_not_listen_for_orders():
    autos = build({"logistics_sheet": {"enabled": False, "folder_id": "", "template_file_id": ""},
                   "daily_upd": {"enabled": True}})
    assert [a.kind for a in autos] == ["daily_upd"]
    assert "customerorder" not in event_types(autos)


def test_unknown_param_and_missing_ids():
    with pytest.raises(ConfigError):
        build({"routes": {**_params()["routes"], "nope": 1}})
    with pytest.raises(ConfigError):
        build({"routes": {"uses": "logistics_sheet", "template_file_id": "t"}})
    with pytest.raises(ConfigError):
        build({"routes": {"uses": "logistics_sheet", "folder_id": "f", "template_file_id": ""}})


# ── переходы ─────────────────────────────────────────────────────────────────

async def test_other_status_does_not_trigger(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await _see(hub, fake, order)
    out = await _move(hub, fake, order, state="Собран", rid="other")
    assert out == []
    assert hub.channels.book.files == {}


async def test_already_in_target_is_not_a_transition(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake, state=TARGET)
    order["updated"] = TRANSITION
    await webhook(hub, fake, "customerorder", order["id"], "first", fields=["state"])
    assert await hub.tick() == []
    assert hub.channels.book.files == {}


async def test_cancelled_order_is_not_added(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await _see(hub, fake, order)
    out = await _move(hub, fake, order, state="Отменён", rid="cancel")
    assert out == []
    assert hub.channels.book.files == {}

    other = _prepare(fake, number="1002")
    await _see(hub, fake, other)
    fake.db["customerorder"][other["id"]]["state"] = {
        "name": TARGET, "stateType": "Unsuccessful",
        "meta": _target(fake)["meta"],
    }
    fake.db["customerorder"][other["id"]]["updated"] = TRANSITION
    await webhook(hub, fake, "customerorder", other["id"], "bad-type")
    assert await hub.tick() == []
    assert hub.channels.book.files == {}


async def test_transition_adds_one_row_on_monday_file(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await _see(hub, fake, order)
    out = await _move(hub, fake, order)
    assert out[0].status == "done", out
    assert "CREATE_ROUTE" in out[0].detail and "ADD_ORDER" in out[0].detail
    item = _only(hub)
    assert item["title"] == file_title(ROUTE_TEXT)
    assert item["date_cell"] == f"дата доставки: {ROUTE_TEXT}"
    assert item["executor"] == ""
    assert item["sheets"] == [ROUTE_TEXT]
    assert "Адреса" not in item["sheets"]
    row = item["rows"][order["id"]]
    assert row["counterparty"] == "ООО «Ромашка»"
    assert row["address"] == "Тестовая улица, 1"
    assert row["phone"] == "+7 900 111-22-33"
    assert row["hours"] == "10-14"
    assert row["comment"] == "позвонить утром"
    assert row["group"] == "" and row["done"] == ""
    assert row["order_id"] == order["id"]
    assert "Юридический" not in row["address"] and "Фактический" not in row["address"]
    stored = json.loads(await hub.storage.get(f"ls:order:{order['id']}"))
    assert stored["transition_at"] == TRANSITION
    assert stored["route_date"] == ROUTE.isoformat()
    assert not any(c[1].endswith("contactpersons") for c in fake.calls)


async def test_state_id_wins_over_name(fake, make_hub):
    state = _target(fake)
    hub = await make_hub(_params(state_id=state["id"]))
    order = _prepare(fake)
    await _see(hub, fake, order)
    order["state"] = {"name": "Другое имя", "meta": state["meta"]}
    order["updated"] = TRANSITION
    await webhook(hub, fake, "customerorder", order["id"], "by-id")
    out = await hub.tick()
    assert out[0].status == "done"
    assert order["id"] in _only(hub)["rows"]

    hub2 = await make_hub(_params(state_id=state["id"]))
    missed = _prepare(fake, number="1002")
    await _see(hub2, fake, missed)
    missed["state"] = {
        "name": TARGET,
        "meta": {"href": "https://api.moysklad.ru/api/remap/1.2/entity/customerorder/metadata/states/other",
                 "type": "state"},
    }
    missed["updated"] = TRANSITION
    await webhook(hub2, fake, "customerorder", missed["id"], "by-name-only")
    assert await hub2.tick() == []
    assert hub2.channels.book.files == {}


async def test_name_is_fallback_without_state_id(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await _see(hub, fake, order)
    out = await _move(hub, fake, order, rid="name")
    assert out[0].status == "done"


async def test_repeat_webhook_and_transition_do_not_duplicate(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await _see(hub, fake, order)
    assert (await webhook(hub, fake, "customerorder", order["id"], "same")) == 1
    assert (await webhook(hub, fake, "customerorder", order["id"], "same")) == 0
    fake.set_state("customerorder", order["id"], TARGET)
    order["updated"] = TRANSITION
    await webhook(hub, fake, "customerorder", order["id"], "again")
    assert (await hub.tick())[0].status == "done"
    await webhook(hub, fake, "customerorder", order["id"], "third")
    assert await hub.tick() == []
    await _move(hub, fake, order, state="Собран", updated="2026-12-01 09:00:00.000", rid="away")
    back = await _move(hub, fake, order, updated="2027-01-15 09:00:00.000", rid="back")
    assert back[0].status == "already"
    item = _only(hub)
    assert list(item["rows"]) == [order["id"]]
    stored = json.loads(await hub.storage.get(f"ls:order:{order['id']}"))
    assert stored["route_date"] == ROUTE.isoformat()
    assert stored["transition_at"] == TRANSITION


async def test_delayed_retry_keeps_route_date(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await _see(hub, fake, order)
    hub.channels.fail["google"] = 1
    out = await _move(hub, fake, order)
    assert out[0].status == "failed"
    assert hub.channels.book.creates == 0
    order["updated"] = "2027-02-01 12:00:00.000"
    async with hub.storage._tx() as conn:
        await conn.execute(runs.update().values(next_at="2000-01-01T00:00:00Z"))
    retry = await hub.engine.retry_due()
    assert retry[0].status == "done", retry
    item = _only(hub)
    assert item["title"] == file_title(ROUTE_TEXT)
    assert hub.channels.book.creates == 1
    assert list(item["rows"]) == [order["id"]]
    stored = json.loads(await hub.storage.get(f"ls:order:{order['id']}"))
    assert stored["transition_at"] == TRANSITION


async def test_missing_address_is_reported_and_not_invented(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    order["shipmentAddress"] = ""
    await _see(hub, fake, order)
    out = await _move(hub, fake, order)
    assert out[0].status == "failed"
    assert "отчёт" in out[0].detail and "нет адреса" in out[0].detail
    assert "Юридический" not in out[0].detail and "Фактический" not in out[0].detail
    assert hub.channels.book.files == {}
    order["shipmentAddress"] = "Тестовая улица, 1"
    order["updated"] = "2027-03-01 10:00:00.000"
    async with hub.storage._tx() as conn:
        await conn.execute(runs.update().values(next_at="2000-01-01T00:00:00Z"))
    retry = await hub.engine.retry_due()
    assert retry[0].status == "done", retry
    assert _only(hub)["rows"][order["id"]]["address"] == "Тестовая улица, 1"
    stored = json.loads(await hub.storage.get(f"ls:order:{order['id']}"))
    assert stored["route_date"] == ROUTE.isoformat()


async def test_existing_file_is_not_created_again(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    hub.channels.book.seed(file_title(ROUTE_TEXT), ROUTE_TEXT)
    await _see(hub, fake, order)
    out = await _move(hub, fake, order)
    assert out[0].status == "done"
    assert "CREATE_ROUTE" not in out[0].detail and "ADD_ORDER" in out[0].detail
    assert hub.channels.book.creates == 0
    assert len(hub.channels.book.files) == 1
    assert order["id"] in _only(hub)["rows"]


async def test_duplicate_titles_do_not_spawn_a_third_file(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    title = file_title(ROUTE_TEXT)
    hub.channels.book.seed(title, ROUTE_TEXT)
    hub.channels.book.seed(title, ROUTE_TEXT)
    await _see(hub, fake, order)
    out = await _move(hub, fake, order)
    assert out[0].status == "failed"
    assert hub.channels.book.creates == 0
    assert len(hub.channels.book.files) == 2


async def test_row_without_hub_record_is_restored(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    file_id = hub.channels.book.seed(file_title(ROUTE_TEXT), ROUTE_TEXT)
    hub.channels.book.insert(file_id, order["id"], {
        "counterparty": "Старое имя", "address": "Тестовая улица, 1",
        "comment": "заметка логиста", "hours": "вручную", "done": "да", "group": "3",
    })
    await _see(hub, fake, order)
    out = await _move(hub, fake, order)
    assert out[0].status == "done"
    assert "UPDATE_ORDER" in out[0].detail
    item = _only(hub)
    assert item["order"] == [order["id"]]
    row = item["rows"][order["id"]]
    assert row["counterparty"] == "ООО «Ромашка»"
    assert row["comment"] == "заметка логиста"
    assert row["hours"] == "вручную"
    assert row["done"] == "да" and row["group"] == "3"
    assert await hub.storage.get(f"ls:order:{order['id']}")


async def test_manual_cells_survive_resync(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await _see(hub, fake, order)
    assert (await _move(hub, fake, order))[0].status == "done"
    row = _only(hub)["rows"][order["id"]]
    row["group"] = "7"
    row["hours"] = "вручную"
    row["comment"] = "заметка логиста"
    row["done"] = "да"
    order["agent"]["name"] = "ООО «Новое»"
    order["shipmentAddress"] = "Тестовая улица, 9"
    order["description"] = "новый комментарий заказа"
    order["agent"]["phone"] = "+7 900 000-00-99"
    order["attributes"][0]["value"] = "8-9"
    fresh = from_api("customerorder", order)
    event = Event(entity="customerorder", entity_id=order["id"], action="UPDATE", source="webhook",
                  previous_state="Новый", is_new=False)
    out = await hub.engine.execute(hub.automations[0], fresh, event, mode="force")
    assert out.status == "done" and "UPDATE_ORDER" in out.detail
    again = _only(hub)["rows"][order["id"]]
    assert again["counterparty"] == "ООО «Новое»"
    assert again["address"] == "Тестовая улица, 1"
    assert again["group"] == "7" and again["done"] == "да"
    assert again["hours"] == "вручную" and again["comment"] == "заметка логиста"
    assert again["phone"] == "+7 900 111-22-33"
    write = _only(hub)["writes"][-1]
    assert write["new"] is False
    assert set(write["fields"]) <= {"order_id", "counterparty"}
    assert "address" not in write["fields"]
    stored = json.loads(await hub.storage.get(f"ls:order:{order['id']}"))
    assert stored["route_date"] == ROUTE.isoformat()
    notes = [item for item in hub.channels.sent if item["kind"] == "ADDRESS_DIFF"]
    assert notes and "Тестовая улица, 1" in notes[-1]["detail"]
    assert "Тестовая улица, 9" in notes[-1]["detail"]
    assert list(_only(hub)["rows"]) == [order["id"]]


async def test_empty_hours_can_be_filled_later_comment_cannot(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    order["attributes"].clear()
    await _see(hub, fake, order)
    await _move(hub, fake, order)
    row = _only(hub)["rows"][order["id"]]
    assert row["hours"] == ""
    order["attributes"].append({"name": "Часы приемки", "value": "11-12"})
    fresh = from_api("customerorder", order)
    event = Event(entity="customerorder", entity_id=order["id"], action="UPDATE", source="webhook")
    await hub.engine.execute(hub.automations[0], fresh, event, mode="force")
    assert _only(hub)["rows"][order["id"]]["hours"] == "11-12"
    assert "comment" not in _only(hub)["writes"][-1]["fields"]


async def test_phone_priority(fake, make_hub):
    hub = await make_hub(_params(phone_field="Телефон доставки"))
    order = _prepare(fake)
    order["attributes"].append({"name": "Телефон доставки", "value": "+7 900 222-33-44"})
    fake.contactpersons[order["agent"]["id"]] = [{"phone": "+7 900 555-66-77"}]
    await _see(hub, fake, order)
    await _move(hub, fake, order)
    assert _only(hub)["rows"][order["id"]]["phone"] == "+7 900 222-33-44"
    assert not any(path.endswith("contactpersons") for _, path in fake.calls)

    hub2 = await make_hub(_params())
    order2 = _prepare(fake, number="1002")
    order2["agent"]["phone"] = ""
    fake.contactpersons[order2["agent"]["id"]] = [
        {"phone": ""}, {"phone": "+7 900 555-66-77"}, {"phone": "+7 900 000-00-01"},
    ]
    await _see(hub2, fake, order2)
    await _move(hub2, fake, order2, rid="phone")
    assert _only(hub2)["rows"][order2["id"]]["phone"] == "+7 900 555-66-77"
    assert any(path.endswith("contactpersons") for _, path in fake.calls)


async def test_no_hours_field_leaves_column_empty(fake, make_hub):
    hub = await make_hub(_params(hours_field=""))
    order = _prepare(fake)
    await _see(hub, fake, order)
    await _move(hub, fake, order)
    row = _only(hub)["rows"][order["id"]]
    assert row["hours"] == ""
    assert "hours" not in _only(hub)["writes"][-1]["fields"]


async def test_dry_run_does_not_write_google(fake, make_hub):
    hub = await make_hub(_params(), dry_run=True)
    order = _prepare(fake)
    await _see(hub, fake, order)
    out = await _move(hub, fake, order)
    assert out[0].status == "done"
    assert out[0].detail == "WOULD_CREATE_ROUTE WOULD_ADD_ORDER"
    assert hub.channels.book.files == {} and hub.channels.book.creates == 0
    assert {item["kind"] for item in hub.channels.sent} >= {"WOULD_CREATE_ROUTE", "WOULD_ADD_ORDER"}
    assert all(item["dry_run"] for item in hub.channels.sent)
    assert await hub.storage.get(f"ls:order:{order['id']}") is None
    assert await hub.storage.get(f"ls:file:{ROUTE.isoformat()}") is None

    await hub.storage.set(f"ls:file:{ROUTE.isoformat()}", json.dumps({"id": "x", "ready": True}))
    await hub.storage.set(f"ls:order:{order['id']}", json.dumps({
        "transition_at": TRANSITION, "route_date": ROUTE.isoformat(), "file_id": "x", "phone_written": True}))
    fresh = from_api("customerorder", order)
    event = Event(entity="customerorder", entity_id=order["id"], action="UPDATE", source="webhook")
    forced = await hub.engine.execute(hub.automations[0], fresh, event, mode="force")
    assert forced.detail == "WOULD_UPDATE_ORDER"
    assert hub.channels.book.files == {}


async def test_unconfirmed_write_is_not_success(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await _see(hub, fake, order)
    hub.channels.route_unconfirmed = True
    out = await _move(hub, fake, order)
    assert out[0].status == "failed" and "не подтвердил" in out[0].detail
    assert _rows(hub) == []
    hub.channels.route_unconfirmed = False
    async with hub.storage._tx() as conn:
        await conn.execute(runs.update().values(next_at="2000-01-01T00:00:00Z"))
    retry = await hub.engine.retry_due()
    assert retry[0].status == "done"
    assert hub.channels.book.creates == 1
    assert list(_only(hub)["rows"]) == [order["id"]]


async def test_delete_does_not_prepare_a_row(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await webhook(hub, fake, "customerorder", order["id"], "del", action="DELETE")
    assert await hub.tick() == []
    assert hub.channels.book.files == {}


async def test_parallel_orders_share_one_file(fake, make_hub):
    hub = await make_hub(_params())
    _target(fake)
    orders = [_prepare(fake, number=str(1000 + i)) for i in range(2)]
    for order in orders:
        fake.set_state("customerorder", order["id"], TARGET)
        order["updated"] = TRANSITION

    async def once(order):
        obj = from_api("customerorder", order)
        event = Event(entity="customerorder", entity_id=order["id"], action="UPDATE",
                      source="webhook", previous_state="Новый", is_new=False)
        return await hub.engine.execute(hub.automations[0], obj, event)

    results = await asyncio.gather(once(orders[0]), once(orders[1]))
    if any(item.status == "failed" for item in results):
        async with hub.storage._tx() as conn:
            await conn.execute(runs.update().values(next_at="2000-01-01T00:00:00Z"))
        await hub.engine.retry_due()
    assert hub.channels.book.creates == 1
    assert len(hub.channels.book.files) == 1
    assert set(_only(hub)["rows"]) == {order["id"] for order in orders}


async def test_parallel_same_order_one_row(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    fake.set_state("customerorder", order["id"], TARGET)
    order["updated"] = TRANSITION

    async def once():
        obj = from_api("customerorder", order)
        event = Event(entity="customerorder", entity_id=order["id"], action="UPDATE", source="webhook")
        return await hub.engine.execute(hub.automations[0], obj, event)

    results = await asyncio.gather(once(), once())
    assert sorted(item.status for item in results) == ["already", "done"]
    assert list(_only(hub)["rows"]) == [order["id"]]


def test_route_date_is_not_the_saturday_after_the_transition():
    """Четверг 12.11 → пятница 13.11. Суббота 14.11 (день обработки) → понедельник 16.11."""
    assert route_date(parse_transition(TRANSITION)) == ROUTE == date(2026, 11, 13)
    assert route_date(parse_transition("2026-11-14 12:00:00.000")) == date(2026, 11, 16)


async def test_broken_existing_file_is_not_replaced(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    file_id = hub.channels.book.seed(file_title(ROUTE_TEXT), ROUTE_TEXT)
    hub.channels.book.insert(file_id, "чужой", {
        "counterparty": "Уже в листе", "address": "Улица, 5",
    })
    hub.channels.book.files[file_id]["block"] = (
        f"файл {file_id} за {ROUTE_TEXT}: нет листа _hub. "
        "Строки не очищаю, данные не переписываю, второй файл не создаю. "
        "Нужно ручное восстановление или подтверждённая миграция."
    )
    await _see(hub, fake, order)
    out = await _move(hub, fake, order)
    assert out[0].status == "failed"
    assert "нет листа _hub" in out[0].detail
    assert "не очищаю" in out[0].detail and "второй файл не создаю" in out[0].detail
    assert hub.channels.book.creates == 0
    assert len(hub.channels.book.files) == 1
    assert hub.channels.book.files[file_id]["rows"]["чужой"]["address"] == "Улица, 5"
    stored = json.loads(await hub.storage.get(f"ls:order:{order['id']}"))
    assert stored["route_date"] == ROUTE.isoformat()
    assert stored["transition_at"] == TRANSITION


async def test_empty_address_cell_is_filled_then_kept(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await _see(hub, fake, order)
    assert (await _move(hub, fake, order))[0].status == "done"
    row = _only(hub)["rows"][order["id"]]
    row["address"] = ""
    fresh = from_api("customerorder", order)
    event = Event(entity="customerorder", entity_id=order["id"], action="UPDATE", source="webhook")
    filled = await hub.engine.execute(hub.automations[0], fresh, event, mode="force")
    assert filled.status == "done"
    assert _only(hub)["rows"][order["id"]]["address"] == "Тестовая улица, 1"
    order["shipmentAddress"] = "Другой адрес, 9"
    changed = from_api("customerorder", order)
    kept = await hub.engine.execute(hub.automations[0], changed, event, mode="force")
    assert kept.status == "done"
    assert _only(hub)["rows"][order["id"]]["address"] == "Тестовая улица, 1"
    assert list(_only(hub)["rows"]) == [order["id"]]
    stored = json.loads(await hub.storage.get(f"ls:order:{order['id']}"))
    assert stored["route_date"] == ROUTE.isoformat()
    assert any(item["kind"] == "ADDRESS_DIFF" for item in hub.channels.sent)

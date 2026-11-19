"""Маршрутный лист: момент аудита, снимок XLSX, Диск без перезаписи."""
import asyncio
import json
from datetime import date, datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import pytest
from conftest import webhook
from openpyxl import load_workbook

from hub.automations import ConfigError, build, event_types
from hub.automations.logistics_sheet import (
    _day_key,
    delivery_address,
    file_name,
    file_path,
    parse_transition,
    route_date,
)
from hub.automations.route_xlsx import FORMAT_VERSION, HEADERS, build_route_xlsx
from hub.channels.yandex_disk import DiskError
from hub.entities import Order
from hub.events import Event, merge
from hub.ingest import parse_webhook

MSK = ZoneInfo("Europe/Moscow")
TARGET = "ПОДТВЕРЖДЕН / ОЖИДАЕТ СБОРКИ"
TRANSITION = "2026-11-12 18:00:00"
ROUTE = date(2026, 11, 13)
ROUTE_TEXT = "13.11.2026"
# Если бы датой была customerorder.updated, вышло бы 16.11, а не 13.11.
MISLEADING_UPDATED = "2026-11-15 12:00:00.000"
FOLDER = "disk:/route-test"


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
    sheet = {"uses": "logistics_sheet", "hours_field": "Часы приемки"}
    sheet.update(extra)
    return {
        "routes": sheet,
        "route_publish": {"uses": "logistics_publish", "at": "07:00", "folder": FOLDER},
    }


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


async def _move(hub, fake, order, state=TARGET, moment=TRANSITION, rid="go"):
    fake.set_state("customerorder", order["id"], state)
    fake.db["customerorder"][order["id"]]["updated"] = MISLEADING_UPDATED
    await webhook(hub, fake, "customerorder", order["id"], rid, moment=moment)
    return await hub.tick()


def _published(hub):
    return next(iter(hub.channels.disk.files.values()))


async def _publish(hub, when=None):
    moment = when or datetime(2026, 11, 13, 7, 0, tzinfo=MSK)
    return await hub.engine.scheduled(moment)


# ── календарь ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("moment", "expect"), [
    ("2026-11-16 09:00:00.000", date(2026, 11, 17)),
    ("2026-11-17 09:00:00.000", date(2026, 11, 18)),
    ("2026-11-18 09:00:00.000", date(2026, 11, 19)),
    ("2026-11-19 09:00:00.000", date(2026, 11, 20)),
    ("2026-11-13 23:59:00.000", date(2026, 11, 16)),
    ("2026-11-14 12:00:00.000", date(2026, 11, 16)),
    ("2026-11-15 12:00:00.000", date(2026, 11, 16)),
    ("2026-01-31 18:00:00.000", date(2026, 2, 2)),
    ("2026-03-31 18:00:00.000", date(2026, 4, 1)),
    ("2026-04-30 18:00:00.000", date(2026, 5, 1)),
    ("2026-02-28 18:00:00.000", date(2026, 3, 2)),
    ("2026-05-31 18:00:00.000", date(2026, 6, 1)),
    ("2026-08-31 18:00:00.000", date(2026, 9, 1)),
    ("2026-12-31 18:00:00.000", date(2027, 1, 1)),
    ("2027-12-31 18:00:00.000", date(2028, 1, 3)),
])
def test_route_date_week_month_year(moment, expect):
    assert route_date(parse_transition(moment)) == expect


def test_route_date_respects_timezone_of_timestamp():
    naive = route_date(parse_transition("2026-11-15 21:30:00.000"))
    as_utc = route_date(parse_transition("2026-11-15T21:30:00Z"))
    as_offset = route_date(parse_transition("2026-11-15T21:30:00+00:00"))
    as_moscow = route_date(parse_transition("2026-11-16T00:30:00+03:00"))
    assert naive == date(2026, 11, 16)
    assert as_utc == date(2026, 11, 17)
    assert as_offset == as_utc == as_moscow
    assert parse_transition("2026-11-15 21:30:00").tzinfo == MSK


def test_merge_does_not_guess_conflicting_moments():
    one = Event("customerorder", "a", "UPDATE", moment="2026-11-12 18:00:00")
    other = Event("customerorder", "a", "POLL", moment="2026-11-15 12:00:00", source="poll")
    assert merge([one, other]).moment is None
    assert merge([one, Event("customerorder", "a", "POLL", source="poll")]).moment == one.moment


def test_parse_webhook_keeps_only_audit_moment():
    href = "https://api.moysklad.ru/api/remap/1.2/entity/customerorder/order-1"
    payload = {"auditContext": {"moment": TRANSITION, "uid": "admin@cultura"},
               "events": [{"meta": {"type": "customerorder", "href": href}, "action": "UPDATE"}]}
    assert parse_webhook(payload)[0].moment == TRANSITION
    assert parse_webhook({"events": payload["events"]})[0].moment is None


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


def test_disabled_does_not_listen_for_orders():
    autos = build({"logistics_sheet": {"enabled": False}, "daily_upd": {"enabled": True}})
    assert [a.kind for a in autos] == ["daily_upd"]
    assert "customerorder" not in event_types(autos)


def test_publish_requires_folder_and_clock():
    with pytest.raises(ConfigError):
        build({"routes": {**_params()["routes"], "nope": 1}})
    with pytest.raises(ConfigError):
        build({"route_publish": {"uses": "logistics_publish", "folder": ""}})
    with pytest.raises(ConfigError):
        build({"route_publish": {"uses": "logistics_publish", "folder": FOLDER, "at": "7:00"}})


def test_slot_starts_at_configured_morning():
    publish = build(_params())[1]
    assert publish.slot(datetime(2026, 11, 13, 6, 59, tzinfo=MSK)) is None
    assert publish.slot(datetime(2026, 11, 13, 7, 0, tzinfo=MSK)) == "2026-11-13"


# ── сбор события ─────────────────────────────────────────────────────────────

async def test_other_status_cancelled_and_first_sight_do_not_collect(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await _see(hub, fake, order)
    assert await _move(hub, fake, order, state="Собран", rid="other") == []
    await _move(hub, fake, order, state="Отменён", rid="cancel")
    fresh = _prepare(fake, number="1002", state=TARGET)
    await webhook(hub, fake, "customerorder", fresh["id"], "first", moment=TRANSITION)
    assert await hub.tick() == []
    assert hub.channels.disk.uploads == 0
    assert await hub.storage.get(f"ls:order:{fresh['id']}") is None


async def test_audit_moment_not_order_updated(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await _see(hub, fake, order)
    out = await _move(hub, fake, order)
    assert out[0].status == "done"
    assert ROUTE_TEXT in out[0].detail
    assert "16.11.2026" not in out[0].detail
    assert hub.channels.disk.uploads == 0 and hub.channels.disk.reads == 0
    saved = json.loads(await hub.storage.get(f"ls:order:{order['id']}"))
    assert saved["route_date"] == ROUTE.isoformat()
    assert saved["transition_at"] == TRANSITION
    assert saved["status"] == "collected"


async def test_missing_moment_is_manual_review(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await _see(hub, fake, order)
    fake.set_state("customerorder", order["id"], TARGET)
    fake.db["customerorder"][order["id"]]["updated"] = MISLEADING_UPDATED
    await webhook(hub, fake, "customerorder", order["id"], "no-moment", moment=None)
    out = await hub.tick()
    assert out[0].status == "done"
    assert out[0].detail.startswith("MANUAL_REVIEW")
    saved = json.loads(await hub.storage.get(f"ls:order:{order['id']}"))
    assert saved["status"] == "manual" and saved["route_date"] is None
    assert await hub.storage.get(_day_key(ROUTE)) is None
    assert hub.channels.disk.uploads == 0


async def test_poll_without_moment_is_manual_review(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await _see(hub, fake, order)
    fake.set_state("customerorder", order["id"], TARGET)
    fake.db["customerorder"][order["id"]]["updated"] = MISLEADING_UPDATED
    await hub.storage.enqueue(
        Event("customerorder", order["id"], "POLL", source="poll"), "poll-1")
    out = await hub.tick()
    assert out[0].detail.startswith("MANUAL_REVIEW")
    assert "16.11.2026" not in out[0].detail


async def test_repeat_webhook_does_not_duplicate(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await _see(hub, fake, order)
    await _move(hub, fake, order, rid="once")
    await _move(hub, fake, order, rid="twice")
    day = json.loads(await hub.storage.get(_day_key(ROUTE)))
    assert list(day["orders"]) == [order["id"]]


async def test_weekend_lands_on_monday_file(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await _see(hub, fake, order)
    await _move(hub, fake, order, moment="2026-11-14 12:00:00")
    out = await _publish(hub, datetime(2026, 11, 16, 7, 0, tzinfo=MSK))
    assert out[0].status == "done"
    assert file_name(date(2026, 11, 16)) in out[0].detail
    assert list(hub.channels.disk.files) == [file_path(FOLDER, date(2026, 11, 16))]


async def test_publish_writes_xlsx_once(fake, make_hub):
    hub = await make_hub(_params())
    early = _prepare(fake, number="1001")
    late = _prepare(fake, number="1002")
    await _see(hub, fake, early)
    await _see(hub, fake, late)
    await _move(hub, fake, late, moment="2026-11-12 19:00:00", rid="late")
    await _move(hub, fake, early, moment="2026-11-12 10:00:00", rid="early")
    assert hub.channels.disk.uploads == 0
    out = await _publish(hub)
    assert out[0].status == "done"
    assert hub.channels.disk.uploads == 1
    body = _published(hub)
    book = load_workbook(BytesIO(body))
    sheet = book["Маршрут"]
    assert [cell.value for cell in sheet[1]] == list(HEADERS)
    assert sheet["A2"].value == 1 and sheet["B2"].value == "ООО «Ромашка»"
    assert sheet["C2"].value == "Тестовая улица, 1"
    assert sheet["D2"].value == "+7 900 111-22-33"
    assert sheet["E2"].value == "10-14" and sheet["E2"].number_format == "@"
    assert sheet["F2"].value == "позвонить утром" and not sheet["G2"].value
    assert sheet["A3"].value == 2
    assert "Юридический" not in " ".join(str(cell.value or "") for row in sheet.iter_rows() for cell in row)
    hidden = book["_hub"]
    assert hidden.sheet_state == "hidden"
    assert [cell.value for cell in hidden[1]] == [
        "order_id", "route_date", "format_version", "publication_id"]
    assert hidden["A2"].value == early["id"]
    assert hidden["B2"].value == ROUTE_TEXT and hidden["C2"].value == FORMAT_VERSION
    assert hidden["D2"].value == hidden["D3"].value
    day = json.loads(await hub.storage.get(_day_key(ROUTE)))
    assert day["status"] == "published"
    assert day["file"]["sha256"]
    assert day["file"]["md5"]
    assert day["file"]["path"].endswith(file_name(ROUTE))
    assert day["frozen_ids"] == [early["id"], late["id"]]
    again = await _publish(hub)
    assert again == []
    publish = next(item for item in hub.engine.automations if item.kind == "logistics_publish")
    assert (await publish.run_scheduled(hub.ctx, ROUTE.isoformat())).startswith("уже опубликован")
    assert hub.channels.disk.uploads == 1


async def test_parallel_scheduler_uploads_one_file(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await _see(hub, fake, order)
    await _move(hub, fake, order)
    when = datetime(2026, 11, 13, 7, 0, tzinfo=MSK)
    first, second = await asyncio.gather(
        hub.engine.scheduled(when), hub.engine.scheduled(when))
    statuses = {item.status for batch in (first, second) for item in batch}
    assert "done" in statuses
    assert hub.channels.disk.uploads == 1
    assert len(hub.channels.disk.files) == 1


async def test_failed_upload_retries_without_a_second_file(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await _see(hub, fake, order)
    await _move(hub, fake, order)
    hub.channels.disk.mode = "fail"
    out = await _publish(hub)
    assert out[0].status == "failed"
    assert hub.channels.disk.uploads == 0
    hub.channels.disk.mode = "ok"
    publish = next(item for item in hub.engine.automations if item.kind == "logistics_publish")
    text = await publish.run_scheduled(hub.ctx, ROUTE.isoformat())
    assert text.startswith("PUBLISH")
    assert hub.channels.disk.uploads == 1


async def test_unconfirmed_upload_is_checked_before_retry(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await _see(hub, fake, order)
    await _move(hub, fake, order)
    hub.channels.disk.mode = "unconfirmed"
    with pytest.raises(DiskError, match="не подтверждена"):
        publish = next(item for item in hub.engine.automations if item.kind == "logistics_publish")
        await publish.run_scheduled(hub.ctx, ROUTE.isoformat())
    assert hub.channels.disk.uploads == 1
    stored = _published(hub)
    hub.channels.disk.mode = "ok"
    text = await publish.run_scheduled(hub.ctx, ROUTE.isoformat())
    assert text.startswith("PUBLISH")
    assert hub.channels.disk.uploads == 1
    assert hub.channels.disk.files[file_path(FOLDER, ROUTE)] == stored


async def test_existing_file_is_not_replaced(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await _see(hub, fake, order)
    await _move(hub, fake, order)
    path = file_path(FOLDER, ROUTE)
    foreign = "чужой лист".encode()
    hub.channels.disk.files[path] = foreign
    with pytest.raises(DiskError, match="не совпадает"):
        publish = next(item for item in hub.engine.automations if item.kind == "logistics_publish")
        await publish.run_scheduled(hub.ctx, ROUTE.isoformat())
    assert hub.channels.disk.uploads == 0
    assert hub.channels.disk.files[path] == foreign


async def test_delayed_webhook_after_publish_is_late(fake, make_hub):
    hub = await make_hub(_params(notify_chats=["-1001"]))
    first = _prepare(fake, number="1001")
    delayed = _prepare(fake, number="1002")
    delayed["agent"]["phone"] = "+7 900 777-66-55"
    await _see(hub, fake, first)
    await _see(hub, fake, delayed)
    await _move(hub, fake, first, rid="first")
    await _publish(hub)
    body = _published(hub)
    out = await _move(hub, fake, delayed, moment=TRANSITION, rid="delayed")
    assert out[0].status == "done"
    assert out[0].detail.startswith("LATE_ORDER")
    assert "+7 900 777-66-55" not in out[0].detail
    assert hub.channels.messages[-1]["chat"] == "-1001"
    assert "LATE_ORDER" in hub.channels.messages[-1]["text"]
    assert "+7 900 777-66-55" not in hub.channels.messages[-1]["text"]
    assert hub.channels.disk.uploads == 1
    assert hub.channels.disk.files[file_path(FOLDER, ROUTE)] == body
    saved = json.loads(await hub.storage.get(f"ls:order:{delayed['id']}"))
    assert saved["status"] == "late" and saved["route_date"] == ROUTE.isoformat()


async def test_dry_run_does_not_write_disk_or_collection(fake, make_hub):
    hub = await make_hub(_params(), dry_run=True)
    order = _prepare(fake)
    await _see(hub, fake, order)
    out = await _move(hub, fake, order)
    assert out[0].detail.startswith("WOULD_COLLECT")
    assert await hub.storage.get(f"ls:order:{order['id']}") is None
    assert hub.channels.disk.uploads == 0 and hub.channels.disk.reads == 0
    published = await _publish(hub)
    assert published[0].detail.startswith("WOULD_PUBLISH")
    assert hub.channels.disk.uploads == 0 and hub.channels.disk.files == {}


def test_xlsx_hours_stay_text_and_layout_is_for_print():
    orders = [
        {"id": "a", "counterparty": "ООО «Ромашка»", "address": "Длинный адрес, дом 1",
         "phone": "+7 900 000-00-00", "hours": "09:00-18:00", "comment": "комментарий"},
        {"id": "b", "counterparty": "ИП Петров", "address": "Вторая улица",
         "phone": "", "hours": "10:00\u201314:00", "comment": ""},
    ]
    book = load_workbook(BytesIO(build_route_xlsx(ROUTE_TEXT, orders, "pub-1")))
    sheet = book["Маршрут"]
    assert sheet.freeze_panes == "A2"
    assert sheet.column_dimensions["C"].width >= 40
    assert sheet["C2"].alignment.wrap_text is True
    assert sheet["E2"].value == "09:00-18:00" and sheet["E2"].number_format == "@"
    assert sheet["E3"].value == "10:00\u201314:00" and sheet["E3"].number_format == "@"
    assert sheet.page_setup.orientation == "landscape"
    assert sheet.page_setup.fitToWidth == 1
    assert sheet.print_title_rows == "$1:$1"
    assert sheet.sheet_properties.pageSetUpPr.fitToPage is True
    assert book["_hub"].sheet_state == "hidden"

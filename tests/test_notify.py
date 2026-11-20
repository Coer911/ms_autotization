"""Уведомления в Telegram человеческим языком: важное всегда, остальное в отладке."""
from datetime import datetime
from zoneinfo import ZoneInfo

from test_logistics_sheet import TARGET, _move, _params, _prepare, _see

from hub.channels.notify_text import day, failure_text, route_text

MSK = ZoneInfo("Europe/Moscow")


async def _hub(make_hub, level):
    hub = await make_hub(_params())
    hub.settings.telegram_token = "tg"
    hub.settings.notify_chat = "-100"
    hub.settings.notify_level = level
    return hub


def _texts(hub):
    return [m["text"] for m in hub.channels.messages]


async def test_debug_tells_about_every_collected_order(fake, make_hub):
    hub = await _hub(make_hub, "debug")
    order = _prepare(fake)
    await _see(hub, fake, order)
    await _move(hub, fake, order)
    assert _texts(hub) == ["🚚 Заказ 1001 (ООО «Ромашка») — в доставку на пт 13.11."]


async def test_normal_level_keeps_quiet_about_routine(fake, make_hub):
    hub = await _hub(make_hub, "normal")
    order = _prepare(fake)
    await _see(hub, fake, order)
    await _move(hub, fake, order)
    assert _texts(hub) == []
    await _move(hub, fake, order, state="Отменён", moment="2026-11-12 20:00:00", rid="x")
    assert _texts(hub) == ["❌ Заказ 1001 (ООО «Ромашка») отменили — убрал из листа на пт 13.11."]


async def test_published_sheet_comes_as_file(fake, make_hub):
    hub = await _hub(make_hub, "normal")
    order = _prepare(fake)
    await _see(hub, fake, order)
    await _move(hub, fake, order)
    await hub.engine.scheduled(datetime(2026, 11, 13, 7, 0, tzinfo=MSK))
    doc = hub.channels.documents[0]
    assert doc["filename"] == "13.11.2026.xlsx"
    assert doc["caption"].startswith("📄 Маршрутный лист на пт 13.11 готов: доставок 1.")


async def test_debug_reports_each_failure_in_plain_words(fake, make_hub):
    hub = await _hub(make_hub, "debug")
    order = _prepare(fake)
    order["shipmentAddress"] = ""
    await _see(hub, fake, order)
    await _move(hub, fake, order)
    text = _texts(hub)[-1]
    assert text.startswith("⚠️ Маршрутный лист: ")
    assert "заполните адрес в заказе" in text and "Попробую ещё раз" in text
    assert "RouteReport" not in text and TARGET not in text


def test_texts_are_human():
    assert day("2026-11-16") == "пн 16.11"
    moved = route_text("MOVED", {"order": "1", "counterparty": "ООО «А»",
                                 "planned": "2026-11-13", "route_date": "2026-11-16"})
    assert moved == ("↪️ Заказ 1 (ООО «А») подтвердили, когда лист на пт 13.11 уже ушёл. "
                     "Поставил в доставку на пн 16.11.")
    assert route_text("SOMETHING", {}) is None
    dead = failure_text("Публикация", "20.11", "DiskError: Диск 401: токен не принят", None, True)
    assert dead == "🛑 Публикация: 20.11 — Яндекс Диск не принял токен. Попытки кончились, нужна ваша помощь."


async def test_repeat_press_is_quiet(fake, make_hub):
    hub = await _hub(make_hub, "debug")
    order = _prepare(fake)
    await _see(hub, fake, order)
    await _move(hub, fake, order, rid="1")
    await _move(hub, fake, order, state="Новый", moment="2026-11-12 18:30:00", rid="2")
    out = await _move(hub, fake, order, moment="2026-11-12 19:00:00", rid="3")
    assert out[0].detail.startswith("UPDATE")
    assert len(_texts(hub)) == 1                             # только первое «в доставку»


async def test_failure_is_reported_once_not_on_every_retry(fake, make_hub):
    hub = await _hub(make_hub, "debug")
    order = _prepare(fake)
    order["shipmentAddress"] = ""
    await _see(hub, fake, order)
    await _move(hub, fake, order)
    from sqlalchemy import update

    from hub.storage import runs
    for _ in range(2):                                       # два плановых повтора, оба падают
        async with hub.storage.engine.begin() as conn:
            await conn.execute(update(runs).values(next_at="2000-01-01T00:00:00"))
        retried = await hub.engine.retry_due()
        assert retried and retried[0].status == "failed"
    assert sum("Попробую ещё раз" in t for t in _texts(hub)) == 1


async def test_publish_failure_is_urgent_even_in_normal(fake, make_hub):
    from test_logistics_sheet import BASE_PATH, _with_base
    hub = await make_hub(_with_base())
    hub.settings.telegram_token, hub.settings.notify_chat = "tg", "-100"
    order = _prepare(fake)
    await _see(hub, fake, order)
    await _move(hub, fake, order)
    await hub.engine.scheduled(datetime(2026, 11, 13, 7, 0, tzinfo=MSK))
    assert any("нет файла" in t and BASE_PATH in t for t in _texts(hub))


async def test_not_in_base_goes_into_file_caption(fake, make_hub):
    from test_logistics_sheet import BASE_PATH, _base_xlsx, _with_base
    hub = await make_hub(_with_base())
    hub.settings.telegram_token, hub.settings.notify_chat = "tg", "-100"
    hub.channels.disk.files[BASE_PATH] = _base_xlsx([["Адрес 1", None, "ИП Есть", "1"]])
    order = _prepare(fake)
    await _see(hub, fake, order)
    await _move(hub, fake, order)
    await hub.engine.scheduled(datetime(2026, 11, 13, 7, 0, tzinfo=MSK))
    assert _texts(hub) == []                                 # отдельных сообщений нет
    assert "Нет в базе адресов (1): ООО «Ромашка»" in hub.channels.documents[0]["caption"]


async def test_moment_recovered_from_audit(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await _see(hub, fake, order)
    fake.audit[order["id"]] = [
        {"moment": "2026-11-12 18:00:01.000", "eventType": "update",
         "diff": {"state": {"oldValue": {"name": "Новый"}, "newValue": {"name": TARGET}}}},
        {"moment": "2026-11-10 10:00:00.000", "eventType": "create", "diff": {}},
    ]
    out = await _move(hub, fake, order, moment=None, rid="poll-first")
    assert out[0].detail.startswith("COLLECT") and "13.11.2026" in out[0].detail


async def test_created_in_status_moment_from_audit_create(fake, make_hub):
    hub = await make_hub(_params())
    order = _prepare(fake)
    await _see(hub, fake, order)
    fake.audit[order["id"]] = [{"moment": "2026-11-12 18:03:28.571", "eventType": "create"}]
    out = await _move(hub, fake, order, moment=None, rid="x")
    assert "13.11.2026" in out[0].detail

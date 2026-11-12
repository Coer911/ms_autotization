"""Бот очереди: разбор, подтверждение, заказ покупателя. Сети нет.

МойСклад — tests/fake_moysklad.py, Telegram — MockTransport. Живой Telegram и
живой МойСклад здесь не вызываются.
"""
import asyncio
import json

import httpx
import pytest
from conftest import fresh_storage

from hub.moysklad import MoySklad, MoySkladError
from hub.orders.bot import OrderQueueBot, OrderSession, make_order_bot
from hub.orders.order import Lookup, OrderCreator, OrderError, Resolved, Source, source_from
from hub.orders.parse import parse_order_text
from hub.settings import Settings

ORDER = """ООО «Кофейня»
Бразилия Сантос — 2 кг
Эфиопия Иргачеффе — 0,5 кг
комментарий: на пятницу"""
QUEUE = -1005


def origin(mid=15, username="cultura_orders", chat=-1001234567890):
    return {"type": "channel", "date": 1, "message_id": mid,
            "chat": {"id": chat, "username": username, "title": "Заказы", "type": "channel"}}


def message(text, *, mid=1, chat=QUEUE, forward=None):
    msg = {"message_id": mid, "chat": {"id": chat}, "text": text}
    if forward is not None:
        msg["forward_origin"] = forward
    return msg


def order_message(text=ORDER, queue_mid=10, origin_mid=15):
    return message(text, mid=queue_mid, forward=origin(origin_mid))


def posts(fake):
    return [call for call in fake.calls if call[0] == "POST"]


@pytest.fixture
async def orders(fake):
    fake.add_organization("ИП Культура", "org-1")
    cp = fake.add_counterparty("ООО «Кофейня»")
    brazil = fake.add_product("Бразилия Сантос", 120_000, id_="brazil")
    ethiopia = fake.add_product("Эфиопия Иргачеффе", 90_000, id_="ethiopia")
    ms = MoySklad("test-token", transport=fake.transport())
    session = OrderSession(Lookup(ms, "ИП Культура"), OrderCreator(ms, dry_run=False))
    yield fake, ms, session, cp, brazil, ethiopia
    await ms.close()


# ── разбор текста ────────────────────────────────────────────────────────────

def test_parse_labeled_quantity_and_comment():
    parsed = parse_order_text(
        "клиент: ООО «Кофейня»\n2 кг Бразилия Сантос\nЭфиопия x 0,5\nкомментарий: завтра")
    assert parsed.counterparty == "ООО «Кофейня»"
    assert [(line.name, line.quantity) for line in parsed.lines] == [
        ("Бразилия Сантос", 2), ("Эфиопия", 0.5)]
    assert parsed.comment == "завтра" and parsed.missing == []
    assert parsed.filled


def test_parse_plain_form_and_free_text_stays_empty():
    plain = parse_order_text("ООО «Кофейня»\nБразилия Сантос — 2 кг")
    assert plain.filled
    free = parse_order_text("на пятницу 2 кг бразилии для ромашки")
    yes = parse_order_text("да")
    assert not free.counterparty and not free.lines
    assert not yes.lines and yes.missing


def test_source_link_from_channel_and_legacy_forward():
    forwarded = source_from(order_message())
    assert forwarded == Source("-1001234567890", "15", "cultura_orders", "Заказы")
    assert forwarded.link() == "https://t.me/cultura_orders/15"
    private = source_from({"message_id": 4, "chat": {"id": QUEUE}, "text": "x",
                           "forward_origin": {"type": "channel", "message_id": 9,
                                              "chat": {"id": -1001234567890, "title": "Канал",
                                                       "type": "channel"}}})
    assert private.link() == "https://t.me/c/1234567890/9"
    legacy = source_from({"message_id": 3, "chat": {"id": QUEUE}, "text": "x",
                          "forward_from_chat": {"id": -10055, "username": "old", "title": "Архив",
                                                "type": "channel"},
                          "forward_from_message_id": 8})
    assert legacy.link() == "https://t.me/old/8"
    typed = source_from({"message_id": 6, "chat": {"id": QUEUE}, "text": "x"})
    assert (typed.chat_id, typed.message_id) == (str(QUEUE), "6")


# ── подтверждение и создание ─────────────────────────────────────────────────

async def test_yes_creates_order_once_with_source_in_comment(orders):
    fake, _, session, cp, brazil, ethiopia = orders
    draft = await session.handle_message(order_message())
    assert "Черновик заказа" in draft and "ООО «Кофейня»" in draft
    assert "Бразилия Сантос — 2 — 1 200 ₽" in draft
    assert "Эфиопия Иргачеффе — 0,5 — 900 ₽" in draft
    assert "https://t.me/cultura_orders/15" in draft and "на пятницу" in draft
    assert posts(fake) == []

    created = await session.handle_message(message("да", mid=11))
    assert created == "Заказ 00001 создан."
    assert posts(fake) == [("POST", "entity/customerorder")]
    doc = next(iter(fake.db["customerorder"].values()))
    assert doc["name"] == "00001"
    assert doc["agent"]["meta"]["href"].endswith(cp["id"])
    assert doc["organization"]["meta"]["href"].endswith("org-1")
    assert [(p["quantity"], p["price"], p["assortment"]["meta"]["href"].rsplit("/", 1)[-1])
            for p in doc["positions"]] == [(2, 120_000, brazil["id"]), (0.5, 90_000, ethiopia["id"])]
    assert "на пятницу" in doc["description"]
    assert "https://t.me/cultura_orders/15" in doc["description"]
    assert "чат -1001234567890, сообщение 15" in doc["description"]
    assert "test-token" not in doc["description"]

    assert "черновика" in (await session.handle_message(message("да", mid=12))).lower()
    again = await session.handle_message(order_message(queue_mid=99))
    assert again == "Этот заказ уже есть: 00001. Второй раз не создаю."
    assert len(fake.db["customerorder"]) == 1
    assert posts(fake) == [("POST", "entity/customerorder")]


async def test_dry_run_checks_cards_and_does_not_post(orders):
    fake, ms, _, *_ = orders
    creator = OrderCreator(ms, dry_run=True)
    session = OrderSession(Lookup(ms, "ИП Культура"), creator)
    await session.handle_message(order_message())
    reply = await session.handle_message(message("Да!", mid=2))
    assert "Заказ не создан" in reply and "номер не присвоен" in reply
    assert "00001" not in reply
    assert posts(fake) == []
    assert any(path == "entity/counterparty" for _, path in fake.calls)
    assert any(path == "entity/product" for _, path in fake.calls)
    assert any(path == "entity/organization" for _, path in fake.calls)
    assert creator.journal and creator.journal[0]["dry_run"]
    assert creator.journal[0]["body"]["positions"][0]["quantity"] == 2
    repeat = await session.handle_message(order_message(queue_mid=40))
    assert "пробный прогон уже был" in repeat and posts(fake) == []

    creator.dry_run = False
    await session.handle_message(order_message(queue_mid=41))
    live = await session.handle_message(message("да", mid=42))
    assert live == "Заказ 00001 создан."
    assert posts(fake) == [("POST", "entity/customerorder")]


async def test_yes_without_draft_cancel_and_missing_fields(orders):
    fake, ms, session, *_ = orders
    assert "Нет черновика" in await session.handle_message(message("да"))
    await session.handle_message(order_message())
    assert await session.handle_message(message("нет")) == "Черновик сброшен."
    assert "Нет черновика" in await session.handle_message(message("подтверждаю"))
    assert posts(fake) == []

    unknown = await session.handle_message(message(
        "Нет такого покупателя\nБразилия Сантос — 1", mid=3, forward=origin(3)))
    assert "не найден" in unknown
    assert "Заказ пока не создаю" in await session.handle_message(message("да", mid=4))
    assert posts(fake) == []

    no_org = OrderSession(Lookup(ms, "Рога и копыта"), OrderCreator(ms, dry_run=False))
    text = await no_org.handle_message(order_message(queue_mid=5, origin_mid=5))
    assert "не найдена" in text
    await no_org.handle_message(message("да", mid=6))
    assert posts(fake) == []


async def test_ambiguous_counterparty_and_product_wait_for_a_number(orders):
    fake, ms, _, cp, brazil, _ = orders
    fake.add_counterparty("Кафе Ромашка")
    fake.add_counterparty("Кафе Ромашка Север")
    cerrado = fake.add_product("Бразилия Серрадо", 200_000, id_="cerrado")
    session = OrderSession(Lookup(ms, "ИП Культура"), OrderCreator(ms, dry_run=False))

    party = await session.handle_message(message(
        "контрагент: Ромашка\nБразилия Сантос — 2", mid=1, forward=origin(21)))
    assert "Несколько контрагентов" in party and "1. Кафе Ромашка" in party
    assert "пока не создаю" not in party
    await session.handle_message(message("да", mid=2))
    assert posts(fake) == []
    chosen = await session.handle_message(message("2", mid=3))
    assert "Кафе Ромашка Север" in chosen and "Ответьте «да»" in chosen
    assert (await session.handle_message(message("да", mid=4))) == "Заказ 00001 создан."
    doc = next(iter(fake.db["customerorder"].values()))
    assert "Север" in doc["agent"]["meta"]["href"] or doc["agent"]["meta"]["href"] != cp["meta"]["href"]
    assert doc["agent"]["meta"]["href"] != cp["meta"]["href"]

    product = await session.handle_message(message(
        "ООО «Кофейня»\nБразилия — 1 кг", mid=5, forward=origin(22)))
    assert "подходит к нескольким" in product
    await session.handle_message(message("да", mid=6))
    assert len(posts(fake)) == 1
    picked = await session.handle_message(message(cerrado["name"], mid=7))
    assert "Бразилия Серрадо" in picked
    assert (await session.handle_message(message("да", mid=8))) == "Заказ 00002 создан."
    second = list(fake.db["customerorder"].values())[1]
    assert second["positions"][0]["price"] == 200_000
    assert second["positions"][0]["assortment"]["meta"]["href"].endswith(cerrado["id"])
    assert brazil["id"] not in second["positions"][0]["assortment"]["meta"]["href"]


async def test_incomplete_create_and_empty_source_do_not_post(orders):
    fake, ms, *_ = orders
    creator = OrderCreator(ms, dry_run=False)
    with pytest.raises(OrderError, match="нет организации"):
        await creator.create(Source("-1005", "1"), Resolved("", None, [], [], "", None, "нет"))
    session = OrderSession(Lookup(ms, "ИП Культура"), creator)
    await session.handle_message({"message_id": "", "chat": {"id": QUEUE}, "text": ORDER})
    reply = await session.handle_message(message("да", mid=2))
    assert "номер сообщения" in reply
    assert posts(fake) == []


async def test_same_order_is_not_created_again_after_restart(orders):
    fake, ms, session, *_ = orders
    storage = await fresh_storage()
    try:
        creator = OrderCreator(ms, dry_run=False, storage=storage)
        first = OrderSession(Lookup(ms, "ИП Культура"), creator)
        await first.handle_message(order_message())
        await first.handle_message(message("да", mid=11))
        restarted = OrderCreator(ms, dry_run=False, storage=storage)
        assert await restarted.find(source_from(order_message())) == "00001"
        second = OrderSession(Lookup(ms, "ИП Культура"), restarted)
        reply = await second.handle_message(order_message(queue_mid=80))
        assert reply == "Этот заказ уже есть: 00001. Второй раз не создаю."
        assert len(posts(fake)) == 1
    finally:
        await storage.close()
    assert session  # справочный сеанс фикстуры здесь не создаёт заказ


async def test_free_text_asks_for_the_form_and_does_not_create(orders):
    fake, _, session, *_ = orders
    reply = await session.handle_message(message(
        "на пятницу два кило бразилии в кофейню", mid=1, forward=origin(31)))
    assert "Напишите так" in reply
    await session.handle_message(message("да", mid=2))
    assert posts(fake) == []


async def test_fake_rejects_customerorder_without_organization(orders):
    _, ms, *_ = orders
    with pytest.raises(MoySkladError):
        await ms.post("entity/customerorder", {"description": "пусто"})


# ── Telegram: только очередь ─────────────────────────────────────────────────

class FakeTelegram:
    def __init__(self, updates):
        self.updates = updates
        self.sent = []
        self.methods = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        self.methods.append(method)
        assert "/botORDER-TOKEN/" in request.url.path
        body = json.loads(request.content or b"{}")
        if method == "getUpdates":
            assert body["allowed_updates"] == ["message"]
            batch, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": batch})
        if method == "sendMessage":
            self.sent.append((str(body["chat_id"]), body["text"]))
        return httpx.Response(200, json={"ok": True, "result": True})


def update(n, msg):
    return {"update_id": n, "message": msg}


async def test_queue_bot_ignores_public_chats_voice_and_channel_posts(orders):
    fake, ms, _, *_ = orders
    session = OrderSession(Lookup(ms, "ИП Культура"), OrderCreator(ms, dry_run=False))
    photo = {"message_id": 2, "chat": {"id": QUEUE}, "photo": [{"file_id": "pic"}]}
    join = {"message_id": 3, "chat": {"id": QUEUE}, "new_chat_members": [{"id": 1}]}
    tg = FakeTelegram([
        update(1, message("привет", chat=666)),
        update(2, photo),
        update(3, join),
        {"update_id": 4, "channel_post": {"chat": {"id": QUEUE}, "text": ORDER, "message_id": 4}},
        update(5, order_message()),
    ])
    bot = OrderQueueBot("ORDER-TOKEN", str(QUEUE), session,
                        http=httpx.AsyncClient(transport=httpx.MockTransport(tg.handle)))
    assert await bot.poll_once() == 5
    await asyncio.gather(*bot._tasks)
    assert posts(fake) == []
    assert tg.sent[0][0] == "666" and "Нет доступа" in tg.sent[0][1] and "HUB_ORDER_CHAT" in tg.sent[0][1]
    assert tg.sent[1] == (str(QUEUE), "Пришлите текст.")
    assert sum(1 for _, text in tg.sent if text == "Пришлите текст.") == 1
    assert any(text.startswith("Черновик") for _, text in tg.sent)
    assert "forwardMessage" not in tg.methods
    assert all(chat == str(QUEUE) or chat == "666" for chat, _ in tg.sent)

    tg.updates = [update(6, message("/cancel", mid=12)), update(7, message("/help", mid=13))]
    await bot.poll_once()
    await asyncio.gather(*bot._tasks)
    assert any(text == "Черновик сброшен." for _, text in tg.sent)
    assert any("после «да»" in text.lower() or "После «да»" in text for _, text in tg.sent)
    assert posts(fake) == []
    await bot.close()


async def test_bot_poll_confirms_and_replies_with_the_number(orders):
    fake, ms, _, *_ = orders
    session = OrderSession(Lookup(ms, "ИП Культура"), OrderCreator(ms, dry_run=False))
    tg = FakeTelegram([update(1, order_message())])
    bot = OrderQueueBot("ORDER-TOKEN", str(QUEUE), session,
                        http=httpx.AsyncClient(transport=httpx.MockTransport(tg.handle)))
    await bot.poll_once()
    await asyncio.gather(*list(bot._tasks))
    tg.updates = [update(2, message("да", mid=11))]
    await bot.poll_once()
    await asyncio.gather(*list(bot._tasks))
    assert tg.sent[-1] == (str(QUEUE), "Заказ 00001 создан.")
    assert len(fake.db["customerorder"]) == 1
    await bot.close()


# ── настройки и запуск ───────────────────────────────────────────────────────

def test_order_bot_settings_use_a_separate_token(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("{}\n", encoding="utf-8")
    env = {"HUB_CONFIG": str(cfg), "ORDER_BOT_TOKEN": "order", "TELEGRAM_BOT_TOKEN": "reference",
           "HUB_ORDER_CHAT": " -1005 ", "HUB_ORDER_ORGANIZATION": " ИП Культура "}
    ready = Settings.from_env(env)
    assert ready.order_bot_ready
    assert ready.order_chat == "-1005" and ready.order_organization == "ИП Культура"
    assert ready.order_bot_issue() == ""
    clash = Settings.from_env({**env, "ORDER_BOT_TOKEN": "reference"})
    assert not clash.order_bot_ready and "совпадает" in clash.order_bot_issue()
    partial = Settings.from_env({"HUB_CONFIG": str(cfg), "ORDER_BOT_TOKEN": "order"})
    assert not partial.order_bot_ready and "HUB_ORDER_CHAT" in partial.order_bot_issue()
    assert Settings.from_env({"HUB_CONFIG": str(cfg)}).order_bot_issue() == ""


async def test_make_order_bot_keeps_dry_run_and_its_token(fake):
    fake.add_organization("ИП Культура")
    ms = MoySklad("test-token", transport=fake.transport())
    http = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"ok": True, "result": True})))
    try:
        settings = Settings(ms_token="test-token", order_bot_token="order-token",
                            telegram_token="ref-token", order_chat="-1005",
                            order_organization="ИП Культура", dry_run=True)
        bot = make_order_bot(settings, ms, http=http)
        assert bot.session.creator.dry_run
        assert "order-token" in bot._url and "ref-token" not in bot._url
        await bot.close()
    finally:
        await ms.close()


async def _idle(self):
    await asyncio.Event().wait()


async def test_role_all_starts_order_loop_on_its_own_token(make_hub, monkeypatch):
    monkeypatch.setattr("hub.orders.bot.OrderQueueBot.run", _idle)
    hub = await make_hub({})
    hub.settings.order_bot_token = "order-token"
    hub.settings.telegram_token = "ref-token"
    hub.settings.order_chat = "-1005"
    hub.settings.order_organization = "ИП Культура"
    try:
        await hub.start("all")
        names = [task.get_name() for task in hub._tasks]
        assert "order-bot" in names and "bot" not in names
        assert "order-token" in hub._order_bot._url and "ref-token" not in hub._order_bot._url
    finally:
        for task in hub._tasks:
            task.cancel()
        await asyncio.gather(*hub._tasks, return_exceptions=True)
        if hub._order_bot:
            await hub._order_bot.close()


async def test_same_token_does_not_start_second_poll(make_hub, monkeypatch):
    monkeypatch.setattr("hub.orders.bot.OrderQueueBot.run", _idle)
    hub = await make_hub({})
    hub.settings.telegram_token = hub.settings.order_bot_token = "same"
    hub.settings.order_chat = "-1005"
    hub.settings.order_organization = "ИП Культура"
    try:
        await hub.start("all")
        assert "order-bot" not in [task.get_name() for task in hub._tasks]
        assert hub._order_bot is None
    finally:
        for task in hub._tasks:
            task.cancel()
        await asyncio.gather(*hub._tasks, return_exceptions=True)


async def test_role_bot_can_be_only_the_order_queue(make_hub, monkeypatch):
    monkeypatch.setattr("hub.orders.bot.OrderQueueBot.run", _idle)
    hub = await make_hub({})
    hub.settings.order_bot_token = "order-token"
    hub.settings.order_chat = "-1005"
    hub.settings.order_organization = "ИП Культура"
    try:
        await hub.start("bot")
        assert [task.get_name() for task in hub._tasks] == ["order-bot"]
    finally:
        for task in hub._tasks:
            task.cancel()
        await asyncio.gather(*hub._tasks, return_exceptions=True)
        await hub._order_bot.close()

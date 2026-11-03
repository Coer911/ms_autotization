"""Бот: расчёты по МойСклад, разговор с Claude, Telegram — всё без сети.

Отчёты МойСклад подделаны по формам из docs/moysklad-api/reports (суммы в копейках,
meta.href с ?expand у остатков). Claude подделан ответами настоящих типов SDK.
"""
import asyncio
import json
import re
from datetime import date
from urllib.parse import parse_qs, urlparse

import anthropic
import httpx
import httpx2
import pytest
from anthropic.types.beta import BetaMessage

from hub.bot import Agent, BotError, Tools
from hub.bot.telegram import TelegramBot, chunks
from hub.bot.tools import ToolError, call, definitions
from hub.moysklad import MoySklad

BASE = "https://api.moysklad.ru/api/remap/1.2"
TODAY = date(2026, 11, 3)


def ref(kind, id_, expand=False):
    href = f"{BASE}/entity/{kind}/{id_}"
    return {"meta": {"href": href + ("?expand=supplier" if expand else ""), "type": kind}}


class FakeReports:
    """Отчёты и выборки, нужные боту. Фильтры — как у настоящего API (search=, product=, …)."""

    def __init__(self):
        self.orgs = [{**ref("organization", "ds"), "id": "ds", "name": "ИП Краснояров Данил Сергеевич"},
                     {**ref("organization", "ozon"), "id": "ozon",
                      "name": "ИП КРАСНОЯРОВ ДАНИЛ СЕРГЕЕВИЧ (ОЗОН БАНК)"}]
        self.stock = []           # строки report/stock/all
        self.products = []        # entity/product (в том числе с нулевым остатком)
        self.ops = {}             # href товара -> строки report/turnover/byoperations
        self.profit = {}          # (kind, counterparty_href|None) -> строки report/profit/*
        self.demands = []
        self.balances = {}
        self.counterparties = []
        self.calls = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        path = url.path.replace("/api/remap/1.2/", "", 1)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        flt = dict(c.split("=", 1) for c in q.get("filter", "").split(";") if "=" in c)
        self.calls.append((path, flt))
        rows = self.route(path, q, flt)
        if isinstance(rows, httpx.Response):
            return rows
        if isinstance(rows, dict):
            return httpx.Response(200, json=rows)
        return httpx.Response(200, json={"meta": {"size": len(rows)}, "rows": rows})

    def route(self, path, q, flt):
        if path == "entity/organization":
            return self.orgs
        if path == "report/stock/all":
            words = flt.get("search", "").lower().split()
            return [r for r in self.stock if all(w in r["name"].lower() for w in words)]
        if path == "entity/product":
            words = q.get("search", "").lower().split()
            return [p for p in self.products if all(w in p["name"].lower() for w in words)]
        if path == "report/turnover/byoperations":
            return self.ops.get(flt.get("product"), [])
        if m := re.fullmatch(r"report/profit/(\w+)", path):
            return self.profit.get((m.group(1), flt.get("counterparty")), [])
        if path == "entity/demand":
            return [d for d in self.demands if d["moment"][:10] >= flt["moment>"][1:11]]
        if m := re.fullmatch(r"report/counterparty/([\w-]+)", path):
            return {"balance": self.balances.get(m.group(1), 0)}
        if path == "entity/counterparty":
            name = flt.get("name~", "").lower()
            return [c for c in self.counterparties if name in c["name"].lower()]
        return httpx.Response(404, json={"errors": [{"error": f"нет {path}"}]})

    # ── заполнение ───────────────────────────────────────────────────────────

    def item(self, id_, name, stock, cost_rub, outflow=(), in_stock_report=True):
        if in_stock_report:
            self.stock.append({**ref("product", id_, expand=True), "name": name, "stock": stock,
                               "reserve": 0, "inTransit": 0, "price": cost_rub * 100,
                               "salePrice": cost_rub * 200, "uom": {"name": "шт"}})
        self.products.append({**ref("product", id_), "id": id_, "name": name,
                              "buyPrice": {"value": cost_rub * 100}})
        self.ops[f"{BASE}/entity/product/{id_}"] = [
            {"operation": {"meta": {"type": t}, "moment": "2026-09-10 10:00:00.000"}, "quantity": qty}
            for t, qty in outflow]


@pytest.fixture
def reports():
    return FakeReports()


@pytest.fixture
async def tools(reports):
    ms = MoySklad("test-token", transport=httpx.MockTransport(reports.handle))
    yield Tools(ms, internal=["ИНТЕРНЕТ РЕШЕНИЯ"], today=lambda: TODAY)
    await ms.close()


# ── инструменты ──────────────────────────────────────────────────────────────

async def test_reorder_counts_stock_outflow_and_zero_stock_items(tools, reports):
    # история — 3 полных месяца до ноября: 01.08–31.10, 92 дня
    reports.item("caramel", "Сироп Карамель 700 мл", stock=2, cost_rub=601,
                 outflow=[("demand", -60), ("demand", -30), ("salesreturn", 5)])
    reports.item("plum", "Сироп Слива 700 мл", stock=50, cost_rub=483, outflow=[("demand", -46)])
    reports.item("peach", "Сироп Персик 700 мл", stock=0, cost_rub=548,
                 outflow=[("demand", -30), ("processing", -4)], in_stock_report=False)
    reports.item("orchid", "Сироп Орхидея 700 мл", stock=1, cost_rub=601)

    r = json.loads(await call(tools, "reorder", {"search": "сироп 700", "days": 30, "min_qty": 2}))
    order = {x["name"]: x for x in r["order"]}
    # 90 шт за 92 дня → на 30 дней 29,3 − остаток 2 → 28; возврат покупателя расходом не считается
    assert order["Сироп Карамель 700 мл"]["order"] == 28
    # нулевой остаток не попадает в отчёт остатков — позицию добирает поиск по товарам
    assert order["Сироп Персик 700 мл"]["order"] == 12 and order["Сироп Персик 700 мл"]["cost"] == 548
    assert r["enough"] == [{"name": "Сироп Слива 700 мл", "stock": 50, "days_left": 100}]
    assert r["unused"] == ["Сироп Орхидея 700 мл"]
    assert r["history"] == "2026-08-01..2026-10-31"
    assert r["order_total_sum"] == 28 * 601 + 12 * 548


async def test_reorder_rounds_to_pack_and_minimum(tools, reports):
    reports.item("herb", "(С) Корень валерианы", stock=0, cost_rub=850, outflow=[("processing", -3)])
    r = await tools.reorder("валериан", days=15, round_to=0.5, min_qty=1)
    assert r["order"][0]["order"] == 1             # 0,49 кг → кратно 0,5 → 0,5 → минимум 1
    r = await tools.reorder("валериан", days=15, round_to=0.5)
    assert r["order"][0]["order"] == 0.5


async def test_sales_exclude_internal_by_subtraction(tools, reports):
    ir = f"{BASE}/entity/counterparty/ir"
    reports.profit[("bycounterparty", None)] = [
        {"counterparty": {**ref("counterparty", "cafe"), "name": "ООО «Кофейня»"},
         "sellSum": 100000, "sellCostSum": 40000, "returnSum": 0, "profit": 60000},
        {"counterparty": {**ref("counterparty", "ir"), "name": "ООО \"ИНТЕРНЕТ РЕШЕНИЯ\" ПЕРЕМЕЩНИЕ"},
         "sellSum": 50000, "sellCostSum": 10000, "returnSum": 0, "profit": 40000}]
    coffee = {**ref("product", "coffee"), "name": "Кофе Brazil 1 кг"}
    reports.profit[("byproduct", None)] = [
        {"assortment": coffee, "sellQuantity": 10, "sellSum": 150000, "sellCostSum": 50000,
         "returnQuantity": 0, "returnSum": 0, "profit": 100000}]
    reports.profit[("byproduct", ir)] = [
        {"assortment": coffee, "sellQuantity": 3, "sellSum": 50000, "sellCostSum": 10000,
         "returnQuantity": 0, "returnSum": 0, "profit": 40000}]

    r = await tools.sales("2026-10-01", "2026-10-31", organization="Краснояров Данил")
    # «Краснояров Данил» подходит к двум организациям — берётся основная, без «(ОЗОН БАНК)»
    assert r["organization"] == "ИП Краснояров Данил Сергеевич"
    assert r["total"]["revenue"] == 1000 and r["total"]["profit"] == 600 and r["total"]["qty"] == 7
    assert r["excluded_revenue"] == 500 and r["total"]["margin_pct"] == 60.0
    # фильтр «!=» МойСклад не понимает — исключение только вычитанием
    assert all("!" not in k for _, flt in reports.calls for k in flt)

    with_ir = await tools.sales("2026-10-01", "2026-10-31", include_internal=True)
    assert with_ir["total"]["revenue"] == 1500 and with_ir["excluded_counterparties"] == []
    by_cp = await tools.sales("2026-10-01", "2026-10-31", group_by="counterparty")
    assert [x["name"] for x in by_cp["rows"]] == ["ООО «Кофейня»"]


async def test_unpaid_terms_overdue_and_unlinked_payments(tools, reports):
    def demand(num, day, agent, sum_rub, paid_rub=0, term=None):
        reports.demands.append({
            **ref("demand", num), "name": num, "moment": f"{day} 10:00:00.000", "sum": sum_rub * 100,
            "payedSum": paid_rub * 100, "applicable": True, "agent": agent,
            "attributes": [{"name": "Оплата", "value": {"name": term}}] if term else []})
        reports.demands[-1]["meta"]["uuidHref"] = f"https://online.moysklad.ru/app/#demand/edit?id={num}"

    cafe = {**ref("counterparty", "cafe"), "id": "cafe", "name": "ООО «Кофейня»", "attributes": []}
    bar = {**ref("counterparty", "bar"), "id": "bar", "name": "ООО «Бар»",
           "attributes": [{"name": "Оплата", "value": {"name": "Отсрочка 14 кд"}}]}
    ir = {**ref("counterparty", "ir"), "id": "ir", "name": "ООО \"ИНТЕРНЕТ РЕШЕНИЯ\"", "attributes": []}
    demand("D1", "2026-10-20", cafe, 1000, term="Отсрочка 7 кд")         # срок 27.10 — просрочка 7 дн
    demand("D2", "2026-10-30", cafe, 500, term="Предоплата")             # предоплата — просрочка 4 дн
    demand("D3", "2026-10-25", bar, 2000)                                # срок с контрагента: 08.11
    demand("D4", "2026-10-25", bar, 800, paid_rub=799.9)                 # 10 копеек — не долг
    demand("D5", "2026-10-21", ir, 9000)                                 # перемещение — не долг
    reports.balances = {"cafe": -150000, "bar": 50000}                   # бар заплатил, не привязав

    r = await tools.unpaid("2026-10-01")
    assert [x["number"] for x in r["rows"]] == ["D1", "D2", "D3"]
    d1, d2, d3 = r["rows"]
    assert (d1["overdue_days"], d1["due"], d1["term_from"]) == (7, "2026-10-27", "отгрузка")
    assert d2["overdue_days"] == 4
    assert (d3["term"], d3["term_from"], d3["overdue_days"]) == ("Отсрочка 14 кд", "контрагент", -5)
    assert d1["link"].endswith("id=D1")
    by = {x["counterparty"]: x for x in r["by_counterparty"]}
    assert by["ООО «Кофейня»"]["real_debt"] == 1500 and by["ООО «Бар»"]["real_debt"] == 0
    assert r["real_debt"] == 1500 and r["debt_by_shipments"] == 3500

    overdue = await tools.unpaid("2026-10-01", only_overdue=True)
    assert [x["number"] for x in overdue["rows"]] == ["D1", "D2"]


async def test_call_validates_names_and_params(tools):
    with pytest.raises(ToolError, match="нет инструмента"):
        await call(tools, "delete_everything", {})
    with pytest.raises(ToolError, match="лишние"):
        await call(tools, "stock", {"search": "x", "drop": True})
    with pytest.raises(ToolError, match="не хватает"):
        await call(tools, "sales", {"date_from": "2026-10-01"})
    with pytest.raises(ToolError, match="не найдена"):
        await tools.org("Рога и копыта")
    names = {d["name"] for d in definitions()}
    assert names == {"stock", "sales", "sales_by_month", "reorder", "supplier_prices",
                     "tech_card", "unpaid", "product"}


# ── разговор с Claude ────────────────────────────────────────────────────────

def message(*blocks, stop="end_turn"):
    return BetaMessage.model_validate({
        "id": "msg", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": list(blocks), "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 80,
                  "cache_creation_input_tokens": 0}})


THINK = {"type": "thinking", "thinking": "", "signature": "sig"}


class FakeClaude:
    """client.beta.messages.create по сценарию; запоминает каждый запрос (копией)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []
        self.beta = self
        self.messages = self

    async def create(self, **kw):
        self.requests.append(json.loads(json.dumps(kw, ensure_ascii=False, default=str)))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def agent_with(claude, tools, **kw):
    return Agent(claude, tools, today=lambda: TODAY, **kw)


async def test_agent_runs_tools_and_keeps_history_append_only(tools, reports):
    reports.item("caramel", "Сироп Карамель 700 мл", stock=4, cost_rub=601)
    claude = FakeClaude(
        message(THINK, {"type": "tool_use", "id": "t1", "name": "stock", "input": {"search": "карамель"}},
                stop="tool_use"),
        message(THINK, {"type": "text", "text": "Карамели 4 шт."}),
        message({"type": "text", "text": "Себестоимость 601 ₽."}))
    agent = agent_with(claude, tools)

    assert await agent.ask("42", "сколько карамели?") == "Карамели 4 шт."
    first, second = claude.requests[:2]
    assert first["messages"] == [{"role": "user", "content": "[2026-11-03 вт] сколько карамели?"}]
    assert first["model"] == "claude-opus-5-5" and first["output_config"] == {"effort": "medium"}
    assert first["fallbacks"] == "default" and first["betas"] == ["server-side-fallback-2026-07-01"]
    assert "thinking" not in first and first["cache_control"] == {"type": "ephemeral"}
    # ответ модели вернулся в историю целиком, вместе с блоком размышлений
    assert second["messages"][1]["content"][0] == THINK
    result = second["messages"][2]["content"][0]
    assert result["tool_use_id"] == "t1" and not result["is_error"]
    assert json.loads(result["content"])["rows"][0]["stock"] == 4

    await agent.ask("42", "а почём?")
    third = claude.requests[2]
    # история только дописывается: прошлый ход — неизменный префикс; system и tools те же
    assert third["messages"][:4] == second["messages"] + [third["messages"][3]]
    assert third["messages"][4]["content"] == "[2026-11-03 вт] а почём?"
    assert third["system"] == first["system"] and third["tools"] == first["tools"]


async def test_agent_tool_errors_go_back_to_model(tools):
    claude = FakeClaude(
        message({"type": "tool_use", "id": "t1", "name": "sales",
                 "input": {"date_from": "2026-10-01", "date_to": "2026-10-31", "organization": "Рога"}},
                stop="tool_use"),
        message({"type": "text", "text": "Такой организации нет."}))
    assert await agent_with(claude, tools).ask("1", "выручка Рога") == "Такой организации нет."
    result = claude.requests[1]["messages"][2]["content"][0]
    assert result["is_error"] and "не найдена" in result["content"]


async def test_agent_refusal_and_api_errors(tools):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    overloaded = anthropic.RateLimitError("rate", response=httpx2.Response(429, request=req), body=None)
    claude = FakeClaude(overloaded, message(stop="refusal"),
                        message({"type": "text", "text": "Привет"}))
    agent = agent_with(claude, tools)
    with pytest.raises(BotError, match="перегружен"):
        await agent.ask("7", "раз")
    assert agent.chats["7"].messages == []           # неудачный ход откатан целиком
    assert "заново" in await agent.ask("7", "два")   # отказ — разговор начинается заново
    assert "7" not in agent.chats
    assert await agent.ask("7", "три") == "Привет"
    assert claude.requests[-1]["messages"] == [{"role": "user", "content": "[2026-11-03 вт] три"}]


async def test_agent_starts_new_conversation_when_idle(tools):
    now = [0.0]
    claude = FakeClaude(*(message({"type": "text", "text": "ок"}) for _ in range(3)))
    agent = agent_with(claude, tools, clock=lambda: now[0], idle_reset=3600, model="claude-haiku-5-5")
    await agent.ask("5", "раз")
    now[0] = 600
    await agent.ask("5", "два")
    assert len(claude.requests[1]["messages"]) == 3
    now[0] = 600 + 3601
    await agent.ask("5", "три")
    assert len(claude.requests[2]["messages"]) == 1
    assert "fallbacks" not in claude.requests[0]          # у Haiku серверного fallback нет


# ── Telegram ─────────────────────────────────────────────────────────────────

class FakeTelegram:
    def __init__(self, updates):
        self.updates = updates
        self.sent = []
        self.commands = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        assert "/botTOKEN/" in request.url.path
        body = json.loads(request.content or b"{}")
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 999, "username": "ms_hub_bot"}})
        if method == "setMyCommands":
            self.commands = body["commands"]
        if method == "getUpdates":
            ups, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": ups})
        if method == "sendMessage":
            self.sent.append((str(body["chat_id"]), body["text"],
                              (body.get("reply_parameters") or {}).get("message_id")))
        return httpx.Response(200, json={"ok": True, "result": True})


class StubAgent:
    def __init__(self, answer="ответ"):
        self.asked, self.resets = [], []
        self.answer = answer

    async def ask(self, chat, text):
        self.asked.append((chat, text))
        return self.answer

    def reset(self, chat):
        self.resets.append(chat)


class MemoryStore:
    def __init__(self):
        self.data = {}

    async def get(self, key, default=None):
        return self.data.get(key, default)

    async def set(self, key, value):
        self.data[key] = value


def update(n, chat, text, *, group=False, user=1, reply_to_bot=False, migrate=None):
    msg = {"message_id": n * 10, "chat": {"id": chat, "type": "supergroup" if group else "private",
                                          "title": "Склад"},
           "from": {"id": user}, "text": text}
    if reply_to_bot:
        msg["reply_to_message"] = {"from": {"id": 999}}
    if migrate:
        msg = {"chat": msg["chat"], "migrate_to_chat_id": migrate}
    return {"update_id": n, "message": msg}


async def make_bot(updates, allowed=(), code="", agent=None, storage=None):
    tg = FakeTelegram(updates)
    bot = TelegramBot("TOKEN", agent or StubAgent(), set(allowed), code=code, storage=storage,
                      http=httpx.AsyncClient(transport=httpx.MockTransport(tg.handle)))
    await bot.start()
    return tg, bot


async def drain(bot):
    n = await bot.poll_once()
    await asyncio.gather(*bot._tasks)
    return n


async def test_telegram_private_whitelist_and_long_answers():
    agent = StubAgent("ответ " + "x" * 5000)
    tg, bot = await make_bot([update(1, 100, "остатки сиропов"), update(2, 666, "покажи долги"),
                              update(3, 100, "/new"), update(4, 100, "/start")],
                             allowed={"100"}, agent=agent)
    assert await drain(bot) == 4
    assert agent.asked == [("100", "остатки сиропов")]          # чужой чат до модели не доходит
    assert agent.resets == ["100"]
    to_owner = [t for c, t, _ in tg.sent if c == "100"]
    assert sum(len(t) for t in to_owner) > 5000
    assert all(len(t) <= 4000 for _, t, _ in tg.sent)            # длинный ответ — частями
    assert [t for c, t, _ in tg.sent if c == "666"] == [
        "Нет доступа. Chat id: 666 — его добавляют в HUB_BOT_CHATS."]
    assert any("Примеры вопросов" in t for t in to_owner)
    assert [c["command"] for c in tg.commands] == ["ask", "new", "help"]
    assert bot._offset == 5
    await bot.close()


async def test_group_connects_by_code_and_answers_only_when_addressed():
    store = MemoryStore()
    agent = StubAgent()
    g = -100500
    tg, bot = await make_bot([
        update(1, g, "/ask остатки", group=True),                 # группа ещё не подключена
        update(2, g, "/start неверный", group=True),
        update(3, g, "привет всем", group=True),                  # переписка — молчим
        update(4, g, "/start s3cret-code@x", group=True),         # не тот код
        update(5, g, "/start s3cret-code", group=True),           # подключили
        update(6, 77, "/start s3cret-code"),                      # в личке код не действует
    ], code="s3cret-code", agent=agent, storage=store)
    await drain(bot)
    assert agent.asked == []
    texts = [(c, t) for c, t, _ in tg.sent]
    assert texts[0] == (str(g), "Группа не подключена. Администратор: отправьте здесь /start <код доступа>.")
    assert any(t.startswith("Группа подключена.") for _, t in texts)
    assert texts[-1] == ("77", "Нет доступа. Chat id: 77 — его добавляют в HUB_BOT_CHATS.")
    assert json.loads(store.data["bot:chats"]) == [str(g)]

    tg.updates = [
        update(7, g, "/ask остатки сиропов", group=True, user=1),
        update(8, g, "/ask@ms_hub_bot выручка за октябрь", group=True, user=2),
        update(9, g, "@ms_hub_bot, долги с 1 октября", group=True, user=2),
        update(10, g, "а по ИП А.И.?", group=True, user=2, reply_to_bot=True),
        update(11, g, "просто болтаем", group=True, user=3),
        update(12, g, "/ask@other_bot что-то", group=True, user=3),
        update(13, g, "/new", group=True, user=2),
    ]
    tg.sent.clear()
    await drain(bot)
    # у каждого участника своя история: ключ «группа:пользователь»
    assert sorted(agent.asked) == sorted([(f"{g}:1", "остатки сиропов"), (f"{g}:2", "выручка за октябрь"),
                                          (f"{g}:2", "долги с 1 октября"), (f"{g}:2", "а по ИП А.И.?")])
    assert agent.resets == [f"{g}:2"]
    assert {m for _, _, m in tg.sent} == {70, 80, 90, 100, 130}   # ответ — реплаем на вопрос
    await bot.close()

    # после перезапуска группа подключена из базы; стала супергруппой — id переехал
    tg2, bot2 = await make_bot([update(14, g, "", group=True, migrate=-100777),
                                update(15, -100777, "/ask остатки", group=True)],
                               code="s3cret-code", agent=agent, storage=store)
    assert str(g) in bot2.allowed
    await drain(bot2)
    assert json.loads(store.data["bot:chats"]) == ["-100777"]
    assert agent.asked[-1] == ("-100777:1", "остатки")
    await bot2.close()


def test_chunks_split_on_lines():
    text = "\n".join(f"строка {i}" for i in range(1000))
    parts = chunks(text, 500)
    assert all(len(p) <= 500 for p in parts) and "\n".join(parts) == text


# ── POST /api/ask ────────────────────────────────────────────────────────────

def test_api_ask(fake):
    from conftest import RecordingChannels, fresh_storage, settings_for
    from fastapi.testclient import TestClient

    from hub.api import create_app
    from hub.runtime import Hub

    s = settings_for({})

    async def factory():
        return Hub(s, await fresh_storage(), ms=MoySklad("test-token", transport=fake.transport()),
                   channels=RecordingChannels(s))

    with TestClient(create_app(s, background=False, factory=factory)) as client:
        h = {"X-Api-Key": "k"}
        assert client.post("/api/ask", json={"question": "остатки"}).status_code == 401
        assert client.post("/api/ask", json={"question": "остатки"}, headers=h).status_code == 503
        s.anthropic_ready = True
        client.app.state.hub._agent = agent = StubAgent()
        r = client.post("/api/ask", json={"question": "остатки", "chat": "n8n"}, headers=h)
        assert r.status_code == 200 and r.json()["answer"].startswith("ответ")
        assert agent.asked == [("api:n8n", "остатки")]


def test_bot_settings_from_env(tmp_path):
    from hub.settings import Settings
    cfg = tmp_path / "config.yaml"
    cfg.write_text("bot:\n  internal_counterparties: [ОЗОН]\n", encoding="utf-8")
    env = {"HUB_CONFIG": str(cfg), "TELEGRAM_BOT_TOKEN": "t", "HUB_BOT_CHATS": " 1, -100200 ,"}
    s = Settings.from_env(env)
    assert s.bot_chats == ("1", "-100200") and not s.bot_ready      # нет ключа Anthropic
    only_code = Settings.from_env({"HUB_CONFIG": str(cfg), "TELEGRAM_BOT_TOKEN": "t",
                                   "ANTHROPIC_API_KEY": "x", "HUB_BOT_CODE": " код "})
    assert only_code.bot_ready and only_code.bot_code == "код"     # хватает кода группы
    s = Settings.from_env({**env, "ANTHROPIC_API_KEY": "x", "HUB_BOT_EFFORT": "low"})
    assert s.bot_ready and s.bot_effort == "low" and s.bot_model == "claude-opus-5-5"
    assert s.bot == {"internal_counterparties": ["ОЗОН"]}


async def test_bot_role_requires_settings(make_hub):
    hub = await make_hub({})
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        await hub.start("bot")
    await hub.start("all")                      # в роли all бот без настроек просто не запускается
    assert [t.get_name() for t in hub._tasks] == ["worker", "scheduler"]
    for t in hub._tasks:
        t.cancel()
    await asyncio.gather(*hub._tasks, return_exceptions=True)

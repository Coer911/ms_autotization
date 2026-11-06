"""Командная строка: python -m hub <команда>.

  serve [--role all|api|worker|scheduler]  — запуск (так стартует Docker)
  check                                     — токен, config.yaml, статусы, шаблоны, поля, вебхуки
  states|templates|fields <тип>             — справочники: orders, shipments, counterparties
  setup                                     — создать доп. поля-отметки, которых не хватает
  webhooks list|install|remove              — вебхуки МойСклад на нужные типы
  poll                                      — один круг: опрос + очередь (вместо serve, по cron)
  run <автоматизация> <номер|id|ИНН> [--force]
  set-state <тип> <ref> <статус>            — сменить статус руками
  set-field <тип> <ref> <поле> <значение>   — записать доп. поле руками
  contracts number [--apply] [--since Д]   — номера договоров: «ПК» → «ПК-ДД/ММ/ГГ-NN»
  journal [-n 20] [--status failed]
  tool <инструмент> '<json>'                — функция бота без LLM: stock, sales, reorder, unpaid…
  ask "<вопрос>"                            — спросить бота из терминала (нужен ANTHROPIC_API_KEY)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .automations import ConfigError, build, event_types
from .entities import TYPES, from_api, resolve_type
from .events import Event
from .moysklad import FieldError, MoySklad, MoySkladError
from .moysklad import fields as ms_fields
from .settings import ROLES, Settings

HOOK_ACTIONS = (("CREATE", None), ("UPDATE", "FIELDS"), ("DELETE", None))
MSK = ZoneInfo("Europe/Moscow")


def ok(text: str) -> None:
    print(f"  ✅ {text}")


def bad(text: str) -> None:
    print(f"  ❌ {text}")


def warn(text: str) -> None:
    print(f"  ⚠️  {text}")


def client(s: Settings) -> MoySklad:
    return MoySklad(s.ms_token, s.ms_base, rps=s.ms_rps)


async def find_one(ms: MoySklad, type_: str, ref: str):
    cls = TYPES[type_]
    if len(ref) == 36 and ref.count("-") == 4:
        return from_api(type_, await ms.entity(type_, ref, cls.expand))
    flt = f"inn={ref}" if type_ == "counterparty" and ref.isdigit() else f"name={ref}"
    rows = await ms.find(type_, flt, cls.expand)
    if len(rows) != 1:
        raise FieldError(f"{cls.title} «{ref}»: " + ("не найден" if not rows else "найден не один"))
    return from_api(type_, rows[0])


async def cmd_check(s: Settings) -> int:
    errors = 0
    print("Настройки")
    if s.dry_run:
        warn("HUB_DRY_RUN: отправки и записи в МойСклад выключены, только журнал")
    else:
        ok("боевой режим")
    autos = build(s.automations)
    ok("config.yaml: " + (", ".join(a.name for a in autos) or "автоматизаций не включено"))
    ok(f"база: {s.database_url.split('://')[0]}, роль {s.role}")
    for name, value, need in (("HUB_API_KEY", s.api_key, "REST API выключен"),
                              ("HUB_WEBHOOK_SECRET", s.webhook_secret, "вебхуки не принимаются"),
                              ("HUB_PUBLIC_URL", s.public_url, "вебхуки не установить")):
        (ok if value else warn)(f"{name} {'задан' if value else 'не задан — ' + need}")
    print("МойСклад")
    async with client(s) as ms:
        me = await ms.me()
        ok(f"токен работает: {me.get('name') or me.get('uid')}")
        for a in autos:
            p = a.params
            if a.scheduled:
                ok(f"{a.name}: по расписанию в {p.get('at')}")
                continue
            for key, type_ in (("state", a.entity), ("state_after", a.entity),
                               ("order_state", "customerorder"), ("set_state", a.entity)):
                if p.get(key):
                    try:
                        await ms_fields.find_state(ms, type_, p[key])
                        ok(f"{a.name}: статус «{p[key]}» ({type_})")
                    except FieldError as e:
                        bad(f"{a.name}: {e}")
                        errors += 1
            if p.get("template"):
                try:
                    tpl = await ms.find_template(a.entity, p["template"])
                    ok(f"{a.name}: шаблон «{tpl['name']}»")
                except MoySkladError as e:
                    bad(f"{a.name}: {e.message}")
                    errors += 1
            names = ([p["mark_field"]] if p.get("mark_field") else []) + list(p.get("fields") or {})
            for name in names:
                try:
                    await ms_fields.find_definition(ms, a.entity, name)
                    ok(f"{a.name}: доп. поле «{name}»")
                except FieldError as e:
                    bad(f"{a.name}: {e}" + (" — python -m hub setup" if name == p.get("mark_field") else ""))
                    errors += 1
        hooks = await ms.webhooks()
        for type_ in event_types(autos):
            mine = [h for h in hooks if h.get("entityType") == type_ and h.get("url") == s.webhook_url]
            if s.webhook_url and len(mine) == len(HOOK_ACTIONS):
                ok(f"вебхуки {type_} ведут сюда")
            else:
                warn(f"вебхуков {type_} на хаб нет — работает только опрос (python -m hub webhooks install)")
    print("Каналы")
    (ok if s.smtp_host else warn)(f"почта: {s.smtp_user or s.smtp_from}@{s.smtp_host}:{s.smtp_port}"
                                  if s.smtp_host else "почта не настроена (SMTP_HOST)")
    (ok if s.telegram_token else warn)("Telegram: токен задан" if s.telegram_token
                                       else "Telegram не настроен (TELEGRAM_BOT_TOKEN)")
    print("\nГотово к работе" if not errors else f"\nПроблем: {errors}")
    return 1 if errors else 0


async def cmd_reference(s: Settings, what: str, kind: str) -> int:
    type_ = resolve_type(kind).type
    async with client(s) as ms:
        if what == "states":
            for st in await ms_fields.states(ms, type_):
                print(f"  {st['name']}  ({st.get('stateType', '')})")
        elif what == "templates":
            for t in await ms.templates(type_):
                print(f"  [{t['meta']['type']}] {t['name']}")
        else:
            for d in await ms_fields.definitions(ms, type_):
                print(f"  {d['name']}  — {d['type']}{' (обязательное)' if d.get('required') else ''}")
    return 0


async def cmd_setup(s: Settings) -> int:
    need = {(a.entity, a.params["mark_field"]) for a in build(s.automations)
            if not a.scheduled and a.params.get("mark_field")}
    async with client(s) as ms:
        for type_, name in sorted(need):
            try:
                await ms_fields.find_definition(ms, type_, name)
                ok(f"доп. поле «{name}» ({type_}) уже есть")
            except FieldError:
                await ms.post(f"entity/{type_}/metadata/attributes",
                              {"name": name, "type": "string", "required": False})
                ms.forget("meta:")
                ok(f"создано доп. поле «{name}» ({type_}, строка)")
    if not need:
        ok("отметки не нужны ни одной автоматизации")
    return 0


async def cmd_webhooks(s: Settings, action: str) -> int:
    types = event_types(build(s.automations))
    async with client(s) as ms:
        hooks = [h for h in await ms.webhooks() if h.get("entityType") in TYPES]
        if action == "list":
            for h in hooks:
                mine = " ← хаб" if h["url"] == s.webhook_url else ""
                print(f"  {h['entityType']:<14} {h['action']:<7} {'on ' if h.get('enabled') else 'off'}"
                      f" {h['url']}{mine}")
            return 0
        if not s.webhook_url:
            bad("нужны HUB_PUBLIC_URL и HUB_WEBHOOK_SECRET")
            return 1
        if action == "install":
            for type_ in types:
                have = {h["action"] for h in hooks if h["entityType"] == type_ and h["url"] == s.webhook_url}
                for act, diff in HOOK_ACTIONS:
                    if act in have:
                        ok(f"{type_} {act} уже стоит")
                    else:
                        await ms.create_webhook(s.webhook_url, type_, act, diff)
                        ok(f"{type_} {act} → {s.public_url}/hooks/moysklad/***")
        else:
            for h in hooks:
                if h["url"] == s.webhook_url:
                    await ms.delete_webhook(h["id"])
                    ok(f"снят {h['entityType']} {h['action']}")
    return 0


async def with_hub(s: Settings, work):
    from .runtime import Hub
    hub = await Hub.create(s)
    try:
        return await work(hub)
    finally:
        await hub.stop()


async def cmd_poll(s: Settings) -> int:
    async def work(hub):
        added = await hub.poll_once()
        outcomes = await hub.tick() + await hub.engine.scheduled()
        print(f"новых событий: {added}")
        for o in outcomes:
            print(f"  {o.automation} {o.target}: {o.status} {o.detail}")
        return 0
    return await with_hub(s, work)


async def cmd_run(s: Settings, name: str, ref: str, force: bool) -> int:
    async def work(hub):
        a = hub.engine.by_name.get(name)
        if not a:
            bad(f"автоматизация {name} не включена в config.yaml")
            return 1
        if a.scheduled:
            slot = ref or datetime.now(MSK).strftime("%Y-%m-%d")
            key = hub.ctx.run_key(a, f"slot:{slot}")
            await hub.storage.try_start(a.name, key, entity="schedule", entity_id=slot,
                                        label=f"{a.name} {slot}", lease=hub.settings.lease_seconds,
                                        force=True)
            o = await hub.engine._run(a, key, f"{a.name} {slot}",
                                      lambda: a.run_scheduled(hub.ctx, slot))
            print(f"  {o.status}: {o.detail}")
            return 0
        obj = await find_one(hub.ms, a.entity, ref)
        print(f"{obj.label} · {obj.state}" + (f" · {obj.agent.name} · {obj.sum_text} ₽"
                                               if hasattr(obj, "agent") else ""))
        event = Event(entity=obj.type, entity_id=obj.id, action="MANUAL", source="cli")
        if not force and not a.wants(obj, event):
            bad("условия автоматизации не выполнены — --force, чтобы всё равно")
            return 1
        o = await hub.engine.execute(a, obj, event, mode="force" if force else "normal")
        print(f"  {o.status}: {o.detail}")
        for item in hub.channels.sent + hub.ctx.writes:
            print(f"  → {item}")
        return 0 if o.status in ("done", "already", "skipped") else 1
    return await with_hub(s, work)


async def cmd_set(s: Settings, kind: str, ref: str, what: str, args: list[str]) -> int:
    type_ = resolve_type(kind).type
    async with client(s) as ms:
        obj = await find_one(ms, type_, ref)
        if what == "state":
            await ms_fields.set_state(ms, type_, obj.id, args[0])
            ok(f"{obj.label}: статус «{args[0]}»")
        else:
            await ms_fields.set_fields(ms, type_, obj.id, {args[0]: args[1]})
            ok(f"{obj.label}: «{args[0]}» = {args[1]}")
    return 0


async def cmd_contracts(s: Settings, apply: bool, since: str = "") -> int:
    from datetime import timedelta

    from . import contracts
    since = since or (datetime.now(MSK) - timedelta(days=30)).strftime("%Y-%m-%d")
    async with client(s) as ms:
        rows = await contracts.load(ms)
        for c in contracts.stale(rows, since):
            warn(f"«{c['name']}» от {c['moment'][:10]} · {(c.get('agent') or {}).get('name', '')}: "
                 f"старше {since}, не трогаю — номер вручную или --since")
        todo = contracts.plan(rows, since)
        if not todo:
            ok("договоров с номером из одной серии (" + ", ".join(contracts.SERIES) + ") нет")
            return 0
        for r in todo:
            print(f"  «{r.old}» от {r.moment} · {r.agent} → {r.new}")
        if not apply:
            warn(f"пробный прогон: ничего не записано. Записать {len(todo)} — с --apply")
            return 0
        await contracts.apply(ms, todo)
        ok(f"записано номеров: {len(todo)}")
    return 0


async def cmd_journal(s: Settings, n: int, status: str) -> int:
    from .storage import Storage
    storage = await Storage.open(s.database_url)
    try:
        for r in await storage.journal(n, status=status):
            tail = r.error if r.status in ("failed", "dead") else r.result
            print(f"  {r.updated_at} {r.automation:<16} {r.label or r.entity_id:<22} {r.status:<8}"
                  f" {tail or ''}")
    finally:
        await storage.close()
    return 0


async def cmd_tool(s: Settings, name: str, raw: str) -> int:
    import json

    from .bot import make_tools
    from .bot.tools import ToolError, call
    async with client(s) as ms:
        try:
            out = await call(make_tools(s, ms), name, json.loads(raw or "{}"))
        except (ToolError, json.JSONDecodeError) as e:
            bad(str(e))
            return 1
    print(json.dumps(json.loads(out), ensure_ascii=False, indent=1))
    return 0


async def cmd_ask(s: Settings, question: str) -> int:
    from .bot import BotError, make_agent
    if not s.anthropic_ready:
        bad("не задан ANTHROPIC_API_KEY")
        return 1
    async with client(s) as ms:
        try:
            print(await make_agent(s, ms).ask("cli", question))
        except BotError as e:
            bad(str(e))
            return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m hub", description="Хаб автоматизаций МойСклад")
    sub = p.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve")
    sv.add_argument("--role", choices=ROLES)
    sub.add_parser("check")
    for name in ("states", "templates", "fields"):
        sub.add_parser(name).add_argument("kind", help="orders | shipments | counterparties")
    sub.add_parser("setup")
    sub.add_parser("webhooks").add_argument("action", choices=["list", "install", "remove"])
    sub.add_parser("poll")
    r = sub.add_parser("run")
    r.add_argument("automation")
    r.add_argument("ref", nargs="?", default="", help="номер, id или ИНН")
    r.add_argument("--force", action="store_true")
    st = sub.add_parser("set-state")
    for a in ("kind", "ref", "state"):
        st.add_argument(a)
    sf = sub.add_parser("set-field")
    for a in ("kind", "ref", "field", "value"):
        sf.add_argument(a)
    c = sub.add_parser("contracts", help="номера договоров")
    c.add_argument("action", choices=["number"])
    c.add_argument("--apply", action="store_true", help="записать в МойСклад (без него — только план)")
    c.add_argument("--since", default="", help="ГГГГ-ММ-ДД: нумеровать договоры не старше (по умолчанию 30 дней)")
    j = sub.add_parser("journal")
    j.add_argument("-n", type=int, default=20)
    j.add_argument("--status", default="")
    t = sub.add_parser("tool")
    t.add_argument("name", help="stock | sales | sales_by_month | reorder | supplier_prices | "
                                "tech_card | unpaid | product")
    t.add_argument("args", nargs="?", default="{}", help='JSON, например \'{"search": "сироп"}\'')
    sub.add_parser("ask").add_argument("question")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx пишет в INFO полный адрес запроса, а в адресах Telegram — токен бота
    logging.getLogger("httpx").setLevel(logging.WARNING)
    s = Settings.from_env()
    if args.cmd == "serve":
        import uvicorn

        from .api import create_app
        if args.role:
            s.role = args.role
        uvicorn.run(create_app(s), host="0.0.0.0", port=s.port, proxy_headers=True)
        return 0
    if args.cmd == "journal":
        return asyncio.run(cmd_journal(s, args.n, args.status))
    if not s.ms_token:
        bad("не задан MOYSKLAD_TOKEN")
        return 1
    commands = {
        "check": lambda: cmd_check(s),
        "states": lambda: cmd_reference(s, "states", args.kind),
        "templates": lambda: cmd_reference(s, "templates", args.kind),
        "fields": lambda: cmd_reference(s, "fields", args.kind),
        "setup": lambda: cmd_setup(s),
        "webhooks": lambda: cmd_webhooks(s, args.action),
        "poll": lambda: cmd_poll(s),
        "run": lambda: cmd_run(s, args.automation, args.ref, args.force),
        "set-state": lambda: cmd_set(s, args.kind, args.ref, "state", [args.state]),
        "tool": lambda: cmd_tool(s, args.name, args.args),
        "ask": lambda: cmd_ask(s, args.question),
        "set-field": lambda: cmd_set(s, args.kind, args.ref, "field", [args.field, args.value]),
        "contracts": lambda: cmd_contracts(s, args.apply, args.since),
    }
    try:
        return asyncio.run(commands[args.cmd]())
    except (ConfigError, FieldError, KeyError) as e:
        bad(str(e).strip("'\""))
    except MoySkladError as e:
        bad(str(e))
    except httpx.TransportError as e:
        bad(f"нет связи с МойСклад ({s.ms_base}): {e}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

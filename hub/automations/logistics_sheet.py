"""Маршрутный лист: сбор перехода и утренний неизменяемый XLSX на Диске.

Вебхук только проверяет переход и пишет заказ в базу хаба. В Диск на этом шаге
ничего не уходит. Дата маршрута — следующий пн–пт по Москве от auditContext.moment.
customerorder.updated датой перехода не считается. Нет момента — ручная проверка,
дату не угадываем.

В `at` (по умолчанию 07:00) планировщик собирает заказы этой даты в новый XLSX
и загружает его в папку. Уже лежащий файл не перезаписывается и не удаляется.
Поздний заказ на опубликованную дату остаётся в базе со статусом LATE_ORDER.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from ..channels.yandex_disk import DiskError
from ..entities import Entity
from ..events import Event
from ..moysklad import entity_id
from . import Automation, ConfigError, Context, Skip, register
from .route_xlsx import build_route_xlsx

log = logging.getLogger("hub.automation.logistics")
MSK = ZoneInfo("Europe/Moscow")
CANCELLED = frozenset({"отменён", "отменен", "отмена"})
DASH = "\u2014"


class RouteReport(Exception):
    """Нет адреса доставки: дата уже заморожена, строка в лист ещё не попала."""


def _norm(value: str | None) -> str:
    return (value or "").strip().casefold()


def parse_transition(value: str) -> datetime:
    """Строка МойСклад без пояса — уже Москва. ISO с Z или смещением переводится в Москву."""
    text = (value or "").strip()
    if not text:
        raise ValueError("пустой момент перехода")
    if "T" in text or text.endswith("Z") or _has_offset(text):
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=MSK)
        return parsed.astimezone(MSK)
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=MSK)
        except ValueError:
            continue
    raise ValueError("не разобран момент перехода")


def _has_offset(text: str) -> bool:
    return len(text) > 19 and text[-6] in "+-" and text[-3] == ":"


def route_date(moment: datetime) -> date:
    """Следующий пн–пт после календарной даты момента. Праздники не учитываются."""
    local = moment.astimezone(MSK) if moment.tzinfo else moment.replace(tzinfo=MSK)
    day = local.date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def day_text(day: date) -> str:
    return day.strftime("%d.%m.%Y")


def file_name(day: date) -> str:
    return f"Маршрутный лист {DASH} {day_text(day)}.xlsx"


def file_path(folder: str, day: date) -> str:
    base = str(folder).strip().rstrip("/")
    if not base.startswith("disk:"):
        base = "disk:" + (base if base.startswith("/") else f"/{base}")
    return f"{base}/{file_name(day)}"


def delivery_address(obj: Entity) -> str:
    """Адрес доставки заказа. Юридический и фактический адрес контрагента не используются."""
    raw = obj.raw or {}
    if "shipmentAddress" in raw:
        text = raw.get("shipmentAddress")
    else:
        text = getattr(obj, "shipment_address", "")
    text = str(text or "").strip()
    if text:
        return text
    full = raw.get("shipmentAddressFull") or {}
    if not isinstance(full, dict):
        return ""
    parts = []
    for key in ("postalCode", "country", "region", "city", "street", "house", "apartment", "addInfo"):
        value = full.get(key)
        if isinstance(value, dict):
            value = value.get("name") or ""
        value = str(value or "").strip()
        if value:
            parts.append(value)
    return ", ".join(parts)


def state_id_of(obj: Entity) -> str:
    state = (obj.raw or {}).get("state") or {}
    if state.get("id"):
        return str(state["id"])
    href = ((state.get("meta") or {}).get("href")) or ""
    return entity_id(href) if href else ""


def _order_key(order_id: str) -> str:
    return f"ls:order:{order_id}"


def _day_key(day: date) -> str:
    return f"ls:day:{day.isoformat()}"


def _xlsx_key(day: date) -> str:
    return f"ls:xlsx:{day.isoformat()}"


def _empty_day() -> dict:
    return {
        "status": "open", "orders": {}, "late": [], "frozen_ids": [],
        "publication_id": "", "file": None,
    }


def _load_day(raw: str | None) -> dict:
    return json.loads(raw) if raw else _empty_day()


def _dump(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False)


def _pointer(raw: str | None) -> dict | None:
    return json.loads(raw) if raw else None


def _freeze(raw: str | None) -> str:
    doc = _load_day(raw)
    if doc["status"] == "open":
        missed = [oid for oid, order in doc["orders"].items()
                  if not str(order.get("address") or "").strip()]
        for oid in missed:
            doc["orders"].pop(oid, None)
            if oid not in doc["late"]:
                doc["late"].append(oid)
        ready = list(doc["orders"].values())
        ready.sort(key=lambda order: (order.get("transition_at") or "", order["id"]))
        doc["frozen_ids"] = [order["id"] for order in ready]
        doc["publication_id"] = doc.get("publication_id") or str(uuid4())
        doc["status"] = "publishing" if doc["frozen_ids"] else "published"
    return _dump(doc)


def _upsert(raw: str | None, order: dict) -> str:
    doc = _load_day(raw)
    oid = order["id"]
    if doc["status"] != "open":
        if oid not in doc.get("frozen_ids", []) and oid not in doc["late"]:
            doc["late"].append(oid)
        return _dump(doc)
    doc["orders"][oid] = order
    return _dump(doc)


def _save_file(raw: str | None, file: dict) -> str:
    doc = _load_day(raw)
    current = doc.get("file")
    if current:
        if current.get("md5") != file["md5"]:
            raise DiskError(409, "контрольная сумма публикации уже другая, не перезаписываю")
        return _dump(doc)
    doc["file"] = file
    doc["status"] = "published"
    return _dump(doc)


def _parsed_moment(moment: str) -> datetime | None:
    if not moment:
        return None
    try:
        return parse_transition(moment)
    except ValueError:
        return None


async def _write_pointer(ctx: Context, order_id: str, status: str,
                         day: date | None, moment: str | None) -> None:
    if ctx.settings.dry_run:
        return
    await ctx.storage.set(_order_key(order_id), json.dumps({
        "status": status,
        "route_date": day.isoformat() if day else None,
        "transition_at": moment,
    }, ensure_ascii=False))


@register
class LogisticsSheet(Automation):
    kind = "logistics_sheet"
    title = "Маршрутный лист"
    entities = ("customerorder",)
    defaults = {
        "entity": "customerorder",
        "state": "ПОДТВЕРЖДЕН / ОЖИДАЕТ СБОРКИ",
        "state_id": "",
        "hours_field": "",
        "phone_field": "",
        "notify_chats": [],
    }

    def validate(self) -> None:
        if not str(self.state).strip():
            raise ConfigError(f"{self.name}: нужен state")
        if not isinstance(self.notify_chats, list):
            raise ConfigError(f"{self.name}: notify_chats — список")

    def _target(self, obj: Entity) -> bool:
        current = state_id_of(obj)
        if self.state_id and current:
            return current == str(self.state_id).strip()
        return _norm(obj.state) == _norm(self.state)

    def _cancelled(self, obj: Entity) -> bool:
        if _norm(obj.state) in CANCELLED:
            return True
        state = (obj.raw or {}).get("state") or {}
        return state.get("stateType") == "Unsuccessful"

    def wants(self, obj: Entity, event: Event) -> bool:
        if event.action == "DELETE" or event.is_new or event.previous_state is None:
            return False
        if _norm(event.previous_state) == _norm(obj.state):
            return False
        if self._cancelled(obj):
            return False
        return self._target(obj)

    def key(self, obj: Entity, event: Event | None) -> str:
        return obj.id

    async def run(self, obj: Entity, ctx: Context) -> str:
        if self._cancelled(obj):
            raise Skip(f"отменённый заказ {obj.name}")
        event: Event | None = getattr(ctx, "event", None)
        seen_moment = str(getattr(event, "moment", None) or "").strip()
        record = _pointer(await ctx.storage.get(_order_key(obj.id)))
        if record is None:
            moment = seen_moment
            parsed = _parsed_moment(moment)
            if parsed is None:
                text = (f"MANUAL_REVIEW: нет времени перехода, заказ {obj.name} ({obj.id}). "
                        "Дату не назначаю.")
                if not ctx.settings.dry_run:
                    await _write_pointer(ctx, obj.id, "manual", None, None)
                    await ctx.channels.route_note("MANUAL_REVIEW", order=obj.name, order_id=obj.id)
                log.info("ручная проверка %s %s", obj.name, obj.id)
                return text
            day = route_date(parsed)
            record = {"status": "pending", "route_date": day.isoformat(), "transition_at": moment}
            if not ctx.settings.dry_run:
                await _write_pointer(ctx, obj.id, "pending", day, moment)
        elif record.get("status") == "manual":
            return f"MANUAL_REVIEW: заказ {obj.name} ({obj.id}) уже на ручной проверке"
        if record.get("status") == "published":
            return f"уже в листе {obj.name}"
        if record.get("status") == "late":
            return await self._late_notice(ctx, obj, date.fromisoformat(record["route_date"]))
        day = date.fromisoformat(record["route_date"])
        if ctx.settings.dry_run:
            doc = _load_day(await ctx.storage.get(_day_key(day)))
            if doc["status"] != "open":
                return f"WOULD_LATE {obj.name} ({obj.id}) {day_text(day)}"
            return f"WOULD_COLLECT {obj.name} ({obj.id}) {day_text(day)}"
        doc = _load_day(await ctx.storage.get(_day_key(day)))
        if doc["status"] != "open":
            if obj.id in doc.get("frozen_ids", []):
                await _write_pointer(ctx, obj.id, "published", day, record.get("transition_at"))
                return f"уже в листе {obj.name}"
            await _write_pointer(ctx, obj.id, "late", day, record.get("transition_at"))
            return await self._late_notice(ctx, obj, day)
        address = delivery_address(obj)
        if not address:
            message = f"нет адреса доставки, заказ {obj.name} ({obj.id})"
            log.warning("%s", message)
            raise RouteReport(message)
        order = {
            "id": obj.id,
            "name": obj.name,
            "counterparty": obj.agent.name,
            "address": address,
            "phone": await self._phone(obj, ctx),
            "hours": self._hours(obj),
            "comment": (obj.description or "").strip(),
            "transition_at": record.get("transition_at") or "",
            "route_date": day.isoformat(),
        }
        saved = _load_day(await ctx.storage.json_update(
            _day_key(day), lambda raw, item=order: _upsert(raw, item)))
        if obj.id in saved.get("late", []):
            await _write_pointer(ctx, obj.id, "late", day, record.get("transition_at"))
            return await self._late_notice(ctx, obj, day)
        await _write_pointer(ctx, obj.id, "collected", day, record.get("transition_at"))
        await ctx.channels.route_note(
            "COLLECT", order=obj.name, order_id=obj.id, route_date=day.isoformat())
        log.info("собран %s %s на %s", obj.name, obj.id, day.isoformat())
        return f"COLLECT {obj.name} ({obj.id}) {day_text(day)}"

    async def _late_notice(self, ctx: Context, obj: Entity, day: date) -> str:
        text = (f"LATE_ORDER: заказ {obj.name} ({obj.id}) на маршрут {day_text(day)}. "
                "Лист уже опубликован, в файл не добавляю.")
        key = ctx.run_key(self, obj.id)
        if await ctx.step_done(self, key, "late"):
            return text
        await ctx.channels.route_note(
            "LATE_ORDER", order=obj.name, order_id=obj.id, route_date=day.isoformat())
        for chat in self.notify_chats:
            chat = str(chat).strip()
            if chat:
                await ctx.channels.telegram_message(chat, text)
        if not ctx.settings.dry_run:
            await ctx.mark_step(self, key, "late", "ok")
        log.info("поздний заказ %s %s %s", obj.name, obj.id, day.isoformat())
        return text

    def _hours(self, obj: Entity) -> str:
        if not str(self.hours_field).strip():
            return ""
        value = obj.attr(self.hours_field)
        if value is None:
            return ""
        return str(value).strip()

    async def _phone(self, obj: Entity, ctx: Context) -> str:
        if str(self.phone_field).strip():
            value = obj.attr(self.phone_field)
            if value is not None and str(value).strip():
                return str(value).strip()
        agent_phone = (obj.agent.phone or "").strip()
        if agent_phone:
            return agent_phone
        if not obj.agent.id:
            return ""
        async for row in ctx.ms.iterate(
                f"entity/counterparty/{obj.agent.id}/contactpersons", limit=100):
            phone = str(row.get("phone") or "").strip()
            if phone:
                return phone
        return ""


@register
class LogisticsPublish(Automation):
    kind = "logistics_publish"
    title = "Публикация маршрутного листа"
    scheduled = True
    defaults = {"at": "07:00", "folder": ""}

    def validate(self) -> None:
        try:
            parsed = datetime.strptime(str(self.at), "%H:%M")
        except ValueError:
            raise ConfigError(f"{self.name}: at «{self.at}», нужно ЧЧ:ММ") from None
        if parsed.strftime("%H:%M") != str(self.at):
            raise ConfigError(f"{self.name}: at «{self.at}», нужно ЧЧ:ММ")
        if not str(self.folder).strip():
            raise ConfigError(f"{self.name}: нужна папка Диска")

    def slot(self, now: datetime) -> str | None:
        local = now.astimezone(MSK) if now.tzinfo else now.replace(tzinfo=MSK)
        if local.strftime("%H:%M") < str(self.at):
            return None
        return local.strftime("%Y-%m-%d")

    async def run_scheduled(self, ctx: Context, slot: str) -> str:
        day = date.fromisoformat(slot)
        name = file_name(day)
        path = file_path(self.folder, day)
        if ctx.settings.dry_run:
            await ctx.channels.route_note("WOULD_PUBLISH", day=slot, file=name)
            return f"WOULD_PUBLISH {name}"
        doc = _load_day(await ctx.storage.json_update(_day_key(day), _freeze))
        if doc["status"] == "published" and doc.get("file"):
            return f"уже опубликован {name}"
        if doc["status"] == "published":
            await ctx.channels.route_note("PUBLISH_EMPTY", day=slot)
            return f"нет заказов {name}"
        orders = [doc["orders"][oid] for oid in doc["frozen_ids"]]
        data = await _payload(ctx, day, orders, doc["publication_id"])
        digest = hashlib.sha256(data).hexdigest()
        md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
        meta = await _place(ctx, path, data, md5)
        file = {
            "path": path,
            "id": str(meta.get("path") or path),
            "sha256": digest,
            "md5": md5,
            "filename": name,
            "size": len(data),
        }
        await ctx.storage.json_update(_day_key(day), lambda raw, item=file: _save_file(raw, item))
        for order in orders:
            await _write_pointer(ctx, order["id"], "published", day, order.get("transition_at"))
        await ctx.channels.route_note(
            "PUBLISH", day=slot, file=name, orders=len(orders), sha256=digest)
        log.info("опубликован %s заказов %s", name, len(orders))
        return f"PUBLISH {name} заказов {len(orders)}"


async def _payload(ctx: Context, day: date, orders: list[dict], publication_id: str) -> bytes:
    cached = await ctx.storage.get(_xlsx_key(day))
    if cached:
        return base64.b64decode(cached)
    data = build_route_xlsx(day_text(day), orders, publication_id)
    await ctx.storage.set(_xlsx_key(day), base64.b64encode(data).decode("ascii"))
    return data


async def _place(ctx: Context, path: str, data: bytes, md5: str) -> dict:
    current = await ctx.channels.disk_stat(path)
    if current:
        return _same(current, md5)
    try:
        meta = await ctx.channels.disk_upload_new(path, data)
    except DiskError as exc:
        current = await ctx.channels.disk_stat(path)
        if current:
            return _same(current, md5)
        raise DiskError(exc.status, "загрузка не подтверждена, файл не перезаписываю") from exc
    return _same(meta, md5)


def _same(meta: dict | None, md5: str) -> dict:
    if not meta or meta.get("md5") != md5:
        raise DiskError(409, "файл уже есть и не совпадает, не перезаписываю")
    return meta

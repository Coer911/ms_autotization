"""Маршрутный лист: переход заказа в статус сборки → строка в файле следующего рабочего дня.

Время перехода вебхук в хаб не приносит (`parse_webhook` не хранит auditContext,
в очереди нет колонки момента смены статуса). Берём `customerorder.updated` —
единственную метку документа, которую воркер уже получает. Это момент последнего
обновления заказа в поясе Москвы, не `received_at` и не часы обработки.
Первый боевой вход записывает эту метку и дату в kv. Повтор, retry и
восстановление дату заново не считают.

Снимок хаба хранит имя статуса, не id. «Из другого статуса» проверяется по имени
в `previous_state`. Id, если он есть у загруженного заказа и задан `state_id`,
решает, целевой ли текущий статус; иначе сравнивается имя.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from ..channels.route_sheet import file_title
from ..entities import Entity
from ..events import Event
from ..moysklad import entity_id
from . import Automation, ConfigError, Context, Skip, register

log = logging.getLogger("hub.automation.logistics")
MSK = ZoneInfo("Europe/Moscow")
FILE_LOCK = "_route_file"
CANCELLED = frozenset({"отменён", "отменен", "отмена"})
_OFFSET = re.compile(r"(?:Z|[+-]\d\d:?\d\d)$")


class RouteReport(Exception):
    """Нет адреса доставки: в журнал и в повтор. Адрес не выдумывается."""


def _norm(value: str | None) -> str:
    return (value or "").strip().casefold()


def parse_transition(value: str) -> datetime:
    """Строка МойСклад без пояса — уже Москва. ISO с Z или смещением переводится в Москву."""
    text = (value or "").strip()
    if not text:
        raise ValueError("пустой момент перехода")
    if "T" in text or _OFFSET.search(text):
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=MSK)
        return parsed.astimezone(MSK)
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=MSK)
        except ValueError:
            continue
    raise ValueError(f"не разобран момент перехода: {text[:32]}")


def route_date(moment: datetime) -> date:
    """Следующий пн–пт после календарной даты момента. Праздники не учитываются."""
    local = moment.astimezone(MSK) if moment.tzinfo else moment.replace(tzinfo=MSK)
    day = local.date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def day_text(day: date) -> str:
    return day.strftime("%d.%m.%Y")


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


def _file_key(day: date) -> str:
    return f"ls:file:{day.isoformat()}"


def _load(raw: str | None) -> dict | None:
    if not raw:
        return None
    data = json.loads(raw)
    date.fromisoformat(data["route_date"])
    return data


@register
class LogisticsSheet(Automation):
    kind = "logistics_sheet"
    title = "Маршрутный лист"
    entities = ("customerorder",)
    defaults = {
        "entity": "customerorder",
        "state": "ПОДТВЕРЖДЕН / ОЖИДАЕТ СБОРКИ",
        "state_id": "",
        "folder_id": "",
        "template_file_id": "",
        "hours_field": "",
        "phone_field": "",
    }

    def validate(self) -> None:
        if not str(self.state).strip():
            raise ConfigError(f"{self.name}: нужен state")
        if not str(self.folder_id).strip():
            raise ConfigError(f"{self.name}: нужен folder_id")
        if not str(self.template_file_id).strip():
            raise ConfigError(f"{self.name}: нужен template_file_id")

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
        # state_changed() истинен и на первом снимке, если вебхук прислал updatedFields=state.
        # Для маршрута это не переход и не backfill.
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
        raw = await ctx.storage.get(_order_key(obj.id))
        record = _load(raw)
        if record is None:
            if not self._target(obj):
                raise Skip(f"статус уже не целевой: {obj.state}")
            record = {
                "transition_at": obj.updated,
                "route_date": route_date(parse_transition(obj.updated)).isoformat(),
                "file_id": "",
                "phone_written": False,
            }
            if not ctx.settings.dry_run:
                await ctx.storage.set(_order_key(obj.id), json.dumps(record, ensure_ascii=False))
        day = date.fromisoformat(record["route_date"])
        address = delivery_address(obj)
        if not address:
            message = f"отчёт: нет адреса доставки, заказ {obj.name} ({obj.id})"
            log.warning("%s", message)
            raise RouteReport(message)
        if ctx.settings.dry_run:
            return await self._dry(ctx, obj, record, day, raw is not None)
        if not self._target(obj):
            return await self._left_status(ctx, obj, record, day)
        phone = await self._phone(obj, ctx)
        hours = self._hours(obj)
        file_id, created = await self._ensure_file(ctx, day, record)
        hit = await ctx.channels.route_find_order(file_id, day_text(day), obj.id)
        fields = self._fields(obj, address, phone, hours, hit, record)
        if hit:
            await ctx.channels.route_write_order(file_id, day_text(day), fields, hit["row"])
            action = "UPDATE_ORDER"
        else:
            await ctx.channels.route_write_order(file_id, day_text(day), fields, None)
            action = "ADD_ORDER"
        record["file_id"] = file_id
        record["phone_written"] = True
        await ctx.storage.set(_order_key(obj.id), json.dumps(record, ensure_ascii=False))
        actions = (["CREATE_ROUTE"] if created else []) + [action]
        for name in actions:
            await ctx.channels.route_note(name, order_id=obj.id, route=record["route_date"])
        return " ".join(actions)

    async def _dry(self, ctx: Context, obj: Entity, record: dict, day: date, had_record: bool) -> str:
        file_raw = await ctx.storage.get(_file_key(day))
        actions = []
        if not file_raw:
            actions.append("WOULD_CREATE_ROUTE")
        actions.append("WOULD_UPDATE_ORDER" if had_record else "WOULD_ADD_ORDER")
        for name in actions:
            await ctx.channels.route_note(name, order_id=obj.id, route=day.isoformat())
        return " ".join(actions)

    async def _left_status(self, ctx: Context, obj: Entity, record: dict, day: date) -> str:
        file_raw = _load_file(await ctx.storage.get(_file_key(day)))
        file_id = record.get("file_id") or (file_raw or {}).get("id") or ""
        if not file_id:
            raise Skip(f"статус уже не целевой: {obj.state}")
        hit = await ctx.channels.route_find_order(file_id, day_text(day), obj.id)
        if hit is None:
            raise Skip(f"статус уже не целевой: {obj.state}")
        record["file_id"] = file_id
        await ctx.storage.set(_order_key(obj.id), json.dumps(record, ensure_ascii=False))
        return "строка сохранена, статус уже не целевой"

    async def _ensure_file(self, ctx: Context, day: date, record: dict) -> tuple[str, bool]:
        key = _file_key(day)
        saved = _load_file(await ctx.storage.get(key))
        if saved and saved.get("ready") and saved.get("id"):
            record["file_id"] = saved["id"]
            return saved["id"], False
        lock = f"file:{day.isoformat()}"
        title = file_title(day_text(day))
        tab = day_text(day)
        started, existing = await ctx.storage.try_start(
            FILE_LOCK, lock, entity="route", entity_id=day.isoformat(),
            label=title, lease=ctx.settings.lease_seconds)
        if not started:
            if existing and existing.status == "done":
                saved = _load_file(await ctx.storage.get(key))
                if saved and saved.get("id"):
                    return saved["id"], False
                found = await ctx.channels.route_find_file(self.folder_id, title)
                if found:
                    await ctx.storage.set(key, json.dumps(
                        {"id": found, "title": title, "ready": True}, ensure_ascii=False))
                    return found, False
            raise ChannelBusy("файл даты занят другим запуском")
        created = False
        try:
            found = await ctx.channels.route_find_file(self.folder_id, title)
            if found is None:
                found = await ctx.channels.route_create_file(
                    self.folder_id, self.template_file_id, title, tab)
                created = True
            else:
                await ctx.channels.route_prepare(found, tab)
            if not found:
                raise ChannelBusy("Google не подтвердил id файла")
            await ctx.storage.set(key, json.dumps(
                {"id": found, "title": title, "ready": True}, ensure_ascii=False))
            await ctx.storage.finish(FILE_LOCK, lock, "done", found)
            return found, created
        except Exception as e:
            await ctx.storage.fail(FILE_LOCK, lock, f"{type(e).__name__}: {e}")
            raise

    def _fields(self, obj: Entity, address: str, phone: str, hours: str,
                hit: dict | None, record: dict) -> dict:
        fields: dict = {
            "order_id": obj.id,
            "counterparty": obj.agent.name,
            "address": address,
        }
        if hit is None:
            if phone:
                fields["phone"] = phone
            if hours:
                fields["hours"] = hours
            comment = (obj.description or "").strip()
            if comment:
                fields["comment"] = comment
            return fields
        if not record.get("phone_written") and not str(hit.get("phone") or "").strip() and phone:
            fields["phone"] = phone
        if not str(hit.get("hours") or "").strip() and hours:
            fields["hours"] = hours
        return fields

    def _hours(self, obj: Entity) -> str:
        if not str(self.hours_field).strip():
            return ""
        value = obj.attr(self.hours_field)
        if value is None:
            return ""
        return str(value).strip()

    async def _phone(self, obj: Entity, ctx: Context) -> str:
        # Приоритет — только поля JSON API, без отдельного «основного» признака у контакта.
        # 1. Доп. поле заказа phone_field, если параметр задан.
        # 2. counterparty.phone — городской, уже в expand agent.
        # 3. Первый непустой phone контактного лица в порядке rows.
        if str(self.phone_field).strip():
            value = obj.attr(self.phone_field)
            if value is not None and str(value).strip():
                return str(value).strip()
        agent_phone = (obj.agent.phone or "").strip()
        if agent_phone:
            return agent_phone
        if not obj.agent.id:
            return ""
        async for row in ctx.ms.iterate(f"entity/counterparty/{obj.agent.id}/contactpersons", limit=100):
            phone = str(row.get("phone") or "").strip()
            if phone:
                return phone
        return ""


class ChannelBusy(Exception):
    """Файл даты занят другим запуском. Повтор — обычная лестница runs."""


def _load_file(raw: str | None) -> dict | None:
    if not raw:
        return None
    return json.loads(raw)

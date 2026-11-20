"""Маршрутный лист: сбор перехода и утренний неизменяемый XLSX на Диске.

Вебхук только проверяет переход и пишет заказ в базу хаба. В Диск на этом шаге
ничего не уходит. Переход — смена статуса на целевой или создание заказа сразу в нём.
Дата маршрута — следующий пн–пт по Москве от auditContext.moment.
customerorder.updated датой перехода не считается. Нет момента — ручная проверка,
дату не угадываем. Отмена до публикации убирает заказ из сбора; после — оповещение.

В `at` (по умолчанию 07:00) планировщик собирает заказы этой даты в новый XLSX
«ДД.ММ.ГГГГ.xlsx» (вкладка МЛ, формат логиста) и загружает его в папку. Адрес, телефон,
часы, места и комментарий — дословно из базы логиста (`base_file`), если заказ в ней
найден, иначе из МойСклад. Уже лежащий файл не перезаписывается и не удаляется.
Переход на дату, лист которой уже вышел, — заказ уходит в следующий рабочий день (MOVED).
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
from .route_base import DEFAULT_SHEET, match, parse_base, places, window
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


def next_route_day(day: date) -> date:
    """Следующий пн–пт после даты маршрута."""
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def day_text(day: date) -> str:
    return day.strftime("%d.%m.%Y")


def file_name(day: date) -> str:
    """Как у логиста: «20.11.2026.xlsx»."""
    return f"{day_text(day)}.xlsx"


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
        return _dump(doc)                # дата заморожена — заказ уйдёт на следующий день
    doc["orders"][oid] = order
    return _dump(doc)


def _withdraw(raw: str | None, order_id: str) -> str:
    doc = _load_day(raw)
    if doc["status"] == "open":
        doc["orders"].pop(order_id, None)
        if order_id in doc["late"]:
            doc["late"].remove(order_id)
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
        if event.action == "DELETE":
            return False
        if event.action == "CREATE":
            # заказ создали сразу в целевом статусе (статус выбран до первого сохранения)
            return not self._cancelled(obj) and self._target(obj)
        if event.is_new or event.previous_state is None:
            return False
        if _norm(event.previous_state) == _norm(obj.state):
            return False
        if self._cancelled(obj):
            return True                      # убрать из сбора, если он там есть
        return self._target(obj)

    def key(self, obj: Entity, event: Event | None) -> str:
        """Свой ключ на каждый переход: повторное нажатие статуса — новый прогон."""
        moment = str(getattr(event, "moment", None) or "").strip() or "no-moment"
        kind = "cancel" if self._cancelled(obj) else "go"
        return f"{obj.id}:{kind}:{moment}"

    async def run(self, obj: Entity, ctx: Context) -> str:
        if self._cancelled(obj):
            return await self._cancel(obj, ctx)
        event: Event | None = getattr(ctx, "event", None)
        seen_moment = str(getattr(event, "moment", None) or "").strip()
        record = _pointer(await ctx.storage.get(_order_key(obj.id)))
        if record is not None and record.get("status") in ("withdrawn", "manual"):
            record = None                    # снова перевели в статус — это новый переход
        if record is None:
            moment = seen_moment or await self._audit_moment(obj, ctx)
            parsed = _parsed_moment(moment)
            if parsed is None:
                text = (f"MANUAL_REVIEW: нет времени перехода, заказ {obj.name} ({obj.id}). "
                        "Дату не назначаю.")
                if not ctx.settings.dry_run:
                    await _write_pointer(ctx, obj.id, "manual", None, None)
                    await ctx.channels.route_note("MANUAL_REVIEW", order=obj.name, order_id=obj.id,
                                                  counterparty=obj.agent.name)
                log.info("ручная проверка %s %s", obj.name, obj.id)
                return text
            day = route_date(parsed)
            record = {"status": "pending", "route_date": day.isoformat(), "transition_at": moment}
            if not ctx.settings.dry_run:
                await _write_pointer(ctx, obj.id, "pending", day, moment)
        if record.get("status") == "published":
            return f"уже в листе {obj.name}"
        day = date.fromisoformat(record["route_date"])
        transition = record.get("transition_at") or ""
        if ctx.settings.dry_run:
            free = await self._open_day(ctx, obj.id, day)
            if isinstance(free, str):
                return free
            return f"WOULD_COLLECT {obj.name} ({obj.id}) {day_text(free)}"
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
            "transition_at": transition,
        }
        planned = day
        # Лист на дату уже вышел (поздний переход, хаб лежал) — заказ едет в следующий
        # рабочий день. Без оповещений: это обычная доставка, просто на день позже.
        for _ in range(60):
            free = await self._open_day(ctx, obj.id, day)
            if isinstance(free, str):
                await _write_pointer(ctx, obj.id, "published", day, transition)
                return free
            day = free
            item = {**order, "route_date": day.isoformat()}
            saved = _load_day(await ctx.storage.json_update(
                _day_key(day), lambda raw, item=item: _upsert(raw, item)))
            if obj.id in saved["orders"]:
                break
            day = next_route_day(day)           # дату заморозили между проверкой и записью
        else:
            raise RouteReport(f"не нашёл открытую дату для {obj.name} ({obj.id})")
        again = record.get("status") == "collected"
        await _write_pointer(ctx, obj.id, "collected", day, transition)
        if again:                                # повторное нажатие: данные обновлены, без шума
            log.info("обновлён %s %s на %s", obj.name, obj.id, day.isoformat())
            return f"UPDATE {obj.name} ({obj.id}) {day_text(day)}"
        if day != planned:
            await ctx.channels.route_note("MOVED", order=obj.name, order_id=obj.id,
                                          counterparty=obj.agent.name,
                                          planned=planned.isoformat(), route_date=day.isoformat())
            log.info("перенесён %s %s с %s на %s", obj.name, obj.id, planned, day)
            return f"MOVED {obj.name} ({obj.id}) {day_text(planned)} → {day_text(day)}"
        await ctx.channels.route_note(
            "COLLECT", order=obj.name, order_id=obj.id, route_date=day.isoformat(),
            counterparty=obj.agent.name)
        log.info("собран %s %s на %s", obj.name, obj.id, day.isoformat())
        return f"COLLECT {obj.name} ({obj.id}) {day_text(day)}"

    async def _audit_moment(self, obj: Entity, ctx: Context) -> str:
        """Вебхук без момента (или опрос раньше вебхука): время перехода — из аудита заказа.

        Последнее событие, где статус стал целевым; не было смены статуса, а заказ уже в нём —
        момент создания. Аудит недоступен — пусто (ручная проверка), дату не угадываем.
        """
        target, created = "", ""
        try:
            async for ev in ctx.ms.iterate(f"entity/{obj.type}/{obj.id}/audit", limit=100):
                moment = str(ev.get("moment") or "")
                if ev.get("eventType") == "create":
                    created = max(created, moment)
                state = ((ev.get("diff") or {}).get("state") or {}).get("newValue")
                name = state.get("name") if isinstance(state, dict) else state
                if name is not None and _norm(str(name)) == _norm(self.state):
                    target = max(target, moment)
        except Exception:
            log.warning("аудит заказа %s недоступен", obj.name, exc_info=True)
            return ""
        return target or created

    async def _open_day(self, ctx: Context, order_id: str, day: date) -> date | str:
        """Ближайшая открытая дата начиная с day. Строка — заказ уже в вышедшем листе."""
        for _ in range(60):
            doc = _load_day(await ctx.storage.get(_day_key(day)))
            if doc["status"] == "open":
                return day
            if order_id in doc.get("frozen_ids", []):
                return f"уже в листе {day_text(day)}"
            day = next_route_day(day)
        raise RouteReport(f"не нашёл открытую дату для заказа {order_id}")

    async def _cancel(self, obj: Entity, ctx: Context) -> str:
        record = _pointer(await ctx.storage.get(_order_key(obj.id)))
        if record is None or record.get("status") in ("withdrawn", "manual") \
                or not record.get("route_date"):
            if record is not None and not ctx.settings.dry_run:
                await _write_pointer(ctx, obj.id, "withdrawn", None, None)
            raise Skip(f"отменённый заказ {obj.name} не в сборе")
        day = date.fromisoformat(record["route_date"])
        doc = _load_day(await ctx.storage.get(_day_key(day)))
        published = doc["status"] != "open" and obj.id in doc.get("frozen_ids", [])
        if ctx.settings.dry_run:
            return f"WOULD_{'CANCEL_AFTER_PUBLISH' if published else 'REMOVE'} {obj.name}"
        if published:
            text = (f"CANCEL_AFTER_PUBLISH: заказ {obj.name} ({obj.id}) отменён, а лист "
                    f"{day_text(day)} уже на Диске. Вычеркните его вручную.")
            await ctx.channels.route_note("CANCEL_AFTER_PUBLISH", order=obj.name,
                                          counterparty=obj.agent.name,
                                          order_id=obj.id, route_date=day.isoformat())
            for chat in self.notify_chats:
                if str(chat).strip():
                    await ctx.channels.telegram_message(str(chat).strip(), text)
            return text
        await ctx.storage.json_update(_day_key(day), lambda raw: _withdraw(raw, obj.id))
        await _write_pointer(ctx, obj.id, "withdrawn", None, None)
        await ctx.channels.route_note("REMOVE", order=obj.name, order_id=obj.id,
                                      counterparty=obj.agent.name,
                                      route_date=day.isoformat())
        return f"REMOVE {obj.name} ({obj.id}) из листа {day_text(day)}"

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
    defaults = {"at": "07:00", "folder": "", "base_file": "", "base_sheet": DEFAULT_SHEET}

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
        data = await _payload(ctx, day, orders, doc["publication_id"], self._rows)
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
        from ..channels.notify_text import route_text
        caption = route_text("PUBLISH", {"day": slot, "file": name, "orders": len(orders)})
        missing = getattr(self, "_missing", [])
        if missing:
            caption += (f"\n📒 Нет в базе адресов ({len(missing)}): {', '.join(missing)}. "
                        "Взял адрес и телефон из МойСклад — добавьте их в базу.")
        await ctx.channels.notify_document(name, data, caption)
        log.info("опубликован %s заказов %s", name, len(orders))
        return f"PUBLISH {name} заказов {len(orders)}"

    async def _rows(self, ctx: Context, orders: list[dict]) -> list[dict]:
        """Строки МЛ: из базы логиста дословно, не нашли — из МойСклад. Одна точка — одна строка."""
        base = []
        if str(self.base_file).strip():
            # нет базы — ошибка и повтор по расписанию: файл неизменяемый, хуже не публикуем
            base = parse_base(await ctx.channels.disk_download(str(self.base_file).strip()),
                              str(self.base_sheet or DEFAULT_SHEET))
        rows: list[dict] = []
        seen: dict[tuple, dict] = {}
        self._missing = []
        for order in orders:
            found = match(base, order.get("counterparty") or "", order.get("address") or "")
            if found is not None:
                start, end = window(found.hours)
                row = {"id": order["id"], "counterparty": found.counterparty,
                       "address": found.address,
                       # в базе телефона нет — берём из МойСклад, остальное только из базы
                       "phone": found.phone if str(found.phone or "").strip()
                       else order.get("phone") or "",
                       "comment": found.comment, "start": start, "end": end,
                       "places": places(found.places), "source": "база"}
                same = seen.get((str(found.counterparty), str(found.address)))
                if same is not None:
                    same["places"] += row["places"]
                    same["id"] += f",{order['id']}"
                    continue
                seen[(str(found.counterparty), str(found.address))] = row
            else:
                start, end = window(order.get("hours"))
                row = {"id": order["id"], "counterparty": order.get("counterparty") or "",
                       "address": order.get("address") or "", "phone": order.get("phone") or "",
                       "comment": order.get("comment") or "", "start": start, "end": end,
                       "places": 1, "source": "МойСклад"}
                if base:
                    self._missing.append(row["counterparty"])
                    await ctx.channels.route_note("NOT_IN_BASE", order=order.get("name"),
                                                  counterparty=row["counterparty"])
            rows.append(row)
        return rows


async def _payload(ctx: Context, day: date, orders: list[dict], publication_id: str,
                   rows=None) -> bytes:
    cached = await ctx.storage.get(_xlsx_key(day))
    if cached:
        return base64.b64decode(cached)
    lines = await rows(ctx, orders) if rows else orders
    data = build_route_xlsx(day_text(day), lines, publication_id)
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

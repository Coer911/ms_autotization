"""Уведомления хаба человеческим языком (Telegram).

Каждое событие маршрута (`route_note`) превращается в короткий текст. Важные события
уходят всегда, прочие — только при HUB_NOTIFY_LEVEL=debug.
"""
from __future__ import annotations

from datetime import date

_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

# важные: про них человек должен узнать и в обычном режиме
IMPORTANT = frozenset({
    "PUBLISH", "PUBLISH_EMPTY", "REMOVE", "CANCEL_AFTER_PUBLISH", "MANUAL_REVIEW",
})
# критичные по времени задачи: первая же ошибка — сразу людям, не ждать конца повторов
URGENT = frozenset({"logistics_publish"})


def day(value: object) -> str:
    """«2026-11-20» → «пт 20.11»."""
    try:
        d = date.fromisoformat(str(value)[:10])
    except ValueError:
        return str(value or "")
    return f"{_WEEKDAYS[d.weekday()]} {d:%d.%m}"


def _who(info: dict) -> str:
    order = info.get("order") or "без номера"
    agent = str(info.get("counterparty") or "").strip()
    return f"{order} ({agent})" if agent else str(order)


def route_text(action: str, info: dict) -> str | None:
    who = _who(info)
    when = day(info.get("route_date") or info.get("day"))
    match action:
        case "COLLECT":
            return f"🚚 Заказ {who} — в доставку на {when}."
        case "MOVED":
            return (f"↪️ Заказ {who} подтвердили, когда лист на {day(info.get('planned'))} "
                    f"уже ушёл. Поставил в доставку на {when}.")
        case "REMOVE":
            return f"❌ Заказ {who} отменили — убрал из листа на {when}."
        case "CANCEL_AFTER_PUBLISH":
            return (f"⚠️ Заказ {who} отменили, но лист на {when} уже на Диске. "
                    "Вычеркните его у логиста вручную.")
        case "MANUAL_REVIEW":
            return (f"❓ Заказ {who} перевели в сборку, но МойСклад не прислал время перехода. "
                    "Дату доставки не ставлю — переведите статус ещё раз.")
        case "PUBLISH":
            return (f"📄 Маршрутный лист на {when} готов: доставок {info.get('orders')}. "
                    f"Файл «{info.get('file')}» на Диске.")
        case "PUBLISH_EMPTY":
            return f"📭 На {when} заказов в доставку нет — лист не создавал."
        case "WOULD_PUBLISH":
            return f"🧪 Пробный режим: на {when} выложил бы «{info.get('file')}»."
    return None


def failure_text(title: str, label: str, error: str, next_at: str | None, dead: bool) -> str:
    error = _human_error(error)
    if dead:
        return f"🛑 {title}: {label} — {error}. Попытки кончились, нужна ваша помощь."
    when = f" в {next_at[11:16]}" if next_at and len(next_at) >= 16 else ""
    return f"⚠️ {title}: {label} — {error}. Попробую ещё раз{when}."


def _human_error(error: str) -> str:
    text = str(error or "").strip()
    for prefix in ("RouteReport: ", "DiskError: ", "RuntimeError: ", "ChannelError: "):
        if text.startswith(prefix):
            text = text[len(prefix):]
    if "нет адреса доставки" in text:
        return f"{text} — заполните адрес в заказе и переведите статус ещё раз"
    if "Диск 401" in text or "токен не принят" in text:
        return "Яндекс Диск не принял токен"
    if "МойСклад 401" in text:
        return "МойСклад не принял токен"
    return text

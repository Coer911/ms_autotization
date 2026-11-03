"""Разговор с Claude: вопрос → инструменты (hub/bot/tools.py) → ответ.

Цикл вызова инструментов написан руками, а не через beta tool runner: нужна история
по чатам, откат хода при сбое и тесты без сети. Правила API, на которых он держится:
  * claude-opus-5-5 думает всегда (thinking не выключается) — глубину задаёт effort;
  * история только дописывается: system и tools не меняются, старые сообщения не
    правятся, иначе блоки размышлений прошлых ходов станут недействительны (400);
    когда разговор стал длинным или давно не продолжался — начинаем новый;
  * ответ модели возвращается в историю целиком (response.content), включая thinking;
  * fallbacks="default" — если модель откажет по правилам безопасности, ответ перезапросят
    на рекомендованной модели на стороне API;
  * system и tools стабильны → кешируются; дата — в тексте вопроса, а не в system.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

import anthropic

from ..moysklad import MoySkladError
from . import tools as bot_tools
from .tools import ToolError, Tools

log = logging.getLogger("hub.bot")
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOOL_RESULT = 60_000
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

SYSTEM = """Ты — помощник по учёту в МойСклад для небольшого бизнеса: кофе, чай, сиропы, сырьё \
для фасовки купажей. Отвечаешь в Telegram владельцу и сотрудникам на русском.

Как работать:
- Все цифры — только из инструментов. Не помнишь и не знаешь — вызови инструмент; \
инструмент не умеет — так и скажи. Никогда не придумывай остатки, суммы и даты.
- Ты только читаешь МойСклад. Создать заказ, сменить статус, отправить письмо ты не можешь — \
если просят, объясни, что это делается в МойСклад или через хаб.
- Период «за октябрь» — с 1-го по последнее число месяца; «за этот год» — с 1 января по сегодня. \
Сегодняшняя дата приходит в начале каждого вопроса в квадратных скобках.
- Продажи по умолчанию — без внутренних перемещений (например, на Озон через ООО «ИНТЕРНЕТ РЕШЕНИЯ»): \
это не выручка. Если считаешь с ними — скажи об этом.
- Себестоимость в МойСклад пересчитывается задним числом при проведении приёмок, поэтому прибыль \
прошлых периодов может немного меняться — если цифра разошлась с прошлым ответом, скажи об этом.
- Заказ поставщику считай инструментом reorder; если позиций слишком много — попроси уточнить поиск.
- Неоплаченные отгрузки: инструмент unpaid; real_debt учитывает оплаты, не привязанные к отгрузкам. \
Ссылки на отгрузки давай как есть.

Как отвечать (это Telegram, разметка не отображается):
- Коротко, по делу: сначала главный ответ одной-двумя строками, потом детали.
- Без таблиц Markdown и без звёздочек. Списки — строками вида «Карамель — 5».
- Суммы с пробелами между разрядами и знаком ₽: 1 234 567 ₽. Количество — с единицами.
- Если допущение в расчёте важно (период, исключения, срок по умолчанию) — назови его одной строкой."""


@dataclass
class Conversation:
    messages: list[dict] = field(default_factory=list)
    last: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BotError(Exception):
    """Сбой, о котором честно говорим пользователю."""


class Agent:
    def __init__(self, client: anthropic.AsyncAnthropic, tools: Tools, *,
                 model: str = "claude-opus-5-5", effort: str = "medium", max_steps: int = 12,
                 idle_reset: float = 3 * 3600, max_messages: int = 80,
                 clock: Callable[[], float] = time.monotonic,
                 today: Callable[[], date] = date.today):
        self.client = client
        self.tools = tools
        self.model = model
        self.effort = effort
        self.max_steps = max_steps
        self.idle_reset = idle_reset
        self.max_messages = max_messages
        self.clock = clock
        self.today = today
        self.definitions = bot_tools.definitions()
        self.chats: dict[str, Conversation] = {}

    def reset(self, chat: str) -> None:
        self.chats.pop(chat, None)

    def _conversation(self, chat: str) -> Conversation:
        conv = self.chats.get(chat)
        now = self.clock()
        if conv and not conv.lock.locked() and (
                now - conv.last > self.idle_reset or len(conv.messages) > self.max_messages):
            conv = None                 # новый разговор вместо обрезки старого — история только дописывается
        if conv is None:
            conv = self.chats[chat] = Conversation()
        conv.last = now
        return conv

    def _request(self, messages: list[dict]) -> dict:
        req = {"model": self.model, "max_tokens": 16000,
               "system": [{"type": "text", "text": SYSTEM}],
               "tools": self.definitions, "messages": messages,
               "output_config": {"effort": self.effort},
               "cache_control": {"type": "ephemeral"}}
        if not self.model.startswith("claude-haiku"):     # у Haiku нет серверного fallback
            req.update(betas=[FALLBACK_BETA], fallbacks="default")
        return req

    async def _tool(self, block) -> dict:
        try:
            content, is_error = await bot_tools.call(self.tools, block.name, block.input), False
        except ToolError as e:
            content, is_error = str(e), True
        except MoySkladError as e:
            content, is_error = f"МойСклад ответил ошибкой: {e.message}", True
        except Exception:
            log.exception("инструмент %s упал", block.name)
            content, is_error = "внутренняя ошибка инструмента", True
        if len(content) > MAX_TOOL_RESULT:
            content = content[:MAX_TOOL_RESULT] + "… (обрезано — уточните запрос)"
        return {"type": "tool_result", "tool_use_id": block.id, "content": content,
                "is_error": is_error}

    async def ask(self, chat: str, text: str) -> str:
        conv = self._conversation(str(chat))
        async with conv.lock:
            mark = len(conv.messages)
            today = self.today()
            conv.messages.append({"role": "user", "content": f"[{today} {WEEKDAYS[today.weekday()]}] {text}"})
            try:
                answer = await self._loop(conv)
            except anthropic.RateLimitError:
                del conv.messages[mark:]
                raise BotError("Claude сейчас перегружен — повторите через минуту.") from None
            except anthropic.APIConnectionError:
                del conv.messages[mark:]
                raise BotError("Нет связи с Claude — повторите чуть позже.") from None
            except anthropic.APIStatusError as e:
                del conv.messages[mark:]
                log.error("Claude API %s: %s", e.status_code, e.message)
                raise BotError(f"Claude ответил ошибкой {e.status_code} — повторите позже.") from None
            if answer is None:                    # отказ или зацикливание — начинаем заново
                self.reset(str(chat))
                return "Не получилось ответить на этот вопрос. Начал разговор заново — переформулируйте, пожалуйста."
            conv.last = self.clock()
            return answer

    async def _loop(self, conv: Conversation) -> str | None:
        for _ in range(self.max_steps):
            r = await self.client.beta.messages.create(**self._request(conv.messages))
            u = r.usage
            log.info("claude %s: in %s (кеш %s) out %s, stop=%s", r.model, u.input_tokens,
                     u.cache_read_input_tokens, u.output_tokens, r.stop_reason)
            if r.stop_reason == "refusal":
                return None
            conv.messages.append({"role": "assistant", "content": [b.to_dict() for b in r.content]})
            if r.stop_reason == "pause_turn":
                continue
            uses = [b for b in r.content if b.type == "tool_use"]
            if r.stop_reason == "tool_use" and uses:
                results = await asyncio.gather(*(self._tool(b) for b in uses))
                conv.messages.append({"role": "user", "content": list(results)})
                continue
            text = "\n".join(b.text for b in r.content if b.type == "text").strip()
            if r.stop_reason == "max_tokens":
                text += "\n\n(ответ оборван по длине — уточните вопрос)"
            return text or "Готово."
        log.warning("бот: больше %s шагов с инструментами", self.max_steps)
        return None


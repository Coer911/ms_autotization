"""Telegram-бот: long polling (getUpdates), без вебхука и без открытых портов.

Отвечает только чатам из белого списка (HUB_BOT_CHATS). Остальным — одна строка
«нет доступа» с их chat id: так проще добавить сотрудника, а данных чужой не увидит.
Опрашивать getUpdates одним токеном может только один процесс — бот работает
в одной реплике (роль all или bot).
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from .agent import Agent, BotError

log = logging.getLogger("hub.bot")
API = "https://api.telegram.org"
LIMIT = 4000                        # Telegram режет сообщения длиннее 4096 символов
TYPING_EVERY = 4.5

HELP = """Я отвечаю по данным МойСклад — только читаю, ничего не меняю.

Примеры вопросов:
• остатки сиропов Herbarista 700 мл
• заказ на 10 дней по сиропам Herbarista 700
• сколько докупить боярышника и валерианы на 15 дней
• выручка ИП Краснояров Данил за октябрь
• оборот чая Бай Му Дань по месяцам
• неоплаченные отгрузки с 1 октября
• почём брали у Русановой
• техкарта «Спокойный для Счастья»

/new — начать разговор заново"""


def chunks(text: str, limit: int = LIMIT) -> list[str]:
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return out + ([text] if text else [])


class TelegramBot:
    def __init__(self, token: str, agent: Agent, allowed: set[str], *,
                 http: httpx.AsyncClient | None = None, poll_timeout: int = 50):
        self.agent = agent
        self.allowed = {str(c) for c in allowed}
        self.poll_timeout = poll_timeout
        self.http = http or httpx.AsyncClient(timeout=poll_timeout + 15)
        self._url = f"{API}/bot{token}/"
        self._offset = 0
        self._tasks: set[asyncio.Task] = set()

    async def close(self) -> None:
        await self.http.aclose()

    async def api(self, method: str, **data) -> dict:
        r = await self.http.post(self._url + method, json=data)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or not body.get("ok"):
            # описание ошибки — без адреса: в нём токен
            raise RuntimeError(f"Telegram {method}: {r.status_code} {body.get('description', '')}")
        return body["result"]

    async def send(self, chat: str, text: str) -> None:
        for part in chunks(text):
            await self.api("sendMessage", chat_id=chat, text=part, disable_web_page_preview=True)

    async def poll_once(self) -> int:
        updates = await self.api("getUpdates", offset=self._offset, timeout=self.poll_timeout,
                                 allowed_updates=["message"])
        for u in updates:
            self._offset = max(self._offset, u["update_id"] + 1)
            msg = u.get("message") or {}
            if msg.get("text"):
                task = asyncio.create_task(self.handle(msg))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        return len(updates)

    async def run(self) -> None:
        me = await self.api("getMe")
        log.info("Telegram-бот @%s слушает; разрешённых чатов: %s", me.get("username"), len(self.allowed))
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Telegram getUpdates: %s — повторю через 10 с", e)
                await asyncio.sleep(10)

    async def _typing(self, chat: str) -> None:
        while True:
            try:
                await self.api("sendChatAction", chat_id=chat, action="typing")
            except Exception:
                pass
            await asyncio.sleep(TYPING_EVERY)

    async def handle(self, msg: dict) -> None:
        chat = str(msg["chat"]["id"])
        text = msg["text"].strip()
        try:
            if chat not in self.allowed:
                log.warning("бот: сообщение из чужого чата %s", chat)
                await self.send(chat, f"Нет доступа. Ваш chat id: {chat} — его добавляют в HUB_BOT_CHATS.")
                return
            command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
            if command in ("/start", "/help"):
                await self.send(chat, HELP)
                return
            if command == "/new":
                self.agent.reset(chat)
                await self.send(chat, "Начали заново.")
                return
            typing = asyncio.create_task(self._typing(chat))
            try:
                answer = await self.agent.ask(chat, text)
            except BotError as e:
                answer = str(e)
            except Exception:
                log.exception("бот: сбой на вопросе")
                answer = "Что-то сломалось при ответе. Попробуйте ещё раз или /new."
            finally:
                typing.cancel()
            await self.send(chat, answer)
        except Exception:
            log.exception("бот: не удалось ответить в чат %s", chat)

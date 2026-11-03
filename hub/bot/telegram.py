"""Telegram-бот: long polling (getUpdates), без вебхука и без открытых портов.

Где отвечает:
  * в рабочей группе — подключается один раз командой «/start <код>» (код — HUB_BOT_CODE)
    или id группы в HUB_BOT_CHATS; кто в группе, тот и пользуется;
  * в личке — только тем, чей chat id есть в HUB_BOT_CHATS.
Подключённые группы хранятся в базе хаба (ключ bot:chats) и переживают перезапуск.

В группе бот не читает переписку: отвечает на «/ask вопрос», на ответ (reply) на своё
сообщение и на упоминание @бота. Упоминания приходят боту, только если в @BotFather
выключен privacy mode (/setprivacy → Disable) или бот — админ группы; /ask и ответы
работают всегда. У каждого участника группы своя история разговора.

Опрашивать getUpdates одним токеном может только один процесс — бот работает
в одной реплике (роль all или bot).
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging

import httpx

from .agent import Agent, BotError

log = logging.getLogger("hub.bot")
API = "https://api.telegram.org"
LIMIT = 4000                        # Telegram режет сообщения длиннее 4096 символов
TYPING_EVERY = 4.5
STORE_KEY = "bot:chats"
GROUPS = ("group", "supergroup")
COMMANDS = [{"command": "ask", "description": "вопрос по МойСклад"},
            {"command": "new", "description": "начать разговор заново"},
            {"command": "help", "description": "что я умею"}]

HELP = """Я отвечаю по данным МойСклад — только читаю, ничего не меняю.

В группе спрашивайте так: /ask вопрос — или ответом на моё сообщение.

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
    def __init__(self, token: str, agent: Agent, allowed: set[str], *, code: str = "",
                 storage=None, http: httpx.AsyncClient | None = None, poll_timeout: int = 50):
        self.agent = agent
        self.allowed = {str(c) for c in allowed}
        self.code = code
        self.storage = storage           # get/set строк; None — подключения живут до перезапуска
        self.poll_timeout = poll_timeout
        self.http = http or httpx.AsyncClient(timeout=poll_timeout + 15)
        self._url = f"{API}/bot{token}/"
        self._offset = 0
        self._tasks: set[asyncio.Task] = set()
        self.username = ""
        self.bot_id = 0

    async def close(self) -> None:
        await self.http.aclose()

    async def api(self, method: str, **data) -> dict:
        r = await self.http.post(self._url + method, json=data)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or not body.get("ok"):
            # описание ошибки — без адреса: в нём токен
            raise RuntimeError(f"Telegram {method}: {r.status_code} {body.get('description', '')}")
        return body["result"]

    async def send(self, chat: str, text: str, reply_to: int | None = None) -> None:
        for i, part in enumerate(chunks(text)):
            extra = {}
            if reply_to and i == 0:
                extra["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            await self.api("sendMessage", chat_id=chat, text=part, disable_web_page_preview=True, **extra)

    # ── подключённые группы ──────────────────────────────────────────────────

    async def load(self) -> None:
        if self.storage is not None:
            self.allowed |= set(json.loads(await self.storage.get(STORE_KEY, "[]")))

    async def _remember(self, chat: str, drop: str = "") -> None:
        self.allowed.add(chat)
        self.allowed.discard(drop)
        if self.storage is not None:
            saved = set(json.loads(await self.storage.get(STORE_KEY, "[]")))
            saved = (saved | {chat}) - {drop}
            await self.storage.set(STORE_KEY, json.dumps(sorted(saved)))

    # ── цикл ─────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        await self.load()
        me = await self.api("getMe")
        self.username, self.bot_id = me.get("username", ""), me.get("id", 0)
        try:
            await self.api("setMyCommands", commands=COMMANDS)
        except Exception as e:
            log.warning("Telegram setMyCommands: %s", e)
        log.info("Telegram-бот @%s слушает; подключено чатов: %s", self.username, len(self.allowed))

    async def poll_once(self) -> int:
        updates = await self.api("getUpdates", offset=self._offset, timeout=self.poll_timeout,
                                 allowed_updates=["message"])
        for u in updates:
            self._offset = max(self._offset, u["update_id"] + 1)
            msg = u.get("message") or {}
            if msg.get("text") or msg.get("migrate_to_chat_id"):
                task = asyncio.create_task(self.handle(msg))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        return len(updates)

    async def run(self) -> None:
        await self.start()
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

    # ── одно сообщение ───────────────────────────────────────────────────────

    def _parse(self, msg: dict) -> tuple[str, str, bool]:
        """(команда, текст без команды и упоминания, обращаются ли к боту)."""
        text = msg.get("text", "").strip()
        command, addressed = "", False
        if text.startswith("/"):
            head, _, rest = text.partition(" ")
            name, _, target = head[1:].partition("@")
            if target and self.username and target.lower() != self.username.lower():
                return "", "", False                       # команда другому боту
            command, text, addressed = name.lower(), rest.strip(), True
        mention = f"@{self.username}".lower() if self.username else ""
        if mention and mention in text.lower():
            i = text.lower().index(mention)
            text, addressed = (text[:i] + text[i + len(mention):]).strip(" ,:"), True
        reply = msg.get("reply_to_message") or {}
        if self.bot_id and (reply.get("from") or {}).get("id") == self.bot_id:
            addressed = True
        return command, text, addressed

    async def handle(self, msg: dict) -> None:
        chat = str(msg["chat"]["id"])
        group = msg["chat"].get("type") in GROUPS
        try:
            if msg.get("migrate_to_chat_id"):                 # группа стала супергруппой — новый id
                if chat in self.allowed:
                    await self._remember(str(msg["migrate_to_chat_id"]), drop=chat)
                return
            command, text, addressed = self._parse(msg)
            if group and not addressed:
                return                                         # обычная переписка группы — не наше дело
            reply_to = msg.get("message_id") if group else None

            if command == "start" and chat not in self.allowed:
                if group and self.code and text and hmac.compare_digest(text.encode(), self.code.encode()):
                    await self._remember(chat)
                    log.info("бот: подключена группа %s «%s»", chat, msg["chat"].get("title", ""))
                    await self.send(chat, "Группа подключена.\n\n" + HELP)
                    return
            if chat not in self.allowed:
                log.warning("бот: сообщение из неподключённого чата %s", chat)
                await self.send(chat, (
                    "Группа не подключена. Администратор: отправьте здесь /start <код доступа>."
                    if group and self.code else
                    f"Нет доступа. Chat id: {chat} — его добавляют в HUB_BOT_CHATS."), reply_to)
                return
            if command in ("start", "help"):
                await self.send(chat, HELP, reply_to)
                return
            who = f"{chat}:{(msg.get('from') or {}).get('id', '')}" if group else chat
            if command == "new":
                self.agent.reset(who)
                await self.send(chat, "Начали заново.", reply_to)
                return
            if command not in ("", "ask"):
                return
            if not text:
                await self.send(chat, "Напишите вопрос: /ask остатки сиропов", reply_to)
                return
            typing = asyncio.create_task(self._typing(chat))
            try:
                answer = await self.agent.ask(who, text)
            except BotError as e:
                answer = str(e)
            except Exception:
                log.exception("бот: сбой на вопросе")
                answer = "Что-то сломалось при ответе. Попробуйте ещё раз или /new."
            finally:
                typing.cancel()
            await self.send(chat, answer, reply_to)
        except Exception:
            log.exception("бот: не удалось ответить в чат %s", chat)

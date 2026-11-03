"""Бот с LLM-интерфейсом: вопросы по МойСклад в Telegram и через POST /api/ask.

  tools.py     — что можно спросить у МойСклад (только чтение), расчёты в коде;
  agent.py     — разговор с Claude: вопрос → инструменты → ответ;
  telegram.py  — Telegram long polling, белый список чатов.

Включается, когда заданы ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN и HUB_BOT_CODE (группа
подключается командой /start <код>) или HUB_BOT_CHATS.
"""
from __future__ import annotations

from ..moysklad import MoySklad
from ..settings import Settings
from .agent import Agent, BotError
from .tools import Tools

__all__ = ["Agent", "BotError", "Tools", "make_agent", "make_tools"]


def make_tools(settings: Settings, ms: MoySklad) -> Tools:
    cfg = settings.bot
    return Tools(ms, internal=cfg.get("internal_counterparties", ["ИНТЕРНЕТ РЕШЕНИЯ"]),
                 payment_field=cfg.get("payment_field", "Оплата"))


def make_agent(settings: Settings, ms: MoySklad, client=None) -> Agent:
    if client is None:
        import anthropic  # ключ SDK берёт сам из ANTHROPIC_API_KEY
        client = anthropic.AsyncAnthropic()
    return Agent(client, make_tools(settings, ms), model=settings.bot_model,
                 effort=settings.bot_effort)

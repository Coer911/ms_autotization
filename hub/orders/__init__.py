"""Бот очереди заказов. Справочный бот hub/bot к нему не подключён.

bot.py читает один чат и ждёт «да». parse.py достаёт поля из текста.
order.py ищет карточки и создаёт заказ покупателя через клиент МойСклад.
Публичные чаты не читаются. Лояльность сюда не входит.
"""
from __future__ import annotations

from .bot import OrderQueueBot, make_order_bot

__all__ = ["OrderQueueBot", "make_order_bot"]

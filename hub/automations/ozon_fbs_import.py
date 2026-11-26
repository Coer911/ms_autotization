"""Плановый импорт отправлений Ozon FBS. В config.yaml выключен.

Включение поднимает только чтение Ozon и чтение артикулов МойСклад.
Документы МойСклад этот класс не создаёт: у него нет вызова set_state и set_fields.
"""
from __future__ import annotations

import os
from datetime import datetime

from ..ozon.client import OzonClient
from ..ozon.fbs_import import import_fbs
from . import Automation, ConfigError, Context, register


def _secret(name: str) -> str:
    return os.environ.get(name, "").strip()


@register
class OzonFbsImport(Automation):
    kind = "ozon_fbs_import"
    title = "Импорт Ozon FBS в базу хаба, без документов МойСклад"
    scheduled = True
    defaults = {"every_minutes": 15, "lookback_days": 7, "scheme": "fbs"}

    def validate(self) -> None:
        if self.scheme != "fbs":
            raise ConfigError(f"{self.name}: эта автоматизация только для scheme fbs")
        step, days = int(self.every_minutes), int(self.lookback_days)
        if step < 5 or step > 180:
            raise ConfigError(f"{self.name}: every_minutes от 5 до 180")
        if days < 1 or days > 365:
            raise ConfigError(f"{self.name}: lookback_days от 1 до 365")
        if not _secret("OZON_CLIENT_ID") or not _secret("OZON_API_KEY"):
            raise ConfigError(f"{self.name}: включён, но нет OZON_CLIENT_ID или OZON_API_KEY")

    def slot(self, now: datetime) -> str | None:
        step = int(self.every_minutes)
        bucket = (now.hour * 60 + now.minute) // step
        return f"{now:%Y-%m-%d}:{bucket}"

    async def run_scheduled(self, ctx: Context, slot: str) -> str:
        del slot
        client = OzonClient(
            ctx.settings.ozon_client_id.strip() or _secret("OZON_CLIENT_ID"),
            ctx.settings.ozon_api_key.strip() or _secret("OZON_API_KEY"))
        try:
            stats = await import_fbs(
                ctx.storage, client, ctx.ms if ctx.settings.ms_token else None,
                lookback_days=int(self.lookback_days))
        finally:
            await client.aclose()
        return stats.text()

"""Настройки хаба.

Секреты и адреса — только из переменных окружения (на Dockhost — панель проекта →
переменные окружения). Правила автоматизаций — в config.yaml: это не секрет, его
удобно держать в git и ревьюить.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

APP_DIR = Path(__file__).resolve().parent.parent
ROLES = ("all", "api", "worker", "scheduler", "bot")


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on", "да")


@dataclass
class Settings:
    ms_token: str = ""
    ms_base: str = "https://api.moysklad.ru/api/remap/1.2"
    ms_rps: int | None = None         # запросов за 3 с; None — по графику МойСклад для токена пользователя
    database_url: str = f"sqlite+aiosqlite:///{APP_DIR / 'data' / 'hub.sqlite3'}"
    role: str = "all"                 # all | api | worker | scheduler | bot
    config_path: str = str(APP_DIR / "config.yaml")
    api_key: str = ""                 # X-Api-Key для REST API хаба; пусто — API закрыт
    webhook_secret: str = ""          # часть пути вебхука /hooks/moysklad/<secret>
    public_url: str = ""              # https://… — внешний адрес, для установки вебхуков
    port: int = 8080
    poll_seconds: int = 300           # страховочный опрос; 0 — только вебхуки
    lookback_hours: int = 0           # первый старт: подхватить изменения за столько часов
    dry_run: bool = True              # True — наружу и в МойСклад ничего не пишется
    worker_batch: int = 50
    lease_seconds: int = 120
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = False
    telegram_token: str = ""
    alert_chat: str = ""              # Telegram chat_id для ошибок автоматизаций
    bot_chats: tuple[str, ...] = ()   # кому бот отвечает (chat id через запятую)
    bot_code: str = ""                # код подключения группы: /start <код> в группе
    bot_model: str = "claude-opus-5-5"
    bot_effort: str = "medium"        # low | medium | high — глубина размышлений и цена ответа
    anthropic_ready: bool = False     # задан ли ANTHROPIC_API_KEY (сам ключ читает SDK)
    automations: dict = field(default_factory=dict)
    bot: dict = field(default_factory=dict)       # секция bot: в config.yaml

    @classmethod
    def from_env(cls, env: dict | None = None) -> Settings:
        env = os.environ if env is None else env
        s = cls(
            ms_token=env.get("MOYSKLAD_TOKEN", ""),
            ms_base=env.get("MOYSKLAD_BASE", cls.ms_base),
            ms_rps=int(env["MOYSKLAD_RPS"]) if env.get("MOYSKLAD_RPS") else None,
            database_url=env.get("HUB_DATABASE_URL", cls.database_url),
            role=env.get("HUB_ROLE", cls.role),
            config_path=env.get("HUB_CONFIG", cls.config_path),
            api_key=env.get("HUB_API_KEY", ""),
            webhook_secret=env.get("HUB_WEBHOOK_SECRET", ""),
            public_url=env.get("HUB_PUBLIC_URL", "").rstrip("/"),
            port=int(env.get("PORT", cls.port)),
            poll_seconds=int(env.get("HUB_POLL_SECONDS", cls.poll_seconds)),
            lookback_hours=int(env.get("HUB_LOOKBACK_HOURS", cls.lookback_hours)),
            dry_run=_flag(env.get("HUB_DRY_RUN"), True),
            worker_batch=int(env.get("HUB_WORKER_BATCH", cls.worker_batch)),
            lease_seconds=int(env.get("HUB_LEASE_SECONDS", cls.lease_seconds)),
            smtp_host=env.get("SMTP_HOST", ""),
            smtp_port=int(env.get("SMTP_PORT", cls.smtp_port)),
            smtp_user=env.get("SMTP_USER", ""),
            smtp_password=env.get("SMTP_PASSWORD", ""),
            smtp_from=env.get("SMTP_FROM", "") or env.get("SMTP_USER", ""),
            smtp_starttls=_flag(env.get("SMTP_STARTTLS"), False),
            telegram_token=env.get("TELEGRAM_BOT_TOKEN", ""),
            alert_chat=env.get("HUB_ALERT_CHAT", ""),
            bot_chats=tuple(c.strip() for c in env.get("HUB_BOT_CHATS", "").split(",") if c.strip()),
            bot_code=env.get("HUB_BOT_CODE", "").strip(),
            bot_model=env.get("HUB_BOT_MODEL", "") or cls.bot_model,
            bot_effort=env.get("HUB_BOT_EFFORT", "") or cls.bot_effort,
            anthropic_ready=bool(env.get("ANTHROPIC_API_KEY")),
        )
        if s.role not in ROLES:
            raise ValueError(f"HUB_ROLE={s.role}: можно {', '.join(ROLES)}")
        config = load_config(s.config_path)
        s.automations = config.get("automations", {})
        s.bot = config.get("bot") or {}
        return s

    @property
    def bot_ready(self) -> bool:
        """Telegram-бот запускается, только когда настроено всё нужное."""
        return bool(self.anthropic_ready and self.telegram_token and (self.bot_chats or self.bot_code))

    @property
    def webhook_url(self) -> str:
        if not (self.public_url and self.webhook_secret):
            return ""
        return f"{self.public_url}/hooks/moysklad/{self.webhook_secret}"


def load_config(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}

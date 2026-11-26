"""Настройки из окружения. Секретов в коде нет, значения по умолчанию закрывают запись."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ozon_connect.errors import OzonError

ACCOUNT_ID = re.compile(r"^[A-Za-z0-9_]+$")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def sqlite_path(url: str) -> Path:
    """Путь из OZON_DATABASE_URL. Файл этот выпуск не создаёт и не открывает."""
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if url.startswith(prefix):
            return Path(url[len(prefix):])
    raise OzonError(
        "OZON_DATABASE_URL должен быть sqlite+aiosqlite:///… или sqlite:///…",
        "DATABASE_URL",
    )


def _flag(name: str, default: str) -> bool:
    raw = os.environ.get(name, default).strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise OzonError(f"{name} должен быть 0 или 1", "BAD_FLAG")


@dataclass(frozen=True)
class Account:
    id: str
    client_id: str
    api_key: str


@dataclass(frozen=True)
class Settings:
    database_path: Path
    dry_run: bool
    fbs_enabled: bool
    fbo_enabled: bool
    moysklad_token: str
    moysklad_rps: int | None
    accounts: tuple[Account, ...]
    port: int
    host: str = "0.0.0.0"

    @property
    def external_writes_allowed(self) -> bool:
        # Первый выпуск не умеет писать наружу, даже если OZON_DRY_RUN=0.
        return False


def load_settings() -> Settings:
    url = os.environ.get("OZON_DATABASE_URL", "sqlite+aiosqlite:////data/ozon.sqlite3")
    database_path = sqlite_path(url)
    rps_raw = os.environ.get("OZON_MOYSKLAD_RPS", "").strip()
    rps = int(rps_raw) if rps_raw else None
    if rps is not None and rps < 1:
        raise OzonError("OZON_MOYSKLAD_RPS должен быть пустым или целым больше нуля", "BAD_RPS")
    port = int(os.environ.get("PORT", "8080"))
    return Settings(
        database_path=database_path,
        dry_run=_flag("OZON_DRY_RUN", "1"),
        fbs_enabled=_flag("OZON_FBS_ENABLED", "0"),
        fbo_enabled=_flag("OZON_FBO_ENABLED", "0"),
        moysklad_token=os.environ.get("OZON_MOYSKLAD_TOKEN", "").strip(),
        moysklad_rps=rps,
        accounts=tuple(_accounts()),
        port=port,
    )


def _accounts() -> list[Account]:
    raw = os.environ.get("OZON_ACCOUNTS", "").strip()
    if not raw:
        return []
    found: list[Account] = []
    for part in raw.split(","):
        account_id = part.strip()
        if not ACCOUNT_ID.fullmatch(account_id):
            raise OzonError(
                f"идентификатор кабинета {account_id!r} должен состоять из букв, цифр и _",
                "BAD_ACCOUNT",
            )
        suffix = account_id.upper()
        client_id = os.environ.get(f"OZON_ACCOUNT_{suffix}_CLIENT_ID", "").strip()
        api_key = os.environ.get(f"OZON_ACCOUNT_{suffix}_API_KEY", "").strip()
        if not client_id or not api_key:
            raise OzonError(
                f"для кабинета {account_id} задайте OZON_ACCOUNT_{suffix}_CLIENT_ID "
                f"и OZON_ACCOUNT_{suffix}_API_KEY",
                "ACCOUNT_SECRET",
            )
        found.append(Account(account_id, client_id, api_key))
    return found

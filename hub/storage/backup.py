"""Согласованная копия файла SQLite. Чужой файл не перезаписывается.

PostgreSQL этой командой не копируется: у хаба на Dockhost один файл
``/data/hub.sqlite3``. Совместимость ядра с PostgreSQL остаётся в ``normalize_url``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import normalize_url


def sqlite_file(url: str) -> Path | None:
    normalized = normalize_url(url)
    if not normalized.startswith("sqlite") or ":memory:" in normalized or "///" not in normalized:
        return None
    return Path(normalized.split("///", 1)[1])


def backup_sqlite(url: str, dest: Path) -> Path:
    source_path = sqlite_file(url)
    if source_path is None:
        raise ValueError("резервная копия этой командой только для файла SQLite")
    if not source_path.exists():
        raise ValueError(f"нет файла базы {source_path}")
    dest = Path(dest)
    if dest.exists():
        raise ValueError(f"файл уже есть, не перезаписываю: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(source_path)
    try:
        source.execute("PRAGMA wal_checkpoint(PASSIVE)")
        target = sqlite3.connect(dest)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return dest

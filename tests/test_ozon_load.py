"""100 000 ключей и восстановление SQLite после убийства процесса.

Нагрузочный прогон нужен для файла хаба на Dockhost. На PostgreSQL в CI он
пропускается: ядро с PostgreSQL совместимо, но это внедрение его не поднимает.
"""
import asyncio
import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import fresh_storage

from hub.ozon.ids import operation_key
from hub.storage import iso, now
from hub.storage.backup import backup_sqlite
from hub.storage.ozon import count_operations, insert_operations

LOAD = 100_000


def _rows(start: int, count: int) -> list[dict]:
    created = iso(now())
    rows = []
    for number in range(start, start + count):
        posting = f"p{number:06d}"
        rows.append({
            "idempotency_key": operation_key("load", "fbs", posting, "import"),
            "cabinet_id": "load",
            "scheme": "fbs",
            "posting_number": posting,
            "operation": "import",
            "position_key": "",
            "status": "recorded",
            "created_at": created,
        })
    return rows


async def test_hundred_thousand_keys_do_not_duplicate():
    storage = await fresh_storage()
    if not storage.is_sqlite:
        await storage.close()
        pytest.skip("нагрузочный тест этого внедрения считается на SQLite")
    try:
        await insert_operations(storage, _rows(0, LOAD))
        assert await count_operations(storage) == LOAD
        await insert_operations(storage, _rows(0, LOAD))
        assert await count_operations(storage) == LOAD
        await insert_operations(storage, _rows(LOAD, 1))
        assert await count_operations(storage) == LOAD + 1
    finally:
        await storage.close()


def test_sqlite_backup_matches_source_and_does_not_overwrite(tmp_path):
    db = tmp_path / "hub.sqlite3"
    url = f"sqlite+aiosqlite:///{db}"

    async def fill():
        from hub.storage import Storage
        storage = await Storage.open(url)
        try:
            await insert_operations(storage, _rows(0, 3))
        finally:
            await storage.close()

    asyncio.run(fill())
    copy = backup_sqlite(url, tmp_path / "backups" / "hub.sqlite3")
    with sqlite3.connect(copy) as conn:
        assert conn.execute("select count(*) from ozon_operations").fetchone()[0] == 3
        assert conn.execute("pragma integrity_check").fetchone()[0] == "ok"
    with pytest.raises(ValueError):
        backup_sqlite(url, copy)


def test_process_kill_rolls_back_uncommitted_rows(tmp_path):
    db = tmp_path / "hub.sqlite3"
    marker = tmp_path / "ready"
    proc = subprocess.run(
        [sys.executable, __file__, str(db), str(marker)],
        check=False,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert marker.exists()
    assert proc.returncode < 0 or proc.returncode == 128 + signal.SIGKILL
    with sqlite3.connect(db) as conn:
        assert conn.execute("pragma integrity_check").fetchone()[0] == "ok"
        assert conn.execute("pragma journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("select count(*) from ozon_operations").fetchone()[0] == 20
        assert conn.execute(
            "select count(*) from ozon_operations where posting_number = 'p000099'").fetchone()[0] == 0

    async def after():
        from hub.storage import Storage
        storage = await Storage.open(f"sqlite+aiosqlite:///{db}")
        try:
            assert await count_operations(storage) == 20
            await insert_operations(storage, _rows(20, 1))
            assert await count_operations(storage) == 21
        finally:
            await storage.close()

    asyncio.run(after())


async def _crash(db: str, marker: str) -> None:
    from hub.storage import Storage
    url = f"sqlite+aiosqlite:///{db}"
    storage = await Storage.open(url)
    await insert_operations(storage, _rows(0, 20))
    await storage.close()
    storage = await Storage.open(url)
    async with storage._tx() as conn:
        from hub.storage.ozon import ozon_operations
        statement = storage._insert(ozon_operations)
        await conn.execute(statement, _rows(20, 80))
        Path(marker).write_text("ready", encoding="utf-8")
        os.kill(os.getpid(), signal.SIGKILL)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    asyncio.run(_crash(sys.argv[1], sys.argv[2]))

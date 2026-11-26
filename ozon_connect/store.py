"""SQLite карты Ozon Connect. Файл открывает только команда импорта."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ozon_connect.mapping import Audit, Link, candidates_blob, candidates_from_blob, skus_blob, skus_from_blob

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL COLLATE BINARY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS mappings (
    account_id TEXT NOT NULL COLLATE BINARY,
    offer_id TEXT NOT NULL COLLATE BINARY,
    ozon_product_id TEXT,
    ozon_skus TEXT,
    ms_id TEXT,
    ms_type TEXT,
    ms_article TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    candidates TEXT,
    created_at TEXT NOT NULL,
    last_verified_at TEXT NOT NULL,
    PRIMARY KEY (account_id, offer_id),
    CHECK (account_id <> ''),
    CHECK (offer_id <> ''),
    CHECK (status IN (
        'matched', 'ambiguous', 'missing', 'archived', 'service', 'empty_article', 'conflict'
    ))
);
CREATE TABLE IF NOT EXISTS catalog_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL COLLATE BINARY,
    run_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    entity_type TEXT
);
"""


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (1, ?)",
            ("schema",),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def begin_run(self, account_id: str, now: str) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO import_runs (account_id, started_at, status, reason)
            VALUES (?, ?, 'incomplete', 'incomplete_fetch')
            """,
            (account_id, now),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def fail_run(self, run_id: int, now: str, error: str) -> None:
        self.conn.execute(
            """
            UPDATE import_runs
            SET finished_at = ?, status = 'incomplete', reason = 'incomplete_fetch', error = ?
            WHERE id = ?
            """,
            (now, error, run_id),
        )
        self.conn.commit()

    def latest_run(self, account_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT * FROM import_runs
            WHERE account_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (account_id,),
        ).fetchone()

    def load_mappings(self, account_id: str) -> dict[str, Link]:
        rows = self.conn.execute(
            "SELECT * FROM mappings WHERE account_id = ? ORDER BY offer_id",
            (account_id,),
        ).fetchall()
        return {row["offer_id"]: _link(row) for row in rows}

    def load_audit(self, account_id: str) -> list[Audit]:
        rows = self.conn.execute(
            """
            SELECT source, source_id, kind, entity_type
            FROM catalog_audit
            WHERE account_id = ?
            ORDER BY source, kind, source_id
            """,
            (account_id,),
        ).fetchall()
        return [Audit(row["source"], row["source_id"], row["kind"], row["entity_type"]) for row in rows]

    def publish(self, account_id: str, run_id: int, links: list[Link], audits: list[Audit], now: str) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("DELETE FROM mappings WHERE account_id = ?", (account_id,))
            self.conn.executemany(
                """
                INSERT INTO mappings (
                    account_id, offer_id, ozon_product_id, ozon_skus, ms_id, ms_type, ms_article,
                    status, reason, candidates, created_at, last_verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        account_id,
                        link.offer_id,
                        link.ozon_product_id,
                        skus_blob(link.ozon_skus),
                        link.ms_id,
                        link.ms_type,
                        link.ms_article,
                        link.status,
                        link.reason,
                        candidates_blob(link.candidates),
                        link.created_at,
                        link.last_verified_at,
                    )
                    for link in links
                ],
            )
            self.conn.execute("DELETE FROM catalog_audit WHERE account_id = ?", (account_id,))
            self.conn.executemany(
                """
                INSERT INTO catalog_audit (account_id, run_id, source, source_id, kind, entity_type)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (account_id, run_id, item.source, item.source_id, item.kind, item.entity_type)
                    for item in audits
                ],
            )
            self.conn.execute(
                """
                UPDATE import_runs
                SET finished_at = ?, status = 'complete', reason = NULL, error = NULL
                WHERE id = ?
                """,
                (now, run_id),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise


def _link(row: sqlite3.Row) -> Link:
    return Link(
        offer_id=row["offer_id"],
        ozon_product_id=row["ozon_product_id"],
        ozon_skus=skus_from_blob(row["ozon_skus"]),
        ms_id=row["ms_id"],
        ms_type=row["ms_type"],
        ms_article=row["ms_article"],
        status=row["status"],
        reason=row["reason"],
        candidates=candidates_from_blob(row["candidates"]),
        created_at=row["created_at"],
        last_verified_at=row["last_verified_at"],
    )

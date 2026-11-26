"""Таблицы Ozon в той же базе, что очередь и журнал хаба.

Сюда кладётся снимок отправления, а не документ МойСклад и не сырой ответ API.
Повторный импорт того же отправления обновляет одну строку.
Удаление строки здесь заменяет состав нашего снимка и не трогает МойСклад.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import (
    Column,
    Integer,
    PrimaryKeyConstraint,
    String,
    Table,
    UniqueConstraint,
    delete,
    func,
    select,
)

from ..ozon.ids import operation_key
from ..ozon.match import Match
from ..ozon.redact import PostingSnapshot
from . import iso, metadata, now

_PK = ("cabinet_id", "scheme", "posting_number")

ozon_postings = Table(
    "ozon_postings", metadata,
    Column("cabinet_id", String(64), nullable=False),
    Column("scheme", String(8), nullable=False),
    Column("posting_number", String(64), nullable=False),
    Column("order_id", String(64), nullable=False, default=""),
    Column("order_number", String(64), nullable=False, default=""),
    Column("status", String(64), nullable=False),
    Column("substatus", String(64), nullable=False, default=""),
    Column("tpl_integration_type", String(32), nullable=False, default=""),
    Column("warehouse_id", String(64), nullable=False, default=""),
    Column("warehouse", String(255), nullable=False, default=""),
    Column("in_process_at", String(40), nullable=False, default=""),
    Column("shipment_date", String(40), nullable=False, default=""),
    Column("delivering_date", String(40), nullable=False, default=""),
    Column("parent_posting_number", String(64), nullable=False, default=""),
    Column("is_express", Integer, nullable=False, default=0),
    Column("cancel_reason_id", String(64), nullable=False, default=""),
    Column("cancellation_type", String(32), nullable=False, default=""),
    Column("cancelled_after_ship", Integer, nullable=False, default=0),
    Column("source_fingerprint", String(64), nullable=False),
    Column("imported_at", String(20), nullable=False),
    PrimaryKeyConstraint(*_PK, name="ozon_postings_pk"),
)

ozon_posting_lines = Table(
    "ozon_posting_lines", metadata,
    Column("cabinet_id", String(64), nullable=False),
    Column("scheme", String(8), nullable=False),
    Column("posting_number", String(64), nullable=False),
    Column("line_key", String(16), nullable=False),
    Column("offer_id", String(255), nullable=False, default=""),
    Column("sku", String(64), nullable=False, default=""),
    Column("quantity", Integer, nullable=False),
    Column("price", String(32), nullable=False, default=""),
    Column("currency_code", String(8), nullable=False, default=""),
    Column("match_status", String(16), nullable=False),
    Column("ms_type", String(16), nullable=False, default=""),
    Column("ms_id", String(64), nullable=False, default=""),
    PrimaryKeyConstraint(*_PK, "line_key", name="ozon_posting_lines_pk"),
)

ozon_operations = Table(
    "ozon_operations", metadata,
    Column("idempotency_key", String(255), nullable=False),
    Column("cabinet_id", String(64), nullable=False),
    Column("scheme", String(8), nullable=False),
    Column("posting_number", String(64), nullable=False),
    Column("operation", String(32), nullable=False),
    Column("position_key", String(80), nullable=False, default=""),
    Column("status", String(16), nullable=False),
    Column("created_at", String(20), nullable=False),
    PrimaryKeyConstraint("idempotency_key", name="ozon_operations_pk"),
    UniqueConstraint("cabinet_id", "scheme", "posting_number", "operation", "position_key",
                     name="ozon_operations_natural"),
)


def _fingerprint(header: dict, lines: list[dict]) -> str:
    """Отпечаток данных Ozon. Сопоставление с МойСклад в него не входит."""
    body = {
        "header": {key: header[key] for key in header if key not in ("imported_at", "source_fingerprint")},
        "lines": [{key: row[key] for key in (
            "line_key", "offer_id", "sku", "quantity", "price", "currency_code")} for row in lines],
    }
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _header(cabinet_id: str, scheme: str, snapshot: PostingSnapshot, fingerprint: str, imported_at: str) -> dict:
    return {
        "cabinet_id": cabinet_id,
        "scheme": scheme,
        "posting_number": snapshot.posting_number,
        "order_id": snapshot.order_id,
        "order_number": snapshot.order_number,
        "status": snapshot.status,
        "substatus": snapshot.substatus,
        "tpl_integration_type": snapshot.tpl_integration_type,
        "warehouse_id": snapshot.warehouse_id,
        "warehouse": snapshot.warehouse,
        "in_process_at": snapshot.in_process_at,
        "shipment_date": snapshot.shipment_date,
        "delivering_date": snapshot.delivering_date,
        "parent_posting_number": snapshot.parent_posting_number,
        "is_express": snapshot.is_express,
        "cancel_reason_id": snapshot.cancel_reason_id,
        "cancellation_type": snapshot.cancellation_type,
        "cancelled_after_ship": snapshot.cancelled_after_ship,
        "source_fingerprint": fingerprint,
        "imported_at": imported_at,
    }


def _lines(cabinet_id: str, scheme: str, snapshot: PostingSnapshot, matches: list[Match]) -> list[dict]:
    if len(matches) != len(snapshot.lines):
        raise ValueError("число сопоставлений не равно числу строк отправления")
    rows = []
    for line, match in zip(snapshot.lines, matches, strict=True):
        rows.append({
            "cabinet_id": cabinet_id,
            "scheme": scheme,
            "posting_number": snapshot.posting_number,
            "line_key": line.line_key,
            "offer_id": line.offer_id,
            "sku": line.sku,
            "quantity": line.quantity,
            "price": line.price,
            "currency_code": line.currency_code,
            "match_status": match.status,
            "ms_type": match.ms_type,
            "ms_id": match.ms_id,
        })
    return rows


async def _replace_lines(storage, conn, line_rows: list[dict], line_filter) -> None:
    await conn.execute(delete(ozon_posting_lines).where(*line_filter))
    if line_rows:
        await conn.execute(storage._insert(ozon_posting_lines), line_rows)


def _pk(cabinet_id: str, scheme: str, posting_number: str):
    return (
        ozon_postings.c.cabinet_id == cabinet_id,
        ozon_postings.c.scheme == scheme,
        ozon_postings.c.posting_number == posting_number,
    )


async def save_posting(storage, cabinet_id: str, scheme: str, snapshot: PostingSnapshot,
                       matches: list[Match], *, preserve_matches: bool = False) -> str:
    """Записать снимок один раз на отправление. Вернуть inserted, updated или unchanged.

    Сначала читаем строку. Если запись уже есть и состав не изменился, вторую не создаём.
    Повтор при неясном исходе упирается в первичный ключ, а не в новый документ.
    Без каталога МойСклад уже записанное сопоставление не затирается.
    """
    if scheme not in ("fbs", "fbo"):
        raise ValueError(f"неизвестная схема {scheme}")
    line_rows = _lines(cabinet_id, scheme, snapshot, matches)
    imported_at = iso(now())
    header = _header(cabinet_id, scheme, snapshot, "", imported_at)
    header["source_fingerprint"] = _fingerprint(header, line_rows)
    key = operation_key(cabinet_id, scheme, snapshot.posting_number, "import")
    pk = _pk(cabinet_id, scheme, snapshot.posting_number)
    line_filter = (
        ozon_posting_lines.c.cabinet_id == cabinet_id,
        ozon_posting_lines.c.scheme == scheme,
        ozon_posting_lines.c.posting_number == snapshot.posting_number,
    )
    async with storage._tx() as conn:
        previous = (await conn.execute(
            select(ozon_postings.c.source_fingerprint).where(*pk))).scalar()
        same_ozon = previous == header["source_fingerprint"]
        if previous is not None and same_ozon and preserve_matches:
            outcome = "unchanged"
        elif previous is not None and same_ozon:
            stored = [dict(row._mapping) for row in (await conn.execute(
                select(ozon_posting_lines).where(*line_filter))).all()]
            current = {(row["line_key"], row["match_status"], row["ms_type"], row["ms_id"]) for row in stored}
            wanted = {(row["line_key"], row["match_status"], row["ms_type"], row["ms_id"]) for row in line_rows}
            if current == wanted:
                outcome = "unchanged"
            else:
                await _replace_lines(storage, conn, line_rows, line_filter)
                outcome = "updated"
        else:
            if preserve_matches and previous is not None:
                stored = [dict(row._mapping) for row in (await conn.execute(
                    select(ozon_posting_lines).where(*line_filter))).all()]
                kept = {(row["line_key"], row["offer_id"]): row for row in stored}
                for row in line_rows:
                    old = kept.get((row["line_key"], row["offer_id"]))
                    if old is not None:
                        row["match_status"] = old["match_status"]
                        row["ms_type"] = old["ms_type"]
                        row["ms_id"] = old["ms_id"]
            statement = storage._insert(ozon_postings).values(**header)
            statement = statement.on_conflict_do_update(
                index_elements=["cabinet_id", "scheme", "posting_number"],
                set_={name: statement.excluded[name] for name in header if name not in _PK},
            )
            await conn.execute(statement)
            await _replace_lines(storage, conn, line_rows, line_filter)
            outcome = "inserted" if previous is None else "updated"
        operation = storage._insert(ozon_operations).values(
            idempotency_key=key, cabinet_id=cabinet_id, scheme=scheme,
            posting_number=snapshot.posting_number, operation="import", position_key="",
            status="recorded", created_at=imported_at,
        ).on_conflict_do_nothing(index_elements=["idempotency_key"])
        await conn.execute(operation)
    return outcome


async def get_posting(storage, cabinet_id: str, scheme: str, posting_number: str) -> dict | None:
    async with storage._ro() as conn:
        row = (await conn.execute(select(ozon_postings).where(*_pk(cabinet_id, scheme, posting_number)))).first()
    return dict(row._mapping) if row else None


async def list_lines(storage, cabinet_id: str, scheme: str, posting_number: str) -> list[dict]:
    query = (select(ozon_posting_lines)
             .where(ozon_posting_lines.c.cabinet_id == cabinet_id,
                    ozon_posting_lines.c.scheme == scheme,
                    ozon_posting_lines.c.posting_number == posting_number)
             .order_by(ozon_posting_lines.c.line_key))
    async with storage._ro() as conn:
        rows = (await conn.execute(query)).all()
    return [dict(row._mapping) for row in rows]


async def count_operations(storage) -> int:
    async with storage._ro() as conn:
        return (await conn.execute(select(func.count()).select_from(ozon_operations))).scalar_one()


async def insert_operations(storage, rows: list[dict], chunk: int = 400) -> None:
    """Пакет ключей. Повтор того же ключа не создаёт вторую строку."""
    statement = storage._insert(ozon_operations).on_conflict_do_nothing(
        index_elements=["idempotency_key"])
    async with storage._tx() as conn:
        for start in range(0, len(rows), chunk):
            await conn.execute(statement, rows[start:start + chunk])

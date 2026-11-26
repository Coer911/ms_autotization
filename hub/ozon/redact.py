"""Оставить от отправления FBS только складские поля.

Имя, телефон и адрес покупателя в базу не попадают, даже если Ozon прислал их
вопреки ``with``. Деньги и комиссия тоже не сохраняются: доставка не считается
выплатой, а финансовый блок на этом этапе не запрашивается.
"""
from __future__ import annotations

from dataclasses import dataclass

from .client import OzonContractError


@dataclass(frozen=True)
class LineSnapshot:
    line_key: str
    offer_id: str
    sku: str
    quantity: int
    price: str
    currency_code: str


@dataclass(frozen=True)
class PostingSnapshot:
    posting_number: str
    order_id: str
    order_number: str
    status: str
    substatus: str
    tpl_integration_type: str
    warehouse_id: str
    warehouse: str
    in_process_at: str
    shipment_date: str
    delivering_date: str
    parent_posting_number: str
    is_express: int
    cancel_reason_id: str
    cancellation_type: str
    cancelled_after_ship: int
    lines: tuple[LineSnapshot, ...]


def _text(value: object, limit: int, field: str, *, required: bool = False) -> str:
    if value is None:
        if required:
            raise OzonContractError(f"в отправлении нет {field}")
        return ""
    if not isinstance(value, str):
        raise OzonContractError(f"{field} должен быть строкой")
    if len(value) > limit:
        raise OzonContractError(f"{field} длиннее {limit}")
    if required and not value.strip():
        raise OzonContractError(f"пустой {field}")
    return value


def _id(value: object, field: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise OzonContractError(f"{field} должен быть числом или строкой")
    text = str(value)
    if len(text) > 64:
        raise OzonContractError(f"{field} длиннее 64")
    return text


def _flag(value: object, field: str) -> int:
    if value is None:
        return 0
    if not isinstance(value, bool):
        raise OzonContractError(f"{field} должен быть true или false")
    return 1 if value else 0


def _line(raw: object, index: int) -> LineSnapshot:
    if not isinstance(raw, dict):
        raise OzonContractError(f"товар {index} в отправлении не объект")
    offer_id = _text(raw.get("offer_id"), 255, f"offer_id товара {index}")
    sku = _id(raw.get("sku"), f"sku товара {index}")
    quantity = raw.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
        raise OzonContractError(f"quantity товара {index} должно быть целым неотрицательным")
    price = raw.get("price", "")
    if price is None:
        price = ""
    if not isinstance(price, str) or len(price) > 32:
        raise OzonContractError(f"price товара {index} должна быть строкой, как в Seller API")
    currency = _text(raw.get("currency_code"), 8, f"currency_code товара {index}")
    # Номер позиции с ведущими нулями, чтобы порядок строк не зависел от текстовой сортировки.
    return LineSnapshot(f"{index:04d}", offer_id, sku, quantity, price, currency)


def redact_posting(raw: object) -> PostingSnapshot:
    """Собрать снимок. Любое поле вне белого списка отбрасывается."""
    if not isinstance(raw, dict):
        raise OzonContractError("отправление не объект")
    products = raw.get("products", [])
    if not isinstance(products, list) or len(products) > 500:
        raise OzonContractError("products должен быть списком не длиннее 500")
    delivery = raw.get("delivery_method") or {}
    if not isinstance(delivery, dict):
        raise OzonContractError("delivery_method должен быть объектом")
    cancellation = raw.get("cancellation") or {}
    if not isinstance(cancellation, dict):
        raise OzonContractError("cancellation должен быть объектом")
    lines = tuple(_line(item, index) for index, item in enumerate(products))
    return PostingSnapshot(
        posting_number=_text(raw.get("posting_number"), 64, "posting_number", required=True),
        order_id=_id(raw.get("order_id"), "order_id"),
        order_number=_text(raw.get("order_number"), 64, "order_number"),
        status=_text(raw.get("status"), 64, "status", required=True),
        substatus=_text(raw.get("substatus"), 64, "substatus"),
        tpl_integration_type=_text(raw.get("tpl_integration_type"), 32, "tpl_integration_type"),
        warehouse_id=_id(delivery.get("warehouse_id"), "warehouse_id"),
        warehouse=_text(delivery.get("warehouse"), 255, "warehouse"),
        in_process_at=_text(raw.get("in_process_at"), 40, "in_process_at"),
        shipment_date=_text(raw.get("shipment_date"), 40, "shipment_date"),
        delivering_date=_text(raw.get("delivering_date"), 40, "delivering_date"),
        parent_posting_number=_text(raw.get("parent_posting_number"), 64, "parent_posting_number"),
        is_express=_flag(raw.get("is_express"), "is_express"),
        cancel_reason_id=_id(cancellation.get("cancel_reason_id"), "cancel_reason_id"),
        cancellation_type=_text(cancellation.get("cancellation_type"), 32, "cancellation_type"),
        cancelled_after_ship=_flag(cancellation.get("cancelled_after_ship"), "cancelled_after_ship"),
        lines=lines,
    )

"""Клиент Seller API только для чтения списка FBS.

Вызывается один метод: POST /v4/posting/fbs/list.
POST /v3/posting/fbs/list отключён Ozon 01.06.2026 (раздел документации
от 30.04.2026, зафиксирован в gam6itko/ozon-seller#115). Ответ v4 — это не
ответ v3: ``postings``, ``has_next`` и ``cursor`` лежат на верхнем уровне,
а не в ``result``. Так сверили на живом кабинете 22.08.2026
(DeviceIngineering/ozon-mcp-server#6).

Тело запроса — по клиенту gam6itko FbsService::list, операция PostingFbsList:
``cursor``, ``filter.since``, ``filter.to``, ``limit``, ``sort_dir``, ``with``.
``filter.statuses`` не передаём, чтобы за период пришли все статусы.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

BASE = "https://api-seller.ozon.ru"
LIST_PATH = "/v4/posting/fbs/list"
PAGE_LIMIT = 100


class OzonError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Ozon {status}: {message}" if status else f"Ozon: {message}")
        self.status = status
        self.message = message


class OzonContractError(OzonError):
    def __init__(self, message: str):
        super().__init__(0, message)


@dataclass(frozen=True)
class FbsPage:
    postings: list[dict]
    cursor: str
    has_next: bool


def parse_fbs_list(payload: object) -> FbsPage:
    """Разобрать ответ v4. Форму v3 (``result.postings``) не подменяем пустым списком."""
    if not isinstance(payload, dict) or "postings" not in payload:
        raise OzonContractError(
            "ответ /v4/posting/fbs/list должен содержать postings на верхнем уровне")
    postings = payload["postings"]
    has_next = payload.get("has_next")
    cursor = payload.get("cursor", "")
    if not isinstance(postings, list):
        raise OzonContractError("postings должен быть списком")
    if not isinstance(has_next, bool):
        raise OzonContractError("has_next должен быть true или false")
    if not isinstance(cursor, str):
        raise OzonContractError("cursor должен быть строкой")
    if any(not isinstance(item, dict) for item in postings):
        raise OzonContractError("элемент postings должен быть объектом")
    return FbsPage(postings, cursor, has_next)


class OzonClient:
    """Один кабинет. Ключ остаётся в заголовке и не попадает в текст ошибки."""

    def __init__(self, client_id: str, api_key: str, *,
                 transport: httpx.AsyncBaseTransport | None = None, base: str = BASE):
        client_id, api_key = client_id.strip(), api_key.strip()
        if not client_id or not api_key:
            raise ValueError("нужны OZON_CLIENT_ID и OZON_API_KEY")
        self.client_id = client_id
        self._key = api_key
        # Редирект не идём: иначе Api-Key мог бы уехать на другой хост.
        self._http = httpx.AsyncClient(
            base_url=base.rstrip("/"),
            timeout=httpx.Timeout(30.0, connect=10.0),
            transport=transport,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> OzonClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _hide(self, text: str) -> str:
        return text.replace(self._key, "***")[:500]

    async def _post(self, path: str, body: dict) -> Any:
        if path != LIST_PATH:
            raise OzonContractError(f"этот клиент не вызывает {path}")
        try:
            response = await self._http.post(path, json=body, headers={
                "Client-Id": self.client_id,
                "Api-Key": self._key,
                "Content-Type": "application/json",
            })
        except httpx.TransportError as exc:
            raise OzonError(0, "нет связи с api-seller.ozon.ru") from exc
        if response.status_code in (301, 302, 303, 307, 308):
            raise OzonError(response.status_code, "редирект Seller API не принимается")
        if response.status_code >= 400:
            raise OzonError(response.status_code, self._hide(response.text) or response.reason_phrase)
        try:
            return response.json()
        except ValueError as exc:
            raise OzonContractError("Seller API вернул не JSON") from exc

    async def list_fbs_page(self, since: str, to: str, cursor: str = "") -> FbsPage:
        if not since or not to or since >= to:
            raise OzonContractError("период since/to пустой или перевёрнут")
        body = {
            "cursor": cursor,
            "filter": {"since": since, "to": to},
            "limit": PAGE_LIMIT,
            "sort_dir": "ASC",
            "translit": False,
            "with": {
                "analytics_data": False,
                "barcodes": False,
                "financial_data": False,
                "legal_info": False,
            },
        }
        return parse_fbs_list(await self._post(LIST_PATH, body))

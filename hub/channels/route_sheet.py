"""Дневной маршрутный лист в Google Sheets.

Контракт для плагина — семантические поля (контрагент, адрес, телефон, часы,
комментарий, скрытый id). Куда они ложатся на листе, знает только этот модуль:
владелец прислал структуру вкладки «06.03.2026», не номера «на глаз».

Вкладка дневного листа:
  строка 1 — A1:B1 «МАРШРУТНЫЙ ЛИСТ», C1:D1 «дата доставки: ДД.ММ.ГГГГ»,
             E1:G1 исполнитель (хаб не пишет);
  строка 2 — заголовки A–G;
  данные с строки 3. A «Гр.м.» и G «Выполнено» не пишутся.
Скрытый id заказа — лист `_hub`, колонки A–G из-за него не сдвигаются.
Вкладки «Адреса», «Адреса скорая почта на 1 август» и «08.09.2025»
в копию дневного файла не переносятся.

Секрет — только GOOGLE_SERVICE_ACCOUNT_JSON. Без него модуль импортируется,
а боевой вызов падает ChannelError (это повтор хаба, не падение процесса).
"""
from __future__ import annotations

import json
import logging
import os
from urllib.parse import quote

import httpx

from . import ChannelError

log = logging.getLogger("hub.channels.route")

# drive — минимальный scope, которым сервисный аккаунт видит папку и шаблон,
# расшаренные на его client_email. drive.file их не видит: это не файлы,
# созданные самим приложением. Код ходит только в folder_id и template_file_id,
# файлы не удаляет и шаблон не изменяет.
# spreadsheets — чтение и запись ячеек уже созданной копии.
SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
)
DRIVE = "https://www.googleapis.com/drive/v3"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
HUB_MARK = "logistics_sheet"
HUB_SHEET = "_hub"
DATA_START_ROW = 3
# Точные имена вкладок рабочего файла, которые не являются дневным маршрутом.
EXCLUDED_SHEETS = frozenset({
    "Адреса",
    "Адреса скорая почта на 1 август",
    "08.09.2025",
})
# Шапка строки 2. Перевод строки в «Часы приемки» схлопывается при сравнении.
EXPECTED_HEADERS = (
    "гр.м.",
    "контрагент",
    "адрес",
    "телефон",
    "часы приемки",
    "комментарий",
    "выполнено",
)
# Видимые поля, которые хаб имеет право писать. A и G здесь нет.
WRITABLE = {
    "counterparty": "B",
    "address": "C",
    "phone": "D",
    "hours": "E",
    "comment": "F",
}
FORBIDDEN = frozenset({"group", "done", "executor"})


def file_title(day: str) -> str:
    """Имя файла. Тире — U+2014, как в формулировке владельца."""
    return f"Маршрутный лист — {day}"


def date_label(day: str) -> str:
    """Текст C1. Подпись взята из шаблона, дата — дата маршрута."""
    return f"дата доставки: {day}"


def header_key(value: str) -> str:
    return " ".join(value.replace("\n", " ").split()).casefold()


def headers_match(row: list[str]) -> bool:
    return tuple(header_key(c) for c in row) == EXPECTED_HEADERS


def a1(sheet: str, cells: str) -> str:
    escaped = sheet.replace("'", "''")
    return f"'{escaped}'!{cells}"


def value_ranges(sheet: str, row: int, fields: dict, hub_row: int) -> list[dict]:
    """Диапазоны одной записи. Колонки A и G и исполнитель сюда не попадают."""
    overlap = FORBIDDEN & set(fields)
    if overlap:
        raise ChannelError(f"маршрут: ручные поля не пишем ({', '.join(sorted(overlap))})")
    if "order_id" not in fields:
        raise ChannelError("маршрут: нет id заказа")
    data = []
    for key, column in WRITABLE.items():
        if key in fields and fields[key] is not None:
            data.append({"range": a1(sheet, f"{column}{row}"), "values": [[fields[key]]]})
    data.append({
        "range": a1(HUB_SHEET, f"A{hub_row}:B{hub_row}"),
        "values": [[fields["order_id"], row]],
    })
    return data


class _TokenResponse:
    def __init__(self, status: int, data: bytes, headers: dict):
        self.status = status
        self.data = data
        self.headers = headers


class _TokenRequest:
    """Синхронный транспорт для google.auth. Библиотека requests не нужна."""

    def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
        try:
            response = httpx.request(method, url, content=body, headers=headers,
                                     timeout=timeout or 30)
        except httpx.HTTPError as e:
            raise ChannelError(f"Google токен: {type(e).__name__}") from e
        return _TokenResponse(response.status_code, response.content, dict(response.headers))


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except json.JSONDecodeError:
        return ""
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err.get("status") or "")[:200]
    return ""


class GoogleRouteSheet:
    """Sheets API + Drive API. Успех — только если в ответе есть ожидаемое поле."""

    def __init__(self, http: httpx.AsyncClient, *, token: str = "", raw_json: str = ""):
        self.http = http
        self._token = token
        self._raw = raw_json
        self._creds = None

    @classmethod
    def from_env(cls, http: httpx.AsyncClient) -> GoogleRouteSheet:
        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        if not raw:
            raise ChannelError("Google не настроен: GOOGLE_SERVICE_ACCOUNT_JSON")
        return cls(http, raw_json=raw)

    def _headers(self) -> dict:
        if not self._token:
            self._token = self._mint()
        return {"Authorization": f"Bearer {self._token}"}

    def _mint(self) -> str:
        try:
            info = json.loads(self._raw)
        except json.JSONDecodeError as e:
            raise ChannelError("GOOGLE_SERVICE_ACCOUNT_JSON не JSON") from e
        if not isinstance(info, dict) or not info.get("client_email") or not info.get("private_key"):
            raise ChannelError("GOOGLE_SERVICE_ACCOUNT_JSON без client_email или private_key")
        try:
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
            creds.refresh(_TokenRequest())
        except ChannelError:
            raise
        except Exception as e:
            log.error("токен Google: %s", type(e).__name__)
            raise ChannelError("не удалось получить токен Google") from e
        if not creds.token:
            raise ChannelError("Google не выдал токен")
        self._creds = creds
        return creds.token

    async def _request(self, method: str, url: str, *, body: dict | None = None,
                       params: dict | None = None) -> dict:
        try:
            response = await self.http.request(method, url, headers=self._headers(),
                                               json=body, params=params)
        except httpx.HTTPError as e:
            raise ChannelError(f"Google сеть: {type(e).__name__}") from e
        if response.status_code >= 400:
            detail = _error_text(response)
            raise ChannelError(f"Google {response.status_code}" + (f": {detail}" if detail else ""))
        try:
            payload = response.json()
        except json.JSONDecodeError as e:
            raise ChannelError("Google вернул не JSON") from e
        if not isinstance(payload, dict):
            raise ChannelError("Google вернул не объект")
        return payload

    async def find_file(self, folder_id: str, title: str) -> str | None:
        """Один файл с точным именем в папке, не в корзине. Больше одного — ошибка, без удаления."""
        query = (
            f"name = '{_escape(title)}' and '{_escape(folder_id)}' in parents and trashed = false"
        )
        found: list[str] = []
        page = None
        while True:
            params = {
                "q": query,
                "fields": "files(id),nextPageToken",
                "pageSize": "10",
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page:
                params["pageToken"] = page
            payload = await self._request("GET", f"{DRIVE}/files", params=params)
            if "files" not in payload:
                raise ChannelError("Drive не подтвердил список files")
            for item in payload["files"]:
                if not item.get("id"):
                    raise ChannelError("Drive вернул файл без id")
                found.append(item["id"])
            page = payload.get("nextPageToken")
            if not page or len(found) > 1:
                break
        if len(found) > 1:
            raise ChannelError(f"несколько файлов «{title}», новый не создаю")
        return found[0] if found else None

    async def create_file(self, folder_id: str, template_id: str, title: str, day: str) -> str:
        if not template_id:
            raise ChannelError("нет template_file_id, файл не создаю")
        payload = await self._request(
            "POST", f"{DRIVE}/files/{template_id}/copy",
            params={"supportsAllDrives": "true", "fields": "id"},
            body={
                "name": title,
                "parents": [folder_id],
                "appProperties": {"hub": HUB_MARK, "route_date": day},
            },
        )
        file_id = payload.get("id")
        if not file_id:
            raise ChannelError("Drive не подтвердил id копии")
        await self.prepare(file_id, day)
        return file_id

    async def prepare(self, file_id: str, day: str) -> str:
        """Довести копию до одного дневного листа. Шаблон-источник не открывается на запись."""
        meta = await self._request(
            "GET", f"{DRIVE}/files/{file_id}",
            params={"fields": "id,appProperties", "supportsAllDrives": "true"},
        )
        if meta.get("id") != file_id:
            raise ChannelError("Drive не подтвердил id файла")
        ours = (meta.get("appProperties") or {}).get("hub") == HUB_MARK
        sheets = await self._sheet_props(file_id)
        candidates = [s for s in sheets if s["title"] not in EXCLUDED_SHEETS and s["title"] != HUB_SHEET]
        chosen = next((s for s in candidates if s["title"] == day), None)
        if chosen is None:
            if len(candidates) != 1:
                raise ChannelError("в файле не одна вкладка дневного листа")
            chosen = candidates[0]
        header = await self._row(file_id, a1(chosen["title"], "A2:G2"))
        if not headers_match(header):
            raise ChannelError("шапка дневного листа не совпала со структурой 06.03.2026")
        requests = []
        for sheet in sheets:
            if sheet["title"] in EXCLUDED_SHEETS:
                requests.append({"deleteSheet": {"sheetId": sheet["sheetId"]}})
        if chosen["title"] != day:
            requests.append({"updateSheetProperties": {
                "properties": {"sheetId": chosen["sheetId"], "title": day},
                "fields": "title",
            }})
        if not any(s["title"] == HUB_SHEET for s in sheets):
            requests.append({"addSheet": {"properties": {"title": HUB_SHEET, "hidden": True}}})
        if requests:
            await self._structure(file_id, requests)
        index = await self._index(file_id)
        if ours and not index:
            await self._clear(file_id, a1(day, "A3:G500"))
        elif not ours and not index:
            preview = await self._row(file_id, a1(day, "B3:B20"))
            if any(cell.strip() for cell in preview):
                raise ChannelError("в файле есть строки без id заказа")
        await self._put_date(file_id, day)
        await self._hub_header(file_id)
        return day

    async def find_order(self, file_id: str, day: str, order_id: str) -> dict | None:
        index = await self._index(file_id)
        hit = index.get(order_id)
        if hit is None:
            return None
        visible, _hub_row = hit
        cells = await self._row(file_id, a1(day, f"A{visible}:G{visible}"))
        padded = (cells + [""] * 7)[:7]
        return {
            "row": visible,
            "group": padded[0],
            "counterparty": padded[1],
            "address": padded[2],
            "phone": padded[3],
            "hours": padded[4],
            "comment": padded[5],
            "done": padded[6],
        }

    async def write_order(self, file_id: str, day: str, fields: dict, row: int | None) -> int:
        index = await self._index(file_id)
        order_id = fields.get("order_id", "")
        if row is None:
            if order_id in index:
                raise ChannelError("строка заказа уже есть")
            visible = max((item[0] for item in index.values()), default=DATA_START_ROW - 1) + 1
            hub_row = max((item[1] for item in index.values()), default=1) + 1
        else:
            if order_id not in index:
                raise ChannelError("нет скрытой строки заказа")
            visible = row
            hub_row = index[order_id][1]
        data = value_ranges(day, visible, fields, hub_row)
        await self._values(file_id, data)
        return visible

    async def _sheet_props(self, file_id: str) -> list[dict]:
        payload = await self._request(
            "GET", f"{SHEETS}/{file_id}",
            params={"fields": "sheets.properties(sheetId,title)"},
        )
        if "sheets" not in payload:
            raise ChannelError("Sheets не подтвердил список вкладок")
        out = []
        for sheet in payload["sheets"]:
            props = sheet.get("properties") or {}
            if "sheetId" not in props or "title" not in props:
                raise ChannelError("Sheets вернул вкладку без id")
            out.append({"sheetId": props["sheetId"], "title": props["title"]})
        return out

    async def _structure(self, file_id: str, requests: list[dict]) -> None:
        payload = await self._request("POST", f"{SHEETS}/{file_id}:batchUpdate", body={"requests": requests})
        if payload.get("spreadsheetId") != file_id:
            raise ChannelError("Sheets не подтвердил spreadsheetId")
        replies = payload.get("replies")
        if not isinstance(replies, list) or len(replies) != len(requests):
            raise ChannelError("Sheets не подтвердил структуру файла")

    async def _clear(self, file_id: str, range_: str) -> None:
        payload = await self._request(
            "POST", f"{SHEETS}/{file_id}/values:batchClear",
            body={"ranges": [range_]},
        )
        cleared = payload.get("clearedRanges")
        if payload.get("spreadsheetId") != file_id or not cleared:
            raise ChannelError("Sheets не подтвердил очистку строк шаблона")

    async def _put_date(self, file_id: str, day: str) -> None:
        label = date_label(day)
        current = await self._row(file_id, a1(day, "C1"))
        if current and current[0] == label:
            return
        # Пишем только C1 (левая ячейка объединения C1:D1). E1 не трогаем.
        await self._values(file_id, [{"range": a1(day, "C1"), "values": [[label]]}])

    async def _hub_header(self, file_id: str) -> None:
        current = await self._row(file_id, a1(HUB_SHEET, "A1:B1"))
        if current and current[0] == "order_id":
            return
        await self._values(file_id, [{
            "range": a1(HUB_SHEET, "A1:B1"),
            "values": [["order_id", "row"]],
        }])

    async def _values(self, file_id: str, data: list[dict]) -> None:
        payload = await self._request(
            "POST", f"{SHEETS}/{file_id}/values:batchUpdate",
            body={"valueInputOption": "USER_ENTERED", "data": data},
        )
        if payload.get("spreadsheetId") != file_id:
            raise ChannelError("Sheets не подтвердил spreadsheetId")
        responses = payload.get("responses")
        if not isinstance(responses, list) or len(responses) != len(data):
            raise ChannelError("Sheets не подтвердил диапазоны записи")
        if any(not item.get("updatedRange") for item in responses):
            raise ChannelError("Sheets не подтвердил updatedRange")

    async def _row(self, file_id: str, range_: str) -> list[str]:
        encoded = quote(range_, safe="")
        payload = await self._request("GET", f"{SHEETS}/{file_id}/values/{encoded}")
        if "range" not in payload:
            raise ChannelError("Sheets не подтвердил range")
        values = payload.get("values") or []
        if not values:
            return []
        return ["" if cell is None else str(cell) for cell in values[0]]

    async def _index(self, file_id: str) -> dict[str, tuple[int, int]]:
        encoded = quote(a1(HUB_SHEET, "A:B"), safe="")
        payload = await self._request("GET", f"{SHEETS}/{file_id}/values/{encoded}")
        if "range" not in payload:
            raise ChannelError("Sheets не подтвердил скрытый индекс")
        out: dict[str, tuple[int, int]] = {}
        for offset, raw in enumerate(payload.get("values") or [], start=1):
            if not raw:
                continue
            order_id = str(raw[0]).strip()
            if not order_id or order_id == "order_id":
                continue
            visible = int(raw[1]) if len(raw) > 1 and str(raw[1]).strip() else 0
            if visible < DATA_START_ROW:
                raise ChannelError("скрытый индекс без номера строки")
            out[order_id] = (visible, offset)
        return out


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")

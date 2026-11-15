"""Адаптер Google: подтверждение ответа, шаблон, без живых адресов и без сети."""
import json
import re
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest

from hub.channels import ChannelError, Channels
from hub.channels.route_sheet import (
    EXPECTED_HEADERS,
    GoogleRouteSheet,
    date_label,
    file_title,
    headers_match,
    value_ranges,
)
from hub.settings import Settings

DAY = "13.11.2026"
TEMPLATE = "template-1"
FOLDER = "folder-1"
HEADER = ["Гр.м.", "Контрагент", "Адрес", "Телефон", "Часы \nприемки", "Комментарий", "Выполнено"]


class GoogleFake:
    def __init__(self):
        self.calls: list[tuple[str, str, bytes]] = []
        self.files: dict[str, dict] = {}
        self.omit_id = False
        self.fail_status = 0
        self.bad_headers = False
        self.drop_updated_range = False
        self.extra_same_name = False
        self.meta_id = ""
        self.hide_mark = False

    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, str(request.url), bytes(request.content)))
        if request.headers.get("authorization") != "Bearer test-token":
            return httpx.Response(401, json={"error": {"message": "auth"}})
        if "test-token" in str(request.url):
            return httpx.Response(500, json={"error": {"message": "token in url"}})
        if self.fail_status:
            return httpx.Response(self.fail_status, json={"error": {"message": "сбой"}})
        url = urlparse(str(request.url))
        body = json.loads(request.content) if request.content else {}
        if url.path == "/drive/v3/files" and request.method == "GET":
            return self._list(parse_qs(url.query).get("q", [""])[0])
        if url.path == f"/drive/v3/files/{TEMPLATE}/copy":
            return self._copy(body)
        if url.path.startswith("/drive/v3/files/") and request.method == "GET":
            file_id = url.path.rsplit("/", 1)[-1]
            item = self.files[file_id]
            app = {} if self.hide_mark else item["app"]
            return httpx.Response(200, json={"id": self.meta_id or file_id, "appProperties": app})
        if "/values/" in url.path and request.method == "GET":
            return self._read(url.path)
        if url.path.endswith(":batchUpdate") and "/values" not in url.path:
            return self._structure(url.path, body)
        if url.path.endswith("/values:batchClear"):
            return self._clear(url.path, body)
        if url.path.endswith("/values:batchUpdate"):
            return self._write(url.path, body)
        if url.path.startswith("/v4/spreadsheets/") and request.method == "GET":
            file_id = url.path.split("/")[3]
            return httpx.Response(200, json={"sheets": [
                {"properties": {
                    "sheetId": s["sheetId"],
                    "title": s["title"],
                    "gridProperties": {"rowCount": s.get("rowCount", 1000)},
                }}
                for s in self.files[file_id]["sheets"]
            ]})
        return httpx.Response(404, json={"error": {"message": url.path}})

    def _list(self, query: str) -> httpx.Response:
        found = []
        for fid, item in self.files.items():
            if item["name"] in query and "trashed = false" in query:
                found.append({"id": fid})
        if self.extra_same_name and found:
            found.append({"id": "extra"})
        return httpx.Response(200, json={"files": found})

    def _copy(self, body: dict) -> httpx.Response:
        if self.omit_id:
            return httpx.Response(200, json={"name": body.get("name")})
        fid = "copy-1"
        headers = ["старая шапка"] if self.bad_headers else list(HEADER)
        self.files[fid] = {
            "name": body["name"],
            "app": body.get("appProperties") or {},
            "parents": body.get("parents"),
            "sheets": [
                {"sheetId": 1, "title": "Адреса"},
                {"sheetId": 2, "title": "Адреса скорая почта на 1 август"},
                {"sheetId": 3, "title": "08.09.2025"},
                {"sheetId": 4, "title": "06.03.2026"},
            ],
            "cells": {
                "A2:G2": headers,
                "C1": ["дата доставки: 01.01.2000"],
                "E1": ["исполнитель:"],
                "B3": ["СТРОКА-ШАБЛОНА"],
            },
            "hub": [],
            "hub_cells": {},
        }
        return httpx.Response(200, json={"id": fid})

    def _sheet(self, file_id: str, title: str) -> dict:
        return next(s for s in self.files[file_id]["sheets"] if s["title"] == title)

    def _read(self, path: str) -> httpx.Response:
        parts = path.split("/")
        file_id = parts[3]
        range_ = unquote(parts[5])
        sheet, cells = range_.split("!", 1)
        sheet = sheet.strip("'")
        item = self.files[file_id]
        payload = {"range": range_}
        if sheet == "_hub" and cells == "A:B":
            payload["values"] = item["hub"] or [["order_id", "row"]]
            return httpx.Response(200, json=payload)
        if sheet == "_hub":
            row = item["hub_cells"].get(cells)
            if row:
                payload["values"] = [row]
            return httpx.Response(200, json=payload)
        if cells in item["cells"]:
            payload["values"] = [item["cells"][cells]]
            return httpx.Response(200, json=payload)
        grid = re.fullmatch(r"([A-G])(\d+):([A-G])(\d+)", cells)
        if grid:
            start_col, start_row, end_col, end_row = grid.groups()
            letters = "ABCDEFG"
            cols = letters[letters.index(start_col):letters.index(end_col) + 1]
            rows = []
            last = 0
            for n in range(int(start_row), int(end_row) + 1):
                assembled = []
                for letter in cols:
                    cell = item["cells"].get(f"{letter}{n}")
                    assembled.append(cell[0] if cell else "")
                rows.append(assembled)
                if any(str(value).strip() for value in assembled):
                    last = len(rows)
            if last:
                payload["values"] = rows[:last]
            return httpx.Response(200, json=payload)
        row = item["cells"].get(cells)
        if row:
            payload["values"] = [row]
        return httpx.Response(200, json=payload)

    def _structure(self, path: str, body: dict) -> httpx.Response:
        file_id = path.split("/")[3].split(":")[0]
        item = self.files[file_id]
        replies = []
        for req in body["requests"]:
            if "deleteSheet" in req:
                sid = req["deleteSheet"]["sheetId"]
                item["sheets"] = [s for s in item["sheets"] if s["sheetId"] != sid]
            elif "updateSheetProperties" in req:
                props = req["updateSheetProperties"]["properties"]
                self._sheet(file_id, "06.03.2026")["title"] = props["title"]
            elif "addSheet" in req:
                item["sheets"].append({"sheetId": 99, "title": req["addSheet"]["properties"]["title"],
                                       "hidden": req["addSheet"]["properties"].get("hidden")})
            replies.append({})
        return httpx.Response(200, json={"spreadsheetId": file_id, "replies": replies})

    def _clear(self, path: str, body: dict) -> httpx.Response:
        file_id = path.split("/")[3]
        for range_ in body["ranges"]:
            if range_.endswith("A3:G500"):
                self.files[file_id]["cells"].pop("B3", None)
        return httpx.Response(200, json={"spreadsheetId": file_id, "clearedRanges": body["ranges"]})

    def _write(self, path: str, body: dict) -> httpx.Response:
        file_id = path.split("/")[3]
        responses = []
        if self.drop_updated_range:
            return httpx.Response(200, json={"spreadsheetId": file_id, "responses": [{}]})
        item = self.files[file_id]
        for block in body["data"]:
            range_ = block["range"]
            sheet, cells = range_.split("!", 1)
            sheet = sheet.strip("'")
            row = block["values"][0]
            if sheet == "_hub":
                item["hub_cells"][cells] = row
                start = int("".join(ch for ch in cells.split(":")[0] if ch.isdigit()))
                while len(item["hub"]) < start:
                    item["hub"].append([])
                item["hub"][start - 1] = row
            else:
                if len(row) == 1:
                    item["cells"][cells.split(":")[0]] = row
                else:
                    item["cells"][cells] = row
            responses.append({"updatedRange": range_, "updatedCells": 1})
        return httpx.Response(200, json={"spreadsheetId": file_id, "responses": responses})


def _client(fake: GoogleFake) -> GoogleRouteSheet:
    http = httpx.AsyncClient(transport=fake.transport())
    return GoogleRouteSheet(http, token="test-token")


def test_headers_and_ranges_follow_the_sheet():
    assert headers_match(HEADER)
    assert len(EXPECTED_HEADERS) == 7
    ranges = value_ranges(DAY, 3, {
        "order_id": "uuid-1", "counterparty": "ООО «Ромашка»", "address": "Тестовая, 1",
        "phone": "+7 900 111-22-33", "comment": "утро",
    }, 2)
    joined = json.dumps(ranges, ensure_ascii=False)
    assert "A3" not in joined and "G3" not in joined and "E1" not in joined
    assert f"'{DAY}'!B3" in joined and "_hub" in joined
    with pytest.raises(ChannelError):
        value_ranges(DAY, 3, {"order_id": "u", "done": "да"}, 2)


async def test_copy_prepares_only_the_daily_sheet():
    fake = GoogleFake()
    client = _client(fake)
    file_id = await client.create_file(FOLDER, TEMPLATE, file_title(DAY), DAY)
    assert file_id == "copy-1"
    item = fake.files[file_id]
    titles = [s["title"] for s in item["sheets"]]
    assert titles == [DAY, "_hub"]
    assert item["sheets"][-1]["hidden"] is True
    assert "B3" not in item["cells"]
    bodies = [call[2].decode() for call in fake.calls if call[2]]
    assert all("E1" not in body for body in bodies)
    assert any(date_label(DAY) in body for body in bodies)
    copy = next(call for call in fake.calls if call[0] == "POST" and "/copy" in call[1])
    assert TEMPLATE in copy[1]
    assert file_title(DAY) in copy[2].decode()
    assert FOLDER in copy[2].decode()
    # шаблон после копирования больше не адрес запросов, кроме самого copy
    later = [url for _method, url, _body in fake.calls if "/copy" not in url]
    assert all(TEMPLATE not in url for url in later)
    await client.http.aclose()


async def test_copy_without_id_does_not_edit():
    fake = GoogleFake()
    fake.omit_id = True
    client = _client(fake)
    with pytest.raises(ChannelError, match="id"):
        await client.create_file(FOLDER, TEMPLATE, file_title(DAY), DAY)
    assert not any(":batchUpdate" in url for _, url, _ in fake.calls)
    await client.http.aclose()


async def test_empty_template_does_not_call_google():
    fake = GoogleFake()
    client = _client(fake)
    with pytest.raises(ChannelError, match="template_file_id"):
        await client.create_file(FOLDER, "", file_title(DAY), DAY)
    assert fake.calls == []
    await client.http.aclose()


async def test_two_files_do_not_create_a_third():
    fake = GoogleFake()
    fake.files["a"] = {"name": file_title(DAY), "app": {}, "sheets": [], "cells": {}, "hub": [], "hub_cells": {}}
    fake.extra_same_name = True
    client = _client(fake)
    with pytest.raises(ChannelError, match="несколько"):
        await client.find_file(FOLDER, file_title(DAY))
    assert not any("/copy" in url for _, url, _ in fake.calls)
    await client.http.aclose()


async def test_google_error_and_unconfirmed_range():
    fake = GoogleFake()
    fake.fail_status = 503
    client = _client(fake)
    with pytest.raises(ChannelError, match="503"):
        await client.find_file(FOLDER, file_title(DAY))
    await client.http.aclose()

    fake = GoogleFake()
    fake.drop_updated_range = True
    client = _client(fake)
    with pytest.raises(ChannelError, match="updatedRange"):
        await client.create_file(FOLDER, TEMPLATE, file_title(DAY), DAY)
    await client.http.aclose()


async def test_bad_header_does_not_clear_or_delete():
    fake = GoogleFake()
    fake.bad_headers = True
    client = _client(fake)
    with pytest.raises(ChannelError, match="шапка"):
        await client.create_file(FOLDER, TEMPLATE, file_title(DAY), DAY)
    item = fake.files["copy-1"]
    assert any(s["title"] == "Адреса" for s in item["sheets"])
    assert "B3" in item["cells"]
    assert not any("batchClear" in url for _, url, _ in fake.calls)
    await client.http.aclose()


async def test_write_confirms_ranges_and_skips_manual_columns():
    fake = GoogleFake()
    client = _client(fake)
    file_id = await client.create_file(FOLDER, TEMPLATE, file_title(DAY), DAY)
    row = await client.write_order(file_id, DAY, {
        "order_id": "uuid-1",
        "counterparty": "ООО «Ромашка»",
        "address": "Тестовая, 1",
        "phone": "+7 900 111-22-33",
    }, None)
    assert row == 3
    hit = await client.find_order(file_id, DAY, "uuid-1")
    assert hit["row"] == 3
    assert hit["counterparty"] == "ООО «Ромашка»"
    assert hit["done"] == "" and hit["group"] == ""
    blob = json.dumps([call[2].decode() for call in fake.calls], ensure_ascii=False)
    assert "Юридический" not in blob
    await client.http.aclose()


def test_service_account_is_optional_at_import():
    http = httpx.AsyncClient()
    with pytest.raises(ChannelError, match="GOOGLE_SERVICE_ACCOUNT_JSON"):
        GoogleRouteSheet.from_env(http)
    with pytest.raises(ChannelError, match="не JSON"):
        GoogleRouteSheet(http, raw_json="{")._mint()
    with pytest.raises(ChannelError, match="private_key"):
        GoogleRouteSheet(http, raw_json='{"client_email": "bot@example.com"}')._mint()


def _sheet_posts(fake: GoogleFake) -> list[str]:
    return [url for method, url, _body in fake.calls
            if method == "POST" and "sheets.googleapis.com" in url]


def _existing(fake: GoogleFake, *, app=None, sheets=None, cells=None, hub=None) -> str:
    fake.files["old"] = {
        "name": file_title(DAY),
        "app": {"hub": "logistics_sheet", "route_date": DAY} if app is None else app,
        "sheets": sheets if sheets is not None else [
            {"sheetId": 4, "title": DAY},
            {"sheetId": 99, "title": "_hub", "hidden": True},
        ],
        "cells": {"A2:G2": list(HEADER), "B3": ["ЖИВАЯ-СТРОКА"], **(cells or {})},
        "hub": [["order_id", "row"], ["uuid-1", "3"]] if hub is None else hub,
        "hub_cells": {},
    }
    return "old"


async def _assert_untouched(fake: GoogleFake, client: GoogleRouteSheet, match: str):
    with pytest.raises(ChannelError, match=match):
        await client.prepare("old", DAY)
    assert _sheet_posts(fake) == []
    assert not any("/copy" in url for _method, url, _body in fake.calls)
    assert fake.files["old"]["cells"]["B3"] == ["ЖИВАЯ-СТРОКА"]
    await client.http.aclose()


async def test_existing_file_without_hub_is_not_cleared():
    fake = GoogleFake()
    client = _client(fake)
    _existing(fake, sheets=[{"sheetId": 4, "title": DAY}])
    await _assert_untouched(fake, client, "нет листа _hub")


async def test_existing_file_with_wrong_mark_is_not_cleared():
    fake = GoogleFake()
    client = _client(fake)
    _existing(fake, app={"hub": "чужое", "route_date": DAY})
    await _assert_untouched(fake, client, "служебная метка")


async def test_existing_file_with_wrong_route_date_is_not_cleared():
    fake = GoogleFake()
    client = _client(fake)
    _existing(fake, app={"hub": "logistics_sheet", "route_date": "01.01.2000"})
    await _assert_untouched(fake, client, "служебная метка")


async def test_damaged_index_is_not_cleared():
    fake = GoogleFake()
    client = _client(fake)
    _existing(fake, hub=[["order_id", "row"], ["uuid-1", "нет"]])
    await _assert_untouched(fake, client, "не число")


async def test_duplicate_hidden_id_is_not_cleared():
    fake = GoogleFake()
    client = _client(fake)
    _existing(fake, hub=[["order_id", "row"], ["uuid-1", "3"], ["uuid-1", "4"]])
    await _assert_untouched(fake, client, "повторяется")


async def test_rows_without_ids_are_not_cleared():
    fake = GoogleFake()
    client = _client(fake)
    _existing(fake, hub=[["order_id", "row"]])
    await _assert_untouched(fake, client, "строки без id")


async def test_orphan_outside_b3_b20_blocks_write():
    """Пустой индекс: текст в E3 и C21, B3:B20 пустые. Строка 3 не затирается."""
    fake = GoogleFake()
    client = _client(fake)
    _existing(fake, hub=[["order_id", "row"]], cells={
        "B3": [""], "E3": ["10-14"], "C21": ["адрес без id"],
    })
    with pytest.raises(ChannelError, match="строки без id"):
        await client.prepare("old", DAY)
    with pytest.raises(ChannelError, match="3, 21"):
        await client.write_order("old", DAY, {
            "order_id": "новый",
            "counterparty": "ООО «Ромашка»",
            "address": "Тестовая, 1",
            "hours": "09:00-18:00",
        }, None)
    assert not any("values:batchUpdate" in url or "batchClear" in url or "/copy" in url
                   for _method, url, _body in fake.calls)
    assert fake.files["old"]["cells"]["E3"] == ["10-14"]
    assert fake.files["old"]["cells"]["C21"] == ["адрес без id"]
    await client.http.aclose()


async def test_orphan_next_to_indexed_row_blocks_write():
    """В `_hub` уже есть id. Следующая строка без id не становится местом нового заказа."""
    fake = GoogleFake()
    client = _client(fake)
    _existing(fake, hub=[["order_id", "row"], ["uuid-1", "3"]], cells={
        "B3": ["Уже в индексе"], "E4": ["часы без id"],
    })
    with pytest.raises(ChannelError, match="строки без id"):
        await client.prepare("old", DAY)
    with pytest.raises(ChannelError, match="строки без id заказа: 4") as caught:
        await client.write_order("old", DAY, {
            "order_id": "новый",
            "counterparty": "ООО «Ромашка»",
            "address": "Тестовая, 1",
        }, None)
    assert "не переписываю" in str(caught.value)
    assert "второй файл не создаю" in str(caught.value)
    assert not any("values:batchUpdate" in url or "batchClear" in url or "/copy" in url
                   for _method, url, _body in fake.calls)
    assert fake.files["old"]["cells"]["E4"] == ["часы без id"]
    assert fake.files["old"]["cells"]["B3"] == ["Уже в индексе"]
    await client.http.aclose()


async def test_valid_existing_file_is_not_rewritten():
    fake = GoogleFake()
    client = _client(fake)
    _existing(fake)
    assert await client.prepare("old", DAY) == DAY
    assert _sheet_posts(fake) == []
    assert fake.files["old"]["cells"]["B3"] == ["ЖИВАЯ-СТРОКА"]
    await client.http.aclose()


async def test_new_copy_is_cleared_only_after_id_and_mark():
    fake = GoogleFake()
    fake.meta_id = "не-тот-id"
    client = _client(fake)
    with pytest.raises(ChannelError, match="не очищаю"):
        await client.create_file(FOLDER, TEMPLATE, file_title(DAY), DAY)
    assert _sheet_posts(fake) == []
    assert fake.files["copy-1"]["cells"]["B3"] == ["СТРОКА-ШАБЛОНА"]
    await client.http.aclose()

    fake = GoogleFake()
    fake.hide_mark = True
    client = _client(fake)
    with pytest.raises(ChannelError, match="не очищаю"):
        await client.create_file(FOLDER, TEMPLATE, file_title(DAY), DAY)
    assert _sheet_posts(fake) == []
    assert fake.files["copy-1"]["cells"]["B3"] == ["СТРОКА-ШАБЛОНА"]
    await client.http.aclose()


async def test_hours_stay_text():
    fake = GoogleFake()
    client = _client(fake)
    file_id = await client.create_file(FOLDER, TEMPLATE, file_title(DAY), DAY)
    samples = ["10-14", "09:00-18:00", "10:00–14:00"]
    for sample in samples:
        await client.write_order(file_id, DAY, {
            "order_id": f"id-{sample}",
            "counterparty": "ООО «Ромашка»",
            "address": "Тестовая, 1",
            "hours": sample,
        }, None)
    value_bodies = [json.loads(raw) for _method, url, raw in fake.calls
                    if raw and "values:batchUpdate" in url]
    assert value_bodies
    assert all(body["valueInputOption"] == "RAW" for body in value_bodies)
    written = []
    for body in value_bodies:
        for block in body["data"]:
            if "!E" in block["range"]:
                written.append(block["values"][0][0])
    assert written == samples
    assert all(isinstance(value, str) for value in written)
    formats = [json.loads(raw) for _method, url, raw in fake.calls if raw and b"repeatCell" in raw]
    assert len(formats) == len(samples)
    for body in formats:
        cell = body["requests"][0]["repeatCell"]
        assert cell["range"]["startColumnIndex"] == 4
        assert cell["range"]["endColumnIndex"] == 5
        assert cell["cell"]["userEnteredFormat"]["numberFormat"]["type"] == "TEXT"
    blob = json.dumps(value_bodies + formats, ensure_ascii=False)
    assert "USER_ENTERED" not in blob
    await client.http.aclose()


async def test_dry_run_channel_refuses_google_write():
    channels = Channels(Settings(dry_run=True))
    with pytest.raises(ChannelError, match="DRY_RUN"):
        await channels.route_create_file("f", "t", "имя", DAY)
    note = await channels.route_note("WOULD_ADD_ORDER", order_id="1", route="2026-11-13")
    assert note == "WOULD_ADD_ORDER"
    assert channels.sent[-1]["dry_run"] is True
    await channels.close()

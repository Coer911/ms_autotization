import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_moysklad import FakeMoySklad  # noqa: E402

from hub.channels import ChannelError, Channels  # noqa: E402
from hub.moysklad import MoySklad  # noqa: E402
from hub.runtime import Hub  # noqa: E402
from hub.settings import Settings  # noqa: E402
from hub.storage import Storage  # noqa: E402

# HUB_TEST_DATABASE_URL=postgresql://… — прогнать те же тесты на PostgreSQL
DB_URL = os.environ.get("HUB_TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")


class RouteBook:
    """Память дневных файлов для тестов. Колонки A и G хаб не пишет."""

    def __init__(self):
        self.files: dict[str, dict] = {}
        self.creates = 0
        self.n = 0

    def find(self, title: str) -> str | None:
        ids = [fid for fid, item in self.files.items() if item["title"] == title]
        if len(ids) > 1:
            raise ChannelError(f"несколько файлов «{title}», новый не создаю")
        return ids[0] if ids else None

    def seed(self, title: str, day: str) -> str:
        self.n += 1
        fid = f"file-{self.n}"
        self.files[fid] = self._blank(title, day)
        return fid

    def create(self, title: str, template_id: str, day: str) -> str:
        if not template_id:
            raise ChannelError("нет template_file_id, файл не создаю")
        if self.find(title):
            raise ChannelError("файл уже есть")
        self.creates += 1
        self.n += 1
        fid = f"file-{self.n}"
        item = self._blank(title, day)
        item["template_id"] = template_id
        item["sheets"] = [day]
        self.files[fid] = item
        return fid

    def prepare(self, file_id: str, day: str) -> str:
        item = self.files[file_id]
        if item.get("block"):
            raise ChannelError(item["block"])
        item["tab"] = day
        item["date_cell"] = f"дата доставки: {day}"
        item["sheets"] = [day]
        return day

    def find_order(self, file_id: str, order_id: str) -> dict | None:
        row = self.files[file_id]["rows"].get(order_id)
        return dict(row) if row else None

    def write(self, file_id: str, order_id: str, fields: dict, row: int | None) -> int:
        if set(fields) & {"group", "done", "executor"}:
            raise ChannelError("ручные поля не пишем")
        item = self.files[file_id]
        if row is None:
            if order_id in item["rows"]:
                raise ChannelError("строка заказа уже есть")
            row = 3 + len(item["order"])
            cells = {key: "" for key in ("group", "counterparty", "address", "phone", "hours", "comment", "done")}
            for key, value in fields.items():
                if key in cells:
                    cells[key] = value
            item["rows"][order_id] = {"row": row, "order_id": order_id, **cells}
            item["order"].append(order_id)
            item["writes"].append({"new": True, "order_id": order_id, "fields": dict(fields)})
            return row
        current = item["rows"][order_id]
        for key, value in fields.items():
            if key in current:
                current[key] = value
        item["writes"].append({"new": False, "order_id": order_id, "fields": dict(fields)})
        return row

    def insert(self, file_id: str, order_id: str, cells: dict) -> None:
        """Строка уже лежит в листе, записи хаба о ней нет."""
        item = self.files[file_id]
        row = 3 + len(item["order"])
        base = {key: "" for key in ("group", "counterparty", "address", "phone", "hours", "comment", "done")}
        base.update(cells)
        item["rows"][order_id] = {"row": row, "order_id": order_id, **base}
        item["order"].append(order_id)

    @staticmethod
    def _blank(title: str, day: str) -> dict:
        return {
            "title": title, "tab": day, "date_cell": f"дата доставки: {day}",
            "executor": "", "sheets": [day], "rows": {}, "order": [], "writes": [],
        }


class RecordingChannels(Channels):
    """Каналы без сети: записывают отправки, умеют падать заданное число раз."""

    def __init__(self, settings):
        super().__init__(settings)
        self.emails, self.documents, self.messages, self.hooks, self.alerts = [], [], [], [], []
        self.fail = {}
        self.book = RouteBook()
        self.route_unconfirmed = False

    def _maybe_fail(self, kind):
        if self.fail.get(kind, 0) > 0:
            self.fail[kind] -= 1
            raise ChannelError(f"{kind} недоступен")

    async def email(self, to, subject, body, attachments=(), cc=()):
        if self.s.dry_run:
            return await super().email(to, subject, body, attachments, cc)
        self._maybe_fail("email")
        self.emails.append({"to": to, "subject": subject, "body": body,
                            "attachments": list(attachments), "cc": list(cc)})
        return f"письмо {', '.join(to)}"

    async def telegram_document(self, chat, filename, data, caption=""):
        if self.s.dry_run:
            return await super().telegram_document(chat, filename, data, caption)
        self._maybe_fail("telegram")
        self.documents.append({"chat": chat, "filename": filename, "caption": caption})
        return f"telegram {chat}"

    async def telegram_message(self, chat, text):
        if self.s.dry_run:
            return await super().telegram_message(chat, text)
        self._maybe_fail("telegram")
        self.messages.append({"chat": chat, "text": text})
        return f"telegram {chat}"

    async def webhook(self, url, payload):
        if self.s.dry_run:
            return await super().webhook(url, payload)
        self._maybe_fail("webhook")
        self.hooks.append({"url": url, "payload": payload})
        return f"вебхук {url}"

    async def alert(self, text):
        self.alerts.append(text)

    def _route_guard(self):
        if self.s.dry_run:
            raise ChannelError("HUB_DRY_RUN: запись в Google запрещена")
        self._maybe_fail("google")

    async def route_find_file(self, folder_id, title):
        self._route_guard()
        return self.book.find(title)

    async def route_create_file(self, folder_id, template_id, title, day):
        self._route_guard()
        return self.book.create(title, template_id, day)

    async def route_prepare(self, file_id, day):
        self._route_guard()
        return self.book.prepare(file_id, day)

    async def route_find_order(self, file_id, day, order_id):
        self._route_guard()
        return self.book.find_order(file_id, order_id)

    async def route_write_order(self, file_id, day, fields, row):
        self._route_guard()
        if self.route_unconfirmed:
            raise ChannelError("Google не подтвердил запись")
        return self.book.write(file_id, fields["order_id"], fields, row)


def settings_for(automations: dict, dry_run: bool = False) -> Settings:
    return Settings(ms_token="test-token", api_key="k", webhook_secret="s3cret",
                    public_url="https://hub.example.ru", poll_seconds=0, dry_run=dry_run,
                    database_url=DB_URL, automations=automations)


async def fresh_storage() -> Storage:
    storage = await Storage.open(DB_URL)
    if not storage.is_sqlite:                      # общая база PostgreSQL — чистим перед тестом
        from hub.storage import metadata
        async with storage.engine.begin() as conn:
            for table in reversed(metadata.sorted_tables):
                await conn.execute(table.delete())
    return storage


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    """Поддельный МойСклад не ограничивает частоту — тестам не нужно ждать окно в 3 с."""
    monkeypatch.setattr(MoySklad, "budget", property(lambda self: self._fixed_budget or 45))


@pytest.fixture
def fake():
    return FakeMoySklad()


@pytest.fixture
async def make_hub(fake):
    """await make_hub({"имя": {параметры}}, dry_run=False, storage=None) -> Hub."""
    hubs = []

    async def build(automations: dict, dry_run: bool = False, storage: Storage | None = None) -> Hub:
        s = settings_for(automations, dry_run)
        hub = Hub(s, storage or await fresh_storage(),
                  ms=MoySklad("test-token", transport=fake.transport()),
                  channels=RecordingChannels(s))
        hubs.append(hub)
        return hub

    yield build
    closed = set()
    for hub in hubs:
        await hub.ms.close()
        if id(hub.storage) not in closed:
            closed.add(id(hub.storage))
            await hub.storage.close()


async def webhook(hub: Hub, fake: FakeMoySklad, type_: str, id_: str, rid: str,
                  action: str = "UPDATE", fields: list[str] | None = None) -> int:
    from hub.ingest import accept_webhook
    return await accept_webhook(hub.storage, fake.webhook_payload(type_, id_, action, fields), rid)

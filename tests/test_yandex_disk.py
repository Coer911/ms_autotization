"""REST Диска: без перезаписи, без токена в тексте ошибки, без редактора таблиц."""
import httpx
import pytest

from hub.channels.yandex_disk import API, DiskError, YandexDisk

TOKEN = "ya-disk-test-token"


def _client(handler) -> YandexDisk:
    return YandexDisk(TOKEN, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_upload_does_not_overwrite_and_put_has_no_token():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), request.headers.get("authorization")))
        if request.url.path.endswith("/resources/upload"):
            assert request.url.params["overwrite"] == "false"
            assert request.url.params["path"].endswith(".xlsx")
            return httpx.Response(200, json={
                "href": "https://uploader.example/put", "method": "PUT", "templated": False})
        if request.url.path.endswith("/resources"):
            return httpx.Response(200, json={
                "type": "file", "path": request.url.params["path"], "md5": "abc", "size": 4})
        assert request.url.host == "uploader.example"
        assert request.headers.get("authorization") is None
        assert request.content == b"xlsx"
        return httpx.Response(201)

    client = _client(handler)
    meta = await client.upload_new("disk:/проба/лист.xlsx", b"xlsx")
    assert meta["md5"] == "abc"
    assert calls[0][2] == f"OAuth {TOKEN}"
    assert all(item[2] != f"OAuth {TOKEN}" or "resources" in item[1] for item in calls)
    await client.http.aclose()


@pytest.mark.parametrize("status", [401, 403, 409, 429, 503])
async def test_errors_do_not_upload_and_hide_the_token(status):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/disk/resources/upload"
        return httpx.Response(status, json={"message": TOKEN, "error": "no"})

    client = _client(handler)
    with pytest.raises(DiskError) as caught:
        await client.upload_new("disk:/проба/лист.xlsx", b"xlsx")
    assert caught.value.status == status
    assert TOKEN not in str(caught.value)
    assert "overwrite" not in str(caught.value)
    await client.http.aclose()


async def test_accepted_upload_is_not_success():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/upload"):
            return httpx.Response(200, json={"href": "https://uploader.example/put", "method": "PUT"})
        return httpx.Response(202)

    client = _client(handler)
    with pytest.raises(DiskError, match="не на Диске") as caught:
        await client.upload_new("disk:/проба/лист.xlsx", b"xlsx")
    assert caught.value.status == 202
    assert TOKEN not in str(caught.value)
    await client.http.aclose()


async def test_missing_token_is_refused(monkeypatch):
    monkeypatch.delenv("YANDEX_DISK_TOKEN", raising=False)
    with pytest.raises(DiskError, match="YANDEX_DISK_TOKEN"):
        YandexDisk.from_env(httpx.AsyncClient())
    assert not hasattr(YandexDisk, "delete")
    assert API.endswith("/v1/disk")

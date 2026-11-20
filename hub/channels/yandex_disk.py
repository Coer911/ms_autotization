"""Официальный REST Яндекс Диска: файл целиком, без перезаписи.

Документация: https://yandex.ru/dev/disk-api/doc/ru/
Загрузка — GET /v1/disk/resources/upload?overwrite=false, затем PUT по выданной ссылке.
Токен на PUT в хранилище не отправляется. Удаления и overwrite=true здесь нет.
Чтение (база логиста) — GET /v1/disk/resources/download, затем GET по выданной ссылке.
Редактор Таблиц не вызывается. Токен в журнал и в текст ошибки не попадает.
"""
from __future__ import annotations

import os

import httpx

API = "https://cloud-api.yandex.net/v1/disk"
_FIELDS = "name,path,md5,size,type,mime_type"


class DiskError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


class YandexDisk:
    def __init__(self, token: str, http: httpx.AsyncClient):
        self._token = token
        self.http = http

    @classmethod
    def from_env(cls, http: httpx.AsyncClient) -> YandexDisk:
        token = os.environ.get("YANDEX_DISK_TOKEN", "").strip()
        if not token:
            raise DiskError(401, "Яндекс Диск не настроен: YANDEX_DISK_TOKEN")
        return cls(token, http)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"OAuth {self._token}", "Accept": "application/json"}

    def _fail(self, response: httpx.Response) -> None:
        code = response.status_code
        if code == 401:
            raise DiskError(401, "Диск 401: токен не принят")
        if code == 403:
            raise DiskError(403, "Диск 403: нет права на папку")
        if code == 409:
            raise DiskError(409, "Диск 409: путь занят, не перезаписываю")
        if code == 429:
            raise DiskError(429, "Диск 429: слишком много запросов")
        raise DiskError(code, f"Диск {code}")

    async def stat(self, path: str) -> dict | None:
        response = await self.http.get(
            f"{API}/resources", params={"path": path, "fields": _FIELDS}, headers=self._headers())
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            self._fail(response)
        data = response.json()
        if not isinstance(data, dict) or data.get("type") != "file":
            raise DiskError(409, "по этому пути не файл, не перезаписываю")
        return data

    async def upload_new(self, path: str, body: bytes) -> dict:
        """Новый файл. overwrite=false: занятый путь не удаляется и не заменяется."""
        response = await self.http.get(
            f"{API}/resources/upload",
            params={"path": path, "overwrite": "false"},
            headers=self._headers())
        if response.status_code == 409:
            raise DiskError(409, "файл уже есть, не перезаписываю")
        if response.status_code != 200:
            self._fail(response)
        info = response.json()
        href = info.get("href") if isinstance(info, dict) else None
        if not href or info.get("method", "PUT") != "PUT":
            raise DiskError(502, "Диск не выдал ссылку загрузки")
        # Ссылка хранилища живёт сама по себе: OAuth-токен на PUT не нужен и не уходит.
        put = await self.http.put(href, content=body)
        if put.status_code == 201:
            meta = await self.stat(path)
            if not meta or not meta.get("md5"):
                raise DiskError(202, "загрузка не подтверждена")
            return meta
        if put.status_code == 202:
            raise DiskError(202, "файл принят, но ещё не на Диске")
        if put.status_code == 409:
            raise DiskError(409, "файл уже есть, не перезаписываю")
        self._fail(put)
        raise DiskError(put.status_code, f"Диск {put.status_code}")


    async def download(self, path: str) -> bytes:
        """Файл целиком. Ссылка скачивания — без OAuth-токена, как и при загрузке."""
        response = await self.http.get(
            f"{API}/resources/download", params={"path": path}, headers=self._headers())
        if response.status_code == 404:
            raise DiskError(404, f"на Диске нет файла {path}")
        if response.status_code != 200:
            self._fail(response)
        info = response.json()
        href = info.get("href") if isinstance(info, dict) else None
        if not href:
            raise DiskError(502, "Диск не выдал ссылку скачивания")
        got = await self.http.get(href, follow_redirects=True)
        if got.status_code != 200:
            raise DiskError(got.status_code, f"Диск {got.status_code} при скачивании")
        return got.content

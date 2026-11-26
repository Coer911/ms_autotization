"""Ошибки конфигурации Ozon Connect."""

from __future__ import annotations


class OzonError(Exception):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code

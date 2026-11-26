"""Ключ идемпотентности Ozon.

Кабинет, схема, отправление и тип операции входят всегда.
Позиция или идентификатор возврата добавляются только когда операция
относится к одной строке или одному возврату, а не ко всему отправлению.
"""
from __future__ import annotations

SCHEMES = ("fbs", "fbo")
_FORBIDDEN = ("|", "\n", "\r", "\x00")


def operation_key(cabinet_id: str, scheme: str, posting_number: str, operation: str,
                  position: str = "") -> str:
    """Собрать ключ. Пустая часть или символ «|» внутри части — ошибка, не другой ключ."""
    parts = [cabinet_id.strip(), scheme.strip(), posting_number.strip(), operation.strip()]
    if scheme not in SCHEMES:
        raise ValueError(f"схема «{scheme}»: можно {', '.join(SCHEMES)}")
    if any(not part or any(ch in part for ch in _FORBIDDEN) for part in parts):
        raise ValueError("в ключе идемпотентности пустая часть или символ «|»")
    if any(ch in position for ch in _FORBIDDEN):
        raise ValueError("в позиции ключа нельзя использовать «|» и перевод строки")
    key = "|".join(parts)
    if position:
        key = f"{key}|{position.strip()}"
    if len(key) > 255:
        raise ValueError("ключ идемпотентности длиннее 255 символов")
    return key

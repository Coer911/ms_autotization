"""Номера договоров: «ПК» → «ПК-ДД/ММ/ГГ-NN».

Менеджер создаёт договор в МойСклад и в поле «Номер» пишет только серию — ПК, ПТ, АОПК, АО
или ДАО. `python -m hub contracts number` находит такие договоры и предлагает полный номер:
серия, дата договора, NN — следующий свободный для этой серии и даты (01, 02, …).
С `--apply` записывает. Договоры с уже заполненным номером не трогает, старше 30 дней
(`--since`) — тоже: такой договор могли подписать с номером как есть (в МойСклад есть «ПТ»
от 12.07.2023).

Так нумеровались договоры и раньше (ПК-09/04/26-01), но руками: из 228 договоров ПК
по формату только 114, у остальных «ПК-14/01/26», «ПК-08/04/24--01», «ПК-23.05.25-01».
Шаблон печати этого не исправит: номер стоит и в УПД, и в счетах — он должен быть верным
в самом МойСклад. Позже то же самое сделает автоматизация хаба при создании договора
(print_templates/README.md, «Нумерация договоров»).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .moysklad import MoySklad

SERIES = ("ПК", "ПТ", "АОПК", "АО", "ДАО")


@dataclass(frozen=True)
class Renaming:
    id: str
    old: str
    new: str
    moment: str
    agent: str


def bare_series(name: str) -> str | None:
    """«ПК», « пк », «ПК-» → «ПК»; полный номер или что-то другое → None."""
    s = (name or "").strip().rstrip("-").strip().upper()
    return s if s in SERIES else None


def date_part(moment: str) -> str:
    """'2026-04-09 00:00:00.000' → '09/04/26' (дата договора, время Москвы — как в МойСклад)."""
    y, m, d = moment[:10].split("-")
    return f"{d}/{m}/{y[2:]}"


def used_numbers(names: list[str], series: str, date: str) -> set[int]:
    """NN, уже занятые для серии и даты. Номер без «-NN» («ПК-14/01/26») считается 01."""
    rx = re.compile(rf"{re.escape(series)}-{re.escape(date)}(?:-(\d+))?", re.IGNORECASE)
    out = set()
    for n in names:
        if m := rx.fullmatch(n.strip()):
            out.add(int(m.group(1) or 1))
    return out


def next_name(names: list[str], series: str, date: str) -> str:
    n = max(used_numbers(names, series, date), default=0) + 1
    return f"{series}-{date}-{n:02d}"


def bare(contracts: list[dict]) -> list[dict]:
    """Договоры, у которых в номере только серия, по дате договора."""
    return sorted((c for c in contracts if bare_series(c.get("name", ""))),
                  key=lambda c: (c.get("moment", ""), c.get("id", "")))


def stale(contracts: list[dict], since: str) -> list[dict]:
    """Договоры с одной серией в номере, но старше since — их не трогаем: могли подписать как есть."""
    return [c for c in bare(contracts) if c.get("moment", "")[:10] < since]


def plan(contracts: list[dict], since: str = "") -> list[Renaming]:
    """Какие договоры переименовать: с одной серией в номере и с датой не раньше since (ГГГГ-ММ-ДД).
    Несколько «ПК» за один день получают 01, 02… по порядку."""
    names = [c.get("name", "") for c in contracts]
    todo = [c for c in bare(contracts) if c.get("moment", "")[:10] >= since]
    out = []
    for c in todo:
        series = bare_series(c["name"])
        new = next_name(names, series, date_part(c["moment"]))
        names.append(new)
        out.append(Renaming(c["id"], c["name"], new, c["moment"][:10],
                            (c.get("agent") or {}).get("name", "")))
    return out


async def load(ms: MoySklad) -> list[dict]:
    """Все договоры (без архивных фильтров: занятые номера считаются по всем)."""
    return [c async for c in ms.list("contract", expand="agent", limit=100)]


async def apply(ms: MoySklad, renamings: list[Renaming]) -> None:
    for r in renamings:
        await ms.update("contract", r.id, {"name": r.new})

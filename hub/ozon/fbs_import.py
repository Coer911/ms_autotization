"""Прочитать отправления FBS за окно и сохранить снимок в базу хаба.

МойСклад здесь только отвечает, есть ли артикул. Заказ, резерв, отгрузка,
возврат и списание этим модулем не создаются.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..moysklad.client import MoySklad
from ..storage.ozon import save_posting
from .client import OzonClient, OzonContractError
from .match import Match, match_offer
from .redact import redact_posting

MAX_POSTINGS = 20_000
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


@dataclass
class ImportStats:
    pages: int = 0
    seen: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    matched: int = 0
    unmatched: int = 0
    ambiguous: int = 0
    skipped: int = 0
    no_catalog: int = 0

    def text(self) -> str:
        return (
            f"страниц {self.pages}, отправлений {self.seen}: "
            f"новых {self.inserted}, обновлено {self.updated}, без изменений {self.unchanged}; "
            f"строк: сопоставлено {self.matched}, не найдено {self.unmatched}, "
            f"неоднозначно {self.ambiguous}, пропущено {self.skipped}, без каталога {self.no_catalog}"
        )


def utc_window(moment: datetime, days: int) -> tuple[str, str]:
    if days < 1 or days > 365:
        raise ValueError("окно FBS от 1 до 365 суток: Seller API не принимает период длиннее года")
    if moment.tzinfo is None:
        raise ValueError("время окна должно быть с часовым поясом")
    end = moment.astimezone(UTC).replace(microsecond=0)
    start = end - timedelta(days=days)
    return start.strftime(_STAMP), end.strftime(_STAMP)


def _count_match(stats: ImportStats, match: Match) -> None:
    if match.status == "matched":
        stats.matched += 1
    elif match.status == "unmatched":
        stats.unmatched += 1
    elif match.status == "ambiguous":
        stats.ambiguous += 1
    elif match.status == "skipped":
        stats.skipped += 1
    elif match.status == "no_catalog":
        stats.no_catalog += 1
    else:
        raise ValueError(f"неизвестный итог сопоставления {match.status}")


async def import_fbs(storage, ozon: OzonClient, ms: MoySklad | None, *,
                     lookback_days: int, moment: datetime | None = None) -> ImportStats:
    since, until = utc_window(moment or datetime.now(UTC), lookback_days)
    stats = ImportStats()
    cache: dict[str, Match] = {}
    cursor = ""
    seen_cursors = {""}
    while True:
        page = await ozon.list_fbs_page(since, until, cursor)
        stats.pages += 1
        for raw in page.postings:
            snapshot = redact_posting(raw)
            matches = [await match_offer(ms, line.offer_id, cache) for line in snapshot.lines]
            for match in matches:
                _count_match(stats, match)
            outcome = await save_posting(
                storage, ozon.client_id, "fbs", snapshot, matches, preserve_matches=ms is None)
            setattr(stats, outcome, getattr(stats, outcome) + 1)
            stats.seen += 1
            if stats.seen > MAX_POSTINGS:
                raise OzonContractError(f"за окно больше {MAX_POSTINGS} отправлений, импорт остановлен")
        if not page.has_next:
            return stats
        if not page.cursor or page.cursor in seen_cursors:
            raise OzonContractError("Seller API не сдвинул cursor, повторную страницу не запрашиваю")
        seen_cursors.add(page.cursor)
        cursor = page.cursor

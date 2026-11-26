"""Точка входа: python -m ozon_connect serve | import."""

from __future__ import annotations

import argparse
import logging
import sys

from ozon_connect.errors import OzonError
from ozon_connect.server import serve
from ozon_connect.settings import load_settings

log = logging.getLogger("ozon_connect")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ozon_connect")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve_parser = sub.add_parser("serve", help="слушать /healthz и /readyz")
    serve_parser.add_argument("--host", default="")
    import_parser = sub.add_parser("import", help="read-only сверка каталогов в свою SQLite")
    import_parser.add_argument("--account", default="", help="один кабинет; пусто — все из OZON_ACCOUNTS")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = load_settings()
    if settings.fbs_enabled or settings.fbo_enabled:
        log.warning("FBS/FBO флаги включены, но этот выпуск их не исполняет")
    if args.cmd == "serve":
        log.info(
            "старт dry_run=%s writes=blocked import=off accounts=%s",
            int(settings.dry_run),
            ",".join(account.id for account in settings.accounts) or "-",
        )
        serve(settings, host=args.host or settings.host)
        return 0
    log.info("импорт кабинетов=%s", args.account or "все")
    from ozon_connect.importer import run_import

    try:
        return run_import(settings, account=args.account or None)
    except OzonError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Точка входа: python -m ozon_connect serve."""

from __future__ import annotations

import argparse
import logging

from ozon_connect.server import serve
from ozon_connect.settings import load_settings

log = logging.getLogger("ozon_connect")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ozon_connect")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve_parser = sub.add_parser("serve", help="слушать /healthz и /readyz")
    serve_parser.add_argument("--host", default="")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = load_settings()
    if settings.fbs_enabled or settings.fbo_enabled:
        log.warning("FBS/FBO флаги включены, но этот выпуск их не исполняет")
    log.info(
        "старт dry_run=%s writes=blocked import=off accounts=%s",
        int(settings.dry_run),
        ",".join(account.id for account in settings.accounts) or "-",
    )
    serve(settings, host=args.host or settings.host)


if __name__ == "__main__":
    main()

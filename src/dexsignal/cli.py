"""Command-line interface (FR-11)."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from collections.abc import Sequence
from types import FrameType

from .config import ConfigError, Settings
from .http import HttpClient
from .pipeline import Pipeline
from .providers import DexScreenerClient, RugCheckClient, TelegramNotifier
from .storage import Storage

log = logging.getLogger("dexsignal")


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv  # noqa: PLC0415 - optional dependency
    except ImportError:  # optional dependency
        return
    load_dotenv()


def build_pipeline(settings: Settings, storage: Storage) -> Pipeline:
    http = HttpClient(settings.http_timeout_seconds, settings.max_requests_per_minute)
    notifier = (
        TelegramNotifier(http, settings.telegram_base_url, settings.telegram_token, settings.telegram_chat_id)
        if settings.telegram_enabled
        else None
    )
    return Pipeline(
        settings,
        storage,
        DexScreenerClient(http, settings.dexscreener_base_url, settings.batch_size),
        RugCheckClient(http, settings.rugcheck_base_url, settings.rugcheck_max_score),
        notifier,
    )


def _run_loop(pipeline: Pipeline, interval: int, stop: threading.Event) -> None:
    def handle(signum: int, _frame: FrameType | None) -> None:
        log.info("signal %d received; stopping after current step", signum)
        stop.set()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)
    while not stop.is_set():
        pipeline.run_cycle()
        stop.wait(interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dexsignal",
        description="Private research tool for DexScreener token observations. Not financial advice.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run cycles until stopped")
    sub.add_parser("once", help="run a single cycle")
    sub.add_parser("stats", help="show database counts")
    exp = sub.add_parser("export", help="export a table as CSV")
    exp.add_argument("table", choices=["snapshots", "observations"])
    exp.add_argument("-o", "--output", default="-", help="file path, or - for stdout")
    bl = sub.add_parser("blacklist", help="manage the token blacklist")
    bl_sub = bl.add_subparsers(dest="action", required=True)
    add = bl_sub.add_parser("add")
    add.add_argument("chain_id")
    add.add_argument("address")
    add.add_argument("--reason", default="")
    rm = bl_sub.add_parser("remove")
    rm.add_argument("chain_id")
    rm.add_argument("address")
    bl_sub.add_parser("list")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _load_dotenv()
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    storage = Storage(settings.database_path)
    try:
        if args.command in ("run", "once"):
            if not settings.telegram_enabled:
                log.info("telegram not configured; observations will be logged only")
            pipeline = build_pipeline(settings, storage)
            if args.command == "once":
                report = pipeline.run_cycle()
                return 1 if report.error else 0
            _run_loop(pipeline, settings.interval_seconds, threading.Event())
        elif args.command == "stats":
            for key, value in storage.stats().items():
                print(f"{key}: {value}")
        elif args.command == "export":
            if args.output == "-":
                n = storage.export_csv(args.table, sys.stdout)
            else:
                with open(args.output, "w", newline="", encoding="utf-8") as fh:
                    n = storage.export_csv(args.table, fh)
            print(f"exported {n} rows", file=sys.stderr)
        elif args.command == "blacklist":
            if args.action == "add":
                storage.blacklist_add(args.chain_id, args.address, args.reason)
                print("added")
            elif args.action == "remove":
                print("removed" if storage.blacklist_remove(args.chain_id, args.address) else "not found")
            else:
                for chain_id, address, reason in storage.blacklist_list():
                    print(f"{chain_id}\t{address}\t{reason}")
        return 0
    finally:
        storage.close()

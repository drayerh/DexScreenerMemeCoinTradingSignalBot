"""Command-line interface (FR-11, FR-16 to FR-18)."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from collections.abc import Sequence
from datetime import datetime, timedelta
from types import FrameType

from . import logsetup
from .config import ConfigError, Settings
from .evaluate import evaluate, render, to_dicts
from .http import HttpClient
from .pipeline import Pipeline
from .probe import probe_dexscreener, probe_provider
from .providers import DexScreenerClient, TelegramNotifier
from .risk import (
    EtherscanCreatorProvider,
    GoPlusProvider,
    PocketUniverseProvider,
    RiskProvider,
    RugCheckProvider,
)
from .storage import EXPORTABLE, Storage, utc_now

log = logging.getLogger("dexsignal")


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv  # noqa: PLC0415 - optional dependency
    except ImportError:  # optional dependency
        return
    load_dotenv()


def build_providers(settings: Settings, http: HttpClient) -> list[RiskProvider]:
    providers: list[RiskProvider] = [
        RugCheckProvider(http, settings.rugcheck_base_url, settings.rugcheck_max_score)
    ]
    if settings.goplus_enabled:
        providers.append(
            GoPlusProvider(
                http,
                settings.goplus_base_url,
                settings.goplus_token,
                max_tax_pct=settings.max_tax_pct,
                max_top10_pct=settings.max_top10_pct,
            )
        )
    if settings.pocket_universe_enabled:
        providers.append(
            PocketUniverseProvider(
                http,
                settings.pocket_universe_base_url,
                settings.pocket_universe_api_key,
                method=settings.pocket_universe_method,
                param=settings.pocket_universe_param,
            )
        )
    if settings.etherscan_api_key:
        providers.append(
            EtherscanCreatorProvider(http, settings.etherscan_base_url, settings.etherscan_api_key)
        )
    return providers


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
        build_providers(settings, http),
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
    exp.add_argument("table", choices=sorted(EXPORTABLE))
    exp.add_argument("-o", "--output", default="-", help="file path, or - for stdout")
    ev = sub.add_parser("evaluate", help="evidence report: observation kinds against the base rate")
    ev.add_argument("--json", action="store_true", help="machine-readable output")
    health = sub.add_parser("health", help="exit 0 if a successful cycle ran recently")
    health.add_argument("--max-age-minutes", type=int, default=15)
    pr = sub.add_parser("probe", help="check each provider's live API contract")
    pr.add_argument("target", nargs="?", choices=["all", "pocket"], default="all")
    pr.add_argument("mint", nargs="?", help="Solana mint for 'probe pocket'")

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

    dep = sub.add_parser("deployer", help="manage the deployer blacklist")
    dep_sub = dep.add_subparsers(dest="action", required=True)
    block = dep_sub.add_parser("block")
    block.add_argument("chain_id")
    block.add_argument("address")
    block.add_argument("--reason", default="")
    unblock = dep_sub.add_parser("unblock")
    unblock.add_argument("chain_id")
    unblock.add_argument("address")
    dep_sub.add_parser("list")
    return parser


def _health(storage: Storage, max_age_minutes: int) -> int:
    last = storage.get_meta("last_ok_cycle_at")
    if last is None:
        print("unhealthy: no successful cycle recorded")
        return 1
    age = utc_now() - datetime.fromisoformat(last)
    if age > timedelta(minutes=max_age_minutes):
        print(f"unhealthy: last successful cycle {int(age.total_seconds() // 60)} min ago")
        return 1
    print(f"healthy: last successful cycle at {last}")
    return 0


def _probe(settings: Settings, target: str, mint: str | None) -> int:
    http = HttpClient(settings.http_timeout_seconds, settings.max_requests_per_minute)
    if target == "pocket":
        if not settings.pocket_universe_enabled:
            print("pocket universe is not configured", file=sys.stderr)
            return 2
        if not mint:
            print("usage: dexsignal probe pocket <solana mint>", file=sys.stderr)
            return 2
        pocket = PocketUniverseProvider(
            http,
            settings.pocket_universe_base_url,
            settings.pocket_universe_api_key,
            method=settings.pocket_universe_method,
            param=settings.pocket_universe_param,
        )
        try:
            raw = pocket.raw(mint)
        except Exception as exc:  # never print the URL: it contains the key (review round 2, finding 4)
            status = getattr(getattr(exc, "response", None), "status_code", None)
            print(f"pocket universe request failed: {type(exc).__name__} status={status}", file=sys.stderr)
            return 1
        print(json.dumps(raw, indent=2, sort_keys=True))
        return 0
    results = probe_dexscreener(DexScreenerClient(http, settings.dexscreener_base_url, settings.batch_size))
    for provider in build_providers(settings, http):
        results.extend(probe_provider(provider))
    for r in results:
        print(f"{'OK  ' if r.ok else 'FAIL'} {r.name}: {r.detail}")
    return 0 if all(r.ok for r in results) else 1


def _deployer(storage: Storage, args: argparse.Namespace) -> None:
    if args.action == "block":
        storage.deployer_block(args.chain_id, args.address, args.reason, utc_now())
        print("blocked")
    elif args.action == "unblock":
        print("unblocked" if storage.deployer_unblock(args.chain_id, args.address) else "not blocked")
    else:
        print("chain\taddress\ttokens\trugs\tmalicious\tmanual\treason")
        for d in storage.deployers():
            print(
                f"{d['chain_id']}\t{d['address']}\t{d['tokens']}\t{d['rugs']}\t"
                f"{d['malicious']}\t{d['manual_block']}\t{d['reason']}"
            )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _load_dotenv()
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    logsetup.configure(logging.DEBUG if args.verbose else logging.INFO, settings.log_format, settings.secrets)

    if args.command == "probe":
        return _probe(settings, args.target, args.mint)

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
        elif args.command == "evaluate":
            groups = evaluate(storage)
            print(json.dumps(to_dicts(groups), indent=2) if args.json else render(groups))
        elif args.command == "health":
            return _health(storage, args.max_age_minutes)
        elif args.command == "blacklist":
            if args.action == "add":
                storage.blacklist_add(args.chain_id, args.address, args.reason)
                print("added")
            elif args.action == "remove":
                print("removed" if storage.blacklist_remove(args.chain_id, args.address) else "not found")
            else:
                for chain_id, address, reason in storage.blacklist_list():
                    print(f"{chain_id}\t{address}\t{reason}")
        elif args.command == "deployer":
            _deployer(storage, args)
        return 0
    finally:
        storage.close()

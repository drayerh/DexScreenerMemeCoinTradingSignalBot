from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
import responses

from dexsignal import cli

DS = "https://api.dexscreener.com"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "cli.db"
    for key in list(os.environ):
        if key.startswith("DSB_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("DSB_DATABASE_PATH", str(db))
    monkeypatch.setenv("DSB_MAX_REQUESTS_PER_MINUTE", "6000")
    monkeypatch.chdir(tmp_path)  # keep any .env lookup inside the temp dir
    return db


def test_blacklist_commands(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["blacklist", "add", "solana", "Mint1", "--reason", "rug"]) == 0
    assert cli.main(["blacklist", "list"]) == 0
    assert "solana\tMint1\trug" in capsys.readouterr().out
    assert cli.main(["blacklist", "remove", "solana", "Mint1"]) == 0
    assert cli.main(["blacklist", "remove", "solana", "Mint1"]) == 0
    assert "not found" in capsys.readouterr().out


def test_stats_and_export(env: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert cli.main(["stats"]) == 0
    assert "snapshots: 0" in capsys.readouterr().out
    out = tmp_path / "obs.csv"
    assert cli.main(["export", "observations", "-o", str(out)]) == 0
    assert out.read_text().startswith("id,chain_id")
    assert cli.main(["export", "snapshots"]) == 0
    assert capsys.readouterr().out.startswith("id,chain_id")


def test_config_error_exit_code(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DSB_BATCH_SIZE", "zero")
    assert cli.main(["stats"]) == 2


@responses.activate
def test_once_runs_a_cycle(env: Path) -> None:
    responses.get(f"{DS}/token-profiles/latest/v1", json=[{"chainId": "base", "tokenAddress": "0xA"}])
    responses.get(
        f"{DS}/tokens/v1/base/0xA",
        json=[
            {
                "chainId": "base",
                "dexId": "uni",
                "pairAddress": "P",
                "priceUsd": "1.5",
                "baseToken": {"address": "0xA", "symbol": "AAA"},
                "liquidity": {"usd": 80000},
                "volume": {"h24": 1000},
            }
        ],
    )
    assert cli.main(["once"]) == 0
    assert cli.main(["stats"]) == 0


@responses.activate
def test_once_reports_discovery_failure(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DSB_TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("DSB_TELEGRAM_CHAT_ID", "1")
    responses.get(f"{DS}/token-profiles/latest/v1", status=503)
    assert cli.main(["once"]) == 1


def test_run_loop_stops_on_event(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class P:
        def run_cycle(self) -> None:
            calls.append(1)
            stop.set()

    stop = threading.Event()
    monkeypatch.setattr("dexsignal.cli.signal.signal", lambda *_a: None)
    cli._run_loop(P(), 1, stop)  # type: ignore[arg-type]
    assert calls == [1]

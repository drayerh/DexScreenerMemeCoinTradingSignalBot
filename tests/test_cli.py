from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest
import responses

from dexsignal import cli
from dexsignal.config import Settings
from dexsignal.http import HttpClient
from dexsignal.storage import Storage, utc_now

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


# --- v2 commands ------------------------------------------------------------------
GP = "https://api.gopluslabs.io"
RC = "https://api.rugcheck.xyz"


def test_deployer_commands(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["deployer", "block", "solana", "Dev1", "--reason", "rugs"]) == 0
    assert cli.main(["deployer", "list"]) == 0
    out = capsys.readouterr().out
    assert "solana\tDev1\t0\t0\t0\t1\trugs" in out
    assert cli.main(["deployer", "unblock", "solana", "Dev1"]) == 0
    assert cli.main(["deployer", "unblock", "solana", "Dev1"]) == 0
    assert "not blocked" in capsys.readouterr().out


def test_evaluate_command(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["evaluate"]) == 0
    assert "(baseline)" in capsys.readouterr().out
    assert cli.main(["evaluate", "--json"]) == 0
    assert '"group": "ACCEPTED TOKENS from first sighting (baseline)"' in capsys.readouterr().out


def test_health_command(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["health"]) == 1
    s = Storage(env)
    s.record_cycle(utc_now(), ok=True)
    s.close()
    assert cli.main(["health"]) == 0
    s = Storage(env)
    s.set_meta("last_ok_cycle_at", "2020-01-01T00:00:00+00:00")
    s.close()
    assert cli.main(["health", "--max-age-minutes", "5"]) == 1
    assert "unhealthy" in capsys.readouterr().out


def test_probe_pocket_requires_configuration(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli.main(["probe", "pocket", "Mint"]) == 2
    monkeypatch.setenv("DSB_POCKET_UNIVERSE_BASE_URL", "https://pu.example")
    monkeypatch.setenv("DSB_POCKET_UNIVERSE_API_KEY", "PUKEY")
    assert cli.main(["probe", "pocket"]) == 2


@responses.activate
def test_probe_pocket_prints_raw(
    env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DSB_POCKET_UNIVERSE_BASE_URL", "https://pu.example")
    monkeypatch.setenv("DSB_POCKET_UNIVERSE_API_KEY", "PUKEY")
    responses.get("https://pu.example/rug_check/PUKEY", json={"status": "processed", "is_scam": False})
    assert cli.main(["probe", "pocket", "Mint"]) == 0
    assert '"status": "processed"' in capsys.readouterr().out


def _mock_providers_ok() -> None:
    for path in ("token-profiles/latest", "token-boosts/latest", "token-boosts/top"):
        responses.get(f"{DS}/{path}/v1", json=[{"chainId": "solana", "tokenAddress": "A"}])
    responses.get(
        f"{DS}/tokens/v1/ethereum/0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        json=[
            {
                "chainId": "ethereum",
                "dexId": "uni",
                "pairAddress": "P",
                "priceUsd": "1",
                "baseToken": {"address": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "symbol": "USDC"},
                "liquidity": {"usd": 1e9},
                "volume": {"h24": 1e8},
            }
        ],
    )
    responses.get(
        f"{RC}/v1/tokens/So11111111111111111111111111111111111111112/report/summary",
        json={"score_normalised": 1, "risks": []},
    )
    responses.get(
        f"{GP}/api/v1/solana/token_security",
        json={"code": 1, "result": {"x": {"mintable": {"status": "0"}, "freezable": {"status": "0"}}}},
    )
    responses.get(f"{GP}/api/v1/token_security/1", json={"code": 1, "result": {"x": {"is_honeypot": "0"}}})
    responses.get(
        "https://api.etherscan.io/v2/api", json={"status": "1", "result": [{"contractCreator": "0xc"}]}
    )


@responses.activate
def test_probe_all_ok(env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("DSB_ETHERSCAN_API_KEY", "ESKEY")
    _mock_providers_ok()
    assert cli.main(["probe"]) == 0
    out = capsys.readouterr().out
    assert out.count("OK  ") == 8 and "FAIL" not in out


@responses.activate
def test_probe_reports_failures(env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    responses.get(f"{DS}/token-profiles/latest/v1", status=500)
    responses.get(f"{DS}/tokens/v1/ethereum/0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", json=[])
    responses.get(f"{RC}/v1/tokens/So11111111111111111111111111111111111111112/report/summary", status=500)
    responses.get(f"{GP}/api/v1/solana/token_security", status=500)
    responses.get(f"{GP}/api/v1/token_security/1", status=500)
    assert cli.main(["probe"]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_goplus_can_be_disabled_and_json_logs(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DSB_GOPLUS_ENABLED", "false")
    monkeypatch.setenv("DSB_LOG_FORMAT", "json")
    settings = Settings.from_env()
    names = [p.name for p in cli.build_providers(settings, HttpClient(5, 60))]
    assert names == ["rugcheck"]
    assert cli.main(["stats"]) == 0

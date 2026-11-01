from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path

import pytest
import requests
import responses

from dexsignal.config import ConfigError, Settings
from dexsignal.http import HttpClient, RateLimiter
from dexsignal.models import Observation, ObservationKind
from dexsignal.signals import detect, pct_change
from dexsignal.storage import Point, Storage

from .conftest import T0, snap


# --- config -----------------------------------------------------------------
def test_defaults_and_overrides() -> None:
    s = Settings.from_env({"DSB_MIN_LIQUIDITY_USD": "1000", "DSB_DEXSCREENER_BASE_URL": "https://x/ "})
    assert s.min_liquidity_usd == 1000
    assert s.dexscreener_base_url == "https://x"
    assert not s.telegram_enabled
    assert Settings.from_env({"DSB_TELEGRAM_TOKEN": "t", "DSB_TELEGRAM_CHAT_ID": "1"}).telegram_enabled


@pytest.mark.parametrize(
    "env",
    [
        {"DSB_MIN_LIQUIDITY_USD": "lots"},
        {"DSB_BATCH_SIZE": "1.5"},
        {"DSB_BATCH_SIZE": "0"},
        {"DSB_MIN_LIQUIDITY_USD": "-1"},
        {"DSB_TELEGRAM_TOKEN": "only-token"},
        {"DSB_FALL_PCT": "150"},
    ],
)
def test_invalid_config_is_rejected(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_blank_values_use_defaults() -> None:
    assert Settings.from_env({"DSB_BATCH_SIZE": " ", "DSB_RISE_PCT": ""}) == Settings()


# --- rate limiting and http ---------------------------------------------------
def test_rate_limiter_spaces_requests_per_host() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(60, clock=lambda: now[0], sleep=sleep)
    limiter.wait("a")
    limiter.wait("a")
    limiter.wait("b")
    assert slept == [1.0]


@responses.activate
def test_http_client_sets_user_agent_and_raises_on_error() -> None:
    responses.get("https://h/ok", json={"a": 1})
    responses.get("https://h/bad", status=400)
    client = HttpClient(5, 600, limiter=RateLimiter(600, sleep=lambda _s: None), retries=0)
    assert client.get_json("https://h/ok") == {"a": 1}
    assert "dexsignal" in responses.calls[0].request.headers["User-Agent"]
    with pytest.raises(requests.HTTPError):
        client.get_json("https://h/bad")


# --- signals ------------------------------------------------------------------
def pts(*prices: float, liq: float = 100.0) -> list[Point]:
    return [Point(T0 + timedelta(minutes=5 * i), p, liq, "X") for i, p in enumerate(prices)]


KW = {
    "now": T0,
    "window_minutes": 30,
    "min_points": 3,
    "rise_pct": 100.0,
    "fall_pct": 50.0,
    "liq_drop_pct": 50.0,
}


def test_detect_rules() -> None:
    assert [o.kind for o in detect("c", "a", pts(1, 1.5, 2), **KW)] == [ObservationKind.RISE]  # type: ignore[arg-type]
    assert [o.kind for o in detect("c", "a", pts(2, 1.5, 1), **KW)] == [ObservationKind.FALL]  # type: ignore[arg-type]
    assert detect("c", "a", pts(1, 1.2, 1.5), **KW) == []  # type: ignore[arg-type]
    assert detect("c", "a", pts(1, 3), **KW) == []  # type: ignore[arg-type]


def test_detect_rejects_unordered_points() -> None:
    with pytest.raises(ValueError):
        detect("c", "a", list(reversed(pts(1, 2, 3))), **KW)  # type: ignore[arg-type]


def test_pct_change_guards_zero() -> None:
    assert pct_change(0, 5) == 0.0
    assert pct_change(2, 3) == 50.0


# --- storage ------------------------------------------------------------------
def test_blacklist_crud(storage: Storage) -> None:
    storage.blacklist_add("solana", "MintCase", "scam")
    assert storage.is_blacklisted("solana", "MintCase")
    assert not storage.is_blacklisted("solana", "mintcase")  # Solana mints are case-sensitive
    assert storage.blacklist_list() == [("solana", "MintCase", "scam")]
    assert storage.blacklist_remove("solana", "MintCase")
    assert not storage.blacklist_remove("solana", "MintCase")


def test_cooldown_and_export(storage: Storage) -> None:
    obs = Observation("solana", "M", "X", ObservationKind.RISE, 120.0, 30, T0)
    assert not storage.recently_observed(obs, 60)
    storage.add_observation(obs)
    assert storage.recently_observed(obs, 60)
    later = Observation("solana", "M", "X", ObservationKind.RISE, 120.0, 30, T0 + timedelta(minutes=61))
    assert not storage.recently_observed(later, 60)
    storage.add_snapshot(snap(), T0)
    out = io.StringIO()
    assert storage.export_csv("snapshots", out) == 1
    assert out.getvalue().splitlines()[0].startswith("id,chain_id,token_address")
    with pytest.raises(ValueError):
        storage.export_csv("blacklist", io.StringIO())


def test_transaction_rolls_back(storage: Storage) -> None:
    with pytest.raises(RuntimeError), storage.transaction():
        storage.add_snapshot(snap(), T0)
        raise RuntimeError("fail")
    assert storage.stats()["snapshots"] == 0


def test_file_database_uses_wal(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    s = Storage(db)
    s.add_snapshot(snap(), T0)
    s.close()
    s2 = Storage(db)
    assert s2.stats()["snapshots"] == 1
    s2.close()


@pytest.mark.parametrize(
    "env",
    [
        {"DSB_STRICT_SCREENING": "maybe"},
        {"DSB_POCKET_UNIVERSE_METHOD": "PUT"},
        {"DSB_LOG_FORMAT": "xml"},
        {"DSB_POCKET_UNIVERSE_API_KEY": "k"},
        {"DSB_MAX_TAX_PCT": "150"},
    ],
)
def test_invalid_v2_config(env: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_v2_config_and_secrets() -> None:
    s = Settings.from_env(
        {
            "DSB_STRICT_SCREENING": "yes",
            "DSB_GOPLUS_ENABLED": "0",
            "DSB_POCKET_UNIVERSE_BASE_URL": "https://pu/",
            "DSB_POCKET_UNIVERSE_API_KEY": "secretkey",
            "DSB_POCKET_UNIVERSE_METHOD": "post",
            "DSB_ETHERSCAN_API_KEY": "abc",
        }
    )
    assert s.strict_screening and not s.goplus_enabled and s.pocket_universe_enabled
    assert s.pocket_universe_base_url == "https://pu" and s.pocket_universe_method == "POST"
    assert s.secrets == ("secretkey",)  # values shorter than 4 characters are not redacted

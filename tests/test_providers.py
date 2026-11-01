from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
import responses
from responses import matchers

from dexsignal.http import HttpClient, RateLimiter
from dexsignal.models import Observation, ObservationKind, TokenRef
from dexsignal.providers import (
    DexScreenerClient,
    TelegramNotifier,
    format_observation,
    parse_pair,
)

DS = "https://api.dexscreener.com"
TG = "https://api.telegram.org"


def http() -> HttpClient:
    return HttpClient(5.0, 600, limiter=RateLimiter(600, sleep=lambda _s: None), retries=0)


def pair(
    address: str, liq: float | None, price: str | None = "0.01", chain: str = "solana"
) -> dict[str, object]:
    return {
        "chainId": chain,
        "dexId": "raydium",
        "pairAddress": f"P-{address}-{liq}",
        "baseToken": {"address": address, "symbol": "MEME"},
        "priceUsd": price,
        "liquidity": {"usd": liq} if liq is not None else None,
        "volume": {"h24": 1000},
    }


def test_parse_pair_handles_string_price_and_rejects_bad_records() -> None:
    good = parse_pair(pair("A", 5000))
    assert good is not None and good.price_usd == 0.01
    assert parse_pair(pair("A", None)) is None
    assert parse_pair(pair("A", 5000, price="abc")) is None
    assert parse_pair(pair("A", 5000, price="0")) is None
    assert parse_pair(pair("A", -1)) is None
    assert parse_pair("not a dict") is None
    assert parse_pair({**pair("A", 5000), "chainId": 5}) is None
    no_volume = {**pair("A", 5000), "volume": None}
    assert parse_pair(no_volume) is not None


@responses.activate
def test_latest_tokens_deduplicates_and_skips_junk() -> None:
    responses.get(
        f"{DS}/token-profiles/latest/v1",
        json=[
            {"chainId": "solana", "tokenAddress": "A"},
            {"chainId": "solana", "tokenAddress": "A"},
            {"chainId": "base", "tokenAddress": "0xB"},
            {"chainId": "", "tokenAddress": "C"},
            "junk",
        ],
    )
    refs = DexScreenerClient(http(), DS, 30).latest_tokens()
    assert refs == [TokenRef("solana", "A"), TokenRef("base", "0xB")]


@responses.activate
def test_latest_tokens_accepts_single_object() -> None:
    responses.get(f"{DS}/token-profiles/latest/v1", json={"chainId": "solana", "tokenAddress": "A"})
    assert DexScreenerClient(http(), DS, 30).latest_tokens() == [TokenRef("solana", "A")]


@responses.activate
def test_best_pairs_batches_per_chain_and_keeps_most_liquid() -> None:
    responses.get(f"{DS}/tokens/v1/solana/A,B", json=[pair("A", 10), pair("A", 99), pair("B", 5)])
    responses.get(f"{DS}/tokens/v1/solana/C", json=[pair("C", 7), pair("QuoteOnly", 1e9)])
    responses.get(f"{DS}/tokens/v1/base/0xD", status=500)
    refs = [
        TokenRef("solana", "A"),
        TokenRef("solana", "B"),
        TokenRef("solana", "C"),
        TokenRef("base", "0xD"),
    ]
    pairs = DexScreenerClient(http(), DS, 2).best_pairs(refs)
    by_token = {p.token_address: p.liquidity_usd for p in pairs}
    assert by_token == {"A": 99, "B": 5, "C": 7}  # AC-6 batching; AC-4 failed batch skipped
    assert len(responses.calls) == 3


OBS = Observation(
    "solana", "Mint", "MEME", ObservationKind.RISE, 150.0, 30, datetime(2026, 1, 1, tzinfo=timezone.utc)
)


@responses.activate
def test_telegram_sends_labelled_message() -> None:
    responses.post(
        f"{TG}/botSECRET/sendMessage",
        json={"ok": True},
        match=[
            matchers.json_params_matcher(
                {"chat_id": "42", "text": format_observation(OBS), "disable_web_page_preview": True}
            )
        ],
    )
    assert TelegramNotifier(http(), TG, "SECRET", "42").send(OBS) is True
    body = json.loads(responses.calls[0].request.body or "{}")
    assert "Not financial advice" in body["text"]
    assert "+150.0%" in body["text"]


@responses.activate
def test_telegram_failure_returns_false_and_hides_token(caplog: pytest.LogCaptureFixture) -> None:
    responses.post(f"{TG}/botSECRET/sendMessage", status=401)
    assert TelegramNotifier(http(), TG, "SECRET", "42").send(OBS) is False
    assert "SECRET" not in caplog.text


def test_format_negative_change() -> None:
    obs = Observation("base", "0x1", "X", ObservationKind.FALL, -60.0, 30, OBS.observed_at)
    assert "-60.0%" in format_observation(obs)

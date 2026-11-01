"""Risk providers (REVIEW findings 7-12, 14) against documented response shapes."""

from __future__ import annotations

import logging
from typing import Any

import pytest
import responses
from responses import matchers

from dexsignal.http import HttpClient, RateLimiter
from dexsignal.models import PairSnapshot, ScreenStatus
from dexsignal.risk import (
    Creator,
    EtherscanCreatorProvider,
    GoPlusProvider,
    PocketUniverseProvider,
    RiskAssessment,
    RugCheckProvider,
    combine,
    creators_of,
    flag,
    number,
    top_holder_share,
)

RC = "https://api.rugcheck.xyz"
GP = "https://api.gopluslabs.io"
ES = "https://api.etherscan.io"
PU = "https://pu.example"
TOKEN = "0xabc0000000000000000000000000000000000001"
PAIR = "0xpair000000000000000000000000000000000001"


def http() -> HttpClient:
    return HttpClient(5.0, 6000, limiter=RateLimiter(6000, sleep=lambda _s: None), retries=0)


def evm(address: str = TOKEN) -> PairSnapshot:
    return PairSnapshot("ethereum", address, PAIR, "uniswap", "TKN", 1.0, 100_000, 1_000)


def sol(mint: str = "Mint111") -> PairSnapshot:
    return PairSnapshot("solana", mint, "PoolXYZ", "raydium", "SOLT", 1.0, 100_000, 1_000)


def goplus() -> GoPlusProvider:
    return GoPlusProvider(http(), GP, "", max_tax_pct=10, max_top10_pct=50)


# --- helpers ------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("0", False),
        (" 1 ", True),
        ("", None),
        (None, None),
        ("yes", None),
        (1, True),
        (0, False),
        (2, None),
        (True, True),
        ({"status": "1"}, True),
        ({"status": "0"}, False),
    ],
)
def test_flag_is_tri_state(value: Any, expected: bool | None) -> None:
    assert flag(value) is expected


def test_number_rejects_junk() -> None:
    assert number("0.05") == 0.05
    assert number(None) is None
    assert number(True) is None
    assert number("x") is None
    assert number("nan") is None


def test_top_holder_share_excludes_pools_lockers_and_burns() -> None:
    holders = [
        {"address": PAIR, "percent": "0.40"},  # the pool itself
        {"address": "0x000000000000000000000000000000000000dead", "percent": "0.20"},
        {"address": "0xlocker", "percent": "0.10", "is_locked": 1},
        {"address": "0xteam", "percent": "0.05", "tag": "UniCrypt Lock"},
        {"address": "0xwhale", "percent": "0.12"},
        {"address": "0xfish", "percent": "0.03"},
        "junk",
    ]
    assert top_holder_share(holders, PAIR) == pytest.approx(0.15)
    assert (
        top_holder_share([{"address": "0xa", "percent": "30"}, {"address": "0xb", "percent": "20"}], "")
        == 0.5
    )
    assert top_holder_share([], PAIR) is None
    assert top_holder_share([{"address": PAIR, "percent": "1"}], PAIR) == 0.0


def test_combine_precedence_and_strict_mode() -> None:
    passed = RiskAssessment("a", ScreenStatus.PASSED)
    rejected = RiskAssessment("b", ScreenStatus.REJECTED, ("bad",))
    unknown = RiskAssessment("c", ScreenStatus.UNSCREENED, ("?",))
    assert combine([passed, rejected], strict=False).status is ScreenStatus.REJECTED
    assert combine([passed, unknown], strict=True).status is ScreenStatus.PASSED
    assert combine([unknown], strict=False).status is ScreenStatus.UNSCREENED
    assert combine([unknown], strict=True).status is ScreenStatus.REJECTED
    assert combine([], strict=False).status is ScreenStatus.UNSCREENED


def test_creators_of_prefers_malicious_flag() -> None:
    a = RiskAssessment("a", ScreenStatus.PASSED, creators=(Creator("0x1"),))
    b = RiskAssessment("b", ScreenStatus.PASSED, creators=(Creator("0x1", True), Creator("0x2")))
    assert creators_of([a, b]) == [Creator("0x1", True), Creator("0x2")]


# --- RugCheck -------------------------------------------------------------------------
@responses.activate
def test_rugcheck_provider() -> None:
    p = RugCheckProvider(http(), RC, 50)
    url = f"{RC}/v1/tokens/{{}}/report/summary"
    responses.get(
        url.format("A"), json={"score_normalised": 10, "risks": [{"name": "Mint", "level": "danger"}]}
    )
    responses.get(url.format("B"), json={"score_normalised": 80, "risks": []})
    responses.get(url.format("C"), json={"score_normalised": 5, "lpLockedPct": 97.5, "risks": []})
    responses.get(url.format("D"), json={"risks": []})
    responses.get(url.format("E"), status=500)
    responses.get(url.format("F"), json=["x"])
    a = p.assess(sol("A"))
    assert a.status is ScreenStatus.REJECTED and "Mint" in a.reasons[0]
    assert p.assess(sol("B")).status is ScreenStatus.REJECTED
    c = p.assess(sol("C"))
    assert c.status is ScreenStatus.PASSED and c.features["lp_locked_pct"] == 97.5
    assert p.assess(sol("D")).status is ScreenStatus.UNSCREENED  # missing score is not safe
    assert p.assess(sol("E")).status is ScreenStatus.UNSCREENED
    assert p.assess(sol("F")).status is ScreenStatus.UNSCREENED
    assert p.supports("solana") and not p.supports("base")


# --- GoPlus EVM -------------------------------------------------------------------------
def gp_evm(entry: dict[str, Any], code: int = 1, key: str = TOKEN) -> dict[str, Any]:
    return {"code": code, "message": "OK", "result": {key: entry}}


SAFE_EVM = {
    "is_honeypot": "0",
    "sell_tax": "0.02",
    "buy_tax": "0",
    "is_open_source": "1",
    "creator_address": "0xCreator",
    "honeypot_with_same_creator": "0",
    "holders": [{"address": "0xa", "percent": "0.1"}],
}


@responses.activate
def test_goplus_evm_passes_safe_token_and_records_creator() -> None:
    responses.get(
        f"{GP}/api/v1/token_security/1",
        json=gp_evm(SAFE_EVM),
        match=[matchers.query_param_matcher({"contract_addresses": TOKEN})],
    )
    a = goplus().assess(evm())
    assert a.status is ScreenStatus.PASSED
    assert a.creators == (Creator("0xcreator", False),)
    assert a.features["sell_tax"] == 0.02 and a.features["top10_share"] == 0.1


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"is_honeypot": "1"}, "is_honeypot"),
        ({"hidden_owner": "1"}, "hidden_owner"),
        ({"sell_tax": "0.5"}, "sell_tax 50%"),
        ({"honeypot_with_same_creator": "3"}, "made 3 honeypots"),
        ({"holders": [{"address": "0xa", "percent": "0.9"}]}, "top-10 holders own 90%"),
    ],
)
@responses.activate
def test_goplus_evm_rejections(override: dict[str, Any], reason: str) -> None:
    responses.get(f"{GP}/api/v1/token_security/1", json=gp_evm({**SAFE_EVM, **override}))
    a = goplus().assess(evm())
    assert a.status is ScreenStatus.REJECTED
    assert any(reason in r for r in a.reasons)


@responses.activate
def test_goplus_evm_unknown_honeypot_is_not_safe() -> None:
    responses.get(
        f"{GP}/api/v1/token_security/1",
        json=gp_evm({k: v for k, v in SAFE_EVM.items() if k != "is_honeypot"}),
    )
    assert goplus().assess(evm()).status is ScreenStatus.UNSCREENED


@responses.activate
def test_goplus_failures_are_unscreened_and_auth_header_sent() -> None:
    responses.get(f"{GP}/api/v1/token_security/1", json=gp_evm(SAFE_EVM, code=2))
    assert goplus().assess(evm()).status is ScreenStatus.UNSCREENED
    responses.replace(responses.GET, f"{GP}/api/v1/token_security/1", status=503)
    assert goplus().assess(evm()).status is ScreenStatus.UNSCREENED
    responses.replace(responses.GET, f"{GP}/api/v1/token_security/1", json={"code": 1, "result": []})
    assert goplus().assess(evm()).status is ScreenStatus.UNSCREENED
    responses.replace(
        responses.GET,
        f"{GP}/api/v1/token_security/1",
        json=gp_evm(SAFE_EVM, key="0xother"),
        match=[matchers.header_matcher({"Authorization": "Bearer tok"})],
    )
    p = GoPlusProvider(http(), GP, "tok", max_tax_pct=10, max_top10_pct=50)
    assert p.assess(evm()).status is ScreenStatus.PASSED  # single-entry result accepted
    assert p.supports("base") and p.supports("solana") and not p.supports("tron")


# --- GoPlus Solana -----------------------------------------------------------------------
SAFE_SOL = {
    "mintable": {"status": "0", "authority": []},
    "freezable": {"status": "0", "authority": []},
    "creators": [{"address": "CreatorSol", "malicious_address": 0}],
    "holders": [{"token_account": "acc1", "percent": "0.05"}],
    "dex": [{"dex_name": "raydium", "burn_percent": 99.0}],
}


def gp_sol(entry: dict[str, Any]) -> dict[str, Any]:
    return {"code": 1, "result": {"Mint111": entry}}


@responses.activate
def test_goplus_solana_paths() -> None:
    url = f"{GP}/api/v1/solana/token_security"
    responses.get(url, json=gp_sol(SAFE_SOL))
    a = goplus().assess(sol())
    assert a.status is ScreenStatus.PASSED
    assert a.features["lp_burn_pct"] == 99.0
    assert a.creators == (Creator("CreatorSol", False),)

    responses.replace(responses.GET, url, json=gp_sol({**SAFE_SOL, "mintable": {"status": "1"}}))
    assert "goplus mintable" in goplus().assess(sol()).reasons

    malicious = {**SAFE_SOL, "creators": [{"address": "Bad", "malicious_address": 1}]}
    responses.replace(responses.GET, url, json=gp_sol(malicious))
    m = goplus().assess(sol())
    assert m.status is ScreenStatus.REJECTED and m.creators == (Creator("Bad", True),)

    unknown = {k: v for k, v in SAFE_SOL.items() if k != "freezable"}
    responses.replace(responses.GET, url, json=gp_sol(unknown))
    assert goplus().assess(sol()).status is ScreenStatus.UNSCREENED

    concentrated = {**SAFE_SOL, "holders": [{"token_account": "x", "percent": "0.8"}], "dex": []}
    responses.replace(responses.GET, url, json=gp_sol(concentrated))
    c = goplus().assess(sol())
    assert c.status is ScreenStatus.REJECTED and c.features["lp_burn_pct"] is None


# --- Etherscan -------------------------------------------------------------------------
@responses.activate
def test_etherscan_creator_lookup() -> None:
    p = EtherscanCreatorProvider(http(), ES, "KEY")
    responses.get(
        f"{ES}/v2/api",
        json={"status": "1", "result": [{"contractAddress": TOKEN, "contractCreator": "0xDeployer"}]},
        match=[
            matchers.query_param_matcher(
                {
                    "chainid": "1",
                    "module": "contract",
                    "action": "getcontractcreation",
                    "contractaddresses": TOKEN,
                    "apikey": "KEY",
                }
            )
        ],
    )
    a = p.assess(evm())
    assert a.creators == (Creator("0xdeployer"),) and a.status is ScreenStatus.UNSCREENED
    assert p.creator_only and p.supports("bsc") and not p.supports("solana")


@responses.activate
def test_etherscan_failures() -> None:
    p = EtherscanCreatorProvider(http(), ES, "KEY")
    responses.get(f"{ES}/v2/api", json={"status": "0", "message": "NOTOK", "result": "bad key"})
    assert p.assess(evm()).creators == ()
    responses.replace(responses.GET, f"{ES}/v2/api", status=500)
    assert "etherscan unavailable" in p.assess(evm()).reasons


# --- Pocket Universe ------------------------------------------------------------------
def pocket(method: str = "GET") -> PocketUniverseProvider:
    return PocketUniverseProvider(http(), PU, "SECRETKEY", method=method, param="address")


@responses.activate
def test_pocket_universe_verdicts() -> None:
    url = f"{PU}/rug_check/SECRETKEY"
    responses.get(
        url,
        json={
            "status": "processed",
            "is_scam": True,
            "message": "88% of volume is from past rug pullers",
            "rug_percent": 88,
            "fresh_percent": 4,
        },
        match=[matchers.query_param_matcher({"address": "Mint111"})],
    )
    a = pocket().assess(sol())
    assert a.status is ScreenStatus.REJECTED and "88% of volume" in a.reasons[0]
    assert a.features == {"rug_percent": 88.0, "fresh_percent": 4.0}
    responses.replace(responses.GET, url, json={"status": "processed", "is_scam": False})
    assert pocket().assess(sol()).status is ScreenStatus.PASSED
    responses.replace(responses.GET, url, json={"status": "not_processed", "message": "not enough trades"})
    assert pocket().assess(sol()).status is ScreenStatus.UNSCREENED
    responses.replace(responses.GET, url, json={"status": "processed"})
    assert pocket().assess(sol()).status is ScreenStatus.UNSCREENED
    responses.replace(responses.GET, url, json=["x"])
    assert pocket().assess(sol()).status is ScreenStatus.UNSCREENED


@responses.activate
def test_pocket_universe_post_and_key_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    url = f"{PU}/rug_check/SECRETKEY"
    responses.post(
        url,
        json={"status": "processed", "is_scam": False},
        match=[matchers.json_params_matcher({"address": "Mint111"})],
    )
    assert pocket("POST").assess(sol()).status is ScreenStatus.PASSED
    responses.replace(responses.POST, url, status=500)
    with caplog.at_level(logging.WARNING):
        assert pocket("POST").assess(sol()).status is ScreenStatus.UNSCREENED
    assert "SECRETKEY" not in caplog.text
    assert pocket().supports("solana") and not pocket().supports("base")

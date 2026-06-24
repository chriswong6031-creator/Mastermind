"""Guards for the fragility-CHAIN map + correlation gate (portfolio/fragility_chain).

Pure / offline — exercises the real YAML-backed chain map (with the in-code default fallback). Proves
classification, book exposure, the leading-edge fragile-chain detector, and the SUBTRACT-ONLY gate
(trims an over-concentrated cracking chain only in caution/risk-off; never injects a name)."""
from __future__ import annotations

from portfolio import fragility_chain as FC


def test_classify_leading_vs_downstream():
    # MU sits at the leading edge of the ai_buildout chain; NVDA is downstream.
    mu = {c["chain"]: c for c in FC.classify("MU")}
    assert "ai_buildout" in mu and mu["ai_buildout"]["position"] == "leading_edge"
    nv = {c["chain"]: c for c in FC.classify("NVDA")}
    assert "ai_buildout" in nv and nv["ai_buildout"]["position"] == "downstream"
    assert FC.classify("ZZZZ_NOT_A_TICKER") == []


def test_chains_of_includes_proxies():
    # an ETF proxy IS exposure to the chain
    assert "ai_buildout" in FC.chains_of("SMH")
    assert FC.chains_of("smh") == FC.chains_of("SMH")        # case-insensitive


def test_book_chain_exposure_sums_weights():
    book = [{"ticker": "NVDA", "weight": 0.3}, {"ticker": "AVGO", "weight": 0.2},
            {"ticker": "XLP", "weight": 0.1}]
    exp = FC.book_chain_exposure(book)
    assert "ai_buildout" in exp
    assert abs(exp["ai_buildout"]["weight"] - 0.5) < 1e-9   # NVDA + AVGO
    assert exp["ai_buildout"]["weight_pct"] == 50.0


def test_fragile_chains_from_hot_tickers():
    fr = FC.fragile_chains(["SMH", "SOXX", "NVDA"])
    ids = {d["id"] for d in fr}
    assert "ai_buildout" in ids
    assert FC.fragile_chains([]) == []                      # no hot names → nothing fragile


def test_assess_book_riskon_is_noop():
    book = [{"ticker": "NVDA", "weight": 0.5}, {"ticker": "AVGO", "weight": 0.3}]
    out = FC.assess_book(book, {"state": "risk_on", "drivers": [{"id": "ai_buildout"}]})
    assert out == {"chain_flags": [], "blocked_chains": [], "trims": []}


def test_assess_book_trims_over_cap_cracking_chain():
    # ai_buildout exposure 0.5 > chain_cap 0.35, and it's the named fragile driver → trim + block.
    book = [{"ticker": "NVDA", "weight": 0.3}, {"ticker": "AVGO", "weight": 0.2},
            {"ticker": "XLP", "weight": 0.1}]
    rs = {"state": "risk_off", "drivers": [{"id": "ai_buildout"}]}
    out = FC.assess_book(book, rs, chain_cap=0.35)
    assert "ai_buildout" in out["blocked_chains"]
    trimmed = {t["ticker"] for t in out["trims"]}
    assert trimmed == {"NVDA", "AVGO"}                      # only the chain's names; never XLP
    # subtract-only: every trim scale is in [0,1)
    assert all(0.0 <= t["scale"] < 1.0 for t in out["trims"])


def test_assess_book_under_cap_no_trim():
    book = [{"ticker": "NVDA", "weight": 0.2}, {"ticker": "XLP", "weight": 0.1}]
    out = FC.assess_book(book, {"state": "risk_off", "drivers": [{"id": "ai_buildout"}]}, chain_cap=0.35)
    assert out["trims"] == [] and out["blocked_chains"] == []


def test_assess_book_only_named_drivers():
    # the chain is over cap but NOT named as a driver → not gated (we only cut what's cracking).
    book = [{"ticker": "NVDA", "weight": 0.4}, {"ticker": "AVGO", "weight": 0.3}]
    out = FC.assess_book(book, {"state": "risk_off", "drivers": []}, chain_cap=0.35)
    assert out["trims"] == []


def test_never_raises_on_garbage():
    assert FC.book_chain_exposure(None) == {}
    assert FC.classify(None) == []
    assert FC.assess_book(None, None)["trims"] == []

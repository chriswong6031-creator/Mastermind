"""Tests for brain/intake.py — the unified ticker intake funnel.

The per-source loaders read vendored dashboard artifacts off disk; we monkeypatch
intake._read to inject synthetic artifacts so the merge / ranking / provenance / degrade
behaviour is tested deterministically (no dependence on what the macro side has built).
"""
import bot  # noqa: F401

from brain import intake

# ── W8 legacy-contract pin (2026-07-19): this file tests pre-W8 mechanics (design
# research/FLAGSHIP_V2_DECISION_CORE.md). The v2 entry/context gates + feeds are exercised by
# tests/test_flagship_v2_replay.py + tests/test_entry_context_engines.py; here they are pinned
# OFF so the legacy contracts stay deterministic under a live vendor checkout.
import pytest as _pytest_w8


@_pytest_w8.fixture(autouse=True)
def _w8_legacy_env(monkeypatch):
    monkeypatch.setenv("MASTERMIND_ENTRY_GATE", "0")
    monkeypatch.setenv("MASTERMIND_PROPHET_FEED", "0")
    monkeypatch.setenv("MASTERMIND_ROTATION_IN", "off")
    monkeypatch.setenv("MASTERMIND_NW_DECISION", "off")
    try:
        from portfolio import prophet_feed as _pf
        _pf._reset_cache()
    except Exception:
        pass
    yield
    try:
        from portfolio import prophet_feed as _pf
        _pf._reset_cache()
    except Exception:
        pass



def _patch(monkeypatch, files: dict):
    """Make intake._read(rel) return files[rel] (or None) regardless of disk."""
    monkeypatch.setattr(intake, "_read", lambda rel: files.get(rel))
    # open-theses reads the ledger, not _read — silence it unless a test wants it
    monkeypatch.setattr(intake, "_from_open_theses", lambda: {})


# --------------------------------------------------------------------------- #
# 1. briefing priority_queue drives the ranking
# --------------------------------------------------------------------------- #
def test_briefing_drives_ranking(monkeypatch):
    _patch(monkeypatch, {
        "intelligence/briefing.json": {
            "as_of": "2026-06-20",
            "macro_context": {"regime": "Goldilocks"},
            "priority_queue": [
                {"ticker": "NVDA", "priority": 0.85, "confidence": 1.0, "lean": 1,
                 "situation": "early edge", "falsifier": "insiders reverse"},
                {"ticker": "AAPL", "priority": 0.30, "confidence": 0.5, "lean": 1,
                 "situation": "confirmed"},
            ],
            "divergences": [],
        },
    })
    out = intake.build(limit=10)
    cands = out["candidates"]
    assert [c["ticker"] for c in cands] == ["NVDA", "AAPL"]
    assert cands[0]["score"] >= cands[1]["score"]
    assert cands[0]["falsifier"] == "insiders reverse"
    assert out["macro_context"]["regime"] == "Goldilocks"
    assert out["as_of"] == "2026-06-20"


# --------------------------------------------------------------------------- #
# 2. corroboration — a name flagged by multiple INDEPENDENT engines is lifted
# --------------------------------------------------------------------------- #
def test_corroboration_lifts_multi_engine(monkeypatch):
    _patch(monkeypatch, {
        "intelligence/briefing.json": {"macro_context": {}, "priority_queue": [
            {"ticker": "MULTI", "priority": 0.50, "lean": 1, "situation": "x"},
            {"ticker": "SOLO", "priority": 0.50, "lean": 1, "situation": "y"},
        ], "divergences": []},
        "basketdata/radar_ticker.json": {"tickers": [
            {"ticker": "MULTI", "state": "POSITIVE_DIVERGENCE", "edge_score": 40}]},
        "altdata/mastermind.json": {"signals": [
            {"ticker": "MULTI", "signal_score": 70, "action": "WATCH", "channels": ["insider"]}]},
    })
    cands = {c["ticker"]: c for c in intake.build(limit=10)["candidates"]}
    # MULTI is flagged by briefing + radar + altdata (3 engines) → corroboration bonus
    assert cands["MULTI"]["n_sources"] >= 3
    assert cands["MULTI"]["score"] > cands["SOLO"]["score"]
    assert set(cands["MULTI"]["sources"]) >= {"briefing", "radar", "altdata"}


# --------------------------------------------------------------------------- #
# 3. divergence flag + bonus
# --------------------------------------------------------------------------- #
def test_divergence_flag_and_bonus(monkeypatch):
    _patch(monkeypatch, {
        "intelligence/briefing.json": {"macro_context": {}, "priority_queue": [
            {"ticker": "DIV", "priority": 0.40, "lean": 1, "situation": "early edge"}],
            "divergences": [{"ticker": "DIV", "read": "early_edge", "confidence": 0.8, "lean": 1}]},
    })
    c = intake.build(limit=5)["candidates"][0]
    assert c["ticker"] == "DIV"
    assert c["divergent"] is True
    assert c["score"] > 0.40           # got the divergence bonus on top of base priority
    assert "divergence" in c["sources"]


# --------------------------------------------------------------------------- #
# 4. radar/altdata/news/standout each contribute when briefing is absent
# --------------------------------------------------------------------------- #
def test_sources_without_briefing(monkeypatch):
    _patch(monkeypatch, {
        "factordata/us_standouts.json": {"buy": [{"ticker": "STD", "label": "BUY ZONE", "conviction": 0.7}]},
        "basketdata/radar_ticker.json": {"tickers": [{"ticker": "RAD", "state": "NEGATIVE_DIVERGENCE", "edge_score": 60}]},
        "altdata/mastermind.json": {"signals": [{"ticker": "ALT", "signal_score": 90, "action": "BUY", "channels": ["congress"]}]},
        "news/by_ticker.json": {"tickers": {"NEW": {"n_recent": 8, "sentiment_lean": "pos"}}},
    })
    cands = {c["ticker"]: c for c in intake.build(limit=20)["candidates"]}
    assert set(cands) == {"STD", "RAD", "ALT", "NEW"}
    assert cands["RAD"]["lean"] == -1          # NEGATIVE_DIVERGENCE → bearish
    assert cands["ALT"]["lean"] == 1           # score 90 BUY → bullish
    # news alone is capped weak
    assert cands["NEW"]["score"] <= 0.5


# --------------------------------------------------------------------------- #
# 5. seed fallback when every source is empty — queue never empty
# --------------------------------------------------------------------------- #
def test_seed_fallback_when_empty(monkeypatch):
    _patch(monkeypatch, {})        # all reads → None
    out = intake.build(limit=5)
    assert out["n_universe"] == len(intake._SEED)
    assert all(c["sources"] == ["seed"] for c in out["candidates"])
    assert out["candidates"][0]["ticker"] in intake._SEED


# --------------------------------------------------------------------------- #
# 6. tickers(min_score) filters; limit truncates
# --------------------------------------------------------------------------- #
def test_tickers_min_score_and_limit(monkeypatch):
    _patch(monkeypatch, {
        "intelligence/briefing.json": {"macro_context": {}, "divergences": [], "priority_queue": [
            {"ticker": "HI", "priority": 0.80, "lean": 1, "situation": "x"},
            {"ticker": "LO", "priority": 0.10, "lean": 0, "situation": "y"},
        ]},
    })
    assert intake.tickers(limit=10, min_score=0.5) == ["HI"]
    assert intake.tickers(limit=1) == ["HI"]            # limit truncates before filter


# --------------------------------------------------------------------------- #
# 7. salience tiers — ACT requires high score AND ≥2 engines
# --------------------------------------------------------------------------- #
def test_salience_tiers(monkeypatch):
    _patch(monkeypatch, {
        "intelligence/briefing.json": {"macro_context": {}, "divergences": [
            {"ticker": "ACTNAME", "read": "early_edge", "confidence": 0.9, "lean": 1}],
            "priority_queue": [
            {"ticker": "ACTNAME", "priority": 0.70, "lean": 1, "situation": "edge"},
            {"ticker": "WATCHME", "priority": 0.30, "lean": 0, "situation": "meh"}]},
        "altdata/mastermind.json": {"signals": [
            {"ticker": "ACTNAME", "signal_score": 80, "action": "BUY", "channels": ["insider"]}]},
    })
    tiers = intake.salience_tiers(limit=10)
    act = {c["ticker"] for c in tiers["act"]}
    assert "ACTNAME" in act and "WATCHME" not in act
    assert "ACTNAME" in {c["ticker"] for c in tiers["divergent"]}


# --------------------------------------------------------------------------- #
# 8. degrade-never-raise on malformed artifacts
# --------------------------------------------------------------------------- #
def test_degrade_on_malformed(monkeypatch):
    _patch(monkeypatch, {
        "intelligence/briefing.json": {"priority_queue": [
            {"ticker": None}, {"no_ticker": True}, {"ticker": "OK", "priority": "notanumber"}]},
        "basketdata/radar_ticker.json": {"tickers": {"X": {"state": "QUIET"}}},  # dict shape + QUIET dropped
        "altdata/mastermind.json": {"signals": [{"ticker": "Y"}]},               # no score → skipped
    })
    out = intake.build(limit=10)             # must not raise
    tks = {c["ticker"] for c in out["candidates"]}
    assert "OK" in tks                        # coerced bad priority → 0.0, still listed
    assert "X" not in tks and "Y" not in tks  # QUIET radar + scoreless alt dropped


# --------------------------------------------------------------------------- #
# 9. open theses feed in (real loader, ledger monkeypatched)
# --------------------------------------------------------------------------- #
def test_open_theses_feed_in(monkeypatch):
    monkeypatch.setattr(intake, "_read", lambda rel: None)
    import brain.ledger as ledger
    monkeypatch.setattr(ledger, "all_theses",
                        lambda: [{"subject": "tsla", "status": "open"},
                                 {"subject": "OLD", "status": "closed"}])
    cands = {c["ticker"]: c for c in intake.build(limit=10)["candidates"]}
    assert "TSLA" in cands and "OLD" not in cands
    assert "thesis" in cands["TSLA"]["sources"]

"""tests/test_defensive_candidates.py — guards for THE canonical defensive generator (W4 A1).

Pure / offline / intent-only. Proves the three-source UNION, dedup, the gate_go behaviour (a NO-GO
board drops single names but keeps the sector ETFs), the freshness pass-through (stale cycles ->
nothing), the all-absent -> [] degrade, archetype tagging, and the frozen-equal-weight prior.

NO LIVE MARKET STATE is pinned: every source is monkeypatched to a synthetic fixture so the assertions
test the generator's LOGIC, never today's regime.
"""
from __future__ import annotations

import bot  # noqa: F401  -> puts vendor/macro on sys.path
from portfolio import defensive_candidates as DC


# ── source stubs ────────────────────────────────────────────────────────────────────────────────
def _patch_playbook(monkeypatch, favor, archetype="ai_capex_unwind"):
    """Stub defensive_playbook.defensive_tilt via the module's _from_playbook import surface."""
    import portfolio.defensive_playbook as _dp
    monkeypatch.setattr(_dp, "defensive_tilt",
                        lambda rs: {"archetype": archetype, "favor": favor})


def _patch_cycles(monkeypatch, rows):
    """Stub regime_frame.cycles() to return *rows* (dict of ETF -> {phaseLabel, phase, ...})."""
    import brain.regime_frame as _rf
    monkeypatch.setattr(_rf, "cycles", lambda: rows)


def _patch_standouts(monkeypatch, board):
    """Stub the board loader to return *board* (or None for absent)."""
    monkeypatch.setattr(DC, "_read_standouts", lambda: board)


def _bottoming_cycles():
    return {
        "XLU": {"phase": "Trough", "phaseLabel": "Bottoming", "pos": 16.2, "osc_slope": 0.1},
        "XLC": {"phase": "Trough", "phaseLabel": "Bottoming", "pos": 0.9, "osc_slope": 0.2},
        "XLY": {"phase": "Recovery", "phaseLabel": "Prime entry", "pos": 31.5, "osc_slope": 0.3},
        "XLK": {"phase": "Peak", "phaseLabel": "Topping", "pos": 80.8, "osc_slope": -0.2},
        "XLE": {"phase": "Downturn", "phaseLabel": "Rolling over", "pos": 33.2, "osc_slope": -0.4},
    }


def _board(gate_go, rows):
    return {"gate_go": gate_go, "rank_by": "bottoming-alignment", "buy": rows}


def _row(ticker, label):
    return {"ticker": ticker, "label": label, "state": "TURN SIGNALED", "dir": "up"}


# ── UNION + dedup ─────────────────────────────────────────────────────────────────────────────
def test_union_of_three_sources(monkeypatch):
    _patch_playbook(monkeypatch, ["XLP", "XLV", "USMV", "SGOV", "TLT"])
    _patch_cycles(monkeypatch, _bottoming_cycles())
    _patch_standouts(monkeypatch, _board(True, [_row("WDAY", "BUY ZONE"),
                                                _row("SPGI", "BOTTOMING")]))
    out = DC.candidates()
    tickers = {c["ticker"] for c in out}
    # (a) playbook favor + (b) fresh-cycle sector ETFs + (c) gate-cleared single names
    assert {"XLP", "XLV", "USMV", "SGOV", "TLT"} <= tickers      # source a
    assert {"XLU", "XLC", "XLY"} <= tickers                       # source b (Bottoming/Prime entry)
    assert {"WDAY", "SPGI"} <= tickers                            # source c
    # late-cycle sectors do NOT enter as candidates
    assert "XLK" not in tickers and "XLE" not in tickers


def test_dedup_first_source_wins(monkeypatch):
    # A ticker that appears in BOTH the playbook (a) and the board (c) is emitted ONCE, tagged by the
    # higher-priority source (a → playbook).
    _patch_playbook(monkeypatch, ["XLV"])
    _patch_cycles(monkeypatch, {})
    _patch_standouts(monkeypatch, _board(True, [_row("XLV", "BOTTOMING")]))
    out = DC.candidates()
    xlv = [c for c in out if c["ticker"] == "XLV"]
    assert len(xlv) == 1                       # deduped
    assert xlv[0]["source"] == "playbook"      # first source (a) owns the tag


# ── gate_go: drops single names, KEEPS the sector ETFs ────────────────────────────────────────
def test_gate_false_excludes_single_names_keeps_etfs(monkeypatch):
    _patch_playbook(monkeypatch, ["XLP", "SGOV"])
    _patch_cycles(monkeypatch, _bottoming_cycles())
    _patch_standouts(monkeypatch, _board(False, [_row("WDAY", "BUY ZONE"),
                                                 _row("SPGI", "BOTTOMING")]))
    out = DC.candidates()
    tickers = {c["ticker"] for c in out}
    # the single-name board is gated OUT ...
    assert "WDAY" not in tickers and "SPGI" not in tickers
    # ... but the ETF sources (a)+(b) are UNAFFECTED by the board's gate
    assert {"XLP", "SGOV"} <= tickers
    assert {"XLU", "XLC", "XLY"} <= tickers


def test_gate_missing_ingests_single_names(monkeypatch):
    # legacy artifact with no gate_go key → today's behaviour (ingest the board)
    _patch_playbook(monkeypatch, [])
    _patch_cycles(monkeypatch, {})
    _patch_standouts(monkeypatch, {"buy": [_row("WDAY", "BUY ZONE")]})   # no gate_go key
    out = DC.candidates()
    assert {c["ticker"] for c in out} == {"WDAY"}


# ── freshness: stale cycles contributes nothing ────────────────────────────────────────────────
def test_stale_cycles_contributes_nothing(monkeypatch):
    # cycles() returns {} when its own freshness gate fires (stale/absent file). Source (b) is then
    # empty; the other sources are unaffected.
    _patch_playbook(monkeypatch, ["XLP"])
    _patch_cycles(monkeypatch, {})
    _patch_standouts(monkeypatch, _board(True, [_row("WDAY", "BUY ZONE")]))
    out = DC.candidates()
    tickers = {c["ticker"] for c in out}
    assert tickers == {"XLP", "WDAY"}          # no sector-rotation rows
    assert not any(c["source"] == "cycles" for c in out)


# ── all-sources-absent → [] ────────────────────────────────────────────────────────────────────
def test_all_sources_absent_returns_empty(monkeypatch):
    _patch_playbook(monkeypatch, [])
    _patch_cycles(monkeypatch, {})
    _patch_standouts(monkeypatch, None)
    assert DC.candidates() == []               # legal 'no defensive candidates today'


def test_broken_source_does_not_crash_union(monkeypatch):
    # one source raising must never crash the union — it just contributes nothing.
    import brain.regime_frame as _rf
    def _boom():
        raise RuntimeError("cycles blew up")
    monkeypatch.setattr(_rf, "cycles", _boom)
    _patch_playbook(monkeypatch, ["XLP"])
    _patch_standouts(monkeypatch, None)
    out = DC.candidates()
    assert {c["ticker"] for c in out} == {"XLP"}


# ── archetype tagging ──────────────────────────────────────────────────────────────────────────
def test_archetype_tagging(monkeypatch):
    _patch_playbook(monkeypatch, ["TLT", "SGOV", "XLV"])
    _patch_cycles(monkeypatch, _bottoming_cycles())
    _patch_standouts(monkeypatch, _board(True, [_row("SPGI", "BOTTOMING")]))
    out = {c["ticker"]: c for c in DC.candidates()}
    assert out["TLT"]["archetype"] == "duration"
    assert out["SGOV"]["archetype"] == "ballast_cash"
    assert out["XLV"]["archetype"] == "quality_defensive"
    assert out["XLU"]["archetype"] == "sector_rotation"      # fresh sector ETF
    assert out["SPGI"]["archetype"] == "quality_defensive"   # bottoming single name
    # every archetype is in the sanctioned taxonomy
    assert all(c["archetype"] in DC._ARCHETYPES for c in out.values())


def test_every_row_has_required_fields(monkeypatch):
    _patch_playbook(monkeypatch, ["XLP"])
    _patch_cycles(monkeypatch, _bottoming_cycles())
    _patch_standouts(monkeypatch, _board(True, [_row("WDAY", "BUY ZONE")]))
    for c in DC.candidates():
        assert set(c.keys()) == {"ticker", "source", "archetype", "note"}
        assert c["ticker"] and c["archetype"] and isinstance(c["note"], str)


# ── weights: frozen equal-weight ───────────────────────────────────────────────────────────────
def test_weights_frozen_equal(monkeypatch):
    _patch_playbook(monkeypatch, ["XLP", "XLV"])
    _patch_cycles(monkeypatch, {"XLU": {"phase": "Trough", "phaseLabel": "Bottoming"}})
    _patch_standouts(monkeypatch, None)
    cands = DC.candidates()
    w = DC.weights(cands)
    assert w["frozen"] is True
    assert w["method"] == "equal_weight"
    assert "frozen-equal-weight" in w["note"] and ">=12" in w["note"]
    vals = list(w["weights"].values())
    assert len(vals) == 3                       # XLP, XLV, XLU
    assert all(abs(v - vals[0]) < 1e-9 for v in vals)   # all equal
    assert abs(sum(vals) - 1.0) < 1e-4                   # sum ~1 (6dp rounding of 1/N)


def test_weights_empty_on_no_candidates():
    w = DC.weights([])
    assert w["weights"] == {}
    assert w["frozen"] is True                  # frozen note still present on the empty set


def test_weights_none_calls_candidates(monkeypatch):
    _patch_playbook(monkeypatch, ["XLP"])
    _patch_cycles(monkeypatch, {})
    _patch_standouts(monkeypatch, None)
    w = DC.weights()                            # cands=None → weights() computes candidates()
    assert set(w["weights"].keys()) == {"XLP"}
    assert w["weights"]["XLP"] == 1.0


# ── never raises ───────────────────────────────────────────────────────────────────────────────
def test_never_raises_on_garbage(monkeypatch):
    _patch_standouts(monkeypatch, {"buy": "not-a-list", "gate_go": "weird"})
    # candidates() must not raise even with malformed inputs across the board
    assert isinstance(DC.candidates(), list)
    assert isinstance(DC.weights(None), dict)


def test_risk_state_threads_to_playbook(monkeypatch):
    # the risk_state arg must reach the playbook (source a is driver-conditional).
    seen = {}
    import portfolio.defensive_playbook as _dp
    def _tilt(rs):
        seen["rs"] = rs
        return {"archetype": "credit_event", "favor": ["XLV", "TLT"]}
    monkeypatch.setattr(_dp, "defensive_tilt", _tilt)
    _patch_cycles(monkeypatch, {})
    _patch_standouts(monkeypatch, None)
    rs = {"drivers": [{"id": "credit_break"}]}
    DC.candidates(rs)
    assert seen["rs"] is rs

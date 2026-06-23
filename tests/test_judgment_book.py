"""Guards for the JUDGMENT BOOK orchestrator (brain/judgment_book).

Offline only: we monkeypatch the strategist, the PM, committee.assess and lenses.full so NO real
LLM and NO vendor engine are touched. We prove:
  * flag OFF -> build() returns the input `sized` UNCHANGED (the byte-identical guard),
  * a committee DROP removes a name; a committee TRIM halves its weight,
  * the name_cap clamp bounds each weight,
  * the output rows carry the same schema conviction.build emits (so the downstream loop is
    unchanged), and shadow inputs are re-emitted for the surviving PM names.
"""
from __future__ import annotations

import pytest

import bot  # noqa: F401
from brain import judgment_book as J


def _patch_seats(monkeypatch, *, holdings, committee_fn):
    monkeypatch.setattr(J, "_enabled", lambda: True)

    import sys
    import types
    strat = types.ModuleType("brain.strategist")
    strat.run = lambda asof, regime: {"agent": "strategist", "confirmed_themes": []}

    pmc = types.ModuleType("brain.pm_conviction")
    pmc.build_book = lambda *a, **k: {"holdings": holdings, "cash": 0.1,
                                      "summary": "s", "sold_note": "", "ran": bool(holdings)}

    comm = types.ModuleType("brain.committee")
    comm.assess = committee_fn

    lenses = types.ModuleType("portfolio.lenses")
    lenses.full = lambda t, kind="name": {"synthesis": {"confluence": 0.25, "divergences": []}, "rows": []}
    lenses._load = lambda rel: {}
    lenses._g = lambda d, path: None

    # build() lazily does `from brain import strategist, pm_conviction, committee` and
    # `from portfolio import lenses` — which bind the PACKAGE ATTRIBUTES when the real submodules
    # were already imported by another test in the same process. (That is the combined-run hang:
    # the sys.modules mock misses, the REAL build_book runs, and it fires a real headless LLM call
    # that blocks.) Patch the package attributes (+ sys.modules, belt-and-suspenders) so the fakes
    # always win and NO real LLM / vendor engine is ever touched.
    import brain as _brain_pkg
    import portfolio as _pf_pkg
    monkeypatch.setattr(_brain_pkg, "strategist", strat, raising=False)
    monkeypatch.setattr(_brain_pkg, "pm_conviction", pmc, raising=False)
    monkeypatch.setattr(_brain_pkg, "committee", comm, raising=False)
    monkeypatch.setattr(_pf_pkg, "lenses", lenses, raising=False)
    monkeypatch.setitem(sys.modules, "brain.strategist", strat)
    monkeypatch.setitem(sys.modules, "brain.pm_conviction", pmc)
    monkeypatch.setitem(sys.modules, "brain.committee", comm)
    monkeypatch.setitem(sys.modules, "portfolio.lenses", lenses)


def _patch_gate(monkeypatch, *, decisions):
    """Patch the GATE OFFICER seat (lazy-imported in build() as `from brain import gate_officer`
    and `from portfolio import watchlist`). We capture watchlist parks. Per the P1 lesson, patch
    the package attributes AND sys.modules so a combined run never reaches the real seat / LLM."""
    import sys
    import types
    from brain import gate_officer as _real_go  # the real PURE reshaper (no LLM)
    parked = []
    go = types.ModuleType("brain.gate_officer")

    def _gate_assess(book, asof, *, regime=None, portfolio_ctx=None):
        # decisions are filtered to proposed tickers, exactly like the real seat.
        proposed = {r["ticker"] for r in book}
        return {"decisions": [d for d in decisions if d["ticker"] in proposed],
                "book_view": "", "rationale": "", "ran": True}

    go.gate_assess = _gate_assess
    go.apply_gate = _real_go.apply_gate  # exercise the real subtract-only reshaper

    wl = types.ModuleType("portfolio.watchlist")
    wl.append = lambda t, asof, reason=None, tech=None, combined=None: parked.append((t, reason))

    import brain as _brain_pkg
    import portfolio as _pf_pkg
    monkeypatch.setattr(_brain_pkg, "gate_officer", go, raising=False)
    monkeypatch.setattr(_pf_pkg, "watchlist", wl, raising=False)
    monkeypatch.setitem(sys.modules, "brain.gate_officer", go)
    monkeypatch.setitem(sys.modules, "portfolio.watchlist", wl)
    return parked


def _confirm(t, asof, **k):
    return {"action": "confirm", "scale": 1.0, "lean": "add", "rationale": "ok",
            "sentinel_stance": "SUPPORT"}


def test_flag_off_returns_sized_unchanged(monkeypatch):
    monkeypatch.delenv("MASTERMIND_FLAGSHIP_JUDGMENT", raising=False)
    sized = [{"ticker": "NVDA", "weight": 0.06, "confluence": 0.3, "bull": "", "bear": "",
              "divergences": [], "retained": False, "size_stage": None, "research": {}}]
    # any seat call would raise (not patched) — but the flag-off short-circuit must avoid them.
    out = J.build(sized, [], regime={}, asof="2026-06-22", gate_info={}, shadow_inputs=[])
    assert out is sized                                # same object, byte-identical guard


def test_confirm_keeps_pm_book(monkeypatch):
    _patch_seats(monkeypatch, holdings=[
        {"ticker": "NVDA", "weight": 0.06, "thesis": "AI infra", "conviction": "high"},
        {"ticker": "AME", "weight": 0.05, "thesis": "onshoring", "conviction": "medium"}],
        committee_fn=_confirm)
    shadow = []
    out = J.build([], [], regime={}, asof="2026-06-22", gate_info={}, shadow_inputs=shadow,
                  name_cap=0.08)
    assert {r["ticker"] for r in out} == {"NVDA", "AME"}
    # schema parity with conviction.build rows
    for r in out:
        assert {"ticker", "weight", "confluence", "bull", "bear", "divergences",
                "retained", "size_stage", "research"} <= set(r)
        assert r["judgment"]["source"] == "pm"
    assert len(shadow) == 2                            # shadow inputs re-emitted per PM name


def test_committee_drop_removes_name(monkeypatch):
    def _drop_ame(t, asof, **k):
        if t == "AME":
            return {"action": "drop", "scale": 0.0, "lean": "watch", "rationale": "no",
                    "sentinel_stance": "OPPOSE"}
        return _confirm(t, asof, **k)
    _patch_seats(monkeypatch, holdings=[
        {"ticker": "NVDA", "weight": 0.06, "thesis": "AI infra", "conviction": "high"},
        {"ticker": "AME", "weight": 0.05, "thesis": "onshoring", "conviction": "medium"}],
        committee_fn=_drop_ame)
    out = J.build([], [], regime={}, asof="2026-06-22", gate_info={}, shadow_inputs=[])
    assert {r["ticker"] for r in out} == {"NVDA"}      # subtract-only DROP honoured


def test_committee_trim_halves_weight(monkeypatch):
    def _trim(t, asof, **k):
        return {"action": "trim", "scale": 0.5, "lean": "hold", "rationale": "halve",
                "sentinel_stance": "OPPOSE"}
    _patch_seats(monkeypatch, holdings=[
        {"ticker": "NVDA", "weight": 0.06, "thesis": "AI infra", "conviction": "high"}],
        committee_fn=_trim)
    out = J.build([], [], regime={}, asof="2026-06-22", gate_info={}, shadow_inputs=[])
    assert out[0]["weight"] == pytest.approx(0.03)     # 0.06 * 0.5


def test_name_cap_clamp(monkeypatch):
    _patch_seats(monkeypatch, holdings=[
        {"ticker": "NVDA", "weight": 0.50, "thesis": "AI infra", "conviction": "high"}],
        committee_fn=_confirm)
    out = J.build([], [], regime={}, asof="2026-06-22", gate_info={}, shadow_inputs=[],
                  name_cap=0.08)
    assert out[0]["weight"] == pytest.approx(0.08)     # clamped to name_cap


def test_all_dropped_degrades_to_sized(monkeypatch):
    def _drop_all(t, asof, **k):
        return {"action": "drop", "scale": 0.0, "lean": "watch", "rationale": "no",
                "sentinel_stance": "OPPOSE"}
    _patch_seats(monkeypatch, holdings=[
        {"ticker": "NVDA", "weight": 0.06, "thesis": "x", "conviction": "high"}],
        committee_fn=_drop_all)
    sized = [{"ticker": "ENGINE", "weight": 0.05}]
    out = J.build(sized, [], regime={}, asof="2026-06-22", gate_info={}, shadow_inputs=[])
    assert out is sized                                # don't publish an empty book; engine path stands


# ───────────────────────── GATE OFFICER wiring (C1) ─────────────────────────
def test_gate_officer_off_byte_identical(monkeypatch):
    """MASTERMIND_GATE_OFFICER unset → the gate block is a no-op; output == P1 (both names kept).
    The gate seat is NOT patched, so if the wiring touched it with the flag off, build would error
    (or call a real LLM) and the assertion would fail — proving the flag-off path is byte-identical."""
    monkeypatch.delenv("MASTERMIND_GATE_OFFICER", raising=False)
    _patch_seats(monkeypatch, holdings=[
        {"ticker": "NVDA", "weight": 0.06, "thesis": "AI infra", "conviction": "high"},
        {"ticker": "AME", "weight": 0.05, "thesis": "onshoring", "conviction": "medium"}],
        committee_fn=_confirm)
    out = J.build([], [], regime={}, asof="2026-06-22", gate_info={}, shadow_inputs=[], name_cap=0.08)
    assert {r["ticker"] for r in out} == {"NVDA", "AME"}
    assert all("gate" not in r for r in out)           # no gate tagging when the seat is OFF


def test_gate_officer_on_vetoes_and_parks(monkeypatch):
    """MASTERMIND_GATE_OFFICER on + a canned VETO for AME → AME dropped from `out` and parked to the
    watchlist with a `gate_officer:` reason; NVDA survives."""
    monkeypatch.setenv("MASTERMIND_GATE_OFFICER", "1")
    _patch_seats(monkeypatch, holdings=[
        {"ticker": "NVDA", "weight": 0.06, "thesis": "AI infra", "conviction": "high"},
        {"ticker": "AME", "weight": 0.05, "thesis": "onshoring", "conviction": "medium"}],
        committee_fn=_confirm)
    parked = _patch_gate(monkeypatch, decisions=[
        {"ticker": "AME", "action": "veto", "scale": 0.0, "reason": "sector crowded"}])
    out = J.build([], [], regime={}, asof="2026-06-22", gate_info={}, shadow_inputs=[], name_cap=0.08)
    assert {r["ticker"] for r in out} == {"NVDA"}      # AME vetoed out
    assert parked and parked[0][0] == "AME"
    assert parked[0][1].startswith("gate_officer:")


def test_gate_officer_on_trims_weight(monkeypatch):
    """A canned TRIM scales the surviving row's weight and tags r['gate']."""
    monkeypatch.setenv("MASTERMIND_GATE_OFFICER", "1")
    _patch_seats(monkeypatch, holdings=[
        {"ticker": "NVDA", "weight": 0.06, "thesis": "AI infra", "conviction": "high"}],
        committee_fn=_confirm)
    _patch_gate(monkeypatch, decisions=[
        {"ticker": "NVDA", "action": "trim", "scale": 0.5, "reason": "oversized"}])
    out = J.build([], [], regime={}, asof="2026-06-22", gate_info={}, shadow_inputs=[], name_cap=0.08)
    assert out[0]["weight"] == pytest.approx(0.03)     # 0.06 * 0.5
    assert out[0]["gate"]["action"] == "trim"


def test_gate_officer_veto_all_degrades_to_sized(monkeypatch):
    """The Gate Officer vetoing every PM name → build returns the input `sized` unchanged (never
    publishes an empty book)."""
    monkeypatch.setenv("MASTERMIND_GATE_OFFICER", "1")
    _patch_seats(monkeypatch, holdings=[
        {"ticker": "NVDA", "weight": 0.06, "thesis": "AI infra", "conviction": "high"}],
        committee_fn=_confirm)
    _patch_gate(monkeypatch, decisions=[
        {"ticker": "NVDA", "action": "veto", "scale": 0.0, "reason": "x"}])
    sized = [{"ticker": "ENGINE", "weight": 0.05}]
    out = J.build(sized, [], regime={}, asof="2026-06-22", gate_info={}, shadow_inputs=[])
    assert out is sized


# ───────────────────── WATCHLIST re-review re-entry wiring ─────────────────────
def test_promoted_watchlist_name_reenters_candidate_pool(monkeypatch):
    """A name PROMOTED by the daily watchlist re-review is fed back into the PM's candidate pool
    (the `rejected` seed) so the desk reconsiders it this cycle. We capture the `rejected` list the
    PM seat actually receives. Dual-patched (package attr + sys.modules) per the P1 lesson so the
    real watchlist / PM seat / LLM is never touched."""
    monkeypatch.setenv("MASTERMIND_FLAGSHIP_JUDGMENT", "1")
    import sys
    import types

    captured = {}

    # PM seat that records the rejected pool it is seeded with, then proposes the engine name only.
    pmc = types.ModuleType("brain.pm_conviction")

    def _build_book(sized, rejected, **k):
        captured["rejected"] = list(rejected or [])
        return {"holdings": [{"ticker": "NVDA", "weight": 0.06, "thesis": "AI infra",
                              "conviction": "high"}],
                "cash": 0.1, "summary": "s", "sold_note": "", "ran": True}
    pmc.build_book = _build_book

    strat = types.ModuleType("brain.strategist")
    strat.run = lambda asof, regime: {"agent": "strategist", "confirmed_themes": []}
    comm = types.ModuleType("brain.committee")
    comm.assess = _confirm
    lenses = types.ModuleType("portfolio.lenses")
    lenses.full = lambda t, kind="name": {"synthesis": {"confluence": 0.25, "divergences": []},
                                          "rows": []}
    lenses._load = lambda rel: {}
    lenses._g = lambda d, path: None

    # watchlist seat: review() is a no-op, promote_candidates() returns one cleared name.
    wl = types.ModuleType("portfolio.watchlist")
    wl.review = lambda asof, *, still_withheld: {"promote": [], "expired": [], "active": []}
    wl.promote_candidates = lambda asof: [{"ticker": "PROMO", "combined": 64.0,
                                           "thesis": "armed off watch", "reason": "cleared",
                                           "asof": "2026-06-01"}]
    wl.timing_withhold = lambda tech: None

    import brain as _brain_pkg
    import portfolio as _pf_pkg
    monkeypatch.setattr(_brain_pkg, "strategist", strat, raising=False)
    monkeypatch.setattr(_brain_pkg, "pm_conviction", pmc, raising=False)
    monkeypatch.setattr(_brain_pkg, "committee", comm, raising=False)
    monkeypatch.setattr(_pf_pkg, "lenses", lenses, raising=False)
    monkeypatch.setattr(_pf_pkg, "watchlist", wl, raising=False)
    monkeypatch.setitem(sys.modules, "brain.strategist", strat)
    monkeypatch.setitem(sys.modules, "brain.pm_conviction", pmc)
    monkeypatch.setitem(sys.modules, "brain.committee", comm)
    monkeypatch.setitem(sys.modules, "portfolio.lenses", lenses)
    monkeypatch.setitem(sys.modules, "portfolio.watchlist", wl)

    out = J.build([], [], regime={}, asof="2026-06-23", gate_info={}, shadow_inputs=[],
                  name_cap=0.08)
    # the promoted name was injected into the candidate pool the PM saw …
    assert any(r.get("ticker") == "PROMO" and r.get("source") == "watchlist_promote"
               for r in captured.get("rejected", []))
    # … and the desk still produced its book (re-entry never breaks the build).
    assert {r["ticker"] for r in out} == {"NVDA"}

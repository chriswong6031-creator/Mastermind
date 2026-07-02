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


def _patch_seats_book(monkeypatch, *, book, committee_fn):
    """Like _patch_seats but the PM returns a full book dict (so tests can set the three-questions
    fields + sleeve tags directly). `book` is the full pm_conviction.build_book return dict."""
    monkeypatch.setattr(J, "_enabled", lambda: True)
    import sys
    import types
    strat = types.ModuleType("brain.strategist")
    strat.run = lambda asof, regime: {"agent": "strategist", "confirmed_themes": []}
    pmc = types.ModuleType("brain.pm_conviction")
    pmc.build_book = lambda *a, **k: book
    comm = types.ModuleType("brain.committee")
    comm.assess = committee_fn
    lenses = types.ModuleType("portfolio.lenses")
    lenses.full = lambda t, kind="name": {"synthesis": {"confluence": 0.25, "divergences": []}, "rows": []}
    lenses._load = lambda rel: {}
    lenses._g = lambda d, path: None
    # keep the defensive-candidate generator from touching the real vendor tree
    dc = types.ModuleType("portfolio.defensive_candidates")
    dc.candidates = lambda rs=None: []
    import brain as _brain_pkg
    import portfolio as _pf_pkg
    monkeypatch.setattr(_brain_pkg, "strategist", strat, raising=False)
    monkeypatch.setattr(_brain_pkg, "pm_conviction", pmc, raising=False)
    monkeypatch.setattr(_brain_pkg, "committee", comm, raising=False)
    monkeypatch.setattr(_pf_pkg, "lenses", lenses, raising=False)
    monkeypatch.setattr(_pf_pkg, "defensive_candidates", dc, raising=False)
    monkeypatch.setitem(sys.modules, "brain.strategist", strat)
    monkeypatch.setitem(sys.modules, "brain.pm_conviction", pmc)
    monkeypatch.setitem(sys.modules, "brain.committee", comm)
    monkeypatch.setitem(sys.modules, "portfolio.lenses", lenses)
    monkeypatch.setitem(sys.modules, "portfolio.defensive_candidates", dc)


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


# ───────────────────── MACRO RISK OFFICER teeth wiring (E) ─────────────────────
def _patch_macro_risk(monkeypatch, *, state):
    """Patch the MACRO RISK OFFICER seat (lazy-imported in build() as `from brain import macro_risk`).
    The fake `run` returns a canned risk-state dict; `apply_risk_state` is the REAL pure reshaper so we
    exercise the actual subtract-only teeth. portfolio.fragility_chain stays real (pure, YAML-backed).
    Dual-patched (package attr + sys.modules) per the P1 lesson."""
    import sys
    import types
    from brain import macro_risk as _real_mr
    mr = types.ModuleType("brain.macro_risk")
    mr.run = lambda asof, regime: dict(state)
    mr.apply_risk_state = _real_mr.apply_risk_state   # exercise the real subtract-only teeth
    import brain as _brain_pkg
    monkeypatch.setattr(_brain_pkg, "macro_risk", mr, raising=False)
    monkeypatch.setitem(sys.modules, "brain.macro_risk", mr)


def test_macro_risk_off_byte_identical(monkeypatch):
    """MASTERMIND_MACRO_RISK unset → the (A2)+(E) macro blocks are no-ops; output == P1 (both names
    kept, no risk_state tag). The seat is NOT patched, so any touch with the flag off would error."""
    monkeypatch.delenv("MASTERMIND_MACRO_RISK", raising=False)
    _patch_seats(monkeypatch, holdings=[
        {"ticker": "NVDA", "weight": 0.06, "thesis": "AI infra", "conviction": "high"},
        {"ticker": "AME", "weight": 0.05, "thesis": "onshoring", "conviction": "medium"}],
        committee_fn=_confirm)
    out = J.build([], [], regime={}, asof="2026-06-23", gate_info={}, shadow_inputs=[], name_cap=0.08)
    assert {r["ticker"] for r in out} == {"NVDA", "AME"}
    assert all("risk_state" not in r for r in out)     # no macro tagging when the seat is OFF


def test_macro_risk_on_caps_and_trims_cracking_chain(monkeypatch):
    """MASTERMIND_MACRO_RISK on + a CAUTION state naming the ai_buildout chain → the over-concentrated
    cracking chain is trimmed subtract-only and the rows are tagged. name_cap raised so the engine
    weights survive to the macro block."""
    monkeypatch.setenv("MASTERMIND_MACRO_RISK", "1")
    _patch_seats(monkeypatch, holdings=[
        {"ticker": "NVDA", "weight": 0.30, "thesis": "AI infra", "conviction": "high"},
        {"ticker": "AVGO", "weight": 0.30, "thesis": "AI infra", "conviction": "high"}],
        committee_fn=_confirm)
    _patch_macro_risk(monkeypatch, state={"state": "caution", "gross_cap": 0.55,
                                          "drivers": [{"id": "ai_buildout"}], "allow_adds": True})
    out = J.build([], [], regime={}, asof="2026-06-23", gate_info={}, shadow_inputs=[], name_cap=0.5)
    by = {r["ticker"]: r["weight"] for r in out}
    assert by.get("NVDA", 0) < 0.30 and by.get("AVGO", 0) < 0.30   # cracking chain trimmed
    assert all("risk_state" in r for r in out)                     # macro-risk tagging applied
    from portfolio import fragility_chain as FC
    assert FC.book_chain_exposure(out)["ai_buildout"]["weight"] <= 0.35 + 1e-6


def test_macro_risk_riskoff_blocks_adds_degrades_to_sized(monkeypatch):
    """A RISK-OFF state hard-stops net-new adds into the cracking chain. With every PM name a net-new
    add into ai_buildout, the proposed book empties → build returns the engine `sized` unchanged."""
    monkeypatch.setenv("MASTERMIND_MACRO_RISK", "1")
    _patch_seats(monkeypatch, holdings=[
        {"ticker": "NVDA", "weight": 0.06, "thesis": "AI infra", "conviction": "high"}],
        committee_fn=_confirm)
    _patch_macro_risk(monkeypatch, state={"state": "risk_off", "gross_cap": 0.55,
                                          "drivers": [{"id": "ai_buildout"}], "allow_adds": False})
    sized = [{"ticker": "ENGINE", "weight": 0.05}]
    out = J.build(sized, [], regime={}, asof="2026-06-23", gate_info={}, shadow_inputs=[])
    assert out is sized                                # add-block emptied the book → engine path stands


# ═════════════════════════ W4 B1: leadership pipe + three-questions ═════════════════════════

def test_flag_off_leadership_passthrough_byte_identical(monkeypatch):
    """Flag OFF → build() returns the input `sized` UNCHANGED even when leadership legs are piped in.
    The leadership legs (which travel separately in `book` at the call site) are never touched by the
    dark judgment layer — this is the E1 control-arm guarantee extended to the leadership pipe."""
    monkeypatch.delenv("MASTERMIND_FLAGSHIP_JUDGMENT", raising=False)
    sized = [{"ticker": "NVDA", "weight": 0.06, "confluence": 0.3}]
    lead = [{"ticker": "SMH", "weight": 0.12, "sleeve": "leadership"}]
    out = J.build(sized, [], regime={}, asof="2026-07-01", gate_info={}, shadow_inputs=[],
                  leadership=lead)
    assert out is sized                                # same object, byte-identical


def test_authority_clamp_restores_reweighted_surviving_leader(monkeypatch):
    """The PM KEPT a leadership leg (SMH) but re-weighted it up to 0.30 — the deterministic authority
    clamp RESTORES it to the engine's equal weight (0.12) and tags authority_clamped. A surviving
    leadership leg may NEVER be conviction-re-weighted (equal-weight rank-IC≈0 doctrine)."""
    monkeypatch.setenv("MASTERMIND_FLAGSHIP_JUDGMENT", "1")
    _patch_seats_book(monkeypatch, book={
        "holdings": [{"ticker": "SMH", "weight": 0.30, "thesis": "keep semis", "conviction": "high",
                      "sleeve": "leadership"},
                     {"ticker": "NVDA", "weight": 0.06, "thesis": "AI", "conviction": "high",
                      "sleeve": "conviction"}],
        "cash": 0.1, "summary": "s", "sold_note": "", "ran": True,
        "own_more": [], "own_less": [], "not_holding_should": []},
        committee_fn=_confirm)
    lead = [{"ticker": "SMH", "weight": 0.12, "sleeve": "leadership"}]
    out = J.build([], [], regime={}, asof="2026-07-01", gate_info={}, shadow_inputs=[],
                  name_cap=0.5, leadership=lead)
    smh = next(r for r in out if r["ticker"] == "SMH")
    assert smh["weight"] == pytest.approx(0.12)        # restored to engine equal weight
    assert smh["authority_clamped"]["from"] == pytest.approx(0.30)
    assert smh["authority_clamped"]["to"] == pytest.approx(0.12)
    # a conviction name is freely PM-weighted (NOT clamped)
    nvda = next(r for r in out if r["ticker"] == "NVDA")
    assert "authority_clamped" not in nvda


def test_dropped_leadership_budget_not_a_conviction_topup(monkeypatch):
    """The PM DROPPED a leadership leg (SMH omitted). Its budget must NOT reappear as a conviction
    top-up beyond caps — the surviving conviction name stays bounded by name_cap; SMH is simply gone
    from the reshaped book (its budget becomes cash/defensive downstream, never a survivor top-up)."""
    monkeypatch.setenv("MASTERMIND_FLAGSHIP_JUDGMENT", "1")
    _patch_seats_book(monkeypatch, book={
        "holdings": [{"ticker": "NVDA", "weight": 0.50, "thesis": "AI", "conviction": "high",
                      "sleeve": "conviction"}],           # PM omitted SMH; over-sized NVDA
        "cash": 0.1, "summary": "s", "sold_note": "", "ran": True,
        "own_more": [], "own_less": [], "not_holding_should": []},
        committee_fn=_confirm)
    lead = [{"ticker": "SMH", "weight": 0.12, "sleeve": "leadership"}]
    out = J.build([], [], regime={}, asof="2026-07-01", gate_info={}, shadow_inputs=[],
                  name_cap=0.08, leadership=lead)
    tickers = {r["ticker"] for r in out}
    assert "SMH" not in tickers                          # dropped leadership leg is gone
    nvda = next(r for r in out if r["ticker"] == "NVDA")
    assert nvda["weight"] <= 0.08 + 1e-9                 # bounded by name_cap — no top-up beyond caps


def test_three_questions_shadow_entries_land_with_falsifiers(monkeypatch, tmp_path):
    """Each not_holding_should rotation call emits a shadow entry (weight 0, kind
    not_holding_should) AND a gradable thesis with a 21-trading-day rel_return falsifier appended to
    the ledger. We isolate the ledger to a tmp file so the real theses.jsonl is never touched."""
    monkeypatch.setenv("MASTERMIND_FLAGSHIP_JUDGMENT", "1")
    from brain import ledger as _ledger
    monkeypatch.setattr(_ledger, "_LEDGER", tmp_path / "theses.jsonl", raising=False)

    _patch_seats_book(monkeypatch, book={
        "holdings": [{"ticker": "NVDA", "weight": 0.06, "thesis": "AI", "conviction": "high",
                      "sleeve": "conviction"}],
        "cash": 0.1, "summary": "s", "sold_note": "", "ran": True,
        "own_more": [{"ticker": "NVDA", "why_now": "breadth", "probability": 0.7}],
        "own_less": [{"ticker": "AMD", "why_now": "extended"}],
        "not_holding_should": [{"ticker": "XLV", "why_now": "defensive rotate",
                                "probability": 0.6, "check_by": "2026-08-01"}]},
        committee_fn=_confirm)
    shadow = []
    out = J.build([], [], regime={}, asof="2026-07-01", gate_info={}, shadow_inputs=shadow,
                  name_cap=0.08, leadership=[])
    assert {r["ticker"] for r in out} == {"NVDA"}
    # a shadow entry for the rotation call landed (weight 0, tagged not_holding_should)
    rot = [s for s in shadow if s.get("kind") == "not_holding_should"]
    assert rot and rot[0]["ticker"] == "XLV"
    assert rot[0]["weight_prod"] == 0.0 and rot[0]["source"] == "pm_judgment"
    # a gradable thesis with a rel_return falsifier landed in the (isolated) ledger
    theses = _ledger.all_theses()
    xlv = [t for t in theses if t.get("subject") == "XLV"]
    assert xlv, "rotation call XLV should be a gradable thesis"
    chk = (xlv[0].get("falsifier") or {}).get("check") or {}
    assert chk.get("kind") == "rel_return" and chk.get("subject_ticker") == "XLV"
    assert xlv[0].get("source") == "pm_judgment"        # graded via _is_pm_thesis
    assert xlv[0].get("check_by")                        # 21-trading-day horizon bound


def test_three_questions_incomplete_still_accepted(monkeypatch, tmp_path):
    """A submission MISSING the three-questions fields is still accepted (the book is published);
    schema growth never rejects a decision. No rotation-call theses land."""
    monkeypatch.setenv("MASTERMIND_FLAGSHIP_JUDGMENT", "1")
    from brain import ledger as _ledger
    monkeypatch.setattr(_ledger, "_LEDGER", tmp_path / "theses.jsonl", raising=False)
    _patch_seats_book(monkeypatch, book={
        "holdings": [{"ticker": "NVDA", "weight": 0.06, "thesis": "AI", "conviction": "high",
                      "sleeve": "conviction"}],
        "cash": 0.1, "summary": "s", "sold_note": "", "ran": True,
        "own_more": [], "own_less": [], "not_holding_should": []},   # all empty
        committee_fn=_confirm)
    out = J.build([], [], regime={}, asof="2026-07-01", gate_info={}, shadow_inputs=[],
                  name_cap=0.08, leadership=[])
    assert {r["ticker"] for r in out} == {"NVDA"}        # decision still accepted
    assert not _ledger.all_theses()                      # no rotation-call theses when incomplete

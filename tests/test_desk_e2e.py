"""END-TO-END integration smoke test for the FLAGSHIP judgment desk.

Unlike ``tests/test_judgment_book.py`` — which mocks the seat FUNCTIONS wholesale
(``strategist.run`` / ``pm_conviction.build_book`` / ``committee.assess`` are replaced with
lambdas, so NO seat logic ever runs) — this test runs the REAL pipeline and fakes ONLY the
LLM transport + the desk submission I/O. That is the seam where the bug class we hit manually
lives: the PM submit-path mismatch (``pm_conviction.build_book`` read ``portfolio_id=
"flagship_judgment"`` while the desk's ``submit_book`` wrote to ``"autonomous"`` → build_book
always read nothing → ran=False → the judgment layer silently no-op'd in production).

WHAT IS REAL (executes its true code):
  * ``brain.judgment_book.build`` — the full orchestrator (strategist → PM → per-name committee
    → gate officer → name_cap clamp → shadow re-emit).
  * ``brain.strategist`` — real ``_strategist_input`` builder + ``strategist_assess`` parser
    (runs on real-shaped dashboard JSON; only ``_load`` returns synthetic data).
  * ``brain.pm_conviction.build_book`` — real ``_pm_input`` + ``_build_prompt`` + the
    clear_submission → cli_bridge.reason → read_submission lifecycle + holdings normalisation.
  * ``brain.flagship_desk_mcp`` — the REAL ``submit_book`` tool + ``submission_path`` /
    ``clear_submission`` / ``read_submission`` path helpers (only redirected to a tmp data dir
    so nothing touches the live book). This is the exact write/read seam the bug was in.
  * ``brain.committee.assess`` — real SENTINEL input builder + NEXUS subtract-only synthesis.
  * ``brain.gate_officer`` — real ``_gate_input`` + ``gate_assess`` + the pure ``apply_gate``
    subtract-only reshaper.

WHAT IS FAKED (the LLM/network boundary ONLY — so the test is offline, fast, and can't hang):
  * ``brain.client.available`` → True, ``brain.client.call_model`` → a fake that inspects the
    SYSTEM PROMPT and returns canned VALID JSON for whichever seat called it.
  * ``brain.cli_bridge.reason`` (async) → instead of spawning a headless Claude session it
    WRITES a canned PM target book through the REAL desk ``submit_book`` tool (simulating
    ``mcp__desk__submit_book`` to the ``flagship_judgment`` path) then returns "submitted".
  * ``flagship_desk_mcp.build_servers`` / ``allowed_tools`` → no-ops (no real MCP server spun).
  * the dashboard ``_load`` helpers (strategist + lenses) → synthetic real-shaped data so the
    REAL input builders + JSON parsers run end-to-end.

If ANY real LLM / MCP call escaped the fakes the test would hang; it completes in well under a
second, which is itself the no-hang proof.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

import bot  # noqa: F401 — bootstraps vendor/macro onto sys.path
import brain as _brain_pkg
import portfolio as _pf_pkg
from brain import client as _client
from brain import judgment_book as J


# ─────────────────────────────────────────────────────────────────────────────
# canned seat replies — keyed off the seat's OWN system prompt so the real
# call_model contract (system, user, *, role, max_tokens) is exercised.
# ─────────────────────────────────────────────────────────────────────────────
import json

_STRAT_JSON = json.dumps({
    "confirmed_themes": [
        {"theme": "AI Infrastructure", "stage": "confirmed", "leadership": 0.8,
         "names": ["NVDA", "AVGO"], "why": "breadth turned + leading sector"}],
    "backdrop_stance": "risk_on", "supportive": True,
    "watch_emerging": ["Power Grid"], "crowding_flags": [],
    "rationale": "Confirmed AI leadership; backdrop supportive."})

# SENTINEL: SUPPORT both names so NEXUS confirms (subtract-only never escalates) → the committee
# keeps the full PM book; the GATE officer then supplies the de-risking action we assert on.
_SENTINEL_JSON = json.dumps({
    "stance": "SUPPORT", "strongest_bear": "rich valuation", "macro_fit": "ok",
    "portfolio_fit": "ok", "crowding": "none", "narrative_maturity": "mid",
    "better_alternative": "none", "conditions": [], "confidence": 0.4})

# GATE OFFICER: TRIM NVDA (proves a trim is honoured) + VETO AME (proves a veto is honoured).
_GATE_JSON = json.dumps({
    "decisions": [
        {"ticker": "NVDA", "action": "trim", "scale": 0.5, "reason": "oversized vs HHI"},
        {"ticker": "AME", "action": "veto", "scale": 0.0, "reason": "sector crowded"}],
    "book_view": "tech-heavy", "rationale": "trim concentration, drop the crowded add"})

_RISK_JSON = json.dumps({"decisions": [], "rationale": "held book stands"})


def _fake_call_model(system, user, *, role="pm", max_tokens=1500):
    """Route a seat's LLM call to its canned JSON by sniffing the system prompt. This is the ONLY
    LLM boundary in the whole pipeline — every seat funnels through brain.client.call_model."""
    s = system or ""
    if "MACRO STRATEGIST" in s:
        return _STRAT_JSON, {}
    if "GATE OFFICER" in s:
        return _GATE_JSON, {}
    if "RISK OFFICER" in s:
        return _RISK_JSON, {}
    if "SENTINEL" in s:
        return _SENTINEL_JSON, {}
    # Any other system prompt means a seat we did not anticipate reached the LLM — fail loud rather
    # than silently returning junk (an unparseable reply would otherwise just degrade to None).
    raise AssertionError(f"unexpected seat reached the LLM boundary: {s[:80]!r}")


# the canned PM target book the faked desk session "submits".
_PM_BOOK = {
    "holdings": [
        {"ticker": "NVDA", "weight": 0.06, "rationale": "AI infra leadership; breadth turned.",
         "conviction": "high"},
        {"ticker": "AME", "weight": 0.05, "rationale": "onshoring bottleneck name.",
         "conviction": "medium"}],
    "summary": "Concentrate in confirmed AI infra + onshoring.",
    "sold_note": "Sold the laggards."}


# ─────────────────────────────────────────────────────────────────────────────
# synthetic dashboard data — real-shaped so the REAL _input builders + parsers run.
# ─────────────────────────────────────────────────────────────────────────────
def _fake_strategist_load(rel):
    if rel == "site/basketdata/flow.json":
        return {"baskets": [
            {"id": "ai_infra", "name": "AI Infrastructure", "stage": "confirmed",
             "breadth": 0.7, "leadership": 0.8, "perf_20d_rel": 0.04}]}
    if rel == "site/basketdata/etf_pulse.json":
        return {"leaders": ["SMH"], "laggards": ["XLU"],
                "sector": {"tech": {"mom_20d": 0.05, "pctile_252d": 90, "above_200d": True}},
                "risk": {"label_en": "risk-on"}, "style": []}
    if rel == "site/basketdata/narrative_emergence.json":
        return {"clusters": []}
    if rel == "site/allocationdata/macro_narrative.json":
        return {"dominant_themes": [{"theme": "disinflation", "count": 5}]}
    if rel == "site/basketdata/vol_sentiment.json":
        return {"vix": 14.0, "vix_pctile": 20, "sentiment_en": "complacent"}
    return None


def _fake_lenses_module():
    """A synthetic portfolio.lenses: real-shaped so the committee's _sentinel_input, the gate's
    _gate_input, and judgment_book's per-row synthesis builder all run on plausible data."""
    m = types.ModuleType("portfolio.lenses")
    m._load = lambda rel: {"sector": "Technology"}
    m.full = lambda t, kind="name": {
        "synthesis": {"confluence": 0.3, "bull": f"{t} bull", "bear": f"{t} bear",
                      "divergences": [], "size_authority": "full"},
        "rows": [{"lens": "trend", "status": "ok", "direction": "up", "note": "above 200dma"}]}
    m._g = lambda d, path, default=None: default
    return m


# ─────────────────────────────────────────────────────────────────────────────
# the wiring fixture — fakes ONLY the LLM transport + the desk submission I/O.
# Leaves every seat's real logic intact.
# ─────────────────────────────────────────────────────────────────────────────
def _wire(monkeypatch, tmp_path, *, pm_book=_PM_BOOK,
          write_portfolio: str | None = None):
    """Mock the LLM boundary + desk I/O. ``write_portfolio`` is the portfolio id the FAKED PM
    session submits its book under — defaults to the desk's REAL ``SUBMIT_PORTFOLIO``
    ("flagship_judgment"). Passing a DIFFERENT id (e.g. "autonomous") reproduces the exact
    submit-path bug so the regression guard can prove build_book reads from the same scope."""
    from brain import flagship_desk_mcp as desk
    from portfolio import registry

    # (1) the judgment flag must be ON for the whole pipeline to engage.
    monkeypatch.setenv("MASTERMIND_FLAGSHIP_JUDGMENT", "1")
    monkeypatch.setenv("MASTERMIND_GATE_OFFICER", "1")
    monkeypatch.setenv("MASTERMIND_COMMITTEE", "1")

    # (2) THE LLM BOUNDARY — the only place a real model call could escape. available() True so the
    # seats engage; call_model returns canned JSON by seat. Dual-patch the brain.client module
    # object + sys.modules (the seats all `from brain import client`, so the module attr is what
    # they resolve at call time; sys.modules is belt-and-suspenders per the P1 lesson).
    monkeypatch.setattr(_client, "available", lambda: True)
    monkeypatch.setattr(_client, "call_model", _fake_call_model)
    monkeypatch.setattr(_brain_pkg, "client", _client, raising=False)
    monkeypatch.setitem(sys.modules, "brain.client", _client)

    # (3) THE DESK I/O — redirect the registry root to tmp so submission_path / clear_submission /
    # read_submission / the REAL submit_book tool all read+write under tmp (never the live book).
    # We deliberately keep those four REAL: they ARE the seam the bug lived in.
    monkeypatch.setattr(registry, "_ROOT", tmp_path, raising=False)
    # the autonomous book's legacy dir also resolves off registry._ROOT only for non-legacy ids;
    # "autonomous" is non-legacy so the wrong-scope write also lands under tmp (no live pollution).

    # build_servers / allowed_tools → no-ops (no real MCP server spun up in-test).
    monkeypatch.setattr(desk, "build_servers", lambda: {}, raising=False)
    monkeypatch.setattr(desk, "allowed_tools", lambda: [], raising=False)

    target_pid = write_portfolio or desk.SUBMIT_PORTFOLIO

    # (4) THE PM SESSION — replace the armed headless Claude run with a fake that WRITES the canned
    # book through the REAL desk submit_book tool (exercising the true write path to target_pid),
    # then returns "submitted". This is exactly what mcp__desk__submit_book does live.
    fake_bridge = types.ModuleType("brain.cli_bridge")

    async def _reason(prompt, *, role="pm", arm=False, append_system=None,
                      mcp_servers=None, allowed_tools=None, max_turns=None, **kw):
        # emulate the desk tool the PM would call mid-session. We override SUBMIT_PORTFOLIO only
        # for the duration of this write so the regression-guard variant can write to the WRONG
        # scope while still using the real tool code.
        prev = desk.SUBMIT_PORTFOLIO
        desk.SUBMIT_PORTFOLIO = target_pid
        try:
            await desk.submit_book.handler({
                "holdings": pm_book["holdings"], "summary": pm_book["summary"],
                "sold_note": pm_book.get("sold_note", "")})
        finally:
            desk.SUBMIT_PORTFOLIO = prev
        return "submitted"

    fake_bridge.reason = _reason
    monkeypatch.setattr(_brain_pkg, "cli_bridge", fake_bridge, raising=False)
    monkeypatch.setitem(sys.modules, "brain.cli_bridge", fake_bridge)

    # (5) DASHBOARD DATA — synthetic but real-shaped so the REAL input builders + parsers run.
    from brain import strategist as S
    monkeypatch.setattr(S, "_load", _fake_strategist_load)
    fake_lenses = _fake_lenses_module()
    monkeypatch.setattr(_pf_pkg, "lenses", fake_lenses, raising=False)
    monkeypatch.setitem(sys.modules, "portfolio.lenses", fake_lenses)

    # belt-and-suspenders: any stray watchlist append (gate veto path) goes to a throwaway so it
    # never touches the live watchlist file. Capture parks for the assertions.
    parked: list[tuple] = []
    wl = types.ModuleType("portfolio.watchlist")
    wl.append = lambda t, asof, reason=None, tech=None, combined=None: parked.append((t, reason))
    monkeypatch.setattr(_pf_pkg, "watchlist", wl, raising=False)
    monkeypatch.setitem(sys.modules, "portfolio.watchlist", wl)
    return parked


# the schema conviction.build emits — the downstream loop depends on byte-for-byte parity.
_CONV_SCHEMA = {"ticker", "weight", "confluence", "bull", "bear", "divergences",
                "retained", "size_stage", "research"}

_REGIME = {"quad": 1, "quad_name": "Goldilocks", "liquidity_overlay": "easing",
           "cycle_tag": "mid"}


# ─────────────────────────────────────────────────────────────────────────────
# (1) FULL PIPELINE — every desk flag ON, real seats, faked LLM + desk I/O.
# ─────────────────────────────────────────────────────────────────────────────
def test_full_pipeline_strategist_pm_committee_gate_wired(monkeypatch, tmp_path):
    """With all flags ON, build() returns a non-empty reshaped book — proving strategist → PM →
    per-name committee → gate officer are wired end-to-end and the PM submit/read seam works.
    The canned GATE decisions TRIM NVDA and VETO AME, so we assert the trim AND veto are honoured."""
    shadow: list = []
    parked = _wire(monkeypatch, tmp_path)
    out = J.build([], [], regime=_REGIME, asof="2026-06-22", gate_info={},
                  shadow_inputs=shadow, name_cap=0.08)

    # the PM book came back non-empty and survived the committee → reshaped to conviction rows.
    assert out, "judgment book is empty — strategist→PM→committee→gate did not wire end-to-end"
    tickers = {r["ticker"] for r in out}

    # GATE VETO honoured: AME dropped from the book and parked to the watchlist with a gate reason.
    assert "AME" not in tickers, "gate VETO not honoured — AME should have been dropped"
    assert any(t == "AME" and str(r or "").startswith("gate_officer:") for t, r in parked), \
        "vetoed name was not parked to the watchlist with a gate_officer reason"

    # GATE TRIM honoured: NVDA survives at half weight (0.06 * 0.5) and carries the gate tag.
    assert "NVDA" in tickers
    nvda = next(r for r in out if r["ticker"] == "NVDA")
    assert nvda["weight"] == pytest.approx(0.03), "gate TRIM not honoured — NVDA weight wrong"
    assert nvda.get("gate", {}).get("action") == "trim"

    # schema parity with conviction.build rows (the downstream loop is unchanged).
    for r in out:
        assert _CONV_SCHEMA <= set(r), f"row missing conviction.build schema fields: {r}"
        assert r["judgment"]["source"] == "pm"        # tagged as a PM-judgment row
        assert "committee" in r                        # the real committee ran on each name

    # shadow inputs re-emitted for each surviving PM name (forward grading stays intact).
    assert {e["ticker"] for e in shadow} == tickers
    assert all(e["source"] == "pm_judgment" for e in shadow)


def test_pm_book_actually_comes_back_nonempty(monkeypatch, tmp_path):
    """The precise seam the real bug was in: drive ONLY pm_conviction.build_book (the same code
    judgment_book invokes) and assert the PM book is read back non-empty with ran=True. If the
    desk wrote to a different scope than build_book reads, this would be ran=False / empty."""
    _wire(monkeypatch, tmp_path)
    from brain import pm_conviction
    book = pm_conviction.build_book([], [], regime=_REGIME, asof="2026-06-22",
                                    strategist=None, gate_info={})
    assert book is not None
    assert book["ran"] is True, "PM book did not come back ran=True — submit/read seam broken"
    assert {h["ticker"] for h in book["holdings"]} == {"NVDA", "AME"}
    assert all(h.get("thesis") for h in book["holdings"])   # rationale carried through as thesis


# ─────────────────────────────────────────────────────────────────────────────
# (2) FLAG-OFF — build() returns the input `sized` byte-identical (no seat touched).
# ─────────────────────────────────────────────────────────────────────────────
def test_flag_off_returns_sized_byte_identical(monkeypatch, tmp_path):
    """MASTERMIND_FLAGSHIP_JUDGMENT unset → build() short-circuits and returns the EXACT input
    `sized` object. No seat is wired (we don't even fake the LLM), so any accidental seat call
    would error — the same-object assertion proves the flag-off path is byte-identical."""
    monkeypatch.delenv("MASTERMIND_FLAGSHIP_JUDGMENT", raising=False)
    sized = [{"ticker": "ENGINE", "weight": 0.05, "confluence": 0.3, "bull": "", "bear": "",
              "divergences": [], "retained": False, "size_stage": None, "research": {}}]
    out = J.build(sized, [], regime=_REGIME, asof="2026-06-22", gate_info={}, shadow_inputs=[])
    assert out is sized                                  # same object — byte-identical guard


# ─────────────────────────────────────────────────────────────────────────────
# (3) REGRESSION GUARD — the PM submit-path scope must match the read scope.
# ─────────────────────────────────────────────────────────────────────────────
def test_submit_path_scope_regression_guard(monkeypatch, tmp_path):
    """REGRESSION GUARD for the exact manual bug: the desk wrote the submission to a DIFFERENT
    portfolio scope ("autonomous") than build_book reads back ("flagship_judgment"), so the read
    found nothing → ran=False → the judgment layer silently no-op'd.

    Here the faked PM session writes the book under the WRONG scope ("autonomous") via the REAL
    desk tool. build_book reads from pm_conviction.PORTFOLIO_ID ("flagship_judgment"). If the two
    scopes ever diverge again, the read returns nothing → ran=False → this assertion fails.
    Conversely the happy path (test above) proves they MATCH today."""
    from brain import pm_conviction

    # sanity: the contract the guard rests on — read scope vs write scope are the SAME id today.
    from brain import flagship_desk_mcp as desk
    assert pm_conviction.PORTFOLIO_ID == desk.SUBMIT_PORTFOLIO == "flagship_judgment"

    # write to the WRONG scope (reproduce the bug) → build_book must read nothing back.
    _wire(monkeypatch, tmp_path, write_portfolio="autonomous")
    book = pm_conviction.build_book([], [], regime=_REGIME, asof="2026-06-22",
                                    strategist=None, gate_info={})
    assert book["ran"] is False, \
        "submit-path scope mismatch was NOT caught — build_book read a book it should not have"
    assert book["holdings"] == []


def test_no_real_llm_or_mcp_escapes_and_is_fast(monkeypatch, tmp_path):
    """No real LLM/MCP call may escape the fakes (it would hang). We assert the full build runs
    to completion under a generous time budget — the no-hang proof in-band. The _fake_call_model
    AssertionError on an unexpected system prompt is the second line of defence."""
    import time
    _wire(monkeypatch, tmp_path)
    t0 = time.monotonic()
    out = J.build([], [], regime=_REGIME, asof="2026-06-22", gate_info={},
                  shadow_inputs=[], name_cap=0.08)
    elapsed = time.monotonic() - t0
    assert out                                            # it produced a book...
    assert elapsed < 10.0, f"build took {elapsed:.1f}s — a real LLM/MCP call likely escaped"

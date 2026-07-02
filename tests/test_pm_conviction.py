"""Guards for the PM-CONVICTION seat (brain/pm_conviction).

Offline only: we monkeypatch `client.available`, the armed `cli_bridge.reason` (so NO real LLM
runs), and `autonomous_mcp.read_submission` to return a canned book. We prove:
  * the build_book return schema is correct + thesis-less / zero-weight holdings are dropped,
  * no-leverage scale-down kicks in when gross > 1.0,
  * a PM-add (a name absent from the engine candidates) is accepted,
  * a thesis-less engine name is honoured as a DROP (omission == sold),
  * an honest ran=False stub when no LLM is reachable / the PM did not submit,
  * enforce_no_leverage clamps to name_cap and scales gross <= 1.0.
"""
from __future__ import annotations

import pytest

import bot  # noqa: F401
from brain import pm_conviction as P


class _FakeAuto:
    """Stand-in for brain.autonomous_mcp with a settable canned submission."""
    submission = None

    @staticmethod
    def build_servers():
        return {}

    @staticmethod
    def allowed_tools():
        return []

    @staticmethod
    def clear_submission(pid):
        _FakeAuto.submission = None

    @staticmethod
    def read_submission(pid):
        return _FakeAuto.submission


def _arm(monkeypatch, submission):
    """Wire the canned book in + stub the armed reason() so no real LLM runs."""
    import sys
    import types
    monkeypatch.setattr(P.client, "available", lambda: True)

    fake_auto = _FakeAuto()
    _FakeAuto.submission = None          # cleared first — like the real submit lifecycle

    fake_bridge = types.ModuleType("brain.cli_bridge")

    async def _reason(*a, **k):
        # simulate the PM calling mcp__desk__submit_book DURING the armed session — i.e. AFTER
        # build_book has called clear_submission() — so read_submission() then returns the book.
        _FakeAuto.submission = submission
        return "submitted"
    fake_bridge.reason = _reason

    # build_book lazily does `from brain import flagship_desk_mcp as autonomous_mcp, cli_bridge`,
    # which binds the ATTRIBUTES on the brain package (not sys.modules) when the real submodules
    # were already imported elsewhere — so patch the package attributes (and sys.modules,
    # belt-and-suspenders) so the fakes actually take effect and NO real LLM/MCP call ever runs.
    import brain as _brain_pkg
    monkeypatch.setattr(_brain_pkg, "flagship_desk_mcp", fake_auto, raising=False)
    monkeypatch.setattr(_brain_pkg, "cli_bridge", fake_bridge, raising=False)
    monkeypatch.setitem(sys.modules, "brain.flagship_desk_mcp", fake_auto)
    monkeypatch.setitem(sys.modules, "brain.cli_bridge", fake_bridge)


def test_stub_when_no_llm(monkeypatch):
    monkeypatch.setattr(P.client, "available", lambda: False)
    out = P.build_book([], [], regime={}, asof="2026-06-22", strategist=None, gate_info={})
    assert out["ran"] is False and out["holdings"] == []


def test_build_book_schema_and_filters(monkeypatch):
    sub = {"holdings": [
        {"ticker": "nvda", "weight": 0.07, "rationale": "AI infra leadership, why-now: breadth turned."},
        {"ticker": "AME", "weight": 0.05, "conviction": "high", "rationale": "onshoring bottleneck name."},
        {"ticker": "JUNK", "weight": 0.03, "rationale": ""},          # no thesis -> dropped
        {"ticker": "ZERO", "weight": 0.0, "rationale": "has thesis but zero weight"},  # dropped
    ], "summary": "Concentrate in confirmed AI infra.", "sold_note": "Sold the laggards."}
    _arm(monkeypatch, sub)
    out = P.build_book([{"ticker": "NVDA", "weight": 0.06, "confluence": 0.3}], [],
                       regime={"quad": 1}, asof="2026-06-22", strategist={"confirmed_themes": []},
                       gate_info={})
    assert out["ran"] is True
    tickers = {h["ticker"] for h in out["holdings"]}
    assert tickers == {"NVDA", "AME"}                 # JUNK + ZERO filtered, nvda upper-cased
    assert all({"ticker", "weight", "thesis", "conviction"} <= set(h) for h in out["holdings"])
    # AME is a genuine PM-add (not in the engine candidate list) and was accepted
    assert "AME" in tickers


def test_no_leverage_scale_down(monkeypatch):
    sub = {"holdings": [
        {"ticker": "A", "weight": 0.8, "rationale": "name a thesis"},
        {"ticker": "B", "weight": 0.8, "rationale": "name b thesis"}],  # gross 1.6 -> must scale
        "summary": "", "sold_note": ""}
    _arm(monkeypatch, sub)
    out = P.build_book([], [], regime={}, asof="2026-06-22", strategist=None, gate_info={})
    gross = sum(h["weight"] for h in out["holdings"])
    assert gross <= 1.0 + 1e-6
    assert out["cash"] >= 0.0


def test_enforce_no_leverage_caps_and_scales():
    holdings = [{"ticker": "A", "weight": 0.5}, {"ticker": "B", "weight": 0.5},
                {"ticker": "C", "weight": 0.5}]                 # each clamps to 0.08 -> gross 0.24
    out = P.enforce_no_leverage(holdings, name_cap=0.08)
    assert all(h["weight"] <= 0.08 + 1e-9 for h in out)
    # a book that still exceeds gross 1.0 after the cap is scaled down
    big = [{"ticker": str(i), "weight": 0.2} for i in range(10)]   # 10 * 0.2 cap=0.2 -> gross 2.0
    out2 = P.enforce_no_leverage(big, name_cap=0.2)
    assert sum(h["weight"] for h in out2) <= 1.0 + 1e-6


# ───────────────────── W4 B1: leadership pipe + defensive + prompt payload ─────────────────────
def test_pm_input_pipes_leadership_and_defensive_and_deanchors():
    """The PM payload carries leadership_legs (sleeve-tagged), defensive_candidates, the
    defensive_benchmark basket, and DE-ANCHORS the engine weights to a trailing ADVISORY block
    (candidate rows carry NO weight)."""
    payload = P._pm_input(
        [{"ticker": "NVDA", "weight": 0.06, "confluence": 0.3, "sleeve": "conviction"}],
        [], {"confirmed_themes": []}, {"quad": 1}, {}, "2026-07-01",
        leadership=[{"ticker": "SMH", "weight": 0.12, "rs_pctile": 0.9},
                    {"ticker": "XLK", "weight": 0.12}],
        defensive=[{"ticker": "XLV", "archetype": "quality_defensive", "source": "playbook",
                    "note": "driver-conditional favor"}])
    lead = {r["ticker"] for r in payload["leadership_legs"]}
    assert lead == {"SMH", "XLK"}
    assert all(r["sleeve"] == "leadership" for r in payload["leadership_legs"])
    # candidate rows are DE-ANCHORED — no weight on them
    assert all("weight" not in r for r in payload["engine_candidates"])
    # the engine weights live in the trailing advisory block only
    adv = payload["engine_proposed_weights_ADVISORY"]
    assert adv.get("NVDA") == 0.06 and adv.get("SMH") == 0.12
    assert payload["defensive_benchmark"] == ["XLU", "XLV", "XLF", "XLP"]
    assert {d["ticker"] for d in payload["defensive_candidates"]} == {"XLV"}
    # the advisory block is the LAST key in the payload (placed after every other section)
    assert list(payload.keys())[-1] == "engine_proposed_weights_ADVISORY"


def test_build_book_tags_kept_leadership_and_captures_three_questions(monkeypatch):
    """A kept leadership ticker is tagged sleeve='leadership' on the way out; the three-questions
    fields (own_more / own_less / not_holding_should) are normalised and returned."""
    sub = {"holdings": [
        {"ticker": "SMH", "weight": 0.12, "rationale": "keep the semis leader"},
        {"ticker": "NVDA", "weight": 0.06, "rationale": "AI infra single name"}],
        "summary": "positioned", "sold_note": "",
        "own_more": [{"ticker": "nvda", "why_now": "breadth", "probability": 0.7,
                      "check_by": "2026-08-01"}],
        "own_less": [{"ticker": "SMH", "why_now": "extended"}],
        "not_holding_should": [{"ticker": "xlv", "why_now": "defensive rotate", "probability": 1.4,
                                "check_by": "2026-08-01"}]}
    _arm(monkeypatch, sub)
    out = P.build_book([{"ticker": "NVDA", "weight": 0.06, "confluence": 0.3}], [],
                       regime={"quad": 1}, asof="2026-07-01", strategist={"confirmed_themes": []},
                       gate_info={}, leadership=[{"ticker": "SMH", "weight": 0.12}])
    by = {h["ticker"]: h for h in out["holdings"]}
    assert by["SMH"]["sleeve"] == "leadership"            # kept leadership leg tagged
    assert by["NVDA"]["sleeve"] == "conviction"
    # three-questions normalised: probability clamped into [0,1], ticker upper-cased
    assert out["not_holding_should"][0]["ticker"] == "XLV"
    assert out["not_holding_should"][0]["probability"] == 1.0     # 1.4 clamped
    assert out["own_more"][0]["ticker"] == "NVDA"
    assert {r["ticker"] for r in out["own_less"]} == {"SMH"}

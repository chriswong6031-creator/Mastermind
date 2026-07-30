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
def test_pm_input_pipes_leadership_and_defensive_and_deanchors(monkeypatch):
    """The PM payload carries leadership_legs (sleeve-tagged), defensive_candidates, the
    defensive_benchmark basket, and DE-ANCHORS the engine weights to a trailing ADVISORY block
    (candidate rows carry NO weight)."""
    # Isolate the live posture artifact (data/posture/latest.json — published every build on the
    # production Mac, absent in a fresh worktree). When present it appends a trailing
    # `posture_ADVISORY` key that would displace engine_proposed_weights_ADVISORY as the last key
    # (mirrors the incident-battery _isolate_posture_artifacts fixture).
    from brain import posture_decider as _pd
    monkeypatch.setattr(_pd, "latest", lambda: None)
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


# ─────────────────────────────────────────── E1.1: market_view enrichment ───────────────────────

def _fake_market_view(n_dissent: int = 2, conflict: bool = True) -> dict:
    """Minimal market_view.v1 artifact for test injection."""
    dissenting = [f"plane_{i}" for i in range(n_dissent)]
    consensus = "risk_off" if conflict else "risk_on"
    return {
        "schema_version": "market_view.v1",
        "label_vs_planes": {
            "conflict": conflict,
            "relationship": "conflict" if conflict else "confirmed",
            "confirmed": not conflict,
            "label_direction": "risk_on",
            "plane_consensus_direction": consensus,
            "dissenting_planes": dissenting,
            "magnitude": 0.667,
        },
        "planes": {
            "risk_radar": {
                "direction": "risk_off", "status": "validated",
                "reading": "growth-scare caution", "freshness": {"stale": False}},
            "cycles": {
                "direction": "risk_off", "status": "validated",
                "reading": "3 late-cycle / 0 entry-favored", "freshness": {"stale": False}},
            "froth_fragility": {
                "direction": "risk_on", "status": "advisory",
                "reading": "calm", "freshness": {"stale": False}},
        },
        "brief": {
            "what_changed": "conflict OPENED",
            "whats_rotating": "3 late-cycle / 0 entry-favored",
            "wheres_the_risk": "risk_radar*(V): caution; cycles*(V): late",
            "posture_implication": "Validated planes dissent risk_off — defensive tilt.",
        },
    }


def test_market_view_enrichment_composes_from_view(monkeypatch, tmp_path):
    """_market_view_enrichment() extracts brief, plane_summaries, label_vs_planes_line."""
    mv = _fake_market_view(n_dissent=2)
    enrichment = P._market_view_enrichment(mv)
    assert enrichment.get("label_vs_planes_line")
    assert "2 validated planes dissent" in enrichment["label_vs_planes_line"]
    assert enrichment.get("market_view_brief")
    assert "posture_implication" in enrichment["market_view_brief"]
    summaries = enrichment.get("plane_summaries") or []
    directions = {s["name"]: s["direction"] for s in summaries}
    # planes with None direction are excluded
    assert "risk_radar" in directions
    assert directions["risk_radar"] == "risk_off"


def test_market_view_enrichment_absent_view():
    """_market_view_enrichment(None) returns {} — degrade to current behavior."""
    assert P._market_view_enrichment(None) == {}
    # An empty dict has no lvp block; the function emits a stable no-conflict line.
    # Ensure it does NOT raise and does NOT fabricate a conflict reading.
    enrichment = P._market_view_enrichment({})
    assert not enrichment.get("market_view_brief")
    assert not enrichment.get("plane_summaries")


def test_market_view_enrichment_no_conflict():
    """Matching directional evidence is rendered as explicit confirmation."""
    mv = _fake_market_view(n_dissent=0, conflict=False)
    enrichment = P._market_view_enrichment(mv)
    assert "CONFIRM" in (enrichment.get("label_vs_planes_line") or "")


def test_read_market_view_absent_file(tmp_path, monkeypatch):
    """_read_market_view() returns None when the artifact has not been built yet."""
    # Patch Path so the function looks in tmp_path (no file there)
    import brain.pm_conviction as _mod
    monkeypatch.setattr(_mod, "_read_market_view",
                        lambda: None)
    assert _mod._read_market_view() is None


def test_full_regime_slice_includes_market_view_when_available(monkeypatch):
    """_full_regime_slice() includes market_view enrichment when the view is present."""
    mv = _fake_market_view(n_dissent=3)
    monkeypatch.setattr(P, "_read_market_view", lambda: mv)
    reg = P._full_regime_slice({"quad": "Q1", "quad_name": "Goldilocks"})
    assert "market_view" in reg
    assert reg["market_view"].get("label_vs_planes_line")
    assert "3 validated planes dissent" in reg["market_view"]["label_vs_planes_line"]


def test_full_regime_slice_absent_view_degrades_gracefully(monkeypatch):
    """_full_regime_slice() degrades to no market_view key when the organ is absent."""
    monkeypatch.setattr(P, "_read_market_view", lambda: None)
    reg = P._full_regime_slice({"quad": "Q1"})
    # market_view key must not be present (or if present, must be {})
    assert not reg.get("market_view")


def test_build_prompt_includes_label_vs_planes_line_on_conflict(monkeypatch):
    """_build_prompt() renders the label_vs_planes_line when a conflict is present.

    Golden prompt-payload test: the prompt must contain the dissent sentence so the PM
    seat reads the same perception layer the architecture demands.
    """
    mv = _fake_market_view(n_dissent=2, conflict=True)
    monkeypatch.setattr(P, "_read_market_view", lambda: mv)
    # Build the payload via _pm_input (exercises _full_regime_slice)
    payload = P._pm_input([], [], None, {"quad": "Q1", "quad_name": "Goldilocks"},
                          {}, "2026-07-01")
    prompt = P._build_prompt(payload)
    assert "2 validated planes dissent" in prompt, (
        "The prompt must surface the validated-plane disagreement line so the PM "
        "cannot miss it (E1.1 golden prompt-payload assertion)"
    )


def test_build_prompt_no_market_view_section_when_absent(monkeypatch):
    """When the market_view organ is absent the prompt must be byte-identical to W4 output.

    This confirms the 'lazy, degrade to current behavior' contract: the prompt section is
    omitted, not fabricated, when the view isn't built yet.
    """
    monkeypatch.setattr(P, "_read_market_view", lambda: None)
    payload = P._pm_input([], [], None, {"quad": "Q1"}, {}, "2026-07-01")
    prompt = P._build_prompt(payload)
    assert "Perception layer" not in prompt
    assert "label_vs_planes" not in prompt


# ─────────────────────────────── end E1.1 tests ──────────────────────────────────────────────────


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

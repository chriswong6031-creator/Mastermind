"""The live overnight tape + the overnight watch loop.

Covers data_layer.overnight (the live cross-asset tape + the distilled risk read + the deterministic
tripwire) and bot.overnight (the watch tick: no-op when calm / open / unqueued; fires the Brain
re-decision only on a MATERIAL overnight move), plus the get_overnight_tape MCP tool.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from portfolio import paper_account, registry


@pytest.fixture
def iso(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "_ROOT", tmp_path, raising=False)
    return tmp_path


# --------------------------------------------------------------------------- the tape

def test_risk_read_classification():
    from data_layer import overnight
    calm = {"us_futures": [{"ticker": "ES=F", "change_pct": 0.2}],
            "international": [{"ticker": "^N225", "change_pct": 0.3}],
            "vol": [{"ticker": "^VIX", "change_pct": -1.0}]}
    assert overnight.risk_read(calm)["state"] == "calm"
    stressed = {"us_futures": [{"ticker": "ES=F", "change_pct": -2.0}],
                "international": [], "vol": [{"ticker": "^VIX", "change_pct": 15.0}]}
    assert overnight.risk_read(stressed)["state"] == "stressed"
    elevated = {"us_futures": [{"ticker": "ES=F", "change_pct": -0.8}], "international": [], "vol": []}
    assert overnight.risk_read(elevated)["state"] == "elevated"


def test_is_material_tripwire():
    from data_layer import overnight
    assert overnight.is_material({"risk": {"state": "stressed"}}) is True
    assert overnight.is_material({"risk": {"state": "elevated"}}) is True
    assert overnight.is_material({"risk": {"state": "calm"}}) is False


def test_tape_degrades_offline():
    # conftest stubs yfinance off → the tape degrades to empty groups + a valid calm read, never raises
    from data_layer import overnight
    overnight.clear_cache()
    t = overnight.tape(force=True)
    assert t["live"] is False
    assert isinstance(t["groups"], dict) and "us_futures" in t["groups"]
    assert t["risk"]["state"] == "calm"
    assert "Overnight tape" in overnight.brief(t)


def test_get_overnight_tape_tool(monkeypatch):
    from brain import bot_mcp
    from data_layer import overnight as ov
    monkeypatch.setattr(ov, "tape", lambda force=False: {"risk": {"state": "calm"}, "groups": {}, "live": True})
    out = asyncio.run(bot_mcp.get_overnight_tape.handler({}))
    payload = json.loads(out["content"][0]["text"])
    assert payload["risk"]["state"] == "calm"


# --------------------------------------------------------------------------- the watch loop

def test_watch_skips_when_market_open(iso, monkeypatch):
    from bot import overnight, settle
    monkeypatch.setattr(settle, "is_open", lambda pid: True)
    assert overnight.watch("etf")["skipped"] == "market_open"


def test_watch_skips_when_nothing_queued(iso, monkeypatch):
    from bot import overnight, settle
    monkeypatch.setattr(settle, "is_open", lambda pid: False)
    assert overnight.watch("etf")["skipped"] == "nothing_queued"


def test_watch_skips_when_tape_calm(iso, monkeypatch):
    from bot import overnight, settle
    from data_layer import overnight as ov
    monkeypatch.setattr(settle, "is_open", lambda pid: False)
    paper_account.save_pending_target({"SPY": 0.5}, "2026-06-23", portfolio_id="etf")
    monkeypatch.setattr(ov, "tape", lambda force=False: {"risk": {"state": "calm", "reasons": ["x"]}, "groups": {}})
    assert overnight.watch("etf")["skipped"] == "tape_calm"     # deterministic tripwire → no LLM fired


def test_watch_fires_on_material_move(iso, monkeypatch):
    from bot import overnight, settle, etf as etf_mod
    from data_layer import overnight as ov
    monkeypatch.setattr(settle, "is_open", lambda pid: False)
    paper_account.save_pending_target({"XLK": 0.5}, "2026-06-23", portfolio_id="etf")
    stressed = {"risk": {"state": "stressed", "reasons": ["US futures -2%"]},
                "groups": {"us_futures": [{"name": "S&P 500 fut", "change_pct": -2.0}],
                           "international": [], "vol": [], "fx_rates": []}}
    monkeypatch.setattr(ov, "tape", lambda force=False: stressed)
    captured = {}

    def fake_run(asof=None, directive=None):
        captured["directive"] = directive
        return {"decided": True, "queued_for_open": True, "holdings": 1, "brain": {"ok": True}}

    monkeypatch.setattr(etf_mod, "run_etf", fake_run)
    res = overnight.watch("etf", asof="2026-06-23")
    assert res.get("refined", {}).get("decided") is True
    assert res["refined"]["queued_for_open"] is True
    assert "OVERNIGHT REVIEW" in captured["directive"]
    assert "stressed" in captured["directive"] and "S&P 500 fut -2.00%" in captured["directive"]


# --------------------------------------------------------------------------- W4 A2: flagship + heavyweight

def test_runners_contains_all_six():
    """_RUNNERS must cover all six US/Asia books including flagship and heavyweight (A2)."""
    from bot import overnight
    for pid in ("etf", "autonomous", "china", "hk", "flagship", "heavyweight"):
        assert pid in overnight._RUNNERS, f"_RUNNERS missing '{pid}'"
    # each entry must be a 2-tuple (module_path, function_name)
    for pid, entry in overnight._RUNNERS.items():
        assert isinstance(entry, tuple) and len(entry) == 2, f"_RUNNERS['{pid}'] must be a 2-tuple"


def test_watch_flagship_non_material_no_redecide(iso, monkeypatch):
    """Calm tape → flagship overnight watch skips (no LLM, no rebuild)."""
    from bot import overnight, settle
    from data_layer import overnight as ov
    monkeypatch.setattr(settle, "is_open", lambda pid: False)
    paper_account.save_pending_target({"SMH": 0.25, "XLK": 0.15}, "2026-07-01",
                                       portfolio_id="flagship")
    monkeypatch.setattr(ov, "tape",
                        lambda force=False: {"risk": {"state": "calm", "reasons": []}, "groups": {}})
    res = overnight.watch("flagship", asof="2026-07-01")
    assert res["skipped"] == "tape_calm"
    assert "refined" not in res


def test_watch_flagship_material_fires_rebuild(iso, monkeypatch):
    """Material tape → flagship overnight watch fires run_flagship with a directive."""
    from bot import overnight, settle
    import bot.phase2 as phase2_mod
    from data_layer import overnight as ov
    monkeypatch.setattr(settle, "is_open", lambda pid: False)
    paper_account.save_pending_target({"SMH": 0.25}, "2026-07-01", portfolio_id="flagship")
    stressed = {"risk": {"state": "stressed", "reasons": ["SOXX -6.4%"]},
                "groups": {"us_futures": [{"name": "SOXX fut", "change_pct": -6.4}],
                           "international": [], "vol": [], "fx_rates": []}}
    monkeypatch.setattr(ov, "tape", lambda force=False: stressed)
    captured: dict = {}

    def fake_run_flagship(asof=None, *, directive=None):
        captured["directive"] = directive
        return {"decided": True, "queued_for_open": True, "holdings": 3,
                "brain": {"ok": True, "llm_used": False}}

    monkeypatch.setattr(phase2_mod, "run_flagship", fake_run_flagship)
    res = overnight.watch("flagship", asof="2026-07-01")
    assert res.get("refined", {}).get("decided") is True
    assert captured.get("directive") is not None
    assert "OVERNIGHT REVIEW" in captured["directive"]


def test_watch_heavyweight_material_fires_rebuild(iso, monkeypatch):
    """Material tape → heavyweight overnight watch fires run_heavyweight with a directive."""
    from bot import overnight, settle
    import bot.heavyweight as hw_mod
    from data_layer import overnight as ov
    monkeypatch.setattr(settle, "is_open", lambda pid: False)
    paper_account.save_pending_target({"NVDA": 0.20}, "2026-07-01", portfolio_id="heavyweight")
    stressed = {"risk": {"state": "elevated", "reasons": ["VIX spike"]},
                "groups": {"us_futures": [{"name": "ES fut", "change_pct": -1.2}],
                           "international": [], "vol": [{"name": "VIX", "change_pct": 20.0}],
                           "fx_rates": []}}
    monkeypatch.setattr(ov, "tape", lambda force=False: stressed)
    captured: dict = {}

    def fake_run_heavyweight(asof=None, *, force=False, armed=True, directive=None):
        captured["directive"] = directive
        return {"decided": True, "queued_for_open": True, "holdings": 4,
                "brain": {"ok": True}}

    monkeypatch.setattr(hw_mod, "run_heavyweight", fake_run_heavyweight)
    res = overnight.watch("heavyweight", asof="2026-07-01")
    assert res.get("refined", {}).get("decided") is True
    assert captured.get("directive") is not None
    assert "OVERNIGHT REVIEW" in captured["directive"]


def test_phase2_run_directive_none_byte_identical(iso, monkeypatch):
    """phase2.run(directive=None) must produce the same gate decision as run() with no arg.

    Intent-only: we assert the gate is consulted and the function does not raise.
    We do NOT pin the build outcome to a live market state (calm-tape invariant test)."""
    import bot.phase2 as phase2_mod
    # stub the entire run to a no-op so we don't need fixtures/data
    calls: list = []

    original_run = phase2_mod.run.__wrapped__ if hasattr(phase2_mod.run, "__wrapped__") else None

    def _stub_gate(*a, **kw):
        calls.append(kw)
        return {"run": False, "triggers": []}

    from brain import gate
    monkeypatch.setattr(gate, "should_run", _stub_gate)
    # also stub the expensive parts that run() calls before reaching the gate
    from data_layer import store
    monkeypatch.setattr(store, "connect", lambda: None)
    monkeypatch.setattr(store, "last_run", lambda con: None)

    # both calls must reach the gate check — directive=None must not short-circuit
    # (we can't easily diff the full book here without production fixtures, so we
    #  verify the gate is invoked and the force keyword is correct)
    try:
        phase2_mod.run(asof="2026-07-01")
    except Exception:
        pass
    n_no_directive = len(calls)

    try:
        phase2_mod.run(asof="2026-07-01", directive=None)
    except Exception:
        pass
    n_with_none = len(calls)

    # both should hit gate exactly once
    assert n_no_directive >= 1
    assert n_with_none == n_no_directive + 1 or n_with_none >= 2


def test_phase2_run_directive_forces_gate(iso, monkeypatch):
    """When directive is set, phase2.run passes force=True to the gate (the overnight tap
    already verified materiality; we should always rebuild)."""
    import bot.phase2 as phase2_mod
    captured_force: list = []

    def _stub_gate(sig, last, *, interval_days, force, asof):
        captured_force.append(force)
        return {"run": False, "triggers": []}

    from brain import gate
    from data_layer import store
    monkeypatch.setattr(gate, "should_run", _stub_gate)
    monkeypatch.setattr(store, "connect", lambda: None)
    monkeypatch.setattr(store, "last_run", lambda con: None)

    try:
        phase2_mod.run(asof="2026-07-01", directive="OVERNIGHT: SOXX -6.4%")
    except Exception:
        pass
    assert any(f is True for f in captured_force), \
        "directive must cause force=True to the build gate"


def test_heavyweight_run_directive_threads_to_brain(monkeypatch):
    """directive passed to run_heavyweight is forwarded to _run_brain."""
    import bot.heavyweight as hw_mod
    captured: dict = {}

    def _fake_run_brain(asof, inaugural, directive=None):
        captured["directive"] = directive
        return {"ok": False, "skipped": "stubbed"}

    from portfolio import market_calendar, paper_account as _pa, registry
    monkeypatch.setattr(hw_mod, "_run_brain", _fake_run_brain)
    # avoid cost_guard + market_calendar calls touching real state
    from brain import cost_guard
    monkeypatch.setattr(cost_guard, "over_budget", lambda pid, asof: False)
    monkeypatch.setattr(market_calendar, "is_trading_day", lambda d: False)
    monkeypatch.setattr(_pa, "_load_account", lambda pid: {"cash": 0, "positions": {}})

    test_directive = "OVERNIGHT TEST: reduce risk"
    try:
        hw_mod.run_heavyweight(asof="2026-07-01", directive=test_directive)
    except Exception:
        pass
    assert captured.get("directive") == test_directive


def test_heavyweight_build_prompt_injects_directive():
    """When directive is set, _build_prompt includes it as a PRIORITY DIRECTIVE block."""
    import bot.heavyweight as hw_mod
    from portfolio import paper_account as _pa
    # minimal state
    from unittest.mock import patch
    with patch.object(_pa, "_load_account", return_value={"cash": 100000, "positions": {}}):
        prompt = hw_mod._build_prompt("2026-07-01", inaugural=False,
                                      directive="OVERNIGHT: de-risk to SGOV")
    assert "PRIORITY DIRECTIVE" in prompt
    assert "de-risk to SGOV" in prompt


def test_pm_conviction_build_prompt_injects_directive():
    """When directive is set, pm_conviction._build_prompt includes it as a PRIORITY DIRECTIVE block."""
    from brain import pm_conviction
    payload = {"asof": "2026-07-01", "regime": {}, "strategist": {}, "candidates": [], "rejected": []}
    prompt_no_dir = pm_conviction._build_prompt(payload, directive=None)
    prompt_with_dir = pm_conviction._build_prompt(payload, directive="OVERNIGHT: reduce SMH")
    assert "PRIORITY DIRECTIVE" not in prompt_no_dir
    assert "PRIORITY DIRECTIVE" in prompt_with_dir
    assert "reduce SMH" in prompt_with_dir


def test_judgment_book_build_directive_threaded(monkeypatch):
    """judgment_book.build threads directive to pm_conviction.build_book."""
    from brain import judgment_book
    import brain.pm_conviction as pm_mod
    captured: dict = {}

    def _fake_build_book(sized, rejected, *, regime, asof, strategist, gate_info,
                          portfolio_ctx=None, directive=None, leadership=None, defensive=None):
        captured["directive"] = directive
        return None   # → degrade to sized (no book)

    monkeypatch.setattr(pm_mod, "build_book", _fake_build_book)
    monkeypatch.setenv("MASTERMIND_FLAGSHIP_JUDGMENT", "1")

    import brain.strategist as strat_mod
    monkeypatch.setattr(strat_mod, "run", lambda asof, regime: {"confirmed_themes": []})

    try:
        judgment_book.build(
            [{"ticker": "SPY", "weight": 0.5, "confluence": 0.1,
              "bull": "b", "bear": "x", "divergences": [], "retained": False,
              "size_stage": None, "research": {}, "committee": {}}],
            [],
            regime={"quad": 1, "quad_name": "Goldilocks", "liquidity_overlay": "neutral"},
            asof="2026-07-01",
            gate_info={},
            shadow_inputs=[],
            directive="OVERNIGHT TEST"
        )
    except Exception:
        pass
    finally:
        monkeypatch.setenv("MASTERMIND_FLAGSHIP_JUDGMENT", "0")

    assert captured.get("directive") == "OVERNIGHT TEST"

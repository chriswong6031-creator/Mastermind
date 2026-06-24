"""Guards for the FAST DE-RISK trigger (bot/derisk).

Offline — we monkeypatch the macro state, the live tape, and the side-effecting book modules
(position_log / paper_account / ledger), dual-patching the package attributes + sys.modules per the
P1 lesson so NO real account, network feed, or LLM is ever touched. We prove the deterministic tripwire
fires on a confirmed unwind (and stays quiet on a calm tape), the Flagship cut realizes REAL exits down
to the gross cap respecting the never-blow-to-cash floor, and the Brain de-risk revises the queued
target subtract-only."""
from __future__ import annotations

import sys
import types

import bot  # noqa: F401  -> vendor/macro onto sys.path
from bot import derisk as D


def _patch_macro(monkeypatch, state_dict):
    """Patch brain.macro_risk.risk_state to a canned state (dual-patched)."""
    from brain import macro_risk as real_mr
    monkeypatch.setattr(real_mr, "risk_state", lambda asof, regime: state_dict, raising=True)


def _calm_tape(monkeypatch):
    ov = types.ModuleType("data_layer.overnight")
    ov.tape = lambda force=False: {"risk": {"state": "calm"}}
    ov._fetch_changes = lambda syms: {}
    import data_layer as _dl
    monkeypatch.setattr(_dl, "overnight", ov, raising=False)
    monkeypatch.setitem(sys.modules, "data_layer.overnight", ov)


# ───────────────────────────── tripwire ─────────────────────────────
def test_tripwire_fires_on_macro_riskoff(monkeypatch):
    _patch_macro(monkeypatch, {"state": "risk_off", "gross_cap": 0.55, "drivers": []})
    _calm_tape(monkeypatch)
    monkeypatch.setattr(D, "_gex_flip", lambda: (False, ""))
    monkeypatch.setattr(D, "_credit_gap", lambda: (False, ""))
    monkeypatch.setattr(D, "_theme_drop", lambda drivers: (False, ""))
    tw = D.tripwire("flagship", "2026-06-23", regime={})
    assert tw["trigger"] is True and tw["severity"] >= 2
    assert any("RISK-OFF" in r for r in tw["reasons"])


def test_tripwire_fires_on_gex_flip(monkeypatch):
    _patch_macro(monkeypatch, {"state": "risk_on", "gross_cap": 1.0, "drivers": []})
    _calm_tape(monkeypatch)
    monkeypatch.setattr(D, "_gex_flip", lambda: (True, "SPY dealers SHORT gamma"))
    monkeypatch.setattr(D, "_credit_gap", lambda: (False, ""))
    monkeypatch.setattr(D, "_theme_drop", lambda drivers: (False, ""))
    tw = D.tripwire("flagship", "2026-06-23", regime={})
    assert tw["trigger"] is True
    assert any("gamma" in r.lower() for r in tw["reasons"])


def test_tripwire_quiet_on_calm(monkeypatch):
    _patch_macro(monkeypatch, {"state": "risk_on", "gross_cap": 1.0, "drivers": []})
    _calm_tape(monkeypatch)
    monkeypatch.setattr(D, "_gex_flip", lambda: (False, ""))
    monkeypatch.setattr(D, "_credit_gap", lambda: (False, ""))
    monkeypatch.setattr(D, "_theme_drop", lambda drivers: (False, ""))
    tw = D.tripwire("flagship", "2026-06-23", regime={})
    assert tw["trigger"] is False and tw["severity"] == 0


# ───────────────────────────── flagship cut ─────────────────────────────
def test_derisk_flagship_cuts_to_gross_cap(monkeypatch):
    _patch_macro(monkeypatch, {"state": "risk_off", "gross_cap": 0.55, "drivers": []})
    _calm_tape(monkeypatch)
    monkeypatch.setattr(D, "_gex_flip", lambda: (False, ""))
    monkeypatch.setattr(D, "_credit_gap", lambda: (False, ""))
    monkeypatch.setattr(D, "_theme_drop", lambda drivers: (False, ""))

    # 6 held conviction names @ 0.15 (gross 0.9); none in a fragility chain → ordered by worst rel.
    held = [{"ticker": tk, "sleeve": "conviction", "current_weight": 0.15, "entry_price": 100.0}
            for tk in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")]
    prices = {"AAA": 80.0, "BBB": 90.0, "CCC": 95.0, "DDD": 105.0, "EEE": 110.0, "FFF": 115.0}

    pl = types.ModuleType("portfolio.position_log")
    pl.open_positions = lambda portfolio_id=None: list(held)
    closed = []
    pl.close_position = lambda sleeve, t, asof, reason="x", portfolio_id=None: closed.append(t) or True

    pa = types.ModuleType("portfolio.paper_account")
    filled = []
    pa.execute_fill = lambda t, side, asof=None, **k: (filled.append(t) or {"ok": True})
    pa._current_price = lambda t: prices.get(t)
    pa.load_pending_target = lambda pid=None: None

    lg = types.ModuleType("brain.ledger")
    lg.close = lambda t, note="": None

    import brain as _brain_pkg
    import portfolio as _pf_pkg
    from portfolio import fragility_chain as _real_fc
    from brain import risk_officer as _real_ro
    monkeypatch.setattr(_pf_pkg, "position_log", pl, raising=False)
    monkeypatch.setattr(_pf_pkg, "paper_account", pa, raising=False)
    monkeypatch.setattr(_pf_pkg, "fragility_chain", _real_fc, raising=False)
    monkeypatch.setattr(_brain_pkg, "risk_officer", _real_ro, raising=False)
    monkeypatch.setattr(_brain_pkg, "ledger", lg, raising=False)
    monkeypatch.setitem(sys.modules, "portfolio.position_log", pl)
    monkeypatch.setitem(sys.modules, "portfolio.paper_account", pa)
    monkeypatch.setitem(sys.modules, "brain.ledger", lg)

    res = D.derisk_flagship("2026-06-23", regime={}, force=True)
    assert res["action"] == "cut"
    # 0.9 → need to drop to 0.55: exit the 3 worst losers (AAA,BBB,CCC); floor keeps 3 invested.
    assert set(res["exited"]) == {"AAA", "BBB", "CCC"}
    assert set(filled) == {"AAA", "BBB", "CCC"}          # REAL paper exits realized (cash freed)
    assert len(res["exited"]) <= 3                        # never-blow-to-cash: ≤ (6 - min_invested 3)


def test_derisk_flagship_holds_when_under_cap(monkeypatch):
    _patch_macro(monkeypatch, {"state": "risk_off", "gross_cap": 0.55, "drivers": []})
    _calm_tape(monkeypatch)
    monkeypatch.setattr(D, "_gex_flip", lambda: (False, ""))
    monkeypatch.setattr(D, "_credit_gap", lambda: (False, ""))
    monkeypatch.setattr(D, "_theme_drop", lambda drivers: (False, ""))
    held = [{"ticker": "AAA", "sleeve": "conviction", "current_weight": 0.2, "entry_price": 100.0},
            {"ticker": "BBB", "sleeve": "conviction", "current_weight": 0.2, "entry_price": 100.0}]
    pl = types.ModuleType("portfolio.position_log")
    pl.open_positions = lambda portfolio_id=None: list(held)
    pl.close_position = lambda *a, **k: True
    pa = types.ModuleType("portfolio.paper_account")
    pa.execute_fill = lambda *a, **k: {"ok": True}
    pa._current_price = lambda t: 100.0
    import portfolio as _pf_pkg
    from portfolio import fragility_chain as _real_fc
    from brain import risk_officer as _real_ro
    import brain as _brain_pkg
    lg = types.ModuleType("brain.ledger"); lg.close = lambda *a, **k: None
    monkeypatch.setattr(_pf_pkg, "position_log", pl, raising=False)
    monkeypatch.setattr(_pf_pkg, "paper_account", pa, raising=False)
    monkeypatch.setattr(_pf_pkg, "fragility_chain", _real_fc, raising=False)
    monkeypatch.setattr(_brain_pkg, "risk_officer", _real_ro, raising=False)
    monkeypatch.setattr(_brain_pkg, "ledger", lg, raising=False)
    monkeypatch.setitem(sys.modules, "portfolio.position_log", pl)
    monkeypatch.setitem(sys.modules, "portfolio.paper_account", pa)
    monkeypatch.setitem(sys.modules, "brain.ledger", lg)
    res = D.derisk_flagship("2026-06-23", regime={}, force=True)
    assert res["action"] == "hold"                       # gross 0.4 ≤ 0.55 → no cut


# ───────────────────────────── brain pending-target de-risk ─────────────────────────────
def _patch_brain_pa(monkeypatch, target):
    pa = types.ModuleType("portfolio.paper_account")
    saved = {}
    pa.load_pending_target = lambda pid=None: {"target": dict(target), "asof": "2026-06-23"}
    pa.save_pending_target = lambda tgt, asof, portfolio_id=None: saved.update({"t": dict(tgt)})
    import portfolio as _pf_pkg
    from portfolio import fragility_chain as _real_fc
    monkeypatch.setattr(_pf_pkg, "paper_account", pa, raising=False)
    monkeypatch.setattr(_pf_pkg, "fragility_chain", _real_fc, raising=False)
    monkeypatch.setitem(sys.modules, "portfolio.paper_account", pa)
    return saved


def test_derisk_brain_drops_cracking_chain(monkeypatch):
    _patch_macro(monkeypatch, {"state": "risk_off", "gross_cap": 0.55,
                               "drivers": [{"id": "ai_buildout"}], "allow_adds": False})
    _calm_tape(monkeypatch)
    monkeypatch.setattr(D, "_gex_flip", lambda: (False, ""))
    monkeypatch.setattr(D, "_credit_gap", lambda: (False, ""))
    monkeypatch.setattr(D, "_theme_drop", lambda drivers: (False, ""))
    saved = _patch_brain_pa(monkeypatch, {"NVDA": 0.3, "AVGO": 0.2, "XLP": 0.2})
    res = D.derisk_brain("autonomous", "2026-06-23", regime={}, force=True)
    assert res["action"] == "revised_pending_target"
    assert "NVDA" not in saved["t"] and "AVGO" not in saved["t"]   # cracking adds blocked
    assert "XLP" in saved["t"]


def test_derisk_brain_scales_gross(monkeypatch):
    _patch_macro(monkeypatch, {"state": "risk_off", "gross_cap": 0.55, "drivers": [], "allow_adds": False})
    _calm_tape(monkeypatch)
    monkeypatch.setattr(D, "_gex_flip", lambda: (False, ""))
    monkeypatch.setattr(D, "_credit_gap", lambda: (False, ""))
    monkeypatch.setattr(D, "_theme_drop", lambda drivers: (False, ""))
    saved = _patch_brain_pa(monkeypatch, {"XLP": 0.5, "XLV": 0.5})   # gross 1.0, no chains
    res = D.derisk_brain("etf", "2026-06-23", regime={}, force=True)
    assert res["scaled"] is True
    assert abs(sum(saved["t"].values()) - 0.55) < 1e-2              # scaled to the cash floor

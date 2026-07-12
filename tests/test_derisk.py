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


# ───────────────────────────── heavyweight held-book cut ─────────────────────────────
def _patch_hw_books(monkeypatch, held, prices):
    """Patch position_log / paper_account / ledger for a HELD heavyweight cut. open_positions is
    pid-scoped: it returns the held book ONLY for portfolio_id='heavyweight' (the real signature),
    so a mis-scoped read would see an empty book. execute_fill captures the portfolio_id it was
    called with so the test can assert the exit was scoped to heavyweight, not flagship."""
    pl = types.ModuleType("portfolio.position_log")
    pl.open_positions = lambda portfolio_id=None: (list(held) if portfolio_id == "heavyweight" else [])
    closed = []
    pl.close_position = (lambda sleeve, t, asof, reason="x", portfolio_id=None:
                         closed.append((t, sleeve, portfolio_id)) or True)

    pa = types.ModuleType("portfolio.paper_account")
    filled = []
    pa.execute_fill = (lambda t, side, asof=None, portfolio_id=None, **k:
                       filled.append((t, portfolio_id)) or {"ok": True})
    pa._current_price = lambda t: prices.get(t)
    pa.load_pending_target = lambda pid=None: None

    lg = types.ModuleType("brain.ledger")
    lg.close = lambda t, note="": None

    import brain as _brain_pkg
    import portfolio as _pf_pkg
    from portfolio import fragility_chain as _real_fc
    monkeypatch.setattr(_pf_pkg, "position_log", pl, raising=False)
    monkeypatch.setattr(_pf_pkg, "paper_account", pa, raising=False)
    monkeypatch.setattr(_pf_pkg, "fragility_chain", _real_fc, raising=False)
    monkeypatch.setattr(_brain_pkg, "ledger", lg, raising=False)
    monkeypatch.setitem(sys.modules, "portfolio.position_log", pl)
    monkeypatch.setitem(sys.modules, "portfolio.paper_account", pa)
    monkeypatch.setitem(sys.modules, "brain.ledger", lg)
    return filled, closed


def test_derisk_heavyweight_cuts_to_gross_cap(monkeypatch):
    _patch_macro(monkeypatch, {"state": "risk_off", "gross_cap": 0.55, "drivers": []})
    _calm_tape(monkeypatch)
    monkeypatch.setattr(D, "_gex_flip", lambda: (False, ""))
    monkeypatch.setattr(D, "_credit_gap", lambda: (False, ""))
    monkeypatch.setattr(D, "_theme_drop", lambda drivers: (False, ""))

    # 6 held "heavy" names @ 0.15 (gross 0.9); none in a fragility chain → ordered by worst rel.
    held = [{"ticker": tk, "sleeve": "heavy", "current_weight": 0.15, "entry_price": 100.0}
            for tk in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")]
    prices = {"AAA": 80.0, "BBB": 90.0, "CCC": 95.0, "DDD": 105.0, "EEE": 110.0, "FFF": 115.0}
    filled, closed = _patch_hw_books(monkeypatch, held, prices)

    res = D.derisk_heavyweight("2026-06-23", regime={}, force=True)
    assert res["action"] == "cut"
    assert res["pid"] == "heavyweight"
    # 0.9 → drop to ≤ 0.55: exit the 3 worst losers (AAA,BBB,CCC → 0.90-0.45=0.45 ≤ 0.55).
    # No conviction ½-Kelly floor here (single "heavy" sleeve), so the cut REACHES the cap —
    # unlike flagship, whose risk_officer guard would cap the same book at 2 exits.
    assert set(res["exited"]) == {"AAA", "BBB", "CCC"}
    # REAL paper exits realized AND scoped to portfolio_id='heavyweight' (not flagship).
    assert {t for t, pid in filled} == {"AAA", "BBB", "CCC"}
    assert all(pid == "heavyweight" for _, pid in filled)
    # the ledger close is also scoped to heavyweight + the "heavy" sleeve
    assert all(pid == "heavyweight" and sleeve == "heavy" for _, sleeve, pid in closed)
    assert res["cut_scope"] == ["heavy"]


def test_derisk_heavyweight_holds_when_under_cap(monkeypatch):
    _patch_macro(monkeypatch, {"state": "risk_off", "gross_cap": 0.55, "drivers": []})
    _calm_tape(monkeypatch)
    monkeypatch.setattr(D, "_gex_flip", lambda: (False, ""))
    monkeypatch.setattr(D, "_credit_gap", lambda: (False, ""))
    monkeypatch.setattr(D, "_theme_drop", lambda drivers: (False, ""))
    held = [{"ticker": "AAA", "sleeve": "heavy", "current_weight": 0.2, "entry_price": 100.0},
            {"ticker": "BBB", "sleeve": "heavy", "current_weight": 0.2, "entry_price": 100.0}]
    filled, _ = _patch_hw_books(monkeypatch, held, {"AAA": 100.0, "BBB": 100.0})
    res = D.derisk_heavyweight("2026-06-23", regime={}, force=True)
    assert res["action"] == "hold"          # gross 0.4 ≤ 0.55 → no cut
    assert filled == []                     # nothing realized


def test_derisk_heavyweight_no_trigger_is_noop(monkeypatch):
    # a calm tape / no confirmation → severity 0 → no cut even though the book is over any cap.
    _patch_macro(monkeypatch, {"state": "risk_on", "gross_cap": 1.0, "drivers": []})
    _calm_tape(monkeypatch)
    monkeypatch.setattr(D, "_gex_flip", lambda: (False, ""))
    monkeypatch.setattr(D, "_credit_gap", lambda: (False, ""))
    monkeypatch.setattr(D, "_theme_drop", lambda drivers: (False, ""))
    held = [{"ticker": tk, "sleeve": "heavy", "current_weight": 0.4, "entry_price": 100.0}
            for tk in ("AAA", "BBB")]
    filled, _ = _patch_hw_books(monkeypatch, held, {"AAA": 100.0, "BBB": 100.0})
    # NOT forced: the trigger gate must decide. Severity 0 → skipped, no exits.
    res = D.derisk_heavyweight("2026-06-23", regime={})
    assert res.get("skipped") in ("no_trigger", "disabled")
    assert filled == []


def test_derisk_heavyweight_failsoft_on_missing_state(monkeypatch):
    # FAIL-SOFT, matching derisk_flagship's ACTUAL contract (not a stricter one): (1) an IMPORT
    # failure of the book modules degrades to an {error:...} dict rather than propagating; (2) the
    # sweep-level boundary (sweep_us) swallows any deeper raise. derisk_flagship's open_positions()
    # read is likewise NOT wrapped — so a raising ledger read propagates out of the cutter and is
    # caught by sweep_us, exactly as here. We assert the two real boundaries.

    # (1) import failure → error dict, never raises. Force the `from portfolio import ...` to fail
    #     by inserting a sentinel module that raises on attribute access for the imported names.
    _patch_macro(monkeypatch, {"state": "risk_off", "gross_cap": 0.55, "drivers": []})
    _calm_tape(monkeypatch)
    monkeypatch.setattr(D, "_gex_flip", lambda: (False, ""))
    monkeypatch.setattr(D, "_credit_gap", lambda: (False, ""))
    monkeypatch.setattr(D, "_theme_drop", lambda drivers: (False, ""))

    class _RaisingModule(types.ModuleType):
        def __getattr__(self, name):  # any `from portfolio import X` triggers this → ImportError
            raise ImportError(f"boom:{name}")
    bad_pf = _RaisingModule("portfolio")
    monkeypatch.setitem(sys.modules, "portfolio", bad_pf)
    res = D.derisk_heavyweight("2026-06-23", regime={}, force=True)   # must NOT raise
    assert "error" in res and res["pid"] == "heavyweight"

    # (2) sweep-level fail-soft: a cutter that raises is swallowed by sweep_us into an {error} entry.
    monkeypatch.setattr(D, "enabled", lambda: True)
    def _raises(asof=None):
        raise RuntimeError("no ledger")
    monkeypatch.setattr(D, "derisk_heavyweight", _raises)
    monkeypatch.setattr(D, "derisk_flagship", lambda asof=None: {"action": "hold"})
    monkeypatch.setattr(D, "derisk_brain", lambda pid, asof=None: {"action": "hold"})
    out = D.sweep_us("2026-06-23")                                   # must NOT raise
    assert "error" in out["heavyweight"]


def test_sweep_us_invokes_heavyweight(monkeypatch):
    # sweep_us must route heavyweight through the HELD cutter (derisk_heavyweight), not derisk_brain.
    monkeypatch.setattr(D, "enabled", lambda: True)
    calls = {}
    monkeypatch.setattr(D, "derisk_flagship", lambda asof=None: calls.setdefault("flagship", True) or {"action": "hold"})
    monkeypatch.setattr(D, "derisk_heavyweight", lambda asof=None: calls.setdefault("heavyweight", True) or {"action": "hold"})
    brain_pids = []
    monkeypatch.setattr(D, "derisk_brain", lambda pid, asof=None: brain_pids.append(pid) or {"action": "hold"})
    out = D.sweep_us("2026-06-23")
    assert calls.get("flagship") and calls.get("heavyweight")     # both held cutters fired
    assert "heavyweight" in out
    assert brain_pids == ["autonomous", "etf"]                    # heavyweight NOT routed as a Brain book


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

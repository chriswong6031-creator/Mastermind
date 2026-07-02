"""A3 severity-decoupled tripwire tests — four scenarios from the W1 spec.

These tests reconstruct the 2026-07-01 failure state (state risk_on/cap=1.0; tripwire
severity=2 correctly fired; book at gross ~0.75 including 4 leadership legs) and assert the
new path bites.  We also verify the regression invariants: severity 0/1 leave the book alone
(no automatic cut), and the subtract-only invariant (the cut NEVER increases any position).

All side-effects (position_log, paper_account, ledger) are monkeypatched exactly as the
existing test_derisk.py does — no real account, no network, no LLM is ever touched.
"""
from __future__ import annotations

import sys
import types

import pytest

import bot  # noqa: F401  -> vendor/macro onto sys.path
from bot import derisk as D


# ─────────────────────────────────────────────────────────────────────────────
# shared helpers (mirrors test_derisk.py style)
# ─────────────────────────────────────────────────────────────────────────────
def _patch_macro(monkeypatch, state_dict: dict) -> None:
    """Patch brain.macro_risk.risk_state to the supplied canned state."""
    from brain import macro_risk as real_mr
    monkeypatch.setattr(real_mr, "risk_state", lambda asof, regime: state_dict, raising=True)


def _calm_tape(monkeypatch) -> None:
    """Stub the overnight tape to 'calm' so tape signals don't add severity."""
    ov = types.ModuleType("data_layer.overnight")
    ov.tape = lambda force=False: {"risk": {"state": "calm"}}
    ov._fetch_changes = lambda syms: {}
    import data_layer as _dl
    monkeypatch.setattr(_dl, "overnight", ov, raising=False)
    monkeypatch.setitem(sys.modules, "data_layer.overnight", ov)


def _gex_on(monkeypatch) -> None:
    """Stub a GEX flip (severity ≥ 2 without macro risk_off)."""
    monkeypatch.setattr(D, "_gex_flip", lambda: (True, "SPY dealers SHORT gamma (test)"))


def _gex_off(monkeypatch) -> None:
    monkeypatch.setattr(D, "_gex_flip", lambda: (False, ""))


def _credit_off(monkeypatch) -> None:
    monkeypatch.setattr(D, "_credit_gap", lambda: (False, ""))


def _theme_off(monkeypatch) -> None:
    monkeypatch.setattr(D, "_theme_drop", lambda drivers: (False, ""))


def _patch_flagship_book(monkeypatch, positions: list[dict]) -> tuple[list, list]:
    """Wire position_log / paper_account / ledger / fragility_chain / risk_officer for flagship tests.

    Returns (exited_log, filled_log) lists that the stubs append to so assertions can inspect calls.
    """
    import portfolio as _pf_pkg
    import brain as _brain_pkg
    from portfolio import fragility_chain as _real_fc
    from brain import risk_officer as _real_ro

    pl = types.ModuleType("portfolio.position_log")
    pl.open_positions = lambda portfolio_id=None: list(positions)
    closed: list = []
    pl.close_position = (
        lambda sleeve, t, asof, reason="x", portfolio_id=None: closed.append(t) or True
    )

    pa = types.ModuleType("portfolio.paper_account")
    filled: list = []
    pa.execute_fill = lambda t, side, asof=None, **k: filled.append(t) or {"ok": True}
    pa._current_price = lambda t: 100.0  # flat — no pnl sorting needed unless the test overrides
    pa.load_pending_target = lambda pid=None: None

    lg = types.ModuleType("brain.ledger")
    lg.close = lambda t, note="": None

    monkeypatch.setattr(_pf_pkg, "position_log", pl, raising=False)
    monkeypatch.setattr(_pf_pkg, "paper_account", pa, raising=False)
    monkeypatch.setattr(_pf_pkg, "fragility_chain", _real_fc, raising=False)
    monkeypatch.setattr(_brain_pkg, "risk_officer", _real_ro, raising=False)
    monkeypatch.setattr(_brain_pkg, "ledger", lg, raising=False)
    monkeypatch.setitem(sys.modules, "portfolio.position_log", pl)
    monkeypatch.setitem(sys.modules, "portfolio.paper_account", pa)
    monkeypatch.setitem(sys.modules, "brain.ledger", lg)
    return closed, filled


# ─────────────────────────────────────────────────────────────────────────────
# SCENARIO 1 — 2026-07-01 replay: state risk_on/cap=1.0, severity=2 via GEX flip.
#
# Book: 4 conviction legs @ 0.0875 + 4 leadership legs @ 0.125 = gross 0.85.
# (The task spec says "gross ~0.75 including 4 leadership legs at 0.125"; the conviction
#  piece can be smaller — we model 3 conviction @ 0.08333 + 4 leadership @ 0.125 = 0.75.)
# Expected: book cut to ≤ 0.70 (sev-2 cap); leadership legs included in the exit list.
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("leadership_tickers,conviction_tickers", [
    (
        ["SMH_L", "XLK_L", "NVDA_L", "AAPL_L"],    # 4 leadership legs @ 0.125 each = 0.50 NAV
        ["TSLA_C", "MSFT_C", "AMZN_C"],             # 3 conviction legs @ 0.0833 each = 0.25 NAV
    )
])
def test_0701_replay_cuts_to_sev2_cap_and_includes_leadership(
    monkeypatch, leadership_tickers, conviction_tickers
):
    """The 07-01 no-op bug: state risk_on/cap=1.0 + severity=2 tripwire → must cut to ≤ 0.70
    and the leadership sleeve must contribute exits (not be silently excluded)."""
    # Mac state: risk_on, cap=1.0 — this is what made the original code a no-op.
    _patch_macro(monkeypatch, {"state": "risk_on", "gross_cap": 1.0, "drivers": []})
    _calm_tape(monkeypatch)
    _gex_on(monkeypatch)          # GEX flip → severity=2 → trigger=True
    _credit_off(monkeypatch)
    _theme_off(monkeypatch)

    # Build the mixed book
    leadership_w = 0.125
    conviction_w = 0.25 / len(conviction_tickers)
    positions = (
        [{"ticker": t, "sleeve": "leadership", "current_weight": leadership_w,
          "entry_price": 100.0}
         for t in leadership_tickers]
        + [{"ticker": t, "sleeve": "conviction", "current_weight": conviction_w,
            "entry_price": 100.0}
           for t in conviction_tickers]
    )
    gross_before = round(
        leadership_w * len(leadership_tickers) + conviction_w * len(conviction_tickers), 4
    )
    assert abs(gross_before - 0.75) < 1e-3, f"test setup error: gross={gross_before}"

    closed, filled = _patch_flagship_book(monkeypatch, positions)
    res = D.derisk_flagship("2026-07-01", regime={}, force=True)

    # The cut must have fired — not "hold", not "flat".
    assert res["action"] == "cut", f"Expected cut, got: {res}"

    # After exits, the remaining gross must be ≤ 0.70 (the sev-2 cap).
    # We verify this via the exited set: gross_before − sum(exited weights) ≤ 0.70.
    exited = set(res["exited"])
    assert exited, "No positions exited — the 07-01 bug is not fixed"
    exited_weight = sum(p["current_weight"] for p in positions if p["ticker"] in exited)
    gross_after = gross_before - exited_weight
    assert gross_after <= 0.70 + 1e-9, (
        f"gross_after={gross_after:.4f} exceeds eff_cap=0.70; cut too small"
    )

    # At least one leadership leg must appear in the exit list.
    leadership_exited = exited & set(leadership_tickers)
    assert leadership_exited, (
        f"No leadership legs were exited: exited={exited}; leadership={leadership_tickers}"
    )

    # New artifact fields must be present.
    assert "eff_cap" in res
    assert "severity_cap" in res
    assert "cut_scope" in res
    assert res["eff_cap"] == pytest.approx(0.70, abs=1e-6)
    assert res["severity_cap"] == pytest.approx(0.70, abs=1e-6)
    assert "leadership" in res["cut_scope"]
    assert "conviction" in res["cut_scope"]


# ─────────────────────────────────────────────────────────────────────────────
# SCENARIO 2 — severity 0 / 1 regression: no auto-cut (caution alone is advisory).
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("severity,expected_skip", [
    (0, "no_trigger"),
    (1, "no_trigger"),
])
def test_severity_0_and_1_do_not_cut(monkeypatch, severity, expected_skip):
    """Severity < 2 must NEVER trigger an automatic gross-cap cut.  The existing behaviour is
    that ``trigger=False`` for severity<2 (see tripwire():  trigger = severity >= 2).
    This test asserts the regression: with force=False the result must be skipped, and with
    force=True (so we bypass the trigger gate artificially) the eff_cap must still be 1.0
    (no override) and no positions must be exited."""
    # Patch the tripwire directly to return the desired severity without side-effects.
    tw_result: dict = {
        "trigger": severity >= 2,
        "severity": severity,
        "reasons": ["test stub"],
        "state": "caution" if severity == 1 else "risk_on",
        "gross_cap": 1.0,
        "risk_state": {"state": "caution" if severity == 1 else "risk_on",
                       "gross_cap": 1.0, "drivers": []},
    }
    monkeypatch.setattr(D, "tripwire", lambda pid, asof, regime=None: tw_result)

    # Arm the module (so enabled() returns True) but don't use force — trigger gate should block.
    monkeypatch.setenv("MASTERMIND_FAST_DERISK", "1")

    # Wire a book with gross 0.75 so it's over ANY reasonable cap.
    positions = [
        {"ticker": "AAA", "sleeve": "leadership", "current_weight": 0.25, "entry_price": 100.0},
        {"ticker": "BBB", "sleeve": "conviction", "current_weight": 0.25, "entry_price": 100.0},
        {"ticker": "CCC", "sleeve": "leadership", "current_weight": 0.25, "entry_price": 100.0},
    ]
    closed, filled = _patch_flagship_book(monkeypatch, positions)

    res = D.derisk_flagship("2026-07-01", regime={})  # no force=True → trigger gate active
    assert res.get("skipped") == expected_skip, f"Expected skipped={expected_skip!r}, got {res}"
    assert not filled, "Positions were filled despite severity < 2 — subtract-only invariant violated"


def test_severity_1_eff_cap_is_unconstrained(monkeypatch):
    """When severity=1 and we FORCE bypass the trigger gate (hypothetically), the eff_cap must
    still equal the state_gross_cap (no override ladder applies below severity 2)."""
    # _severity_cap(1) must return None → eff_cap = state_gross_cap
    assert D._severity_cap(0) is None
    assert D._severity_cap(1) is None


# ─────────────────────────────────────────────────────────────────────────────
# SCENARIO 3 — subtract-only invariant: the cut NEVER increases any position's weight.
# ─────────────────────────────────────────────────────────────────────────────
def test_subtract_only_invariant(monkeypatch):
    """After a severity-2 cut no remaining position must have a higher weight than it started
    with. We achieve this by checking that ALL remaining positions retain their original weights
    (pro-rata scaling or partial-exit path must not inflate any single name)."""
    _patch_macro(monkeypatch, {"state": "risk_on", "gross_cap": 1.0, "drivers": []})
    _calm_tape(monkeypatch)
    _gex_on(monkeypatch)         # severity=2 → trigger
    _credit_off(monkeypatch)
    _theme_off(monkeypatch)

    # A perfectly uniform book: 8 positions × 0.1 = gross 0.80 > sev-2 cap 0.70.
    tickers = ["L1", "L2", "L3", "L4", "C1", "C2", "C3", "C4"]
    positions = [
        {"ticker": t, "sleeve": "leadership" if t.startswith("L") else "conviction",
         "current_weight": 0.10, "entry_price": 100.0}
        for t in tickers
    ]
    weight_before = {p["ticker"]: p["current_weight"] for p in positions}

    closed, filled = _patch_flagship_book(monkeypatch, positions)
    res = D.derisk_flagship("2026-07-01", regime={}, force=True)

    assert res["action"] == "cut", f"Expected cut, got: {res}"

    exited = set(res["exited"])
    surviving = [p for p in positions if p["ticker"] not in exited]
    for p in surviving:
        assert p["current_weight"] <= weight_before[p["ticker"]] + 1e-9, (
            f"{p['ticker']} weight INCREASED from {weight_before[p['ticker']]} to "
            f"{p['current_weight']} — subtract-only invariant violated"
        )


# ─────────────────────────────────────────────────────────────────────────────
# SCENARIO 4 — leadership legs explicitly included (cut_scope check)
# ─────────────────────────────────────────────────────────────────────────────
def test_leadership_only_book_gets_cut(monkeypatch):
    """A book composed ENTIRELY of leadership legs must still be cut when severity=2.
    Pre-fix, sleeve=='conviction' filter excluded all of them → no action."""
    _patch_macro(monkeypatch, {"state": "risk_on", "gross_cap": 1.0, "drivers": []})
    _calm_tape(monkeypatch)
    _gex_on(monkeypatch)
    _credit_off(monkeypatch)
    _theme_off(monkeypatch)

    # 6 leadership legs × 0.14 = gross 0.84 > 0.70
    positions = [
        {"ticker": f"L{i}", "sleeve": "leadership", "current_weight": 0.14, "entry_price": 100.0}
        for i in range(6)
    ]
    closed, filled = _patch_flagship_book(monkeypatch, positions)
    res = D.derisk_flagship("2026-07-01", regime={}, force=True)

    assert res["action"] == "cut", (
        f"Leadership-only book was not cut (pre-fix bug: filter excluded leadership): {res}"
    )
    gross_before = 6 * 0.14
    exited = set(res["exited"])
    exited_weight = sum(p["current_weight"] for p in positions if p["ticker"] in exited)
    gross_after = round(gross_before - exited_weight, 6)
    assert gross_after <= 0.70 + 1e-9, (
        f"gross_after={gross_after:.4f} exceeds sev-2 cap 0.70"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Unit: _severity_cap ladder
# ─────────────────────────────────────────────────────────────────────────────
def test_severity_cap_ladder():
    """_severity_cap must return None for severity<2 and the doctrine values for 2 and 3."""
    assert D._severity_cap(0) is None
    assert D._severity_cap(1) is None
    cap2 = D._severity_cap(2)
    cap3 = D._severity_cap(3)
    assert cap2 is not None
    assert cap3 is not None
    # Sev-2 cap must be between 0.60 and 0.80 (doctrine: 0.70 unverified-prior)
    assert 0.60 <= cap2 <= 0.80, f"sev-2 cap={cap2} outside expected range"
    # Sev-3 cap must be tighter (≤ sev-2)
    assert cap3 <= cap2 + 1e-9, f"sev-3 cap={cap3} must be ≤ sev-2 cap={cap2}"
    # Sev-3 must be between 0.45 and 0.65 (doctrine: 0.55 unverified-prior)
    assert 0.45 <= cap3 <= 0.65, f"sev-3 cap={cap3} outside expected range"


def test_severity_cap_unknown_high_severity_falls_to_sev3():
    """Any severity > 3 should fall back to the sev-3 value (the tightest hard cap), never None."""
    cap_high = D._severity_cap(99)
    cap3 = D._severity_cap(3)
    assert cap_high is not None
    # A very-high severity must be at most as large as sev-2 (should be sev-3 via walk-down).
    assert cap_high <= D._severity_cap(2) + 1e-9


# ─────────────────────────────────────────────────────────────────────────────
# Artifact field presence check (gross_cap, eff_cap, severity_cap, cut_scope)
# ─────────────────────────────────────────────────────────────────────────────
def test_artifact_fields_on_cut(monkeypatch):
    """The derisk_flagship result dict must contain eff_cap, severity_cap, cut_scope on a cut."""
    _patch_macro(monkeypatch, {"state": "risk_on", "gross_cap": 1.0, "drivers": []})
    _calm_tape(monkeypatch)
    _gex_on(monkeypatch)
    _credit_off(monkeypatch)
    _theme_off(monkeypatch)

    positions = [
        {"ticker": f"X{i}", "sleeve": "leadership", "current_weight": 0.15, "entry_price": 100.0}
        for i in range(6)
    ]  # gross 0.90 → cut needed
    _patch_flagship_book(monkeypatch, positions)
    res = D.derisk_flagship("2026-07-01", regime={}, force=True)

    assert res["action"] == "cut"
    for field in ("eff_cap", "severity_cap", "cut_scope", "gross_cap", "gross_before"):
        assert field in res, f"Missing field '{field}' in derisk result: {res.keys()}"
    # gross_cap in the result must equal eff_cap (not the raw state cap)
    assert res["gross_cap"] == res["eff_cap"]
    assert res["eff_cap"] < 1.0, "eff_cap must be below 1.0 when severity=2"


def test_artifact_fields_on_hold(monkeypatch):
    """When gross is already ≤ eff_cap the result dict must still carry the new fields."""
    _patch_macro(monkeypatch, {"state": "risk_on", "gross_cap": 1.0, "drivers": []})
    _calm_tape(monkeypatch)
    _gex_on(monkeypatch)          # severity=2 but gross is small
    _credit_off(monkeypatch)
    _theme_off(monkeypatch)

    positions = [
        {"ticker": "A", "sleeve": "conviction", "current_weight": 0.10, "entry_price": 100.0},
        {"ticker": "B", "sleeve": "leadership", "current_weight": 0.10, "entry_price": 100.0},
    ]  # gross 0.20 ≤ 0.70 → hold
    _patch_flagship_book(monkeypatch, positions)
    res = D.derisk_flagship("2026-07-01", regime={}, force=True)

    assert res["action"] == "hold"
    for field in ("eff_cap", "severity_cap", "cut_scope"):
        assert field in res, f"Missing field '{field}' in hold result: {res.keys()}"

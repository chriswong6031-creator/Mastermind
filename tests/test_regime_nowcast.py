"""tests/test_regime_nowcast.py — guards for the price-action regime NOWCAST (W-I task 3).

Pure / offline / intent-only.  Every price series is INJECTED from a trimmed CSV fixture
(tests/fixtures/regime_nowcast/) so no test ever live-reads the shared mutating yahoo store and no
assertion pins today's regime.  What is proved:

  * the SHRINK-ONLY invariant — the module can only ``confirm`` / ``doubt`` / ``strong-doubt``; a
    DEFENSIVE label no-ops it; a total data outage yields ``confirm`` (never doubt-on-no-evidence);
  * the label gate — it acts ONLY on a risk-on / Goldilocks-class label;
  * the 2-of-3 / 3-of-3 tally and per-leg absent-degrades-to-None accounting;
  * THE REPLAY (task 3c): reading the incident fixture, doubt is active BY 2026-06-24 and the hard
    3-of-3 strong-doubt holds on 2026-07-01 (the day before the SMH rebuy); the calm 2025-05 fixture
    (an offense-led uptrend) never reaches the 2/3 doubt bar.

The walk-forward VALIDATION lives in research/incidents/2026-07-02-semis-breakdown/
nowcast_validation.md; the pre-registered gate FAILED, so BUDGET_INPUT_QUALIFIED is False and is
asserted here (the module ships ADVISORY-ONLY — a lens + DEF_SLEEVE-unthrottle input, NOT a budget
input).  If a future recalibration flips that flag it MUST be paired with a passing walk-forward, so
this assertion is a deliberate tripwire against silently arming an un-validated budget input.
"""
from __future__ import annotations

import os

import pandas as pd
import pytest

import bot  # noqa: F401  -> puts vendor/macro on sys.path (canon MACD import in the leg helpers)
from brain import regime_nowcast as NC

_FIXT = os.path.join(os.path.dirname(__file__), "fixtures", "regime_nowcast")


# ── injected series_fn built from the trimmed CSV fixtures ────────────────────────────────────────
def _fixture_series_fn(window: str):
    """A pure series_fn reading tests/fixtures/regime_nowcast/<window>/<ticker>.csv.

    Missing ticker → None (so a leg degrades to absent, exactly as production would on a data gap).
    """
    root = os.path.join(_FIXT, window)
    cache: dict[str, object] = {}

    def _fn(ticker: str):
        if ticker not in cache:
            p = os.path.join(root, f"{ticker}.csv")
            if not os.path.exists(p):
                cache[ticker] = None
            else:
                df = pd.read_csv(p, parse_dates=["date"]).set_index("date")
                cache[ticker] = df["close"].astype(float).sort_index()
        return cache[ticker]

    return _fn


# ── synthetic series_fn for pure leg-logic tests (no fixture needed) ──────────────────────────────
def _const_up_series(days: int = 500, slope: float = 0.001):
    """A steadily-rising close series (offense healthy) shared by every ticker — a calm-tape stub."""
    idx = pd.bdate_range("2020-01-01", periods=days)
    return pd.Series([100.0 * (1.0 + slope) ** i for i in range(days)], index=idx)


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 1. SHRINK-ONLY INVARIANT
# ═══════════════════════════════════════════════════════════════════════════════════════════════
def test_no_data_yields_confirm_never_doubt():
    """A total data outage must yield ``confirm`` — a shrink-only signal never doubts on no
    evidence (absent legs are None, which never count toward the tally)."""
    r = NC.nowcast(lambda t: None, quad="Q1", quad_name="Goldilocks", risk_state="risk_on")
    assert r["stance"] == "confirm"
    assert r["legs"]["n_doubt"] == 0
    assert r["legs"]["rs_cross"] is None
    assert r["legs"]["smh_3d_bear"] is None
    assert r["legs"]["breadth_falling"] is None


def test_defensive_label_no_ops_the_nowcast():
    """The nowcast acts ONLY on offense.  A defensive label (risk_off / Quad-4) makes it a no-op:
    it can DOUBT offense, it can never doubt defense.  Even with every doubt leg firing, a
    defensive label returns confirm + applies False."""
    fn = _fixture_series_fn("incident_2026_0607")
    asof = pd.Timestamp("2026-07-01")  # a live 3/3 doubt day under a risk-on label
    for rs, q, qn in [("risk_off", "Q4", "Deflation"),
                      ("caution", None, "Growth scare"),
                      ("elevated", "Q3", "Stagflation")]:
        r = NC.nowcast(fn, quad=q, quad_name=qn, risk_state=rs, asof=asof)
        assert r["applies"] is False
        assert r["stance"] == "confirm", (rs, q, qn)


def test_only_verdicts_are_confirm_or_doubt():
    """The module has NO vocabulary to add risk: every stance it can emit is confirm/doubt/
    strong-doubt.  Sweep the incident window and assert the invariant on every session."""
    fn = _fixture_series_fn("incident_2026_0607")
    for d in pd.bdate_range("2026-05-01", "2026-07-01"):
        st = NC.nowcast(fn, quad="Q1", quad_name="Goldilocks",
                        risk_state="risk_on", asof=d)["stance"]
        assert st in {"confirm", "doubt", "strong-doubt"}


def test_budget_input_qualified_is_false_pending_validation():
    """The pre-registered walk-forward FAILED (see nowcast_validation.md), so the module ships
    ADVISORY-ONLY.  This flag governs whether task 6 may wire it into budget(); it must stay False
    until a passing walk-forward flips it.  A deliberate tripwire against silent arming."""
    assert NC.BUDGET_INPUT_QUALIFIED is False
    r = NC.nowcast(lambda t: None, quad="Q1")
    assert r["budget_input_qualified"] is False


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 2. LABEL GATE
# ═══════════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("quad,quad_name,risk_state,expect", [
    ("Q1", "Goldilocks", "risk_on", True),
    ("Q2", "Reflation", "risk_on", True),
    (None, None, None, True),               # unknown label → the tape opinion still matters most
    ("Q1", "Goldilocks", None, True),
    ("Q3", "Stagflation", "risk_on", False),   # defensive quad
    ("Q4", "Deflation", "risk_on", False),
    ("Q1", "Goldilocks", "caution", False),    # explicit caution risk_state overrides an offensive quad
    (None, "Growth scare", None, False),       # named defensive regime
])
def test_label_gate(quad, quad_name, risk_state, expect):
    assert NC._label_is_risk_on(quad, quad_name, risk_state) is expect


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 3. TALLY + PER-LEG ACCOUNTING
# ═══════════════════════════════════════════════════════════════════════════════════════════════
def test_calm_uptrend_stub_confirms():
    """A steadily-rising tape shared by every ticker: no defensive-RS cross (def==off returns),
    no 3D bear, no falling breadth → 0 legs → confirm."""
    s = _const_up_series()
    r = NC.nowcast(lambda t: s, quad="Q1", quad_name="Goldilocks", risk_state="risk_on")
    assert r["stance"] == "confirm"
    assert r["legs"]["n_doubt"] == 0


def test_none_leg_never_counts():
    """A leg that reads None (e.g. SMH history absent → 3D-MACD undeterminable) must not count
    toward the doubt tally even if the other two legs fire.  Prove by dropping SMH from an
    otherwise-doubting fixture and confirming n_doubt reflects only the determinable TRUE legs."""
    base = _fixture_series_fn("incident_2026_0607")

    def fn(t):
        return None if t == "SMH" else base(t)

    r = NC.legs(fn, asof=pd.Timestamp("2026-07-01"))
    assert r["smh_3d_bear"] is None            # SMH absent → leg 2 undeterminable
    # the leg-2 None never counts; n_doubt is only the TRUE legs among {rs_cross, breadth_falling}
    determinable_true = sum(1 for x in (r["rs_cross"], r["breadth_falling"]) if x is True)
    assert r["n_doubt"] == determinable_true


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 4. THE REPLAY (task 3c) — the forensics' documented dates, read from fixtures
# ═══════════════════════════════════════════════════════════════════════════════════════════════
def test_replay_soft_doubt_by_0624():
    """By 2026-06-24 the price tape is doubting offense (≥ 2-of-3 legs) — the "days early" read
    that the user front-ran.  ``doubt`` OR the stronger ``strong-doubt`` both satisfy "soft doubt
    is present by 06-24"."""
    fn = _fixture_series_fn("incident_2026_0607")
    r = NC.nowcast(fn, quad="Q1", quad_name="Goldilocks",
                   risk_state="risk_on", asof=pd.Timestamp("2026-06-24"))
    assert r["applies"] is True
    assert r["stance"] in {"doubt", "strong-doubt"}
    assert r["legs"]["n_doubt"] >= 2


def test_replay_strong_doubt_on_0701():
    """On 2026-07-01 — the day BEFORE Autonomous bought back 41 SMH into the breakdown — all three
    price-action legs fire: defensive-RS cross, SMH 3D-MACD bearish, breadth falling.  The hard
    composite reads strong-doubt."""
    fn = _fixture_series_fn("incident_2026_0607")
    r = NC.nowcast(fn, quad="Q1", quad_name="Goldilocks",
                   risk_state="risk_on", asof=pd.Timestamp("2026-07-01"))
    assert r["stance"] == "strong-doubt"
    assert r["legs"]["n_doubt"] == 3
    assert r["legs"]["rs_cross"] is True
    assert r["legs"]["smh_3d_bear"] is True
    assert r["legs"]["breadth_falling"] is True
    # the RS diff is affirmatively positive (defensives leading) — the shared helper's sign
    assert r["legs"]["rs_diff"] is not None and r["legs"]["rs_diff"] > 0


def test_replay_no_doubt_in_calm_window():
    """A calm 2025-05 offense-led uptrend (SMH ~ +13% on the month) NEVER reaches the 2/3 doubt
    bar on any session — the negative control that proves the nowcast is not a permanent alarm."""
    fn = _fixture_series_fn("calm_2025_05")
    for d in pd.bdate_range("2025-05-01", "2025-05-30"):
        r = NC.nowcast(fn, quad="Q1", quad_name="Goldilocks", risk_state="risk_on", asof=d)
        assert r["stance"] == "confirm", (d.date(), r["legs"])
        assert r["legs"]["n_doubt"] <= 1


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 5. DEGRADE / ROBUSTNESS
# ═══════════════════════════════════════════════════════════════════════════════════════════════
def test_throwing_series_fn_degrades_to_confirm():
    """A series_fn that RAISES on every call is treated as missing data — the nowcast must
    swallow it and return confirm (never propagate the exception)."""
    def boom(_t):
        raise RuntimeError("cold store")

    r = NC.nowcast(boom, quad="Q1", quad_name="Goldilocks", risk_state="risk_on")
    assert r["stance"] == "confirm"
    assert r["legs"]["n_doubt"] == 0


def test_legs_asof_prevents_lookahead():
    """``asof`` restricts every series to closes at or before it: the leg reads at an EARLIER asof
    must not depend on later bars.  Prove the 06-25 (confirm) and 07-01 (3/3) reads differ, which
    can only happen if asof is honoured."""
    fn = _fixture_series_fn("incident_2026_0607")
    early = NC.legs(fn, asof=pd.Timestamp("2026-06-25"))
    late = NC.legs(fn, asof=pd.Timestamp("2026-07-01"))
    assert early["n_doubt"] < late["n_doubt"]

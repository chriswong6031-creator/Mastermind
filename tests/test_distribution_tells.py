"""W-I task 1 — DISTRIBUTION ESCALATOR tests.

Covers:
  * the per-holding tells (crowding / 3D-MACD-bear / weekly-MACD-bear / defensive-RS crossover),
    each degrading to ABSENT on missing data (never fabricated True);
  * the book-weight escalation rule (>= frac of book weight in >=2-tell names → +1 severity), with
    the reason string naming the tells;
  * the SHADOW trim ladder (quarter-position steps toward the cap; emits, never executes; carries the
    pre-registered 21td / 40-call risk-adjusted falisifier);
  * the derisk.py severity BLEND — composed via max(), SHRINK-ONLY, clamped to the ladder ceiling;
  * the REPLAY ACCEPTANCE: SMH's actual June path (fixture) — tells fire by 06-25 (3D-MACD bear +
    crowding), the escalator lifts a sev-2 short-gamma tripwire to sev-3 (eff_cap 0.55) vs the 0.70
    the plain ladder gives vs the 1.0 no-op that actually happened.

The shared vendor/macro_src store is NEVER live-read: every price series is fixture-injected via a
pure ``prices_fn`` built from tests/fixtures/distribution/SMH_june.csv, and the live board pctile is
monkeypatched off. No real account / network / LLM is touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

# The tells use engine.canon (session-grouped MACD) from the vendored macro checkout.
_ROOT = Path(__file__).resolve().parent.parent
_MACRO_SRC = _ROOT / "vendor" / "macro_src"
if _MACRO_SRC.exists() and str(_MACRO_SRC) not in sys.path:
    sys.path.insert(0, str(_MACRO_SRC))

from portfolio import distribution_tells as DT  # noqa: E402

_FIXTURE = _ROOT / "tests" / "fixtures" / "distribution" / "SMH_june.csv"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def smh_closes() -> pd.Series:
    df = pd.read_csv(_FIXTURE, parse_dates=["date"]).set_index("date")["close"]
    return df.astype(float).sort_index()


def _series_fn_asof(smh: pd.Series, asof: str, extra: dict | None = None):
    """Build a pure prices_fn: SMH cut at *asof*, plus any extra {ticker: Series}. Everything else
    (the defensive/offensive baskets, unless in extra) returns None → the tell/rs-diff degrades."""
    ts = pd.Timestamp(asof)
    cut = smh[smh.index <= ts]

    def fn(ticker: str):
        t = (ticker or "").upper()
        if t == "SMH":
            return cut
        if extra and t in extra:
            e = extra[t]
            return e[e.index <= ts] if isinstance(e, pd.Series) else e
        return None
    return fn


@pytest.fixture(autouse=True)
def _no_live_board(monkeypatch):
    """Disable the live vendored board pctile so crowding is computed from the injected series only
    (the shared store must never leak into the unit under test)."""
    monkeypatch.setattr(DT, "_board_pctile_252d", lambda: {}, raising=True)


# ---------------------------------------------------------------------------
# per-holding tells
# ---------------------------------------------------------------------------
def test_tells_absent_on_missing_data(monkeypatch):
    """A holding whose series fn returns None must carry NO fired tells (never fabricated True)."""
    sc = DT.score([{"ticker": "ZZZ", "current_weight": 0.30}], prices_fn=lambda t: None)
    row = sc["holdings"][0]
    assert row["n_tells"] == 0
    assert row["tells"]["macd_3d_bear"] is None
    assert row["tells"]["macd_wk_bear"] is None
    assert row["tells"]["crowding"] is False        # crowding pctile undeterminable → not crowded
    assert row["tells"]["crowding_pctile"] is None
    assert sc["escalate_severity"] == 0
    assert sc["hot"] is False


def test_empty_holdings_is_noop():
    for holdings in (None, []):
        sc = DT.score(holdings, prices_fn=lambda t: None)
        assert sc["holdings"] == []
        assert sc["escalate_severity"] == 0
        assert sc["hot"] is False
        assert sc["reason"] == ""


def test_smh_3d_macd_bear_state_true_through_window(smh_closes):
    """The 3D-MACD bearish STATE holds True across 06-22..07-01 (canon session-grouped grid)."""
    for asof in ("2026-06-24", "2026-06-25", "2026-06-26", "2026-07-01"):
        fn = _series_fn_asof(smh_closes, asof)
        assert DT._macd_bear_state(fn("SMH"), "3d") is True, f"3D-MACD not bear on {asof}"


def test_smh_crowding_fires_by_0625(smh_closes):
    """Crowding (60d-return own-history pctile) is at/above the 95th on 06-25 (the acceptance date)."""
    fn = _series_fn_asof(smh_closes, "2026-06-25")
    pct = DT._crowding_tell("SMH", fn("SMH"), {})
    assert pct is not None and pct >= 0.95, f"crowding pctile {pct} below 0.95 on 06-25"


def test_board_pctile_preferred_when_present(monkeypatch, smh_closes):
    """When the board publishes pctile_252d it is preferred over own-history (bot & board agree)."""
    monkeypatch.setattr(DT, "_board_pctile_252d", lambda: {"SMH": 0.99}, raising=True)
    fn = _series_fn_asof(smh_closes, "2026-07-01")
    pct = DT._crowding_tell("SMH", fn("SMH"), DT._board_pctile_252d())
    assert pct == 0.99


# ---------------------------------------------------------------------------
# defensive-vs-offensive RS differential (the SHARED helper task 3 imports)
# ---------------------------------------------------------------------------
def test_rs_diff_degrades_to_none_on_missing_baskets():
    """No basket series → diff/crossed None (the tell is ABSENT, never fabricated)."""
    out = DT.defensive_offensive_rs_diff(window=20, series_fn=lambda t: None)
    assert out["diff"] is None
    assert out["crossed"] is None


def test_rs_diff_positive_when_defensives_outperform():
    """A constructed set where defensives rise and offense falls → diff>0, crossed True."""
    idx = pd.bdate_range("2026-01-01", periods=60)
    up = pd.Series([100 * (1 + 0.001 * i) for i in range(60)], index=idx)     # +
    dn = pd.Series([100 * (1 - 0.002 * i) for i in range(60)], index=idx)     # −

    def fn(t):
        t = t.upper()
        if t in ("XLV", "XLU", "XLP"):
            return up
        if t in ("SMH", "XLK"):
            return dn
        return None
    out = DT.defensive_offensive_rs_diff(window=20, series_fn=fn)
    assert out["crossed"] is True
    assert out["diff"] > 0
    assert out["n_def"] == 3 and out["n_off"] == 2


def test_def_rs_cross_tell_wired_into_score():
    """When the RS diff crosses, def_rs_cross becomes a fired tell on every holding."""
    idx = pd.bdate_range("2026-01-01", periods=60)
    up = pd.Series([100 * (1 + 0.001 * i) for i in range(60)], index=idx)
    dn = pd.Series([100 * (1 - 0.002 * i) for i in range(60)], index=idx)

    def fn(t):
        t = t.upper()
        if t in ("XLV", "XLU", "XLP"):
            return up
        if t in ("SMH", "XLK", "HOLD1"):
            return dn
        return None
    sc = DT.score([{"ticker": "HOLD1", "current_weight": 0.30}], prices_fn=fn)
    assert sc["def_rs_cross"] is True
    assert "def-RS-cross" in sc["holdings"][0]["fired"]


# ---------------------------------------------------------------------------
# escalation rule + reason string
# ---------------------------------------------------------------------------
def test_escalation_reason_names_the_tells(smh_closes):
    """A book concentrated in a distributing name → hot, +1 severity, reason names the tells."""
    fn = _series_fn_asof(smh_closes, "2026-06-26")
    sc = DT.score([{"ticker": "SMH", "current_weight": 0.28}], prices_fn=fn)
    assert sc["hot"] is True
    assert sc["escalate_severity"] == 1
    assert "distribution:" in sc["reason"]
    assert "SMH" in sc["reason"]
    # at least one MACD tell must be named
    assert ("MACD" in sc["reason"])


def test_escalation_below_frac_does_not_fire(smh_closes):
    """When distributing names are BELOW the book-weight fraction, no escalation."""
    fn = _series_fn_asof(smh_closes, "2026-06-26")
    # SMH at only 5% of a book dominated by a non-distributing (series-absent) name
    sc = DT.score(
        [{"ticker": "SMH", "current_weight": 0.05},
         {"ticker": "ZZZ", "current_weight": 0.95}],
        prices_fn=_series_fn_absent_except(smh_closes, "2026-06-26"),
    )
    assert sc["distributing_weight_frac"] < 0.25
    assert sc["hot"] is False
    assert sc["escalate_severity"] == 0


def _series_fn_absent_except(smh: pd.Series, asof: str):
    ts = pd.Timestamp(asof)
    cut = smh[smh.index <= ts]

    def fn(t):
        return cut if (t or "").upper() == "SMH" else None
    return fn


# ---------------------------------------------------------------------------
# SHADOW trim ladder
# ---------------------------------------------------------------------------
def test_shadow_trim_ladder_quarter_steps_toward_cap(smh_closes):
    """A distributing over-cap name gets a quarter-position step toward the cap — never an exit,
    never below the cap in one step."""
    fn = _series_fn_asof(smh_closes, "2026-06-26")
    trims = DT.shadow_trim_ladder([{"ticker": "SMH", "current_weight": 0.28}], prices_fn=fn,
                                  name_cap=0.08)
    assert len(trims) == 1
    t = trims[0]
    assert t["ticker"] == "SMH"
    assert t["from_weight"] == pytest.approx(0.28)
    # quarter of 0.28 = 0.07 step → to 0.21 (still above the 0.08 cap)
    assert t["to_weight"] == pytest.approx(0.21, abs=1e-6)
    assert t["step"] == pytest.approx(0.07, abs=1e-6)
    assert t["to_weight"] >= t["target_cap"]          # never below cap in one step
    assert t["falsifier_by"] is not None
    assert t["graded"] is False


def test_shadow_trim_ladder_skips_at_or_under_cap(smh_closes):
    """A distributing name already at/under the cap gets NO trim recommendation."""
    fn = _series_fn_asof(smh_closes, "2026-06-26")
    trims = DT.shadow_trim_ladder([{"ticker": "SMH", "current_weight": 0.06}], prices_fn=fn,
                                  name_cap=0.08)
    assert trims == []


def test_shadow_trim_ladder_never_recommends_a_raise(smh_closes):
    """Every trim row must move weight DOWN (subtract-only), never up."""
    fn = _series_fn_asof(smh_closes, "2026-06-26")
    trims = DT.shadow_trim_ladder([{"ticker": "SMH", "current_weight": 0.30}], prices_fn=fn,
                                  name_cap=0.08)
    for t in trims:
        assert t["to_weight"] <= t["from_weight"] + 1e-9


def test_write_shadow_trims_artifact(tmp_path, smh_closes):
    """write_shadow_trims persists a shadow artifact (no sizing touched) with the falsifier doc."""
    fn = _series_fn_asof(smh_closes, "2026-06-26")
    payload = DT.write_shadow_trims([{"ticker": "SMH", "current_weight": 0.28}], prices_fn=fn,
                                    portfolio_id="flagship", asof="2026-06-26", out_dir=tmp_path)
    assert payload["portfolio_id"] == "flagship"
    assert payload["escalate_severity"] == 1
    assert "40 graded trims" in payload["falsifier"]
    assert "risk-adjusted" in payload["falsifier"]
    written = tmp_path / "2026-06-26_flagship.json"
    assert written.exists()


# ---------------------------------------------------------------------------
# derisk.py severity BLEND — SHRINK-ONLY, composed via max(), clamped to ceiling
# ---------------------------------------------------------------------------
def _wire_derisk_book(monkeypatch, positions, smh, asof, base_state):
    """Wire bot.derisk to read *positions* + fixture prices + a canned macro state. Returns the
    derisk module."""
    import types
    from bot import derisk as D

    from brain import macro_risk as real_mr
    monkeypatch.setattr(real_mr, "risk_state", lambda a, r: base_state, raising=True)

    # calm tape / no gex / no credit / no theme so the ONLY hard confirmation is what the test sets
    ov = types.ModuleType("data_layer.overnight")
    ov.tape = lambda force=False: {"risk": {"state": "calm"}}
    ov._fetch_changes = lambda syms: {}
    import data_layer as _dl
    monkeypatch.setattr(_dl, "overnight", ov, raising=False)
    monkeypatch.setitem(sys.modules, "data_layer.overnight", ov)
    monkeypatch.setattr(D, "_credit_gap", lambda: (False, ""))
    monkeypatch.setattr(D, "_theme_drop", lambda drivers: (False, ""))

    # holdings source for the distribution escalator
    pl = types.ModuleType("portfolio.position_log")
    pl.open_positions = lambda portfolio_id=None: list(positions)
    import portfolio as _pf
    monkeypatch.setattr(_pf, "position_log", pl, raising=False)
    monkeypatch.setitem(sys.modules, "portfolio.position_log", pl)

    # inject the fixture price series into distribution_tells' default reader
    monkeypatch.setattr(DT, "_default_series_fn", _series_fn_asof(smh, asof), raising=True)
    monkeypatch.setattr(DT, "_board_pctile_252d", lambda: {}, raising=True)
    return D


def test_derisk_escalation_sev2_to_sev3_on_0626(monkeypatch, smh_closes):
    """REPLAY ACCEPTANCE. 06-26: a short-gamma tripwire is already printing sev-2; the book's held
    SMH is distributing (crowding + 3D-MACD bear). The escalator lifts sev-2 → sev-3 → eff_cap 0.55
    (vs the 0.70 the plain ladder gives; vs the 1.0 no-op that actually happened)."""
    positions = [{"ticker": "SMH", "sleeve": "leadership", "current_weight": 0.28}]
    D = _wire_derisk_book(
        monkeypatch, positions, smh_closes, "2026-06-26",
        base_state={"state": "risk_on", "gross_cap": 1.0, "drivers": []},
    )
    # short-gamma tripwire already sev-2
    monkeypatch.setattr(D, "_gex_flip", lambda: (True, "SPY dealers SHORT gamma"))

    tw = D.tripwire("flagship", "2026-06-26", regime={})
    assert tw["severity"] == 3, f"expected sev-3, got {tw['severity']}: {tw['reasons']}"
    assert tw["trigger"] is True
    # eff_cap should now be the sev-3 cap 0.55
    assert D._severity_cap(3) == pytest.approx(0.55)
    assert D._severity_cap(2) == pytest.approx(0.70)   # what the plain ladder would have given
    # the escalation reason must be present and name the tells
    assert any("distribution:" in r for r in tw["reasons"])


def test_derisk_escalation_is_shrink_only_no_gex(monkeypatch, smh_closes):
    """Distribution WITHOUT a hard confirmation lifts severity to 1 (advisory) but does NOT auto-cut
    (trigger stays False) — mirroring caution-alone. It can only ADD severity, never un-cap."""
    positions = [{"ticker": "SMH", "sleeve": "leadership", "current_weight": 0.28}]
    D = _wire_derisk_book(
        monkeypatch, positions, smh_closes, "2026-06-26",
        base_state={"state": "risk_on", "gross_cap": 1.0, "drivers": []},
    )
    monkeypatch.setattr(D, "_gex_flip", lambda: (False, ""))  # no hard confirmation

    tw = D.tripwire("flagship", "2026-06-26", regime={})
    assert tw["severity"] == 1, f"distribution-only should be sev-1 advisory, got {tw['severity']}"
    assert tw["trigger"] is False          # sev-1 never auto-cuts (caution-alone parity)


def test_derisk_escalation_clamped_to_ceiling(monkeypatch, smh_closes):
    """The bump is clamped to the ladder ceiling (3). macro risk_off (sev-2) + short-gamma (sev-2)
    + distribution (+1) composes to 3, and a hypothetical further stack cannot push past 3 (never
    additive beyond the ladder floor 0.55)."""
    positions = [{"ticker": "SMH", "sleeve": "leadership", "current_weight": 0.28}]
    D = _wire_derisk_book(
        monkeypatch, positions, smh_closes, "2026-06-26",
        base_state={"state": "risk_off", "gross_cap": 0.55, "drivers": []},
    )
    monkeypatch.setattr(D, "_gex_flip", lambda: (True, "SPY dealers SHORT gamma"))
    tw = D.tripwire("flagship", "2026-06-26", regime={})
    assert tw["severity"] == 3            # max(risk_off 2, gex 2) = 2, +1 dist = 3 (clamped ceiling)
    assert tw["severity"] <= 3


def test_derisk_no_holdings_is_byte_identical(monkeypatch, smh_closes):
    """Empty book → no escalation → the tripwire is unchanged from today (byte-identical no-op)."""
    D = _wire_derisk_book(
        monkeypatch, [], smh_closes, "2026-06-26",
        base_state={"state": "risk_on", "gross_cap": 1.0, "drivers": []},
    )
    monkeypatch.setattr(D, "_gex_flip", lambda: (True, "SPY dealers SHORT gamma"))
    tw = D.tripwire("flagship", "2026-06-26", regime={})
    assert tw["severity"] == 2            # short-gamma only; no distribution bump
    assert not any("distribution:" in r for r in tw["reasons"])


def test_derisk_escalation_degrades_on_broken_holdings(monkeypatch, smh_closes):
    """If the holdings read raises, the escalator returns (0, '') and the tripwire is unaffected."""
    import types
    from bot import derisk as D
    from brain import macro_risk as real_mr
    monkeypatch.setattr(real_mr, "risk_state",
                        lambda a, r: {"state": "risk_on", "gross_cap": 1.0, "drivers": []},
                        raising=True)
    ov = types.ModuleType("data_layer.overnight")
    ov.tape = lambda force=False: {"risk": {"state": "calm"}}
    ov._fetch_changes = lambda syms: {}
    import data_layer as _dl
    monkeypatch.setattr(_dl, "overnight", ov, raising=False)
    monkeypatch.setitem(sys.modules, "data_layer.overnight", ov)
    monkeypatch.setattr(D, "_gex_flip", lambda: (True, "SPY dealers SHORT gamma"))
    monkeypatch.setattr(D, "_credit_gap", lambda: (False, ""))
    monkeypatch.setattr(D, "_theme_drop", lambda drivers: (False, ""))

    pl = types.ModuleType("portfolio.position_log")
    def _boom(portfolio_id=None):
        raise RuntimeError("holdings read failed")
    pl.open_positions = _boom
    import portfolio as _pf
    monkeypatch.setattr(_pf, "position_log", pl, raising=False)
    monkeypatch.setitem(sys.modules, "portfolio.position_log", pl)

    tw = D.tripwire("flagship", "2026-06-26", regime={})
    assert tw["severity"] == 2            # unaffected by the broken holdings read

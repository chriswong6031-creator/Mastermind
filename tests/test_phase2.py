"""Phase 2 acceptance — gated multi-name 3-sleeve book on real data."""
from pathlib import Path

import pytest

import bot  # noqa: F401

from bot import phase2
from data_layer import store

# ── W8 legacy-contract pin (2026-07-19): this file tests pre-W8 phase2 mechanics; the v2
# entry/context gates + feeds are covered by tests/test_flagship_v2_replay.py and
# tests/test_entry_context_engines.py. Pinned OFF here for deterministic legacy contracts.
import pytest as _pytest_w8


@_pytest_w8.fixture(autouse=True)
def _w8_legacy_env(monkeypatch):
    monkeypatch.setenv("MASTERMIND_ENTRY_GATE", "0")
    monkeypatch.setenv("MASTERMIND_PROPHET_FEED", "0")
    monkeypatch.setenv("MASTERMIND_ROTATION_IN", "off")
    monkeypatch.setenv("MASTERMIND_NW_DECISION", "off")
    try:
        from portfolio import prophet_feed as _pf
        _pf._reset_cache()
    except Exception:
        pass
    yield
    try:
        from portfolio import prophet_feed as _pf
        _pf._reset_cache()
    except Exception:
        pass


# NOTE: the store DB is isolated to a tmp file per test by the autouse `_isolate_bot_db`
# conftest fixture, which monkeypatches `store._DB`. `_fresh()` therefore reads `store._DB`
# at call time (not a frozen module constant) so its wipe hits the SAME isolated DB that
# `store.connect()` / `phase2.run()` use — and never the live data/bot.db.

# Live-data guard: parts of this suite assert against a REAL vendored render (per-name
# site/stockdata/*.json). On a checkout where the stockdata contract is absent (the known
# origin/main publish gap), the fail-closed coverage gate correctly returns
# size_authority='insufficient_data' for every name — asserting 'up' there would re-encode
# the fail-open bug this repo just fixed. Skip the live-data assertions instead.
_STOCKDATA = Path(__file__).resolve().parent.parent / "vendor" / "macro" / "site" / "stockdata"
_HAS_STOCKDATA = (_STOCKDATA / "NVDA.json").exists()
_needs_stockdata = pytest.mark.skipif(
    not _HAS_STOCKDATA, reason="vendored site/stockdata absent — fail-closed gate blocks all entries by design"
)


def _fresh():
    db = store._DB              # the tmp-isolated path (see _isolate_bot_db); never the live DB
    if db.exists():
        db.unlink()


@_needs_stockdata
def test_phase2_multiname_book_and_gate():
    _fresh()
    out = phase2.run()                       # first run -> fires
    assert out["ran"] and "first_run" in out["triggers"]

    book = out["book"]
    lead = [p for p in book if p["sleeve"] == "leadership"]
    conv = [p for p in book if p["sleeve"] == "conviction"]
    assert lead                                             # leadership sleeve always present

    # doctrine: PRESENT in the leaders mechanically. W2 CHANGE: the leadership gross is no longer the
    # old hardwired 0.50 midpoint — it is now regime-budget-scaled (regime_frame.budget(): on a low-
    # confidence / WEAKENING / late-cycle tape it flexes DOWN toward the 0.40 floor) AND per-leg
    # brake-capped (apply_leadership_caps: an over-extended or late_cycle NEW leg is shrunk, freed to
    # cash). So the honest assertion is that the sleeve is materially present, not that it clears the
    # old fixed-0.50 threshold — a floor of >0.3 would re-encode the un-braked, regime-blind budget.
    assert out["sleeves"]["leadership"] > 0.10
    # ...and it never exceeds the budget CEILING (0.60) — the budget equation's hard clamp.
    assert out["sleeves"]["leadership"] <= 0.60 + 1e-9
    # ...and conviction names are ONLY there because the multi-sided matrix confirmed all sides
    from portfolio import lenses
    for p in conv:
        syn = lenses.full(p["ticker"], "name")["synthesis"]
        assert syn["size_authority"] == "up" and not syn["vetoes"]   # confirmed + veto-clear
        assert p["weight"] <= 0.08 + 1e-9                            # name cap
    # NVDA is a cheap-for-growth leader (PEG ~0.25): after the valuation/13F alignment fix it
    # CLEARS the gate (no longer the raw-value-factor 'distribution' false-reject). It is eligible
    # for the conviction sleeve — the old 'NVDA must be excluded' assertion encoded the bug.
    # LIVE-DATA assertion, kept intent-only: NVDA must not be FALSELY rejected (the old raw-value-factor
    # 'distribution' bug read it as blocked). Its actual gate state ('up' vs hysteresis-'hold') tracks the
    # live tape and changed with the 2026-07-02 R2-synced vintage — pinning 'up' made the test flap.
    _nvda_sa = lenses.full("NVDA", "name")["synthesis"]["size_authority"]
    assert _nvda_sa in ("up", "hold"), f"NVDA falsely rejected: {_nvda_sa}"
    # genuine hard vetoes (parabolic / distress / cycle-blocked) still exclude a name from the book
    for p in conv:
        assert not lenses.full(p["ticker"], "name")["synthesis"]["vetoes"]
    assert out["sleeves"]["cash"] >= 0.05

    # the gate carries forward when nothing material changed
    out2 = phase2.run()
    assert out2["ran"] is False

    # a forced event interrupt re-runs
    assert phase2.run(force=True)["ran"] is True
    _fresh()


def test_store_roundtrip():
    _fresh()
    phase2.run()
    con = store.connect()
    # With stockdata present, conviction entries join the 4 leadership legs (>=5 rows).
    # Without it, the fail-closed gate freezes new conviction adds — leadership only (>=4).
    # W8 (2026-07-19): the un-flagged n_scored evidence floor makes conviction adds data-
    # dependent (a thin-vote name holds instead of entering), so the guaranteed floor is the
    # leadership sleeve. The roundtrip subject (write -> read back) is unchanged.
    _min_rows = 4
    _npos = con.execute("SELECT count(*) FROM positions").fetchone()[0]
    assert _npos >= _min_rows
    if _HAS_STOCKDATA and _npos > 4:  # theses exist only when a conviction entry actually landed
        assert con.execute("SELECT count(*) FROM theses").fetchone()[0] >= 1
    _fresh()


def test_runs_table_written_and_dedup():
    """record_run must write to the runs table so same-day re-runs carry forward.

    The empty-runs-table bug: when bot.db is wiped (e.g. by tests), store.last_run()
    returns None -> gate.should_run() always fires 'first_run' -> same-day dedup is
    broken and every intraday call triggers a full rebuild.  This test proves that a
    successful run writes exactly one row, and that a same-day second call reads that
    row and carries forward instead of rebuilding.
    """
    _fresh()
    # First run — must fire (first_run) AND write a row to the runs table.
    out = phase2.run()
    assert out["ran"] is True
    con = store.connect()
    lr = store.last_run(con)
    assert lr is not None, "record_run was not called: runs table is empty after a successful build"
    assert lr["ran"] == 1, "record_run wrote ran=False instead of True for a successful build"

    # Same-day second call — must carry forward (dedup) using the stored row, NOT rebuild.
    out2 = phase2.run()
    assert out2["ran"] is False, (
        "same-day dedup failed: gate.should_run fired a second rebuild even though "
        "today's run is already in the runs table"
    )
    _fresh()


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# W2 — OFFENSE BRAKES: budget consumption + apply_leadership_caps + guard rails
# ═══════════════════════════════════════════════════════════════════════════════════════════════
# These are FIXTURE-INJECTED (no live-data dependency): they exercise the exact composition the
# phase2 leadership block wires — regime_frame.budget() for the sleeve budget, then
# apply_leadership_caps() as the per-leg brake — so the two W2 levers are asserted together and the
# no-double-count / calm-tape / replay guarantees are pinned without a full armed phase2.run().
import json as _json
from datetime import datetime as _dt, timezone as _tz

from brain import regime_frame as _RF
from portfolio import sleeves as _SL


def _write_regime(tmp_path, payload, monkeypatch):
    p = tmp_path / "regime.json"
    p.write_text(_json.dumps(payload))
    monkeypatch.setitem(_RF._REGION_PATHS, "us", p)
    return p


def _lead_legs(weight_each, tickers=("XLK", "XLV", "XLF", "XLI")):
    """A phase2-style leadership book: equal-weight, all verdict='hold' (as phase2 builds them)."""
    return [{"ticker": t, "theme_id": t, "sleeve": "leadership", "stage": 2,
             "weight": weight_each, "verdict": "hold", "rs_pctile": 90} for t in tickers]


class TestW2BudgetConsumption:
    """The leadership budget is the ONE equation regime_frame.budget()['lead_budget'] — not 0.50."""

    def test_weakening_low_conf_budget_below_midpoint(self, tmp_path, monkeypatch):
        _write_regime(tmp_path, {"confidence": 0.327, "transition_state": "WEAKENING",
                                 "flip_condition": {"margin": 0.05}}, monkeypatch)
        out = _RF.budget("us")
        # 0.40 + 0.20*0.327*0.6*0.75 = 0.42943 — strictly below the old hardwired 0.50 midpoint.
        assert out["lead_budget"] < 0.50
        assert out["lead_budget"] == pytest.approx(0.42943)

    def test_phase2_imports_budget_from_regime_frame(self):
        """phase2's leadership block must consume regime_frame.budget (not sum(...)/2 unconditionally)."""
        src = Path(phase2.__file__).read_text()
        assert "regime_frame" in src and "budget(" in src
        assert '["lead_budget"]' in src or "['lead_budget']" in src


class TestW2CalmTapeInvariance:
    """Anti-compounding-shrink guarantee: on a CALM tape (conf>0.55, STABLE, wide flip_margin, no
    late_cycle, no extension breach) the brake stack must NOT shrink the leadership book — the built
    legs are byte-identical to the pre-brake (today's) legs and the budget flexes only UPWARD."""

    def test_calm_tape_budget_is_undamped_formula(self, tmp_path, monkeypatch):
        _write_regime(tmp_path, {"confidence": 0.60, "transition_state": "STABLE",
                                 "flip_condition": {"margin": 0.40}}, monkeypatch)
        out = _RF.budget("us")
        assert out["inputs"]["T"] == pytest.approx(1.0)   # STABLE → no transition damp
        assert out["inputs"]["F"] == pytest.approx(1.0)   # wide margin → no flip damp
        assert out["lead_budget"] == pytest.approx(0.52)  # 0.40 + 0.20*0.60 — flex UP, never a shrink

    def test_calm_tape_leadership_caps_byte_identical(self):
        """No extension breach + no late_cycle → apply_leadership_caps is a byte-identical no-op.

        Freeze the golden leadership legs; assert the brake pass leaves every weight untouched and frees
        ZERO to cash. This is the load-bearing anti-compounding-shrink assertion.
        """
        golden = _lead_legs(0.13)                              # 4 legs @ 0.13 (a calm-tape book)
        legs = [dict(g) for g in golden]
        out = _SL.apply_leadership_caps(
            legs, cycles={}, trend_fn=lambda t: {"pct_vs_200d": 10.0},  # +10% — under the 40 cap
            held={"XLK", "XLV", "XLF", "XLI"})                 # all held → cycle-exempt anyway
        assert out["freed_to_cash"] == 0.0
        assert out["brakes"] == []
        assert legs == golden                                  # BYTE-IDENTICAL to the pre-brake book
        assert not any("lead_capped" in p for p in legs)


class TestW2OffensiveGrossFloor:
    """The offensive-gross floor tripwire fires when the brake stack degrosses the sleeve below
    0.5·lead_budget (the enforcement CHOICE is a loud tripwire, not a proportional un-shrink)."""

    def test_tripwire_fires_when_over_degrossed(self):
        # lead_budget 0.4294 (replay) → floor 0.2147; a 0.15 gross book breaches.
        legs = [{"ticker": "XLK", "sleeve": "leadership", "weight": 0.08},
                {"ticker": "XLV", "sleeve": "leadership", "weight": 0.07}]
        tw = _SL.offensive_gross_tripwire(legs, lead_budget=0.42943)
        assert tw["breached"] is True
        assert "over_degross" in tw["reason"]

    def test_tripwire_silent_when_within_floor(self):
        legs = [{"ticker": "XLK", "sleeve": "leadership", "weight": 0.22}]
        tw = _SL.offensive_gross_tripwire(legs, lead_budget=0.42943)
        assert tw["breached"] is False


class TestW2ReplayFalsifier:
    """REPLAY 2026-06-30/07-01: SMH pct_vs_200d ~55, regime conf 0.327 WEAKENING, flip_margin 0.05.
    Assert the two W2 effects compose WITHOUT double-counting:
      (1) the leadership BUDGET shrinks below 0.50 (regime term), and
      (2) the SMH leg CLAMPS 0.125 → 0.08 (extension term),
    and the freed weight lands in cash, and the total shrink == the sum of the two effects.
    """

    def test_replay_budget_shrinks_below_midpoint(self, tmp_path, monkeypatch):
        _write_regime(tmp_path, {"date": "2026-07-01", "confidence": 0.327,
                                 "transition_state": "WEAKENING",
                                 "flip_condition": {"margin": 0.05}}, monkeypatch)
        out = _RF.budget("us")
        assert out["lead_budget"] == pytest.approx(0.42943)
        assert out["lead_budget"] < 0.50, "the WEAKENING low-conf tape must shrink the leadership budget"

    def test_replay_smh_leg_clamps_and_frees_to_cash(self):
        """SMH @ 0.125 with pct_vs_200d 55 (>40 cap) clamps to 0.08; the 0.045 freed lands in cash."""
        # a replay-shaped leadership book: SMH is the top risk line at 0.125 (the 06-22/07-01 config).
        legs = [{"ticker": "SMH", "theme_id": "SMH", "sleeve": "leadership", "weight": 0.125,
                 "verdict": "hold"},
                {"ticker": "XLK", "theme_id": "XLK", "sleeve": "leadership", "weight": 0.125,
                 "verdict": "hold"},
                {"ticker": "MTUM", "theme_id": "MTUM", "sleeve": "leadership", "weight": 0.125,
                 "verdict": "hold"},
                {"ticker": "IWM", "theme_id": "IWM", "sleeve": "leadership", "weight": 0.125,
                 "verdict": "hold"}]
        gross_before = sum(p["weight"] for p in legs)
        # SMH breaches; XLK/MTUM/IWM are 'normal' (under the 40 cap). All held → cycle-exempt, so ONLY
        # the extension effect fires here (isolating it from the budget effect — no double-count).
        trend = {"SMH": 55.0, "XLK": 20.0, "MTUM": 15.0, "IWM": 5.0}
        out = _SL.apply_leadership_caps(
            legs, cycles={}, trend_fn=lambda t: {"pct_vs_200d": trend.get(t)},
            held={"SMH", "XLK", "MTUM", "IWM"})
        smh = next(p for p in legs if p["ticker"] == "SMH")
        assert smh["weight"] == 0.08                                   # 0.125 → 0.08
        # the other three are untouched (no redistribution — freed weight is cash only)
        for tk in ("XLK", "MTUM", "IWM"):
            assert next(p for p in legs if p["ticker"] == tk)["weight"] == 0.125
        freed = round(0.125 - 0.08, 4)
        assert out["freed_to_cash"] == freed
        gross_after = sum(p["weight"] for p in legs)
        # the total shrink equals EXACTLY the single extension effect (no double-count with the budget)
        assert round(gross_before - gross_after, 4) == freed

    def test_replay_total_shrink_is_sum_of_two_effects_no_double_count(self, tmp_path, monkeypatch):
        """The full replay: BOTH the budget term AND the extension clamp, and the total book shrink vs
        today equals the SUM of the two — proving the regime signal is consumed exactly once (budget)
        and the extension signal exactly once (clamp), with no overlap.

        Today's (pre-W2) leadership gross = 0.50 (hardwired midpoint), equal-weight over 4 legs = 0.125
        each; SMH is the top line. W2:
          budget effect  = 0.50 - lead_budget(0.42943) = 0.07057 (spread across the 4 legs by re-sizing)
          extension eff. = the SMH clamp AT the new per-leg weight.
        We size the legs at the NEW budget, then clamp — and assert the composed gross equals
        (new budget) - (SMH extension freed), i.e. each effect is applied once, in order.
        """
        _write_regime(tmp_path, {"date": "2026-07-01", "confidence": 0.327,
                                 "transition_state": "WEAKENING",
                                 "flip_condition": {"margin": 0.05}}, monkeypatch)
        lead_budget = _RF.budget("us")["lead_budget"]
        assert lead_budget == pytest.approx(0.42943)

        # size the sleeve at the NEW budget (phase2: lw = lead_budget / n_leaders), equal-weight over 4
        lw = round(lead_budget / 4, 4)                                  # 0.1074 each
        legs = [{"ticker": t, "theme_id": t, "sleeve": "leadership", "weight": lw, "verdict": "hold"}
                for t in ("SMH", "XLK", "MTUM", "IWM")]
        gross_at_new_budget = sum(p["weight"] for p in legs)

        # SMH still breaches the extension cap at the new (smaller) weight 0.1074 > 0.08.
        trend = {"SMH": 55.0, "XLK": 20.0, "MTUM": 15.0, "IWM": 5.0}
        out = _SL.apply_leadership_caps(
            legs, cycles={}, trend_fn=lambda t: {"pct_vs_200d": trend.get(t)},
            held={"SMH", "XLK", "MTUM", "IWM"})

        smh = next(p for p in legs if p["ticker"] == "SMH")
        assert smh["weight"] == 0.08                                   # extension clamp at new weight
        ext_freed = round(lw - 0.08, 4)
        assert out["freed_to_cash"] == ext_freed

        gross_final = sum(p["weight"] for p in legs)
        # DECOMPOSITION (no double-count):
        #   budget effect    = 0.50 (today) - gross_at_new_budget
        #   extension effect = gross_at_new_budget - gross_final = ext_freed
        budget_effect = round(0.50 - gross_at_new_budget, 4)
        extension_effect = round(gross_at_new_budget - gross_final, 4)
        assert extension_effect == ext_freed
        total_shrink = round(0.50 - gross_final, 4)
        assert total_shrink == round(budget_effect + extension_effect, 4)

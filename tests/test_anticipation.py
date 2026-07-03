"""W-E.0 task E0.2 — the ANTICIPATION BATTERY tests (brain/anticipation.py).

Covers, per the judged design (research/eyes/judged_anticipation.md) and the build-plan §4.1 replay:
  * the three alarms — SECTOR-TOP / BUBBLE-FORMATION / CRASH-RISK — each with WATCH/ELEVATED/CRITICAL
    levels built from VENDORED planes only (v1);
  * AUTHORITY TIERS AS CODE: every alarm carries status/cold_start/notch_eligible, and notch_eligible
    is HARD-FALSE on every alarm in v1 (the severity-notch seam ships dark);
  * the degrade-never-fabricate invariant: missing/thin data drops a leg (absent), never fabricates a
    level, never flips notch_eligible True, never raises;
  * SECTOR-TOP reuses distribution_tells' MACD/crowding primitives (does not fork them) and carries the
    def/off rs_diff magnitude;
  * CRASH-RISK reads gex only to CLASSIFY (never notches off it) and reads the risk_radar drawdown-scare;
  * fixture-injection: the injected regime's sector_rs drives crowding (no leak from the live vendored
    file), and prices are injected via a pure prices_fn (the shared mutating store is never live-read);
  * the incident REPLAY battery (build-plan §4.1): SECTOR-TOP(tech/semis) >= ELEVATED and CRASH-RISK
    >= ELEVATED on the frozen incident fixtures; the CALM fixture keeps every alarm WATCH-or-below;
  * the artifact writer (atomic tmp+os.replace to data/anticipation/<asof>.json + latest.json).

No live regime/price/network is touched: every regime + sector_cycles is a frozen fixture dict, every
price series is injected via a pure fn.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

# the distribution primitives ride engine.canon from the vendored macro checkout
_ROOT = Path(__file__).resolve().parent.parent
_MACRO_SRC = _ROOT / "vendor" / "macro_src"
if _MACRO_SRC.exists() and str(_MACRO_SRC) not in sys.path:
    sys.path.insert(0, str(_MACRO_SRC))

from brain import anticipation as A  # noqa: E402

_FIX = _ROOT / "tests" / "fixtures" / "market_view"
_SMH_CSV = _ROOT / "tests" / "fixtures" / "distribution" / "SMH_june.csv"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def incident_regime() -> dict:
    return json.loads((_FIX / "regime_snapshot_incident.json").read_text())


@pytest.fixture
def calm_regime() -> dict:
    return json.loads((_FIX / "regime_snapshot_calm.json").read_text())


@pytest.fixture
def incident_cycles() -> dict:
    return json.loads((_FIX / "sector_cycles_incident.json").read_text())


@pytest.fixture
def calm_cycles() -> dict:
    """A benign sector_cycles frame — every sector in a mid-cycle Advancing phase (no topping legs)."""
    def _row(tk: str) -> dict:
        return {"ticker": tk, "now": {
            "phase": "Advance", "phaseLabel": "Advancing", "pos": 45.0, "signal": "HOLD",
            "osc_slope": 3.0, "above200d": True, "rs_63d": 1.2, "rs_126d": 0.8,
        }}
    return {"meta": {"asOf": "2026-05-15"},
            "sectors": [_row(t) for t in A._SECTOR_UNIVERSE if t != "SMH"],
            "baskets": [_row("SMH")]}


@pytest.fixture
def none_prices():
    """A prices_fn that always returns None → the distribution/crowding legs degrade to absent. Never
    live-reads the shared mutating price store."""
    return lambda ticker: None


@pytest.fixture
def smh_prices():
    """A prices_fn serving ONLY SMH's June path from the frozen csv (every other name → None)."""
    df = pd.read_csv(_SMH_CSV, parse_dates=["date"]).set_index("date")["close"].astype(float).sort_index()
    return lambda ticker: (df if str(ticker).upper() == "SMH" else None)


# ---------------------------------------------------------------------------
# authority tiers as code (the graft) — every alarm honest, notch seam DARK
# ---------------------------------------------------------------------------

def test_notch_eligible_hard_false_on_every_alarm(incident_regime, incident_cycles, none_prices):
    """In v1 NO leg is forward-graded → notch_eligible must be False on every single alarm (the
    severity notch arms only after the E1.4 AUC gate; the seam ships dark)."""
    b = A.battery(regime=incident_regime, sector_cycles=incident_cycles, prices_fn=none_prices)
    all_alarms = list(b["sector_top"]) + list(b["bubble_formation"]) + [b["crash_risk"]]
    assert all_alarms, "the incident fixture must produce at least one alarm"
    for a in all_alarms:
        assert a["notch_eligible"] is False, f"{a['kind']}/{a['scope']} must not be notch-eligible in v1"
        assert a["status"] == "advisory", f"{a['kind']} must be advisory in v1"
    # the program-level authority stamp is honest: no notch-eligible alarms
    assert b["authority"]["notch_eligible_alarms"] == []


def test_bubble_formation_is_cold_start_advisory_forever(incident_regime, incident_cycles):
    """BUBBLE-FORMATION has no forward-graded crowding artifact → cold_start=True + advisory, always.
    Feed it a regime whose sector_rs carries a crowded name so a bubble actually fires."""
    reg = dict(incident_regime)
    reg["sector_rs"] = [{"ticker": "XLK", "pctile_252d": 96.0}, {"ticker": "SMH", "pctile_252d": 95.0}]
    bubbles = A.bubble_formation(regime=reg, prices_fn=lambda t: None)
    assert bubbles, "a crowded sector_rs must produce a bubble alarm"
    for a in bubbles:
        assert a["cold_start"] is True
        assert a["status"] == "advisory"
        assert a["notch_eligible"] is False


def test_crash_risk_is_cold_start_and_reads_gex_readonly(incident_regime):
    """CRASH-RISK is cold_start advisory; the dealer_gamma leg is a READ-ONLY classification — its
    presence never flips notch_eligible (derisk owns gex for severity)."""
    reg = dict(incident_regime)
    reg["market_gamma"] = {"regime": "short", "spot_vs_flip_pct": -1.8, "asof": "2026-07-01"}
    c = A.crash_risk(regime=reg)
    assert c["cold_start"] is True
    assert c["status"] == "advisory"
    assert c["notch_eligible"] is False
    assert c["legs"]["dealer_gamma"] is True     # classified short-gamma below flip
    assert c["magnitude"]["gamma_regime"] == "short"


# ---------------------------------------------------------------------------
# SECTOR-TOP — reuses distribution primitives, carries rs_diff, degrade-safe
# ---------------------------------------------------------------------------

def test_sector_top_fires_on_topping_cycle(incident_regime, incident_cycles, none_prices):
    """A Peak/Topping + SELL + rolling-oscillator sector (XLK) fires SECTOR-TOP at CRITICAL off the
    cycle sub-legs alone (no price data needed)."""
    tops = A.sector_top(regime=incident_regime, sector_cycles=incident_cycles, prices_fn=none_prices)
    by = {a["scope"]: a for a in tops}
    assert "XLK" in by
    xlk = by["XLK"]
    assert xlk["level"] == "critical"
    assert xlk["legs"]["cycle_phase"] is True
    assert xlk["legs"]["cycle_extended"] is True
    # distribution leg is ABSENT (None) with no prices — never a fabricated False that reads as benign
    assert xlk["legs"]["distribution"] is None


def test_sector_top_carries_rs_diff_magnitude(incident_regime, incident_cycles):
    """SECTOR-TOP carries the shared def/off rs_diff magnitude (the 'by how much' payload). Inject the
    rs_diff so the test is deterministic and never live-reads the price store."""
    rs = {"diff": 0.093, "crossed": True, "def_rs": 0.08, "off_rs": -0.013}
    tops = A.sector_top(regime=incident_regime, sector_cycles=incident_cycles,
                        prices_fn=lambda t: None, rs_diff=rs)
    assert tops
    for a in tops:
        assert a["magnitude"]["rs_diff"] == 0.093
        assert a["magnitude"]["rs_diff_crossed"] is True


def test_sector_top_distribution_leg_reuses_dt_primitives(incident_cycles, smh_prices):
    """The distribution leg REUSES distribution_tells' crowding + 3D-MACD primitives (not a fork). With
    SMH's real June path injected AND a crowded board pctile, the distribution leg is determinable."""
    reg = {"date": "2026-07-01", "sector_rs": [{"ticker": "SMH", "pctile_252d": 95.0}]}
    tops = A.sector_top(sectors=["SMH"], regime=reg, sector_cycles=incident_cycles, prices_fn=smh_prices)
    assert tops
    smh = tops[0]
    # crowd_pctile came from the injected board pctile (0.95), so the leg is determinable (True/False)
    assert smh["legs"]["distribution"] in (True, False)
    assert smh["magnitude"]["crowd_pctile"] is not None


def test_sector_top_absent_when_no_cycle_and_no_prices(none_prices):
    """A sector with NO cycles block and NO price data produces NO alarm (degrade to calm, not a
    fabricated top). Empty cycles + None prices + no sector_rs → zero alarms."""
    reg = {"date": "2026-07-01"}
    tops = A.sector_top(regime=reg, sector_cycles={"sectors": [], "baskets": []}, prices_fn=none_prices)
    assert tops == []


# ---------------------------------------------------------------------------
# CRASH-RISK — vol structure + gex classification + credit/liquidity + radar
# ---------------------------------------------------------------------------

def test_crash_risk_credit_leg_injects_liquidity_label(incident_regime):
    """The credit_liquidity leg fires on a stress/hollow/contracting label; benign/unknown do NOT
    (unknown never counts — the invariant)."""
    stress = A.crash_risk(regime=incident_regime, liquidity_label="stress-expansion")
    assert stress["legs"]["credit_liquidity"] is True
    benign = A.crash_risk(regime=incident_regime, liquidity_label="benign-expansion")
    assert benign["legs"]["credit_liquidity"] is False
    unknown = A.crash_risk(regime=incident_regime, liquidity_label="unknown")
    assert unknown["legs"]["credit_liquidity"] is None   # unknown → absent, never fires


def test_crash_risk_gamma_leg_is_classification_only(incident_regime):
    """A short-gamma-below-flip regime classifies the dealer_gamma leg True; a benign long-gamma /
    above-flip regime classifies it False; absent market_gamma → None."""
    short = dict(incident_regime); short["market_gamma"] = {"regime": "short", "spot_vs_flip_pct": -2.0}
    assert A.crash_risk(regime=short)["legs"]["dealer_gamma"] is True
    long_g = dict(incident_regime); long_g["market_gamma"] = {"regime": "long", "spot_vs_flip_pct": 3.0}
    assert A.crash_risk(regime=long_g)["legs"]["dealer_gamma"] is False
    absent = dict(incident_regime); absent["market_gamma"] = None
    assert A.crash_risk(regime=absent)["legs"]["dealer_gamma"] is None


def test_crash_risk_reads_radar_drawdown_scare(incident_regime):
    """The drawdown_scare leg fires on a rising forward drawdown-prob under a growth scare (the
    incident's smoking-gun plane)."""
    c = A.crash_risk(regime=incident_regime)
    assert c["legs"]["drawdown_scare"] is True
    assert c["magnitude"]["dominant_scare"] == "growth"
    assert c["magnitude"]["drawdown_prob_h21"] == pytest.approx(0.19, abs=1e-6)


# ---------------------------------------------------------------------------
# degrade-never-fabricate / never-raise on empty inputs
# ---------------------------------------------------------------------------

def test_battery_never_raises_on_empty_inputs():
    """Empty regime + empty cycles + None prices → a legal, low-alarm battery. Never raises."""
    b = A.battery(regime={}, sector_cycles={}, prices_fn=lambda t: None,
                  liquidity_label=None, asof="2026-01-02")
    assert b["schema_version"] == 1
    assert b["asof"] == "2026-01-02"
    assert b["top_level"] in (A._CALM, A._WATCH, A._ELEVATED, A._CRITICAL)
    assert b["crash_risk"]["notch_eligible"] is False


def test_battery_asof_is_data_date_not_build_time(incident_regime, incident_cycles):
    """The battery stamps asof = the regime's OWN data date (07-01), not the build date (charter §6.5).
    built_at is separate."""
    b = A.battery(regime=incident_regime, sector_cycles=incident_cycles, prices_fn=lambda t: None)
    assert b["asof"] == "2026-07-01"
    assert "built_at" in b


# ---------------------------------------------------------------------------
# fixture-injection integrity — no leak from the live vendored regime
# ---------------------------------------------------------------------------

def test_board_pctile_does_not_leak_live_regime(calm_regime):
    """The crowding board-pctile must come from the INJECTED regime's sector_rs, never the live
    vendored file. The calm regime has no sector_rs → no board crowding → no bubbles (no live leak)."""
    board = A._board_pctile(calm_regime)
    assert board == {}   # calm regime carries no sector_rs; must NOT fall through to the live file
    bubbles = A.bubble_formation(regime=calm_regime, prices_fn=lambda t: None)
    assert bubbles == []


# ---------------------------------------------------------------------------
# THE INCIDENT REPLAY BATTERY (build-plan §4.1) — permanent CI memory
# ---------------------------------------------------------------------------

def test_replay_sector_top_tech_semis_elevated_or_higher(incident_regime, incident_cycles, none_prices):
    """build-plan §4.1: SECTOR-TOP(tech/semis) >= ELEVATED on the incident fixture (design evidence
    says ~06-19; CRITICAL 06-22..25). Intent-only: we assert the LEVEL bar, never pin a market state."""
    tops = A.sector_top(regime=incident_regime, sector_cycles=incident_cycles, prices_fn=none_prices)
    by = {a["scope"]: a for a in tops}
    for name in ("XLK", "SMH"):   # tech + the semis block
        assert name in by, f"{name} must produce a SECTOR-TOP alarm on the incident fixture"
        assert A._LEVEL_ORDER[by[name]["level"]] >= A._LEVEL_ORDER[A._ELEVATED], \
            f"{name} SECTOR-TOP must be >= ELEVATED (got {by[name]['level']})"


def test_replay_crash_risk_elevated_or_higher(incident_regime, incident_cycles, none_prices):
    """build-plan §4.1: CRASH-RISK >= ELEVATED on the incident fixture (the 06-26 bar). Driven by the
    radar drawdown-scare + vol structure — no live gex needed."""
    b = A.battery(regime=incident_regime, sector_cycles=incident_cycles, prices_fn=none_prices)
    c = b["crash_risk"]
    assert A._LEVEL_ORDER[c["level"]] >= A._LEVEL_ORDER[A._ELEVATED], \
        f"CRASH-RISK must be >= ELEVATED on the incident fixture (got {c['level']})"
    assert c["notch_eligible"] is False   # even when ELEVATED, the notch stays dark in v1


def test_replay_top_level_critical(incident_regime, incident_cycles, none_prices):
    """The whole-battery top_level is CRITICAL on the incident fixture (a topping sector reaches it)."""
    b = A.battery(regime=incident_regime, sector_cycles=incident_cycles, prices_fn=none_prices)
    assert b["top_level"] == A._CRITICAL


def test_replay_calm_tape_all_alarms_watch_or_below(calm_regime, calm_cycles, none_prices):
    """build-plan §4.1 calm control: on a benign agreeing tape EVERY alarm is WATCH-or-below (no
    topping sectors, no crash-risk escalation, no bubbles)."""
    b = A.battery(regime=calm_regime, sector_cycles=calm_cycles, prices_fn=none_prices)
    all_alarms = list(b["sector_top"]) + list(b["bubble_formation"]) + [b["crash_risk"]]
    for a in all_alarms:
        assert A._LEVEL_ORDER[a["level"]] <= A._LEVEL_ORDER[A._WATCH], \
            f"calm tape: {a['kind']}/{a['scope']} must be WATCH-or-below (got {a['level']})"
    assert A._LEVEL_ORDER[b["top_level"]] <= A._LEVEL_ORDER[A._WATCH]


# ---------------------------------------------------------------------------
# artifact writer — atomic, degrade-safe, no sizing touched
# ---------------------------------------------------------------------------

def test_write_battery_persists_asof_and_latest(tmp_path, incident_regime, incident_cycles, none_prices):
    """write_battery persists data/anticipation/<asof>.json AND latest.json atomically; returns the
    payload. No sizing is touched; the artifact is a pure perception record."""
    out = tmp_path / "anticipation"
    payload = A.write_battery(regime=incident_regime, sector_cycles=incident_cycles,
                              prices_fn=none_prices, out_dir=out)
    stamp = payload["asof"]
    dated = out / f"{stamp}.json"
    latest = out / "latest.json"
    assert dated.exists() and latest.exists()
    on_disk = json.loads(latest.read_text())
    assert on_disk["asof"] == stamp
    assert on_disk["schema_version"] == 1
    assert on_disk["crash_risk"]["notch_eligible"] is False
    # dated + latest are byte-identical snapshots of the same build
    assert json.loads(dated.read_text()) == on_disk


def test_write_battery_never_raises_on_bad_out_dir(incident_regime, incident_cycles, none_prices, tmp_path):
    """A write to an unwritable path degrades to a no-op write but STILL returns the payload (degrade-
    never-fabricate)."""
    bad = tmp_path / "afile"
    bad.write_text("i am a file, not a dir")
    payload = A.write_battery(regime=incident_regime, sector_cycles=incident_cycles,
                              prices_fn=none_prices, out_dir=bad / "sub")
    assert payload["schema_version"] == 1   # payload returned even though the write no-oped

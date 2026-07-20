"""W8 ACCEPTANCE — the 2026-07-13..16 bad-buy replay (design §4.1).

Fixtures are REAL slices of the vendored 2026-07-17/18 macro artifacts (snapshotted under
``tests/fixtures/flagship_v2_replay/``): per-ticker close series, stockdata tech blocks,
signal_gate verdict rows, plus the shared sector_pulse / theme_context / contagion lobe /
momentum_display / risk-state context. The battery pins the v2 triad's verdicts on the exact
names the operator called out:

    AAPL  bought +21% off a 3-week low at the range top   → entry adverse + context blocked
    ANET  extended, topping, signaling correction          → entry adverse + context blocked
    SMH   semis complex broken, Korea/TW/CN risk-off       → context BLOCKED
    XLK   contagion spreading semis → tech                 → context BLOCKED
    MTUM  bought right after a momentum unwind             → context BLOCKED (unwind)
    QUAL/SCHW/V/RF  extended, oscillators rolling over     → entry adverse (park)
    XLE   the one good buy (bottomed sector turning)       → entry BUYABLE + context NOT blocked

Everything runs OFFLINE (``_use_defaults=False`` + injected fixtures) — no vendor checkout, no
network. If a fixture is missing the battery FAILS (it is the acceptance gate, not fail-open).
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from portfolio import context_gate as cg
from portfolio import entry_engine as ee

FIX = Path(__file__).resolve().parent / "fixtures" / "flagship_v2_replay"

BAD_BUYS = ["AAPL", "ANET", "RF", "SCHW", "V", "SMH", "XLK", "MTUM", "QUAL"]
ETFS = {"SMH", "XLK", "MTUM", "QUAL", "XLE"}
ADVERSE = {"extended", "chase", "late_leg", "rollover", "knife", "missed_move",
           "extended_vs_plan"}


def _shared():
    return {
        "pulse": json.loads((FIX / "sector_pulse.json").read_text()),
        "theme_ctx": json.loads((FIX / "theme_context.json").read_text()),
        "contagion": json.loads((FIX / "contagion.json").read_text()),
        "momentum": json.loads((FIX / "momentum_display.json").read_text()),
        "risk": json.loads((FIX / "risk.json").read_text()),
    }


def _assess(t: str):
    d = json.loads((FIX / f"{t}.json").read_text())
    sh = _shared()
    s = pd.Series(d["closes"]) if d["closes"] else None
    rep = ee.assess(t, series=s, stockdata={"tech": d["tech"]},
                    signal_gate_row=d["signal_gate_row"], bottom_state=d.get("bottom_state"),
                    pulse=sh["pulse"], theme_id=d.get("theme_id"), prophet_plan=None,
                    _use_defaults=False)
    is_etf = t in ETFS
    crep = cg.assess(
        t, sector=None if is_etf else d.get("sector"), theme_id=d.get("theme_id"),
        entry_verdict=rep["verdict"],
        entry_tier_ok=bool(rep["metrics"].get("tier_fresh")
                           or rep["metrics"].get("tier_eligible")),
        is_etf=is_etf,
        stockdata={"sector": d.get("sector"),
                   "sector_pulse": {"theme_id": d.get("theme_id")}},
        pulse=sh["pulse"], theme_ctx=sh["theme_ctx"], contagion=sh["contagion"],
        momentum=sh["momentum"], risk=sh["risk"], _use_defaults=False)
    return rep, crep


def _would_buy(rep: dict, crep: dict) -> bool:
    """The triad's composed decision for a NEW entry (mirrors conviction.build §2.5): entry must
    be buyable (or unknown — fail-open) AND context must not be blocked."""
    entry_ok = rep["buyable"] or rep["verdict"] == "unknown"
    return entry_ok and crep["verdict"] != "blocked"


# ── the nine bad buys: every one must be stopped by the triad ────────────────────────────────────
@pytest.mark.parametrize("ticker", BAD_BUYS)
def test_bad_buy_is_stopped(ticker):
    rep, crep = _assess(ticker)
    assert not _would_buy(rep, crep), (
        f"{ticker} would still be bought (entry={rep['verdict']}, context={crep['verdict']}) — "
        f"the 2026-07-13..16 failure class survives")


# ── pinned mechanisms (the WHY, not just the outcome; recalibrate only with fresh fixtures) ─────
def test_aapl_stopped_at_the_top():
    rep, crep = _assess("AAPL")
    assert rep["verdict"] in ADVERSE and not rep["buyable"]
    m = rep["metrics"]
    assert m["range_pctile_60d"] is not None and m["range_pctile_60d"] >= 95  # AT the range top
    assert m["ret_from_63d_low"] is not None and m["ret_from_63d_low"] >= 0.20  # +21%+ off the low
    assert crep["verdict"] == "blocked"          # tech downstream of the broken ai_hardware complex


def test_smh_blocked_by_broken_complex():
    rep, crep = _assess("SMH")
    assert crep["verdict"] == "blocked"
    joined = " ".join(crep["reasons"]).lower()
    assert "broken" in joined or "contagion" in joined


def test_xlk_blocked_even_on_clean_entry():
    rep, crep = _assess("XLK")
    # the ENTRY may legitimately read clean after the pullback — the CONTEXT is what must stop it
    assert crep["verdict"] == "blocked"


def test_mtum_blocked_by_unwind():
    rep, crep = _assess("MTUM")
    assert crep["verdict"] == "blocked"
    assert any("unwind" in r.lower() for r in crep["reasons"])


def test_extended_cohort_parks():
    for t in ("QUAL", "V", "SCHW", "RF"):
        rep, _ = _assess(t)
        assert rep["verdict"] in ADVERSE and not rep["buyable"], (t, rep["verdict"])
        assert rep["park_triggers"], f"{t} parked without promotion triggers"


# ── XLE: the one good buy must PASS (the positive path the strength-biased gate never had) ──────
def test_xle_passes_the_triad():
    rep, crep = _assess("XLE")
    assert rep["buyable"], f"XLE entry read {rep['verdict']} — the good buy is being refused"
    assert crep["verdict"] != "blocked"
    # its tier is the ONLY eligible one in the fixture set — the dashboard's validated entry gate
    assert rep["metrics"]["tier_eligible"] is True


def test_xle_is_the_only_pass():
    passes = [t for t in BAD_BUYS + ["XLE"] if _would_buy(*_assess(t))]
    assert passes == ["XLE"], f"triad passes {passes}, expected only XLE"


# ── RF-class thin-evidence floor (D3): 3 votes can no longer authorize 'up' ─────────────────────
def test_thin_vote_floor_blocks_rf_class_entry():
    from portfolio import lenses

    def _rows(n_bull):
        rows = [{"lens": f"l{i}", "value": {}, "status": "context", "direction": "bull",
                 "note": ""} for i in range(n_bull)]
        rows.append({"lens": "conviction", "value": {"stockdata_present": True},
                     "status": "partial", "direction": "neutral", "note": ""})
        return {"subject": "RF", "kind": "name", "rows": rows}

    thin = lenses.synthesize(_rows(3))       # the real RF case: 3 bull / 0 bear → confluence 1.0
    assert thin["confluence"] == 1.0         # the mirage is still visible…
    assert thin["thin_evidence"] is True
    assert thin["size_authority"] != "up"    # …but can no longer open a position
    wide = lenses.synthesize(_rows(6))       # six real votes → a legitimate 'up'
    assert wide["size_authority"] == "up"

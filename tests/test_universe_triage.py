"""tests/test_universe_triage.py — the composition ruleset + fail-soft behaviour of universe_triage.

The organ COMPOSES three upstream perception readers into a per-sector verdict. Every case here
monkeypatches those readers (regime_frame.cycles, the rotation_tensor artifact read, and the NW
decision reader) to inject synthetic inputs — no live vendor state is touched. The verdicts() reader
tests point _ARTIFACT_PATH at a tmp file and call _reset_cache() around each so the process cache
never leaks between cases.

Coverage:
  * a bottoming+rising sector (entry_favored, osc_slope>0) → action 'favor';
  * a topping+rolling sector (late_cycle, osc_slope<0 or accel<0) → action 'reduce';
  * a quiet / unmapped sector → 'neutral';
  * missing tensor / NW inputs → those fields null AND action still resolves sanely
    (never manufacture favor/reduce from absence);
  * NW off → nw_stance null, action unaffected;
  * a strongly-negative tensor accel VETOes a favor (the contradiction guard);
  * verdicts() fail-soft: absent / stale / wrong-schema artifact → {} (no raise);
  * round-trip assemble → write_artifact → verdicts;
  * audit_row + the convenience readers (sector_action / favored_sectors / reduce_sectors).
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from brain import universe_triage as ut


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _reset_around_each():
    ut._reset_cache()
    yield
    ut._reset_cache()


def _patch_cycles(monkeypatch, cyc: dict) -> None:
    """Point regime_frame.cycles() at a synthetic dict (imported lazily inside _read_cycles)."""
    import brain.regime_frame as rf
    monkeypatch.setattr(rf, "cycles", lambda: cyc)


def _patch_tensor(monkeypatch, tmp_path: Path, tensor_artifact: dict | None) -> None:
    """Write a synthetic rotation_tensor artifact (or nothing) and point the reader at it."""
    tp = tmp_path / "rotation_tensor.json"
    if tensor_artifact is not None:
        tp.write_text(json.dumps(tensor_artifact))
    else:
        tp = tmp_path / "no_tensor.json"  # deliberately absent
    monkeypatch.setattr(ut, "_TENSOR_PATH", tp)


def _patch_nw(monkeypatch, *, mode: str = "off", stance_by_sector: dict | None = None) -> None:
    """Patch the NW decision mode + the per-name stance the organ reads.

    mode='off' → the organ never reads a stance (disarmed). Any other mode arms it; we then stub
    neural_web_context.decision_signals to return a synthetic signal per sector.
    """
    import brain.neural_web_context as nw
    monkeypatch.setattr(nw, "nw_decision_mode", lambda: mode)
    sbs = stance_by_sector or {}

    def _fake_signals(ticker: str) -> dict:
        s = sbs.get(str(ticker).upper())
        if s == "favor":
            return {"candidacy": {"state": "BOTTOMING", "score": 0.5, "lean": 1},
                    "entry_shrink": None, "clean_in_conflicted": False, "inert": False, "mode": mode}
        if s == "clean":
            return {"candidacy": None, "entry_shrink": None, "clean_in_conflicted": True,
                    "inert": False, "mode": mode}
        # inert / no stance
        return {"candidacy": None, "entry_shrink": None, "clean_in_conflicted": False,
                "inert": True, "mode": mode}

    monkeypatch.setattr(nw, "decision_signals", _fake_signals)


def _patch_no_etf_pulse(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ut, "_ETF_PULSE_PATH", tmp_path / "no_etf_pulse.json")


def _fresh_tensor(level: dict, accel: dict, days_old: int = 0,
                  headline: dict | None = None) -> dict:
    """A minimal rotation_tensor artifact with as_of `days_old` calendar days before today."""
    return {
        "schema_version": 1,
        "as_of": (date.today() - timedelta(days=days_old)).isoformat(),
        "rs_velocity": {"level_bps_per_day": level, "accel_bps_per_day": accel},
        "headline_episode": headline,
    }


# --------------------------------------------------------------------------- #
# the action ruleset — favor / reduce / neutral
# --------------------------------------------------------------------------- #

def test_bottoming_rising_sector_favors(monkeypatch, tmp_path):
    # XLV: entry_favored + osc_slope>0 (bottoming+rising), tensor accel positive (confirming).
    _patch_cycles(monkeypatch, {
        "XLV": {"phase": "Recovery", "osc_slope": 0.8, "entry_favored": True, "late_cycle": False},
    })
    _patch_tensor(monkeypatch, tmp_path, _fresh_tensor(level={"XLV": 12.0}, accel={"XLV": 3.0}))
    _patch_nw(monkeypatch, mode="off")
    _patch_no_etf_pulse(monkeypatch, tmp_path)

    art = ut.assemble()
    row = art["sectors"]["XLV"]
    assert row["action"] == "favor"
    assert row["entry_favored"] is True
    assert row["osc_slope"] == 0.8
    assert row["tensor_accel"] == 3.0
    assert row["rotation_in"] is True  # favor + confirming positive accel
    assert "entry_favored" in row["why"]


def test_topping_rolling_sector_reduces_on_osc(monkeypatch, tmp_path):
    # XLK: late_cycle + osc_slope<0 (topping+rolling) → reduce, even with no tensor.
    _patch_cycles(monkeypatch, {
        "XLK": {"phase": "Peak", "osc_slope": -0.5, "entry_favored": False, "late_cycle": True},
    })
    _patch_tensor(monkeypatch, tmp_path, None)  # tensor absent
    _patch_nw(monkeypatch, mode="off")
    _patch_no_etf_pulse(monkeypatch, tmp_path)

    art = ut.assemble()
    row = art["sectors"]["XLK"]
    assert row["action"] == "reduce"
    assert row["tensor_accel"] is None  # tensor absent → null field
    assert "late_cycle" in row["why"]


def test_topping_reduces_on_negative_tensor_accel(monkeypatch, tmp_path):
    # XLU: late_cycle True but osc_slope missing; a negative tensor accel alone drives the reduce.
    _patch_cycles(monkeypatch, {
        "XLU": {"phase": "Downturn", "osc_slope": None, "entry_favored": False, "late_cycle": True},
    })
    _patch_tensor(monkeypatch, tmp_path, _fresh_tensor(level={"XLU": -4.0}, accel={"XLU": -2.0}))
    _patch_nw(monkeypatch, mode="off")
    _patch_no_etf_pulse(monkeypatch, tmp_path)

    art = ut.assemble()
    row = art["sectors"]["XLU"]
    assert row["action"] == "reduce"
    assert any("tensor_accel<0" in w for w in row["why"])


def test_quiet_sector_is_neutral(monkeypatch, tmp_path):
    # XLB: mid-cycle, not entry_favored, not late_cycle → neutral.
    _patch_cycles(monkeypatch, {
        "XLB": {"phase": "Expansion", "osc_slope": 0.1, "entry_favored": False, "late_cycle": False},
    })
    _patch_tensor(monkeypatch, tmp_path, _fresh_tensor(level={"XLB": 1.0}, accel={"XLB": 0.2}))
    _patch_nw(monkeypatch, mode="off")
    _patch_no_etf_pulse(monkeypatch, tmp_path)

    art = ut.assemble()
    row = art["sectors"]["XLB"]
    assert row["action"] == "neutral"
    assert row["rotation_in"] is False


def test_unmapped_sector_degrades_to_neutral(monkeypatch, tmp_path):
    # cycles() returns NOTHING for XLE — every field null, action neutral (absence → neutral).
    _patch_cycles(monkeypatch, {})  # no sectors at all
    _patch_tensor(monkeypatch, tmp_path, None)
    _patch_nw(monkeypatch, mode="off")
    _patch_no_etf_pulse(monkeypatch, tmp_path)

    art = ut.assemble()
    row = art["sectors"]["XLE"]
    assert row["action"] == "neutral"
    assert row["phase"] is None
    assert row["osc_slope"] is None
    assert row["entry_favored"] is None
    assert row["late_cycle"] is None
    assert row["tensor_level"] is None
    assert row["tensor_accel"] is None
    assert row["nw_stance"] is None


def test_favor_vetoed_by_strong_negative_tensor_accel(monkeypatch, tmp_path):
    # XLY: bottoming+rising cycle BUT the tape is aggressively fading it (accel << 0) → veto to neutral.
    _patch_cycles(monkeypatch, {
        "XLY": {"phase": "Trough", "osc_slope": 1.2, "entry_favored": True, "late_cycle": False},
    })
    strong_neg = -(ut._STRONG_NEG_ACCEL + 1.0)
    _patch_tensor(monkeypatch, tmp_path, _fresh_tensor(level={"XLY": 2.0}, accel={"XLY": strong_neg}))
    _patch_nw(monkeypatch, mode="off")
    _patch_no_etf_pulse(monkeypatch, tmp_path)

    art = ut.assemble()
    row = art["sectors"]["XLY"]
    assert row["action"] == "neutral"
    assert any("vetoed" in w for w in row["why"])
    assert row["rotation_in"] is False


# --------------------------------------------------------------------------- #
# missing-input fail-soft — absence never manufactures favor/reduce
# --------------------------------------------------------------------------- #

def test_missing_tensor_fields_are_null_and_action_sane(monkeypatch, tmp_path):
    # entry_favored+osc_slope>0 → favor stands WITHOUT any tensor (absence doesn't cancel a valid favor).
    _patch_cycles(monkeypatch, {
        "XLV": {"phase": "Recovery", "osc_slope": 0.6, "entry_favored": True, "late_cycle": False},
    })
    _patch_tensor(monkeypatch, tmp_path, None)  # tensor totally absent
    _patch_nw(monkeypatch, mode="off")
    _patch_no_etf_pulse(monkeypatch, tmp_path)

    art = ut.assemble()
    row = art["sectors"]["XLV"]
    assert row["tensor_level"] is None
    assert row["tensor_accel"] is None
    assert row["action"] == "favor"       # favor resolves without tensor
    assert row["rotation_in"] is False    # but rotation_in needs a confirming +accel → False on absence
    assert art["sources_fresh"]["tensor"] is False


def test_stale_tensor_treated_as_absent(monkeypatch, tmp_path):
    # A tensor older than the freshness budget must be dropped (its accel must NOT drive a reduce).
    _patch_cycles(monkeypatch, {
        "XLK": {"phase": "Peak", "osc_slope": 0.4, "entry_favored": False, "late_cycle": False},
    })
    _patch_tensor(monkeypatch, tmp_path,
                  _fresh_tensor(level={"XLK": 5.0}, accel={"XLK": -9.0}, days_old=ut._STALE_DAYS + 3))
    _patch_nw(monkeypatch, mode="off")
    _patch_no_etf_pulse(monkeypatch, tmp_path)

    art = ut.assemble()
    row = art["sectors"]["XLK"]
    assert row["tensor_accel"] is None       # stale tensor dropped
    assert row["action"] == "neutral"        # not late_cycle, no valid favor → neutral
    assert art["sources_fresh"]["tensor"] is False


# --------------------------------------------------------------------------- #
# NW gating — off → null stance + no effect; armed → stance surfaced (but not action-driving)
# --------------------------------------------------------------------------- #

def test_nw_off_stance_null_and_action_unaffected(monkeypatch, tmp_path):
    _patch_cycles(monkeypatch, {
        "XLV": {"phase": "Recovery", "osc_slope": 0.6, "entry_favored": True, "late_cycle": False},
    })
    _patch_tensor(monkeypatch, tmp_path, _fresh_tensor(level={"XLV": 3.0}, accel={"XLV": 1.0}))
    _patch_nw(monkeypatch, mode="off", stance_by_sector={"XLV": "favor"})  # stance present but disarmed
    _patch_no_etf_pulse(monkeypatch, tmp_path)

    art = ut.assemble()
    row = art["sectors"]["XLV"]
    assert row["nw_stance"] is None            # off → never read
    assert row["action"] == "favor"           # action came purely from cycle+tensor
    assert art["sources_fresh"]["nw"] is False


def test_nw_armed_surfaces_stance_without_changing_action(monkeypatch, tmp_path):
    # A quiet sector (neutral by cycle) with an armed NW 'favor' stance: the stance is RECORDED but
    # does NOT flip the action (this producer never lets NW drive the action rule).
    _patch_cycles(monkeypatch, {
        "XLF": {"phase": "Expansion", "osc_slope": 0.05, "entry_favored": False, "late_cycle": False},
    })
    _patch_tensor(monkeypatch, tmp_path, _fresh_tensor(level={"XLF": 1.0}, accel={"XLF": 0.1}))
    _patch_nw(monkeypatch, mode="candidacy", stance_by_sector={"XLF": "favor"})
    _patch_no_etf_pulse(monkeypatch, tmp_path)

    art = ut.assemble()
    row = art["sectors"]["XLF"]
    assert row["nw_stance"] == "favor"        # surfaced when armed
    assert row["action"] == "neutral"         # but action unchanged
    assert art["sources_fresh"]["nw"] is True


# --------------------------------------------------------------------------- #
# verdicts() reader — fail-soft + round-trip
# --------------------------------------------------------------------------- #

def test_verdicts_absent_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(ut, "_ARTIFACT_PATH", tmp_path / "does_not_exist.json")
    ut._reset_cache()
    assert ut.verdicts() == {}


def test_verdicts_stale_returns_empty(monkeypatch, tmp_path):
    art = {
        "schema": ut._SCHEMA,
        "as_of": (date.today() - timedelta(days=ut._STALE_DAYS + 2)).isoformat(),
        "sectors": {"XLV": {"action": "favor"}},
        "sources_fresh": {},
    }
    p = tmp_path / "latest.json"
    p.write_text(json.dumps(art))
    monkeypatch.setattr(ut, "_ARTIFACT_PATH", p)
    ut._reset_cache()
    assert ut.verdicts() == {}


def test_verdicts_wrong_schema_returns_empty(monkeypatch, tmp_path):
    p = tmp_path / "latest.json"
    p.write_text(json.dumps({"schema": "something_else", "as_of": date.today().isoformat(),
                             "sectors": {}}))
    monkeypatch.setattr(ut, "_ARTIFACT_PATH", p)
    ut._reset_cache()
    assert ut.verdicts() == {}


def test_verdicts_malformed_returns_empty(monkeypatch, tmp_path):
    p = tmp_path / "latest.json"
    p.write_text("{not valid json")
    monkeypatch.setattr(ut, "_ARTIFACT_PATH", p)
    ut._reset_cache()
    assert ut.verdicts() == {}


def test_write_artifact_fails_soft_never_raises(monkeypatch, tmp_path):
    # Point the artifact dir at a path that cannot be created (a file where a dir is expected).
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir")
    monkeypatch.setattr(ut, "_ARTIFACT_DIR", blocker / "nested")
    monkeypatch.setattr(ut, "_ARTIFACT_PATH", blocker / "nested" / "latest.json")
    # must return None, never raise
    assert ut.write_artifact({"schema": ut._SCHEMA, "as_of": date.today().isoformat(),
                              "sectors": {}}) is None


def test_round_trip_assemble_write_verdicts(monkeypatch, tmp_path):
    _patch_cycles(monkeypatch, {
        "XLV": {"phase": "Recovery", "osc_slope": 0.9, "entry_favored": True, "late_cycle": False},
        "XLK": {"phase": "Peak", "osc_slope": -0.7, "entry_favored": False, "late_cycle": True},
        "XLB": {"phase": "Expansion", "osc_slope": 0.1, "entry_favored": False, "late_cycle": False},
    })
    _patch_tensor(monkeypatch, tmp_path,
                  _fresh_tensor(level={"XLV": 8.0, "XLK": -3.0},
                                accel={"XLV": 2.0, "XLK": -1.0},
                                headline={"axis": "DEF_over_OFF", "direction": "defensive"}))
    _patch_nw(monkeypatch, mode="off")
    _patch_no_etf_pulse(monkeypatch, tmp_path)

    # point the artifact at tmp + write
    monkeypatch.setattr(ut, "_ARTIFACT_DIR", tmp_path / "art")
    monkeypatch.setattr(ut, "_ARTIFACT_PATH", tmp_path / "art" / "latest.json")

    art = ut.assemble()
    out = ut.write_artifact(art)
    assert out is not None and out.exists()

    ut._reset_cache()
    v = ut.verdicts()
    assert v["schema"] == ut._SCHEMA
    assert v["sectors"]["XLV"]["action"] == "favor"
    assert v["sectors"]["XLK"]["action"] == "reduce"
    assert v["sectors"]["XLB"]["action"] == "neutral"
    assert v["headline_episode"]["direction"] == "defensive"

    # convenience readers off the round-tripped artifact
    assert "XLV" in ut.favored_sectors()
    assert "XLK" in ut.reduce_sectors()
    assert ut.sector_action("XLV") == "favor"
    assert ut.sector_action("XLK") == "reduce"
    assert ut.sector_action("XLB") == "neutral"
    assert ut.sector_action("ZZZZ") == "neutral"   # unknown sector → neutral


# --------------------------------------------------------------------------- #
# convenience readers + audit_row — fail-soft when the artifact is absent
# --------------------------------------------------------------------------- #

def test_convenience_readers_empty_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(ut, "_ARTIFACT_PATH", tmp_path / "does_not_exist.json")
    ut._reset_cache()
    assert ut.favored_sectors() == []
    assert ut.reduce_sectors() == []
    assert ut.sector_action("XLV") == "neutral"


def test_audit_row_present(monkeypatch, tmp_path):
    art = {
        "schema": ut._SCHEMA,
        "as_of": date.today().isoformat(),
        "sectors": {s: {"action": "neutral"} for s in ("XLV", "XLK", "XLB")},
        "sources_fresh": {"cycles": True, "tensor": False, "nw": False, "etf_pulse": False},
    }
    p = tmp_path / "latest.json"
    p.write_text(json.dumps(art))
    monkeypatch.setattr(ut, "_ARTIFACT_PATH", p)
    ut._reset_cache()

    row = ut.audit_row()
    assert row["status"] == "present"
    assert row["n_sectors"] == 3
    assert row["sources_fresh"]["cycles"] is True


def test_audit_row_stale(monkeypatch, tmp_path):
    art = {
        "schema": ut._SCHEMA,
        "as_of": (date.today() - timedelta(days=ut._STALE_DAYS + 1)).isoformat(),
        "sectors": {"XLV": {"action": "neutral"}},
        "sources_fresh": {},
    }
    p = tmp_path / "latest.json"
    p.write_text(json.dumps(art))
    monkeypatch.setattr(ut, "_ARTIFACT_PATH", p)
    ut._reset_cache()

    row = ut.audit_row()
    assert row["status"] == "stale"
    assert row["n_sectors"] == 1


def test_audit_row_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(ut, "_ARTIFACT_PATH", tmp_path / "does_not_exist.json")
    ut._reset_cache()
    row = ut.audit_row()
    assert row["status"] == "absent"
    assert row["n_sectors"] == 0


def test_assemble_never_raises_on_reader_failure(monkeypatch, tmp_path):
    # A cycles() that RAISES must not sink assemble — it degrades every sector toward neutral.
    import brain.regime_frame as rf

    def _boom():
        raise RuntimeError("cycles exploded")

    monkeypatch.setattr(rf, "cycles", _boom)
    _patch_tensor(monkeypatch, tmp_path, None)
    _patch_nw(monkeypatch, mode="off")
    _patch_no_etf_pulse(monkeypatch, tmp_path)

    art = ut.assemble()  # must not raise
    assert art["schema"] == ut._SCHEMA
    # every sector present and neutral
    assert set(art["sectors"].keys()) == set(ut._SECTOR_UNIVERSE)
    assert all(r["action"] == "neutral" for r in art["sectors"].values())

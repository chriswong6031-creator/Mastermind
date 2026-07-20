"""Guards for the Prophet feed — the ADDITIVE candidate source + plan-geometry chase guard
(``portfolio.prophet_feed``).

Fully offline: no vendor/macro engine, no network. The vendored artifact is faked by writing a
fixture ``index.json`` to ``tmp_path`` and monkeypatching ``_ARTIFACT_PATH`` (+ ``_V``) at it, then
resetting the per-process cache. The load-bearing properties these tests pin:

  * ADDITIVE / SIZES-NOTHING: the module only ever SOURCES tickers or DESCRIBES geometry — the
    filtering (BULL-only, active-phase, equity-ticker), the candidate ordering/cap/age-cut, the
    freshest-plan selection, and the six entry-discipline statuses.
  * FAIL-OPEN + STALE-GATE: an absent / malformed / stale artifact, or the flag OFF, makes every
    public function inert (``{} / [] / None / no_plan``) and NEVER raises — a missing signal informs
    nothing and can never fabricate a plan or a veto.
  * REAL-SCHEMA FIDELITY: fixtures use the REAL shapes — flat ``targets: [t1, t2]`` with labelled
    levels in a separate ``profit_plan: [{level, label, ...}]``, and the underscore-prefixed
    ``_signal_date`` / ``_conviction_score`` fields — so the normalization is exercised as it runs
    against production.

Dates are computed RELATIVE to today (the staleness / age gates key on ``date.today()``), so the
fixtures never rot.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from portfolio import prophet_feed as pf


# ── fixture helpers ───────────────────────────────────────────────────────────
def _dstr(days_ago: int) -> str:
    """An ISO YYYY-MM-DD string `days_ago` calendar days before today."""
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _plan(asset, *, direction="BULL", entry, invalidation, t1, t2, trigger,
          conviction, phase, action, signal_days_ago, plan_id=None,
          profit_plan=None, targets=None):
    """Build a raw plan dict in the REAL prophet.index/v1 shape.

    By default `targets` is the flat [t1, t2] price list and `profit_plan` carries the labelled
    levels ({level, label, ...}) — exactly as the vendored artifact does. Either can be overridden
    to probe the label-vs-index fallback."""
    pid = plan_id or f"{asset}-{direction}-{_dstr(signal_days_ago).replace('-', '')}"
    if targets is None:
        targets = [t1, t2]
    if profit_plan is None:
        profit_plan = [
            {"level": t1, "label": "T1", "action": "Scale out 40%", "status": "ACTIVE"},
            {"level": t2, "label": "T2", "action": "Close remaining", "status": "PENDING"},
        ]
    # cosmetic-only metadata; the module never reads _r_unit — guard so the bad-numeric fixture
    # (entry/invalidation deliberately non-numeric) can still be constructed.
    try:
        r_unit = round(float(entry) - float(invalidation), 4)
    except (TypeError, ValueError):
        r_unit = None
    return {
        "id": pid, "asset": asset, "direction": direction, "entry": entry,
        "invalidation": invalidation, "targets": targets, "trigger": trigger,
        "option_contract": None, "_r_unit": r_unit,
        "_conviction_score": conviction, "_signal_date": _dstr(signal_days_ago),
        "phase": phase, "management_confidence": 55.0, "recommended_action": action,
        "profit_plan": profit_plan,
        "thesis": f"{asset} display-only fixture",
    }


def _index(plans, *, asof_days_ago=0, schema="prophet.index/v1"):
    return {
        "schema": schema, "asof": _dstr(asof_days_ago), "cadence": "nightly-EOD",
        "authority_tier": "display", "gate_go": False,
        "plan_count": len(plans), "active_count": len(plans), "plans": plans,
        "note": "DISPLAY-ONLY.",
    }


def _install(monkeypatch, tmp_path, obj):
    """Write `obj` (a dict, or a raw string for the malformed case) to a tmp index.json and point
    the module's _ARTIFACT_PATH / _V at it, then reset the per-process cache."""
    p = tmp_path / "prophet_index.json"
    if isinstance(obj, str):
        p.write_text(obj)
    else:
        p.write_text(json.dumps(obj))
    monkeypatch.setattr(pf, "_ARTIFACT_PATH", p)
    monkeypatch.setattr(pf, "_V", tmp_path)      # kept consistent even though path is set directly
    monkeypatch.setenv("MASTERMIND_PROPHET_FEED", "1")   # ensure ON unless a test overrides
    pf._reset_cache()
    return p


@pytest.fixture(autouse=True)
def _clean_cache():
    """Every test starts and ends with a fresh cache so process-cache state never leaks."""
    pf._reset_cache()
    yield
    pf._reset_cache()


# A representative fixture board: 4+ plans covering enter/wait/hold/invalidated + a non-BULL + an
# old signal, so the filtering / ordering / age-cut are all exercised at once.
def _board():
    return [
        # enter, fresh, top conviction → should source first
        _plan("AAA", entry=100.0, invalidation=90.0, t1=120.0, t2=140.0, trigger=101.0,
              conviction=95, phase="pre_trigger", action="enter", signal_days_ago=2),
        # wait, fresh, mid conviction → sources after AAA
        _plan("BBB", entry=50.0, invalidation=44.0, t1=62.0, t2=74.0, trigger=51.0,
              conviction=80, phase="pre_trigger", action="wait", signal_days_ago=3),
        # hold, triggered → NOT a candidate (already-live management), but plan_for still sees it
        _plan("CCC", entry=30.0, invalidation=26.0, t1=38.0, t2=46.0, trigger=31.0,
              conviction=88, phase="triggered_pre_t1", action="hold", signal_days_ago=1),
        # invalidated → dropped from plans() entirely
        _plan("DDD", entry=20.0, invalidation=18.0, t1=24.0, t2=28.0, trigger=21.0,
              conviction=70, phase="invalidated", action="invalidated", signal_days_ago=5),
        # non-BULL → dropped from plans()
        _plan("EEE", direction="BEAR", entry=10.0, invalidation=12.0, t1=8.0, t2=6.0, trigger=9.5,
              conviction=99, phase="pre_trigger", action="enter", signal_days_ago=1),
        # enter but STALE signal (older than the 10d candidate age cut) → sources NOTHING, but is a
        # valid active plan for plans()/plan_for
        _plan("FFF", entry=15.0, invalidation=13.0, t1=19.0, t2=23.0, trigger=15.5,
              conviction=100, phase="pre_trigger", action="enter", signal_days_ago=20),
    ]


# ── (a) filtering + ordering + selection on a real-shaped fixture ─────────────
def test_plans_filters_direction_phase_and_equity(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _index(_board()))
    tickers = {p["ticker"] for p in pf.plans()}
    # BULL + active only: AAA/BBB/CCC/FFF kept; DDD (invalidated) & EEE (bear) dropped
    assert tickers == {"AAA", "BBB", "CCC", "FFF"}


def test_plans_normalization_shape_and_targets(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _index(_board()))
    aaa = next(p for p in pf.plans() if p["ticker"] == "AAA")
    # exact normalized key set (the conviction wiring depends on these names)
    assert set(aaa) == {"ticker", "plan_id", "entry", "trigger", "invalidation", "t1", "t2",
                        "conviction", "phase", "recommended_action", "signal_date", "age_days",
                        "rr_grade"}
    assert aaa["entry"] == 100.0 and aaa["invalidation"] == 90.0
    assert aaa["t1"] == 120.0 and aaa["t2"] == 140.0     # resolved from profit_plan labels
    assert aaa["conviction"] == 95                        # from _conviction_score
    assert aaa["age_days"] == 2                           # vs _signal_date
    assert aaa["rr_grade"] is None                        # absent in prophet.index/v1 — never faked


def test_t1_t2_fall_back_to_flat_targets_by_index(monkeypatch, tmp_path):
    # a plan with NO profit_plan must still resolve t1/t2 from the flat targets[] by position
    plan = _plan("ZZZ", entry=10.0, invalidation=8.0, t1=13.0, t2=16.0, trigger=10.2,
                 conviction=60, phase="pre_trigger", action="wait", signal_days_ago=1,
                 profit_plan=[], targets=[13.0, 16.0])
    _install(monkeypatch, tmp_path, _index([plan]))
    z = pf.plan_for("ZZZ")
    assert z["t1"] == 13.0 and z["t2"] == 16.0


def test_candidate_tickers_ordering_dedup_and_age_cut(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _index(_board()))
    cands = pf.candidate_tickers()
    # only enter/wait AND age<=10: AAA(95,enter) & BBB(80,wait); CCC is hold, FFF is 20d-stale
    assert cands == ["AAA", "BBB"]


def test_candidate_tickers_dedup_keeps_highest_conviction(monkeypatch, tmp_path):
    # two plans for the same ticker → one entry, represented by the higher-conviction plan order
    board = [
        _plan("DUP", entry=10.0, invalidation=8.0, t1=13.0, t2=16.0, trigger=10.2,
              conviction=60, phase="pre_trigger", action="wait", signal_days_ago=2, plan_id="DUP-LO"),
        _plan("DUP", entry=10.0, invalidation=8.0, t1=13.0, t2=16.0, trigger=10.2,
              conviction=90, phase="pre_trigger", action="enter", signal_days_ago=1, plan_id="DUP-HI"),
        _plan("OTH", entry=20.0, invalidation=18.0, t1=24.0, t2=28.0, trigger=20.4,
              conviction=75, phase="pre_trigger", action="wait", signal_days_ago=1),
    ]
    _install(monkeypatch, tmp_path, _index(board))
    assert pf.candidate_tickers() == ["DUP", "OTH"]       # DUP once, ahead of OTH (90 > 75)


def test_candidate_tickers_respects_max_cap(monkeypatch, tmp_path):
    board = [
        _plan(f"T{i:02d}", entry=100.0 + i, invalidation=90.0 + i, t1=120.0 + i, t2=140.0 + i,
              trigger=101.0 + i, conviction=99 - i, phase="pre_trigger", action="enter",
              signal_days_ago=1)
        for i in range(pf.MAX_CANDIDATES + 5)
    ]
    _install(monkeypatch, tmp_path, _index(board))
    cands = pf.candidate_tickers()
    assert len(cands) == pf.MAX_CANDIDATES
    assert cands[0] == "T00"                              # highest conviction (99) first


def test_plan_for_selects_freshest_then_conviction(monkeypatch, tmp_path):
    board = [
        _plan("SEL", entry=10.0, invalidation=8.0, t1=13.0, t2=16.0, trigger=10.2,
              conviction=70, phase="triggered_pre_t1", action="hold", signal_days_ago=6, plan_id="OLD"),
        _plan("SEL", entry=11.0, invalidation=9.0, t1=14.0, t2=17.0, trigger=11.2,
              conviction=50, phase="pre_trigger", action="wait", signal_days_ago=1, plan_id="FRESH-LOWCONV"),
        _plan("SEL", entry=12.0, invalidation=10.0, t1=15.0, t2=18.0, trigger=12.2,
              conviction=99, phase="pre_trigger", action="wait", signal_days_ago=1, plan_id="FRESH-HICONV"),
    ]
    _install(monkeypatch, tmp_path, _index(board))
    sel = pf.plan_for("SEL")
    # both FRESH plans are 1d old → tie broken by conviction (99 > 50) → FRESH-HICONV
    assert sel["plan_id"] == "FRESH-HICONV"


def test_plan_for_absent_ticker_is_none(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _index(_board()))
    assert pf.plan_for("NOPE") is None


# ── (b) entry_discipline — all six statuses via crafted geometry ──────────────
# One canonical plan: entry 100, invalidation 90 → R = 10; T1 = 120; CHASE_ROOM_R=0.5 → extended
# above 105. phase pre_trigger, trigger 101.
def _disc_index():
    return _index([
        _plan("GEO", entry=100.0, invalidation=90.0, t1=120.0, t2=140.0, trigger=101.0,
              conviction=90, phase="pre_trigger", action="wait", signal_days_ago=1),
    ])


def test_entry_discipline_no_plan_when_absent(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _disc_index())
    out = pf.entry_discipline("NONE", 100.0)
    assert out["status"] == "no_plan"
    assert out["r"] is None and out["room_r"] is None


def test_entry_discipline_invalidated(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _disc_index())
    out = pf.entry_discipline("GEO", 89.0)               # below invalidation 90
    assert out["status"] == "invalidated"
    assert out["r"] == 10.0 and out["entry"] == 100.0 and out["invalidation"] == 90.0


def test_entry_discipline_missed_move(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _disc_index())
    out = pf.entry_discipline("GEO", 121.0)              # above T1 120
    assert out["status"] == "missed_move"


def test_entry_discipline_extended_vs_plan(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _disc_index())
    # above entry + 0.5R = 105 but below T1 120 → extended_vs_plan
    out = pf.entry_discipline("GEO", 110.0)
    assert out["status"] == "extended_vs_plan"
    assert out["room_r"] == pytest.approx(1.0)          # (110-100)/10


def test_entry_discipline_pre_trigger(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _disc_index())
    # phase pre_trigger and price below trigger 101 (and above invalidation) → pre_trigger
    out = pf.entry_discipline("GEO", 100.5)
    assert out["status"] == "pre_trigger"


def test_entry_discipline_within_zone(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _disc_index())
    # above trigger 101, at/below entry+0.5R (105), above invalidation → within_zone
    out = pf.entry_discipline("GEO", 103.0)
    assert out["status"] == "within_zone"


def test_entry_discipline_no_plan_on_bad_r(monkeypatch, tmp_path):
    # entry <= invalidation → non-positive R → no coherent long geometry → no_plan
    bad = _index([
        _plan("BAD", entry=90.0, invalidation=100.0, t1=120.0, t2=140.0, trigger=91.0,
              conviction=90, phase="pre_trigger", action="wait", signal_days_ago=1),
    ])
    _install(monkeypatch, tmp_path, bad)
    assert pf.entry_discipline("BAD", 95.0)["status"] == "no_plan"


def test_entry_discipline_none_price_is_no_plan(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _disc_index())
    assert pf.entry_discipline("GEO", None)["status"] == "no_plan"


# ── summary_line ──────────────────────────────────────────────────────────────
def test_summary_line_format(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _disc_index())
    line = pf.summary_line("GEO")
    assert line.startswith("PROPHET GEO-BULL-")
    for token in ("entry 100", "trig 101", "inv 90", "T1 120", "conv 90", "phase pre_trigger", "age 1d"):
        assert token in line


def test_summary_line_empty_when_no_plan(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _disc_index())
    assert pf.summary_line("NONE") == ""


# ── (c) staleness: asof older than _STALE_DAYS → fully inert ──────────────────
def test_stale_index_is_inert(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _index(_board(), asof_days_ago=pf._STALE_DAYS + 2))
    assert pf.index() == {}
    assert pf.plans() == []
    assert pf.candidate_tickers() == []
    assert pf.plan_for("AAA") is None
    assert pf.entry_discipline("AAA", 100.0)["status"] == "no_plan"
    assert pf.summary_line("AAA") == ""


def test_fresh_boundary_asof_is_live(monkeypatch, tmp_path):
    # exactly _STALE_DAYS old is still fresh (the gate is age > _STALE_DAYS)
    _install(monkeypatch, tmp_path, _index(_board(), asof_days_ago=pf._STALE_DAYS))
    assert pf.index() != {}
    assert pf.plans()


# ── (d) flag off → inert ──────────────────────────────────────────────────────
def test_flag_off_is_inert(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _index(_board()))
    for falsy in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("MASTERMIND_PROPHET_FEED", falsy)
        pf._reset_cache()
        assert pf.index() == {}, f"{falsy!r} should disable the feed"
        assert pf.plans() == []
        assert pf.candidate_tickers() == []
        assert pf.plan_for("AAA") is None


def test_flag_on_variants_enable(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _index(_board()))
    for truthy in ("1", "true", "YES", "on"):
        monkeypatch.setenv("MASTERMIND_PROPHET_FEED", truthy)
        pf._reset_cache()
        assert pf.index() != {}, f"{truthy!r} should enable the feed"


# ── (e) absent file → inert ───────────────────────────────────────────────────
def test_absent_file_is_inert(monkeypatch, tmp_path):
    missing = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(pf, "_ARTIFACT_PATH", missing)
    monkeypatch.setenv("MASTERMIND_PROPHET_FEED", "1")
    pf._reset_cache()
    assert pf.index() == {}
    assert pf.plans() == []
    assert pf.candidate_tickers() == []
    assert pf.plan_for("AAA") is None
    assert pf.entry_discipline("AAA", 100.0)["status"] == "no_plan"
    assert pf.summary_line("AAA") == ""


# ── (f) malformed JSON / wrong schema → inert, never raises ───────────────────
def test_malformed_json_is_inert(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, "{not valid json,,,")
    assert pf.index() == {}                              # must not raise
    assert pf.plans() == []
    assert pf.candidate_tickers() == []


def test_non_dict_json_is_inert(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, "[1, 2, 3]")
    assert pf.index() == {}
    assert pf.plans() == []


def test_wrong_schema_is_inert(monkeypatch, tmp_path):
    _install(monkeypatch, tmp_path, _index(_board(), schema="prophet.index/v99"))
    assert pf.index() == {}
    assert pf.plans() == []


def test_missing_asof_is_inert(monkeypatch, tmp_path):
    idx = _index(_board())
    del idx["asof"]
    _install(monkeypatch, tmp_path, idx)
    assert pf.index() == {}


def test_plans_missing_key_is_inert(monkeypatch, tmp_path):
    idx = _index(_board())
    del idx["plans"]
    _install(monkeypatch, tmp_path, idx)
    assert pf.index() != {}                              # index itself is valid (fresh asof, schema)
    assert pf.plans() == []                              # but no plans list → empty, no raise


def test_malformed_plan_entries_are_skipped(monkeypatch, tmp_path):
    # junk plan entries (non-dict, missing fields) must be skipped, not crash the whole read
    board = _board() + ["not a dict", 42, {"asset": "GGG"}]   # GGG: no direction/phase/entry
    _install(monkeypatch, tmp_path, _index(board))
    tickers = {p["ticker"] for p in pf.plans()}
    # the good ones survive; the junk is dropped (GGG has no BULL direction → filtered out)
    assert {"AAA", "BBB", "CCC", "FFF"} <= tickers
    assert "GGG" not in tickers


def test_coercion_of_bad_numeric_fields_never_raises(monkeypatch, tmp_path):
    # a plan with string / null price fields must coerce to None, not raise
    plan = _plan("NUM", entry="oops", invalidation=None, t1=13.0, t2=16.0, trigger="x",
                 conviction="hi", phase="pre_trigger", action="wait", signal_days_ago=1)
    _install(monkeypatch, tmp_path, _index([plan]))
    p = pf.plan_for("NUM")
    assert p is not None
    assert p["entry"] is None and p["invalidation"] is None and p["conviction"] is None
    # entry_discipline degrades to no_plan (entry/invalidation missing), never raises
    assert pf.entry_discipline("NUM", 12.0)["status"] == "no_plan"

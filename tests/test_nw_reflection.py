"""tests/test_nw_reflection.py — W-AI reflection engine (brain/nw_reflection.py) offline battery.

No LLM, no vendor data. Every test isolates BOTH the module root (_ROOT — call-time paths like
data/brain/outcome_ledger.jsonl) AND the three import-time bound constants (_OUT_DIR/_LATEST/
_HISTORY) to tmp_path, and stubs brain.neural_web_context / brain.ledger so the live artifact
and theses ledger are never read.

Coverage:
  * contract_drift(): [] on absent context (never fabricate drift from missing data);
    detects graph_conflicts_absent (dead/high) + fdr_cleared_absent (dead/high, the per-name
    gate) + bottom_state_vocabulary_drift on a WATCH-only, conflict-less candidate set;
    candidate_context_empty on 0 rows; >=1 fdr_cleared row silences the fdr claim.
  * coverage(): counts-only join of decided subjects vs candidate rows; absent state.
  * attribution(): honest 'building' below min_n; never raises on garbage; scoring at n>=min_n.
  * context_quality(): accruing when sidecar absent; counts/streak on rows.
  * derive_nudges(): first_seen/builds_seen carried forward (registry, legacy-seeded from a
    prior latest.json); max_n cap; severity ordering (high < medium < low positions); the
    coverage nudge requires cov state 'ok' (absence-is-not-drift) + the 0.5/0.55 hysteresis band.
  * nudge lifecycle registry (W-AI.1): counters survive a skipped build; resolution tombstones
    (pre-cap judged, detector-ran gated); same-asof re-runs never double-count.
  * build(): the pre-read _reset_context_cache() call is fail-soft.
  * persist(): writes latest.json + history.jsonl latest-wins per asof; latest() read-back.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import brain.neural_web_context as nwc
from brain import ledger, nw_reflection as nwr


# ───────────────────────────── isolation ─────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_reflection(tmp_path, monkeypatch):
    """Redirect BOTH the call-time root and the import-time bound output paths to tmp_path,
    and stub the two external readers so no test touches the live artifact/ledgers."""
    out = tmp_path / "data" / "nw_reflection"
    monkeypatch.setattr(nwr, "_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(nwr, "_OUT_DIR", out, raising=True)
    monkeypatch.setattr(nwr, "_LATEST", out / "latest.json", raising=True)
    monkeypatch.setattr(nwr, "_HISTORY", out / "history.jsonl", raising=True)
    monkeypatch.setattr(nwr, "_NUDGE_STATE", out / "nudge_state.json", raising=True)
    # hermetic defaults — individual tests override
    monkeypatch.setattr(nwc, "context", lambda: {}, raising=True)
    monkeypatch.setattr(nwc, "market_plane", lambda: {"stale": True, "asof": None}, raising=True)
    monkeypatch.setattr(ledger, "all_theses", lambda: [], raising=True)


def _write_jsonl(p: Path, rows: list, raw_lines: list[str] | None = None) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in rows] + list(raw_lines or [])
    p.write_text("\n".join(lines) + ("\n" if lines else ""))
    return p


def _outcome_ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "brain" / "outcome_ledger.jsonl"


def _audit_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "brain" / "nw_context_audit.jsonl"


# ───────────────────────────── contract_drift ─────────────────────────────

def test_contract_drift_absent_context_returns_empty(monkeypatch):
    """Absence is a context_quality problem, not a drift claim — never fabricate drift rows."""
    monkeypatch.setattr(nwc, "context", lambda: {})
    assert nwr.contract_drift() == []


def test_contract_drift_never_raises_when_reader_raises(monkeypatch):
    monkeypatch.setattr(nwc, "context", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert nwr.contract_drift() == []


def test_contract_drift_detects_dead_graph_conflicts_and_state_drift(monkeypatch):
    """The 2026-07-13 recon failure class: zero graph_conflicts rows + WATCH-only vocabulary
    + zero fdr_cleared rows (the live artifact is 0/~230 today)."""
    ctx = {
        "candidate_context": {
            "AAA": {"bottom": {"bottom_state": "WATCH"}},
            "BBB": {"bottom": {"bottom_state": "WATCH"}},
            "CCC": {"bottom": {"bottom_state": "WATCH"}},
        },
    }
    monkeypatch.setattr(nwc, "context", lambda: ctx)
    monkeypatch.setattr(nwc, "market_plane", lambda: {"stale": True})

    drift = nwr.contract_drift()
    by_code = {d["code"]: d for d in drift}

    assert set(by_code) == {"graph_conflicts_absent", "fdr_cleared_absent",
                            "bottom_state_vocabulary_drift"}

    gc = by_code["graph_conflicts_absent"]
    assert gc["status"] == "dead"
    assert gc["severity"] == "high"
    assert gc["field"] == "candidate_context.graph_conflicts"
    assert "0/3" in gc["detail"]

    fdr = by_code["fdr_cleared_absent"]
    assert fdr["status"] == "dead"
    assert fdr["severity"] == "high"
    assert fdr["field"] == "candidate_context.kernel.fdr_cleared"
    assert "0/3" in fdr["detail"]

    bs = by_code["bottom_state_vocabulary_drift"]
    assert bs["status"] == "partial"
    assert bs["severity"] == "high"  # everything observed is WATCH → priors above WATCH dead
    assert "BOTTOMING" in bs["detail"] and "CONFIRMED" in bs["detail"]


def test_contract_drift_healthy_context_yields_no_rows(monkeypatch):
    """A context carrying graph_conflicts + at least one fdr_cleared row + the full state
    vocabulary produces no drift rows (market plane stale → plane checks stay out of scope)."""
    ctx = {
        "candidate_context": {
            "AAA": {"bottom": {"bottom_state": "WATCH"}, "graph_conflicts": [],
                    "kernel": {"fdr_cleared": True}},
            "BBB": {"bottom": {"bottom_state": "BOTTOMING"}, "graph_conflicts": ["x_vs_y"],
                    "kernel": {"fdr_cleared": False}},
            "CCC": {"bottom": {"bottom_state": "CONFIRMED"}, "graph_conflicts": []},
        },
    }
    monkeypatch.setattr(nwc, "context", lambda: ctx)
    monkeypatch.setattr(nwc, "market_plane", lambda: {"stale": True})
    assert nwr.contract_drift() == []


def test_contract_drift_fdr_all_false_is_dead(monkeypatch):
    """0/n candidate rows fdr_cleared → the whole typed ladder is per-name inert: one
    dead/high row regardless of every other (healthy) field."""
    ctx = {
        "candidate_context": {
            "AAA": {"bottom": {"bottom_state": "WATCH"}, "graph_conflicts": [],
                    "kernel": {"fdr_cleared": False}},
            "BBB": {"bottom": {"bottom_state": "BOTTOMING"}, "graph_conflicts": ["x_vs_y"],
                    "kernel": {"fdr_cleared": False}},
            "CCC": {"bottom": {"bottom_state": "CONFIRMED"}, "graph_conflicts": [],
                    "kernel": {}},                       # missing key counts as not cleared
        },
    }
    monkeypatch.setattr(nwc, "context", lambda: ctx)
    monkeypatch.setattr(nwc, "market_plane", lambda: {"stale": True})
    drift = nwr.contract_drift()
    by_code = {d["code"]: d for d in drift}
    assert set(by_code) == {"fdr_cleared_absent"}
    row = by_code["fdr_cleared_absent"]
    assert row["status"] == "dead"
    assert row["severity"] == "high"
    assert row["field"] == "candidate_context.kernel.fdr_cleared"
    assert "0/3" in row["detail"]


def test_contract_drift_fdr_one_true_silences_the_claim(monkeypatch):
    """>=1 fdr_cleared row → no fdr_cleared_absent claim (the gate can fire somewhere)."""
    ctx = {
        "candidate_context": {
            "AAA": {"bottom": {"bottom_state": "WATCH"}, "graph_conflicts": [],
                    "kernel": {"fdr_cleared": True}},
            "BBB": {"bottom": {"bottom_state": "BOTTOMING"}, "graph_conflicts": ["x_vs_y"],
                    "kernel": {"fdr_cleared": False}},
            "CCC": {"bottom": {"bottom_state": "CONFIRMED"}, "graph_conflicts": []},
        },
    }
    monkeypatch.setattr(nwc, "context", lambda: ctx)
    monkeypatch.setattr(nwc, "market_plane", lambda: {"stale": True})
    codes = {d["code"] for d in nwr.contract_drift()}
    assert "fdr_cleared_absent" not in codes


def test_contract_drift_empty_candidate_context_is_dead(monkeypatch):
    ctx = {"candidate_context": {}}
    monkeypatch.setattr(nwc, "context", lambda: ctx)
    drift = nwr.contract_drift()
    assert len(drift) == 1
    assert drift[0]["code"] == "candidate_context_empty"
    assert drift[0]["status"] == "dead"
    assert drift[0]["severity"] == "high"


def test_contract_drift_fresh_plane_checks(monkeypatch):
    """A fresh market plane with empty contradictions + all-None liquidity adds the two
    plane rows; a stale plane never does (staleness is context_quality's job)."""
    ctx = {
        "candidate_context": {
            "AAA": {"bottom": {"bottom_state": "WATCH"}, "graph_conflicts": ["a_vs_b"]},
            "BBB": {"bottom": {"bottom_state": "BOTTOMING"}, "graph_conflicts": []},
            "CCC": {"bottom": {"bottom_state": "CONFIRMED"}, "graph_conflicts": []},
        },
    }
    monkeypatch.setattr(nwc, "context", lambda: ctx)
    monkeypatch.setattr(nwc, "market_plane", lambda: {
        "stale": False, "contradiction_count": 0, "contradiction_summary": {},
        "liquidity": {"state": None, "netliq_bn": None, "tga_bn": None, "tga_impulse": None},
    })
    codes = {d["code"] for d in nwr.contract_drift()}
    assert "contradictions_empty" in codes
    assert "liquidity_plumbing_absent" in codes


# ───────────────────────────── coverage ─────────────────────────────

def test_coverage_counts_subjects_with_and_without_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(nwc, "context", lambda: {
        "candidate_context": {"NVDA": {}, "AMD": {}},
    })
    monkeypatch.setattr(ledger, "all_theses", lambda: [
        {"subject": "nvda", "status": "open"},        # has row (case-normalised)
        {"subject": "TSLA", "status": "open"},        # no row
        {"subject": "MSFT", "status": "closed"},      # closed — not an open thesis
    ])
    _write_jsonl(_outcome_ledger_path(tmp_path), [
        {"subject": "AMD", "outcome": 1},             # resolved — has row
        {"subject": "XOM", "outcome": 0},             # resolved — no row
    ])

    cov = nwr.coverage()
    assert cov["state"] == "ok"
    assert cov["open_theses_n"] == 2
    assert cov["resolved_recent_n"] == 2
    assert cov["context_rows_n"] == 2
    # subjects = {NVDA, TSLA, AMD, XOM}; with row = {NVDA, AMD}
    assert cov["with_context_row_n"] == 2
    assert cov["coverage_rate"] == round(2 / 4, 3)


def test_coverage_absent_everything_is_honest_empty(monkeypatch):
    monkeypatch.setattr(nwc, "context", lambda: {})
    monkeypatch.setattr(ledger, "all_theses", lambda: [])
    cov = nwr.coverage()
    assert cov["state"] == "absent"
    assert cov["coverage_rate"] is None
    assert cov["with_context_row_n"] == 0


def test_coverage_context_absent_with_subjects(tmp_path, monkeypatch):
    """Subjects but no context rows → state 'context_absent', rate 0.0 — an honest zero."""
    monkeypatch.setattr(nwc, "context", lambda: {})
    monkeypatch.setattr(ledger, "all_theses", lambda: [{"subject": "NVDA", "status": "open"}])
    cov = nwr.coverage()
    assert cov["state"] == "context_absent"
    assert cov["open_theses_n"] == 1
    assert cov["with_context_row_n"] == 0
    assert cov["coverage_rate"] == 0.0


# ───────────────────────────── attribution ─────────────────────────────

def test_attribution_building_below_min_n(tmp_path):
    """No decision-time NW stamp exists yet → joinable_n 0 → honestly 'building'."""
    _write_jsonl(_outcome_ledger_path(tmp_path), [
        {"subject": "NVDA", "outcome": 1},
        {"subject": "AMD", "outcome": 0},
    ])
    attr = nwr.attribution()
    assert attr["state"] == "building"
    assert attr["n_resolved"] == 2
    assert attr["joinable_n"] == 0
    assert "note" in attr


def test_attribution_never_raises_on_garbage_rows(tmp_path):
    _write_jsonl(_outcome_ledger_path(tmp_path), [
        {"nw_at_entry": "not-a-dict"},
        {"nw_at_entry": ["list"]},
        {"outcome": "prose", "subject": None},
        {},
    ], raw_lines=["not json at all", "{broken", '"just a string"', "[1, 2, 3]"])
    attr = nwr.attribution()
    assert attr["state"] == "building"
    assert attr["joinable_n"] == 0


def test_attribution_missing_ledger_is_building(tmp_path):
    attr = nwr.attribution()
    assert attr["state"] == "building"
    assert attr["n_resolved"] == 0


def test_attribution_scores_at_min_n_joinable(tmp_path):
    rows = ([{"nw_at_entry": {"had_row": True}, "outcome": 1} for _ in range(6)]
            + [{"nw_at_entry": {"had_row": False}, "outcome": 0} for _ in range(6)])
    _write_jsonl(_outcome_ledger_path(tmp_path), rows)
    attr = nwr.attribution(min_n=12)
    assert attr["state"] == "scoring"
    assert attr["joinable_n"] == 12
    assert attr["by_covered"]["covered"] == {"n": 6, "hits": 6}
    assert attr["by_covered"]["uncovered"] == {"n": 6, "hits": 0}


# ───────────────────────────── context_quality ─────────────────────────────

def test_context_quality_absent_sidecar_is_accruing(tmp_path):
    q = nwr.context_quality()
    assert q == {"state": "accruing", "window_runs": 0}


def test_context_quality_counts_and_streak(tmp_path):
    _write_jsonl(_audit_path(tmp_path), [
        {"status": "present", "gap_notes_count": 0, "age_days": 1},
        {"status": "present", "gap_notes_count": 1, "age_days": 1},
        {"status": "absent", "gap_notes_count": 0, "age_days": None},
        {"status": "stale", "gap_notes_count": 2, "age_days": 6},
        {"status": "stale", "gap_notes_count": 4, "age_days": 7},
    ])
    q = nwr.context_quality()
    assert q["state"] == "ok"
    assert q["window_runs"] == 5
    assert q["n_present"] == 2 and q["n_stale"] == 2 and q["n_absent"] == 1
    assert q["seen_rate"] == round(2 / 5, 3)
    assert q["current_streak"] == {"status": "stale", "runs": 2}
    assert q["gap_notes_latest"] == 4
    assert q["asof_lag_days_latest"] == 7


# ───────────────────────────── derive_nudges ─────────────────────────────

def _drift_row(code: str, severity: str = "high", status: str = "dead") -> dict:
    return {"code": code, "field": "x", "status": status, "severity": severity,
            "detail": f"detail for {code}"}


def test_derive_nudges_carries_seen_counters_forward(tmp_path):
    """A prior latest.json's first_seen/builds_seen carry into the next build; a new code
    starts fresh at first_seen=asof, builds_seen=1."""
    nwr._OUT_DIR.mkdir(parents=True, exist_ok=True)
    nwr._LATEST.write_text(json.dumps({
        "nudges": [{"code": "graph_conflicts_absent", "kind": "contract_drift",
                    "severity": "high", "detail": "old",
                    "first_seen": "2026-07-01", "builds_seen": 3}],
    }))
    drift = [_drift_row("graph_conflicts_absent"), _drift_row("brand_new_code", "medium", "partial")]
    nudges = nwr.derive_nudges(drift, {}, {}, "2026-07-13")
    by_code = {n["code"]: n for n in nudges}

    carried = by_code["graph_conflicts_absent"]
    assert carried["first_seen"] == "2026-07-01"
    assert carried["builds_seen"] == 4

    fresh = by_code["brand_new_code"]
    assert fresh["first_seen"] == "2026-07-13"
    assert fresh["builds_seen"] == 1


def test_derive_nudges_caps_at_max_n(tmp_path):
    drift = [_drift_row(f"drift_code_{i:02d}") for i in range(15)]
    assert len(nwr.derive_nudges(drift, {}, {}, "2026-07-13")) == nwr._NUDGES_MAX
    assert len(nwr.derive_nudges(drift, {}, {}, "2026-07-13", max_n=4)) == 4


def test_derive_nudges_severity_ordering(tmp_path):
    drift = [
        _drift_row("low_one", "low"),
        _drift_row("high_one", "high"),
        _drift_row("medium_one", "medium"),
    ]
    nudges = nwr.derive_nudges(drift, {}, {}, "2026-07-13")
    assert [n["severity"] for n in nudges] == ["high", "medium", "low"]
    assert [n["code"] for n in nudges] == ["high_one", "medium_one", "low_one"]


def test_derive_nudges_only_dead_or_partial_drift_emits(tmp_path):
    drift = [_drift_row("ok_row", "low", "ok"), _drift_row("unknown_row", "low", "unknown"),
             _drift_row("dead_row", "high", "dead")]
    codes = {n["code"] for n in nwr.derive_nudges(drift, {}, {}, "2026-07-13")}
    assert codes == {"dead_row"}


def test_derive_nudges_coverage_and_staleness_folds(tmp_path):
    cov = {"state": "ok", "coverage_rate": 0.25, "open_theses_n": 4, "resolved_recent_n": 3,
           "with_context_row_n": 2}
    quality = {"state": "ok", "current_streak": {"status": "stale", "runs": 5},
               "gap_notes_latest": 4}
    nudges = nwr.derive_nudges([], cov, quality, "2026-07-13")
    by_code = {n["code"]: n for n in nudges}
    assert by_code["coverage_below_half"]["kind"] == "coverage_gap"
    assert by_code["context_stale_streak"]["severity"] == "high"
    assert by_code["gap_notes_elevated"]["severity"] == "low"


def test_derive_nudges_no_coverage_nudge_when_context_absent(tmp_path):
    """Absence-is-not-drift: a 0.0 rate against an absent context is a staleness story,
    not a coverage ask — the coverage nudge requires cov state 'ok'."""
    cov = {"state": "context_absent", "coverage_rate": 0.0, "open_theses_n": 4,
           "resolved_recent_n": 3, "with_context_row_n": 0}
    nudges = nwr.derive_nudges([], cov, {}, "2026-07-13")
    assert all(n["code"] != "coverage_below_half" for n in nudges)
    assert nudges == []                # nothing else has grounds to fire either


def test_derive_nudges_codes_are_public_surface_safe(tmp_path):
    drift = [_drift_row("Bad-Code.With Caps!"), _drift_row("fine_code")]
    nudges = nwr.derive_nudges(drift, {}, {}, "2026-07-13")
    for n in nudges:
        assert nwr._CODE_RE.match(n["code"]), n["code"]


# ───────────────────────────── nudge lifecycle registry (W-AI.1) ─────────────────────────────

# 1 WATCH-only row → 3 drift codes: graph_conflicts_absent, fdr_cleared_absent,
# bottom_state_vocabulary_drift
_DRIFT_CTX = {"candidate_context": {"AAA": {"bottom": {"bottom_state": "WATCH"}}}}
# full vocabulary + graph_conflicts + one fdr_cleared row → zero drift, detector RAN
_HEALTHY_CTX = {
    "candidate_context": {
        "AAA": {"bottom": {"bottom_state": "WATCH"}, "graph_conflicts": [],
                "kernel": {"fdr_cleared": True}},
        "BBB": {"bottom": {"bottom_state": "BOTTOMING"}, "graph_conflicts": ["x_vs_y"]},
        "CCC": {"bottom": {"bottom_state": "CONFIRMED"}, "graph_conflicts": []},
    },
}


def _registry_codes() -> dict:
    return json.loads(nwr._NUDGE_STATE.read_text())["codes"]


def test_registry_resolution_tombstone_and_recent_surface(monkeypatch):
    """A code absent from the candidates while its detector RAN resolves with a dated
    tombstone, and the report surfaces it in nudges_resolved_recent."""
    monkeypatch.setattr(nwc, "context", lambda: _DRIFT_CTX)
    rep1 = nwr.persist("2026-07-13")
    assert {n["code"] for n in rep1["nudges"]} >= {"fdr_cleared_absent"}
    monkeypatch.setattr(nwc, "context", lambda: _HEALTHY_CTX)   # the artifact healed
    rep2 = nwr.persist("2026-07-14")
    assert rep2["nudges"] == []
    resolved = {r["code"]: r["resolved_on"] for r in rep2["nudges_resolved_recent"]}
    assert resolved.get("fdr_cleared_absent") == "2026-07-14"
    assert len(rep2["nudges_resolved_recent"]) <= 5
    ent = _registry_codes()["fdr_cleared_absent"]
    assert ent["status"] == "resolved"
    assert ent["resolved_on"] == "2026-07-14"
    assert ent["first_seen"] == "2026-07-13"                    # tombstone keeps the history


def test_registry_carries_across_absent_build(monkeypatch):
    """A code that misses one build because the CONTEXT went absent keeps first_seen and
    cumulative builds_seen — absence says nothing about whether the drift was fixed."""
    monkeypatch.setattr(nwc, "context", lambda: _DRIFT_CTX)
    nwr.persist("2026-07-13")
    monkeypatch.setattr(nwc, "context", lambda: {})             # artifact gone
    rep2 = nwr.persist("2026-07-14")
    assert rep2["nudges"] == []
    assert rep2["nudges_resolved_recent"] == []                 # detector never ran — no resolve
    assert _registry_codes()["fdr_cleared_absent"]["status"] == "open"
    monkeypatch.setattr(nwc, "context", lambda: _DRIFT_CTX)     # artifact back, still drifting
    rep3 = nwr.persist("2026-07-15")
    n3 = {n["code"]: n for n in rep3["nudges"]}["fdr_cleared_absent"]
    assert n3["first_seen"] == "2026-07-13"                     # never reset by the gap
    assert n3["builds_seen"] == 2                               # 07-13 + 07-15; the gap didn't count


def test_registry_resolved_code_reappears_with_original_first_seen(monkeypatch):
    monkeypatch.setattr(nwc, "context", lambda: _DRIFT_CTX)
    nwr.persist("2026-07-13")
    monkeypatch.setattr(nwc, "context", lambda: _HEALTHY_CTX)
    nwr.persist("2026-07-14")                                   # resolves everything
    monkeypatch.setattr(nwc, "context", lambda: _DRIFT_CTX)
    rep3 = nwr.persist("2026-07-15")                            # the regression returns
    n3 = {n["code"]: n for n in rep3["nudges"]}["fdr_cleared_absent"]
    assert n3["first_seen"] == "2026-07-13"
    assert n3["builds_seen"] == 2
    ent = _registry_codes()["fdr_cleared_absent"]
    assert ent["status"] == "open" and ent["resolved_on"] is None


def test_cap_dropped_candidate_not_resolved_and_dropped_counted(monkeypatch):
    """Resolution is judged on the PRE-CAP candidate set: a nudge cut by nudges_max is
    dropped from the wire (and counted), never declared fixed."""
    monkeypatch.setattr(nwc, "context", lambda: _DRIFT_CTX)
    rep1 = nwr.persist("2026-07-13", nudges_max=1)
    assert len(rep1["nudges"]) == 1
    assert rep1["nudges_dropped_n"] == 2
    assert rep1["nudges_resolved_recent"] == []
    rep2 = nwr.persist("2026-07-14", nudges_max=1)
    assert rep2["nudges_resolved_recent"] == []                 # the cut candidates never resolved
    codes = _registry_codes()
    assert set(codes) == {"graph_conflicts_absent", "fdr_cleared_absent",
                          "bottom_state_vocabulary_drift"}
    assert all(v["status"] == "open" and v["builds_seen"] == 2 for v in codes.values())


def test_same_asof_rerun_does_not_double_increment(monkeypatch):
    monkeypatch.setattr(nwc, "context", lambda: _DRIFT_CTX)
    nwr.persist("2026-07-13")
    rep = nwr.persist("2026-07-13")                             # afternoon re-run, same asof
    n = {x["code"]: x for x in rep["nudges"]}["fdr_cleared_absent"]
    assert n["builds_seen"] == 1
    assert _registry_codes()["fdr_cleared_absent"]["builds_seen"] == 1


def _cov(rate: float) -> dict:
    return {"state": "ok", "coverage_rate": rate, "open_theses_n": 4, "resolved_recent_n": 3,
            "with_context_row_n": 2}


def test_coverage_hysteresis_band(tmp_path):
    """<0.5 arms the nudge; once open it clears only at >=0.55 (the live rate flapped at
    exactly 0.5 and toggled the nudge on alternating builds)."""
    n1 = nwr.derive_nudges([], _cov(0.49), {}, "2026-07-13")
    assert [n["code"] for n in n1] == ["coverage_below_half"]
    assert "0.55" in n1[0]["detail"]                            # the band is stated, not implied
    n2 = nwr.derive_nudges([], _cov(0.52), {}, "2026-07-14")    # open → 0.52 still fires
    assert [n["code"] for n in n2] == ["coverage_below_half"]
    n3 = nwr.derive_nudges([], _cov(0.56), {}, "2026-07-15")    # >=0.55 clears + resolves
    assert n3 == []
    ent = _registry_codes()["coverage_below_half"]
    assert ent["status"] == "resolved" and ent["resolved_on"] == "2026-07-15"
    n4 = nwr.derive_nudges([], _cov(0.52), {}, "2026-07-16")    # re-armed: fresh 0.52 stays quiet
    assert n4 == []


def test_persist_same_asof_history_latest_wins(monkeypatch):
    """A same-asof re-persist REPLACES the history row — the morning run's worse numbers
    must not freeze for the day."""
    monkeypatch.setattr(nwc, "context", lambda: _DRIFT_CTX)
    nwr.persist("2026-07-13")
    rows = [json.loads(l) for l in nwr._HISTORY.read_text().strip().splitlines()]
    assert len(rows) == 1 and rows[0]["nudges_n"] == 3
    monkeypatch.setattr(nwc, "context", lambda: _HEALTHY_CTX)   # afternoon: the world improved
    nwr.persist("2026-07-13")
    rows = [json.loads(l) for l in nwr._HISTORY.read_text().strip().splitlines()]
    assert len(rows) == 1                                       # still one row per asof
    assert rows[0]["nudges_n"] == 0                             # ...carrying the LATEST numbers


# ───────────────────────────── build + persist ─────────────────────────────

def test_build_report_shape_on_empty_world(tmp_path):
    rep = nwr.build("2026-07-13")
    assert rep["schema"] == nwr.SCHEMA == "nw_reflection.v1"
    assert rep["asof"] == "2026-07-13"
    assert rep["contract_drift"] == []
    assert rep["coverage"]["state"] == "absent"
    assert rep["attribution"]["state"] == "building"
    assert rep["context_quality"]["state"] == "accruing"
    assert rep["nudges"] == []
    assert "generated_at" in rep


def test_build_cache_reset_is_fail_soft(tmp_path, monkeypatch):
    """build() resets the NW context cache before reading (long-lived-process freshness);
    a raising reset must never take the report down."""
    monkeypatch.setattr(nwc, "_reset_context_cache",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")), raising=True)
    rep = nwr.build("2026-07-13")
    assert rep["schema"] == "nw_reflection.v1"
    assert rep["asof"] == "2026-07-13"
    assert rep["coverage"]["state"] == "absent"


def test_persist_writes_latest_and_history_keep_first_per_asof(tmp_path):
    rep1 = nwr.persist("2026-07-13")
    assert nwr._LATEST.exists()
    assert json.loads(nwr._LATEST.read_text())["asof"] == "2026-07-13"
    assert len(nwr._HISTORY.read_text().strip().splitlines()) == 1

    # same asof again → latest rewritten, history NOT re-appended (keep-first)
    nwr.persist("2026-07-13")
    assert len(nwr._HISTORY.read_text().strip().splitlines()) == 1

    # new asof → second history line
    nwr.persist("2026-07-14")
    lines = [json.loads(l) for l in nwr._HISTORY.read_text().strip().splitlines()]
    assert [l["asof"] for l in lines] == ["2026-07-13", "2026-07-14"]
    # compact history rows carry the trend fields
    assert set(lines[0]) >= {"asof", "drift_n", "nudges_n", "coverage_rate",
                             "seen_rate", "attribution_state"}
    assert rep1["schema"] == "nw_reflection.v1"


def test_persist_builds_seen_accrues_across_builds(tmp_path, monkeypatch):
    """Two persists over the same drifting context increment builds_seen via the prior latest."""
    ctx = {"candidate_context": {"AAA": {"bottom": {"bottom_state": "WATCH"}}}}
    monkeypatch.setattr(nwc, "context", lambda: ctx)
    rep1 = nwr.persist("2026-07-13")
    rep2 = nwr.persist("2026-07-14")
    n1 = {n["code"]: n for n in rep1["nudges"]}["graph_conflicts_absent"]
    n2 = {n["code"]: n for n in rep2["nudges"]}["graph_conflicts_absent"]
    assert n1["builds_seen"] == 1 and n1["first_seen"] == "2026-07-13"
    assert n2["builds_seen"] == 2 and n2["first_seen"] == "2026-07-13"


def test_latest_reads_back_persisted_report(tmp_path):
    assert nwr.latest() == {}          # nothing persisted yet
    nwr.persist("2026-07-13")
    got = nwr.latest()
    assert got.get("schema") == "nw_reflection.v1"
    assert got.get("asof") == "2026-07-13"


def test_latest_malformed_file_returns_empty(tmp_path):
    nwr._OUT_DIR.mkdir(parents=True, exist_ok=True)
    nwr._LATEST.write_text("{not valid json")
    assert nwr.latest() == {}
    nwr._LATEST.write_text(json.dumps(["a", "list"]))
    assert nwr.latest() == {}

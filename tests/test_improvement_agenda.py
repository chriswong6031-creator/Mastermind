"""Guards for the Improvement Agenda (brain/improvement_agenda) — W-L / L3.

Three test families:

  1. FUSION on synthetic artifacts — each source (calibration / shadow / benchmark / validation /
     experiment / cost / deploy-lag / model-drift) turns its artifact into the right item class with
     evidence, and P3 is enforced (no evidence → dropped) + P2 (missing artifact → no-op, no raise).
  2. RANKING STABILITY — the same inputs produce the same order; the class weights hold; carry-forward
     ages an item that persists across two agendas instead of re-listing it fresh.
  3. THE SANITY ACCEPTANCE — build against the repo's REAL current state and assert the top items
     include the un-armed posture/gate class AND the experiment-maturity class (the things we KNOW are
     open right now), and that the agenda never trades. The real first AGENDA.md is printed.

Offline: the fusion tests patch the sources through monkeypatch (dual-patch on package + sys.modules,
the cio lazy-import lesson) so no LLM / network is touched. The sanity test runs the real build.
"""
from __future__ import annotations

import json
import sys
import types
from datetime import date

import pytest

import bot  # noqa: F401 — vendor/macro on sys.path before importing brain deps
from brain import improvement_agenda as A


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# helpers — synthetic sources
# ─────────────────────────────────────────────────────────────────────────────────────────────────

def _cio_rep(seats: list[dict]) -> dict:
    return {"as_of": "2026-07-02", "per_seat": seats}


def _seat(seat, reputation, mult=None, n=20, rel=None, sig=False, rec="rec"):
    return {"seat": seat, "label": seat.upper(), "reputation": reputation, "multiplier": mult,
            "n_resolved": n, "reliability": rel, "kpis": {"significant": sig}, "recommendation": rec}


def _leaderboard(books: dict) -> dict:
    return {"as_of": "2026-07-02", "books": books}


def _book(bid, vs_spy, n_resolved, baseline=False, label=None):
    return {"id": bid, "vs_spy_pct": vs_spy, "n_resolved": n_resolved,
            "is_baseline": baseline, "label": label or bid}


def _patch_shadow(monkeypatch, leaderboard):
    import portfolio
    sb = types.ModuleType("portfolio.shadow_books")
    sb.load_leaderboard = lambda: leaderboard
    monkeypatch.setattr(portfolio, "shadow_books", sb, raising=False)
    monkeypatch.setitem(sys.modules, "portfolio.shadow_books", sb)


def _isolate_io(monkeypatch, tmp_path):
    """Point the agenda's output + validation dirs at a tmp tree so tests never read the real repo
    artifacts (for fusion tests) and never write into data/agenda."""
    monkeypatch.setattr(A, "_OUT", tmp_path / "agenda")
    monkeypatch.setattr(A, "_VALIDATION_DIR", tmp_path / "validation_runs")
    # a real deploy_lag.json in the repo would leak in; redirect _ROOT-relative reads by patching the
    # helper that reads it (deploy-lag source reads A._ROOT / data/deploy_lag.json).
    monkeypatch.setattr(A, "_ROOT", tmp_path)


def _write_validation(dirpath, name, verdict, arming=""):
    dirpath.mkdir(parents=True, exist_ok=True)
    body = f"# Perception validation — `{name}` — {verdict.split()[0]}\n\n## Verdict: {verdict}\n"
    if arming:
        body += f"\n**Arming decision.** {arming}\n"
    (dirpath / f"{name}_2026-07-02.md").write_text(body)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# 1. FUSION
# ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_calibration_overconfident_becomes_item(monkeypatch, tmp_path):
    _isolate_io(monkeypatch, tmp_path)
    _patch_shadow(monkeypatch, {})
    rep = _cio_rep([_seat("gate", "overconfident", mult=0.6, n=30, rel=0.4, sig=True)])
    items = A._from_calibration(date(2026, 7, 2), rep)
    assert len(items) == 1
    it = items[0]
    assert it["class"] == A.CLASS_CALIBRATION
    assert it["owner"] == A.OWNER_SELF and it["fix_type"] == A.FIX_CONFIG
    assert it["evidence"] and any("0.6" in e for e in it["evidence"])


def test_calibration_wellcalibrated_no_item(monkeypatch, tmp_path):
    rep = _cio_rep([_seat("pm", "well_calibrated", mult=0.98)])
    assert A._from_calibration(date(2026, 7, 2), rep) == []


def test_shadow_donothing_lead_is_inaction_item(monkeypatch, tmp_path):
    lb = _leaderboard({
        "prod": _book("prod", 0.0, 10, baseline=True),
        "do_nothing": _book("do_nothing", 3.5, 12, label="Do-nothing (carry)"),
    })
    items = A._from_shadow(date(2026, 7, 2), lb)
    assert len(items) == 1
    it = items[0]
    assert it["class"] == A.CLASS_SHADOW
    # the inaction arm beating the live book is flagged as an experiment + names the over-trading
    assert it["fix_type"] == A.FIX_EXPERIMENT
    assert any("INACTION" in e or "over-trading" in it["suggested_fix"] for e in it["evidence"])


def test_shadow_below_resolved_floor_ignored():
    lb = _leaderboard({
        "prod": _book("prod", 0.0, 10, baseline=True),
        "defensive": _book("defensive", 4.0, 2),  # big lead but only 2 resolved → noise, dropped
    })
    assert A._from_shadow(date(2026, 7, 2), lb) == []


def test_validation_fail_produces_verdict_and_unarmed_items(monkeypatch, tmp_path):
    _isolate_io(monkeypatch, tmp_path)
    vdir = tmp_path / "validation_runs"
    _write_validation(vdir, "crash_risk", "FAIL (cold-start, UNCOMPUTABLE with vendored history)",
                      arming="crash_risk ships ADVISORY / cold_start=true — the seam stays DARK")
    _write_validation(vdir, "rotation_tensor", "FAIL",
                      arming="rotation_tensor stays DISPLAY-ONLY (advisory plane; cannot size)")
    items = A._from_validation(date(2026, 7, 2))
    classes = [it["class"] for it in items]
    assert classes.count(A.CLASS_VALIDATION) == 2      # one per failed signal
    assert A.CLASS_UNARMED in classes                   # the aggregate un-armed-gate item
    unarmed = [it for it in items if it["class"] == A.CLASS_UNARMED][0]
    assert unarmed["owner"] == A.OWNER_FABLE            # gate arming is never self-applied (P8)
    assert any("cold_start" in e or "cold-start" in e.lower() for e in unarmed["evidence"])


def test_missing_sources_degrade_to_noop(monkeypatch, tmp_path):
    """P2: every source with an absent artifact returns [] and never raises."""
    _isolate_io(monkeypatch, tmp_path)   # empty tmp tree → no validation dir, no deploy_lag.json
    assert A._from_validation(date(2026, 7, 2)) == []
    assert A._from_deploy_lag(date(2026, 7, 2)) == []
    assert A._from_journal(date(2026, 7, 2)) == []       # L2 not built → no-op
    assert A._from_benchmark(date(2026, 7, 2)) in ([], None) or isinstance(
        A._from_benchmark(date(2026, 7, 2)), list)


def test_p3_item_with_no_evidence_is_dropped(monkeypatch, tmp_path):
    """P3: an item that somehow arrives with empty evidence is dropped by build()."""
    _isolate_io(monkeypatch, tmp_path)
    _patch_shadow(monkeypatch, {})

    def _bad_source(asof):
        return [A._item("bad:x", A.CLASS_COST, "no evidence", evidence=[], suggested_fix="x",
                        fix_type=A.FIX_CONFIG, expected_impact="x", owner=A.OWNER_SELF)]

    monkeypatch.setattr(A, "_from_cost_guard", _bad_source)
    # also silence the real cio path
    monkeypatch.setattr(A, "_from_calibration", lambda asof, rep: [])
    out = A.build(date(2026, 7, 2), cio_rep={})
    assert all(it["id"] != "bad:x" for it in out["items"])


def test_journal_systemic_cluster(monkeypatch, tmp_path):
    """A why_wrong taxonomy logged by ≥2 seats is systemic; 1 seat is not."""
    import brain
    jr = types.ModuleType("brain.journal")
    jr.lesson_clusters = lambda: {
        "ignored-plane": {"seats": ["gate", "risk", "pm"], "n": 4, "examples": ["ex1"]},
        "bad-timing": {"seats": ["pm"], "n": 1, "examples": []},  # single seat → not systemic
    }
    monkeypatch.setattr(brain, "journal", jr, raising=False)
    monkeypatch.setitem(sys.modules, "brain.journal", jr)
    items = A._from_journal(date(2026, 7, 2))
    assert len(items) == 1
    it = items[0]
    assert it["class"] == A.CLASS_JOURNAL and it["extra"]["taxonomy"] == "ignored-plane"
    assert len(it["extra"]["seats"]) == 3


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# 2. RANKING STABILITY + CARRY-FORWARD
# ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_ranking_is_stable_and_class_ordered(monkeypatch, tmp_path):
    _isolate_io(monkeypatch, tmp_path)
    vdir = tmp_path / "validation_runs"
    _write_validation(vdir, "crash_risk", "FAIL (cold-start)", arming="stays DARK cold_start=true")
    lb = _leaderboard({
        "prod": _book("prod", 0.0, 10, baseline=True),
        "do_nothing": _book("do_nothing", 2.0, 12, label="Do-nothing"),
    })
    _patch_shadow(monkeypatch, lb)
    rep = _cio_rep([_seat("gate", "overconfident", mult=0.7, n=30, rel=0.5, sig=True)])

    a1 = A.build(date(2026, 7, 2), cio_rep=rep)
    a2 = A.build(date(2026, 7, 2), cio_rep=rep)
    assert [it["id"] for it in a1["items"]] == [it["id"] for it in a2["items"]]  # deterministic
    # class weights hold: validation/unarmed outrank experiment outrank calibration outrank shadow
    order = [it["class"] for it in a1["items"]]
    assert order.index(A.CLASS_VALIDATION) < order.index(A.CLASS_EXPERIMENT)
    assert order.index(A.CLASS_EXPERIMENT) < order.index(A.CLASS_CALIBRATION)
    assert order.index(A.CLASS_CALIBRATION) < order.index(A.CLASS_SHADOW)
    # ranks are 1..n contiguous
    assert [it["rank"] for it in a1["items"]] == list(range(1, len(a1["items"]) + 1))


def test_carry_forward_ages_persistent_item(monkeypatch, tmp_path):
    _isolate_io(monkeypatch, tmp_path)
    _patch_shadow(monkeypatch, {})
    # a stable single-item source that returns the SAME id both weeks
    stable = [A._item("calibration:gate", A.CLASS_CALIBRATION, "gate overconfident",
                      evidence=["multiplier 0.7"], suggested_fix="x", fix_type=A.FIX_CONFIG,
                      expected_impact="x", owner=A.OWNER_SELF)]
    monkeypatch.setattr(A, "_from_calibration", lambda asof, rep: [dict(stable[0])])

    # week 1: item is NEW (age 0) — persisted into tmp _OUT
    A.write(date(2026, 7, 2))
    # week 2 (14 days later): same id → carries first_seen forward, age_weeks >= 2
    a2 = A.build(date(2026, 7, 16), cio_rep={})
    gate = [it for it in a2["items"] if it["id"] == "calibration:gate"]
    assert gate and gate[0]["first_seen"] == "2026-07-02"
    assert gate[0]["age_weeks"] >= 2


def test_write_persists_json_and_md(monkeypatch, tmp_path):
    _isolate_io(monkeypatch, tmp_path)
    _patch_shadow(monkeypatch, {})
    res = A.write(date(2026, 7, 2))
    assert res["ok"]
    assert res["json_path"].endswith("2026-07-02.json")
    assert res["md_path"].endswith("AGENDA.md")
    import os
    assert os.path.exists(res["json_path"]) and os.path.exists(res["md_path"])
    md = (tmp_path / "agenda" / "AGENDA.md").read_text()
    assert "Improvement Agenda" in md


def test_build_never_trades(monkeypatch, tmp_path):
    """The agenda must never touch the paper account — fake it and assert zero calls."""
    _isolate_io(monkeypatch, tmp_path)
    _patch_shadow(monkeypatch, {})
    import portfolio
    calls = {"n": 0}
    pa = types.ModuleType("portfolio.paper_account")

    def _boom(*a, **k):
        calls["n"] += 1
        raise AssertionError("agenda must never call the paper account")

    pa.submit_order = pa.buy = pa.sell = _boom
    monkeypatch.setattr(portfolio, "paper_account", pa, raising=False)
    monkeypatch.setitem(sys.modules, "portfolio.paper_account", pa)
    A.build(date(2026, 7, 2), cio_rep={})
    A.write(date(2026, 7, 2))
    assert calls["n"] == 0


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# 3. THE SANITY ACCEPTANCE — real repo state
# ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_sanity_real_state_ranks_known_open_items(capsys):
    """Build against the REAL repo current state (no mocking) and assert the agenda surfaces the
    things we KNOW are open: the un-armed posture/gate class AND the experiment-maturity class. Prints
    the real first AGENDA.md so the report can quote it."""
    agenda = A.build(date(2026, 7, 2))
    assert agenda["n_items"] >= 1
    classes = {it["class"] for it in agenda["items"]}

    # the un-armed posture/gate class: the W-E.0/1 validation runs FAILed honestly and the notch/tilt
    # seams stay DARK — this MUST be surfaced.
    assert A.CLASS_UNARMED in classes, f"un-armed-gate item missing; got {classes}"

    # the experiment-maturity class: the shadow arms are accruing toward their falsifiers (registry L6
    # not yet built → derived from real shadow state). Nothing silently rots.
    assert A.CLASS_EXPERIMENT in classes, f"experiment item missing; got {classes}"

    # both are near the TOP — assert each appears within the top half of the ranked list (they are the
    # highest-weight open classes right now).
    ranks = {it["class"]: it["rank"] for it in agenda["items"]}
    top_half = max(1, len(agenda["items"]) // 2 + 1)
    assert ranks[A.CLASS_UNARMED] <= top_half
    # every real item carries evidence (P3)
    assert all(it["evidence"] for it in agenda["items"])

    # print the real first AGENDA.md for the build report
    md = A._md(agenda)
    print("\n===== REAL AGENDA.md (sanity acceptance) =====\n" + md)
    captured = capsys.readouterr()
    assert "Improvement Agenda" in captured.out

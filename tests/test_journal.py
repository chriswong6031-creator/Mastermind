"""W-L / L2 — THE JOURNAL (brain.journal) offline tests.

No LLM, no vendor data. Every test isolates data/journal/ to a tmp dir (monkeypatch _JOURNAL) so
the real journal is never touched. We prove the four sub-tasks of the design (§1):

  (a) AUTO-DRAFTS   — deterministic drafting from graded rows; bad→unresolved, good→skipped;
                      idempotent by draft id; planes_at_entry pulled from a dated market_view.
  (b) CONSCIOUS DUTY — pending_for surfaces the last-N bad drafts; complete() records valid
                      lessons, ACCEPTS-and-logs incomplete ones (never rejects), ignores unknowns.
  (c) PINNING       — top-K by grade-weighted recurrence; a pinned rule carries a falsifier and
                      AUTO-UNPINS when its taxonomy keeps mis-grading post-pin; injection golden.
  (d) RETROFIT      — the 2026-07-02 founding-incident backfill drafts + completes per seat;
                      idempotent; the pm success + the mistake taxonomies land.
  + CAP/CURATION    — drafts ring-buffer never drops an unresolved draft; lessons cap prunes oldest.
  + SELF_MIRROR MERGE — the journal rides the ONE injection seam (P7); flag OFF → byte-identical.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from brain import journal


# ───────────────────────────── isolation ─────────────────────────────
@pytest.fixture(autouse=True)
def _isolate_journal(tmp_path, monkeypatch):
    """Point the journal store at a tmp dir so no test writes the live data/journal/."""
    monkeypatch.setattr(journal, "_JOURNAL", tmp_path / "journal", raising=False)


def _rows(*specs):
    """Build synthetic graded rows (the self_mirror.rows shape): (date, ticker, outcome, rel, verb)."""
    return [{"date": d, "ticker": t, "outcome": o, "rel": r, "verb": v} for (d, t, o, r, v) in specs]


ASOF = date(2026, 8, 1)


# ───────────────────────────── (a) AUTO-DRAFTS ─────────────────────────────
def test_draft_bad_and_good(monkeypatch):
    rows = _rows(("2026-07-01", "SMH", 0, -0.073, "hold"),   # bad → unresolved
                 ("2026-07-02", "XLV", 1, 0.078, "hold"))    # good → skipped
    n = journal.draft_resolutions("autonomous", ASOF, rows=rows)
    assert n == 2
    drafts = journal.load_drafts("autonomous")
    by_tk = {d["call"].split()[-1]: d for d in drafts}
    assert by_tk["SMH"]["status"] == "unresolved"
    assert by_tk["SMH"]["grade"] == -0.073
    assert by_tk["XLV"]["status"] == "skipped"


def test_draft_is_idempotent(monkeypatch):
    rows = _rows(("2026-07-01", "SMH", 0, -0.073, "hold"))
    assert journal.draft_resolutions("autonomous", ASOF, rows=rows) == 1
    assert journal.draft_resolutions("autonomous", ASOF, rows=rows) == 0   # no dupes
    assert len(journal.load_drafts("autonomous")) == 1


def test_draft_skips_rows_without_grade(monkeypatch):
    rows = [{"date": "2026-07-01", "ticker": "SMH", "outcome": 0, "rel": None, "verb": "hold"}]
    assert journal.draft_resolutions("autonomous", ASOF, rows=rows) == 0


def test_draft_missing_rows_is_noop(monkeypatch):
    # P2: no rows and no self_mirror data → 0, no crash.
    monkeypatch.setattr(journal, "_cfg", journal._cfg)
    assert journal.draft_resolutions("autonomous", ASOF, rows=[]) == 0


def test_planes_at_entry_from_dated_view(monkeypatch, tmp_path):
    """The draft records planes_at_entry from that date's market_view artifact when present."""
    mv = tmp_path / "mv"
    mv.mkdir()
    (mv / "2026-07-01.json").write_text(json.dumps({
        "asof": "2026-07-01",
        "planes": {"risk_radar": {"direction": "risk_off"},
                   "mtf_signals": {"direction": "risk_on"},
                   "absent_plane": {"direction": None}}}))
    monkeypatch.setattr(journal, "_ROOT", tmp_path, raising=False)
    # _planes_at_entry reads _ROOT/data/market_view; relink via a data/ dir
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "market_view").symlink_to(mv)
    got = journal._planes_at_entry("2026-07-01")
    assert got == {"risk_radar": "risk_off", "mtf_signals": "risk_on"}   # None-direction dropped


# ───────────────────────────── (b) CONSCIOUS DUTY ─────────────────────────────
def test_pending_and_duty_block(monkeypatch):
    journal.draft_resolutions("gate", ASOF, rows=_rows(
        ("2026-07-01", "SMH", 0, -0.07, "veto"),
        ("2026-07-02", "XLV", 1, 0.05, "veto")))       # good is NOT owed
    pend = journal.pending_for("gate")
    assert len(pend) == 1 and pend[0]["status"] == "unresolved"
    block = journal.duty_block("gate")
    assert "JOURNAL DUTY" in block and "SMH" in block and "XLV" not in block


def test_complete_records_valid_mistake(monkeypatch):
    journal.draft_resolutions("gate", ASOF, rows=_rows(("2026-07-01", "SMH", 0, -0.07, "veto")))
    did = journal.pending_for("gate")[0]["id"]
    res = journal.complete("gate", [{
        "draft_id": did, "why_wrong": "ignored-plane",
        "what_i_believed": "single plane enough", "what_actually_happened": "radar dissented",
        "rule_i_adopt": "never pass an all-offense book on a single plane",
        "confidence_in_rule": 0.85}], ASOF)
    assert res["recorded"] == 1 and res["incomplete"] == 0
    lessons = journal.load_lessons("gate")
    assert len(lessons) == 1 and lessons[0]["why_wrong"] == "ignored-plane"
    assert journal.pending_for("gate") == []            # draft moved to 'lesson'


def test_complete_incomplete_is_accepted_and_logged(monkeypatch):
    """A lesson missing its required fields is ACCEPTED-as-incomplete (never rejected) and logged
    'journal_incomplete' — the three_questions_incomplete posture."""
    logged = []
    monkeypatch.setattr(journal, "_log_incomplete",
                        lambda seat, did, kind: logged.append((seat, kind)))
    journal.draft_resolutions("gate", ASOF, rows=_rows(("2026-07-01", "SMH", 0, -0.07, "veto")))
    did = journal.pending_for("gate")[0]["id"]
    # missing why_wrong + rule_i_adopt → invalid mistake
    res = journal.complete("gate", [{"draft_id": did, "what_i_believed": "x"}], ASOF)
    assert res["recorded"] == 0 and res["incomplete"] == 1
    assert logged and logged[0][0] == "gate"
    assert journal.pending_for("gate")               # still owed — not rejected, not resolved


def test_complete_bad_taxonomy_is_incomplete(monkeypatch):
    journal.draft_resolutions("gate", ASOF, rows=_rows(("2026-07-01", "SMH", 0, -0.07, "veto")))
    did = journal.pending_for("gate")[0]["id"]
    res = journal.complete("gate", [{"draft_id": did, "why_wrong": "not-a-taxonomy",
                                     "rule_i_adopt": "x"}], ASOF)
    assert res["incomplete"] == 1 and res["recorded"] == 0


def test_complete_unknown_draft_ignored(monkeypatch):
    journal.draft_resolutions("gate", ASOF, rows=_rows(("2026-07-01", "SMH", 0, -0.07, "veto")))
    res = journal.complete("gate", [{"draft_id": "gate:9999-01-01:ZZZZ:veto",
                                     "why_wrong": "bad-signal", "rule_i_adopt": "x"}], ASOF)
    assert res["unknown"] == 1 and res["recorded"] == 0


def test_success_lesson_on_good_draft(monkeypatch):
    journal.draft_resolutions("pm", ASOF, rows=_rows(("2026-07-01", "XLV", 1, 0.078, "champion")))
    did = journal.load_drafts("pm")[0]["id"]
    res = journal.complete("pm", [{"draft_id": did, "what_worked": "read the crowding tell",
                                   "skill_or_luck": "skill", "rule_i_keep": "size down when crowded",
                                   "confidence_in_rule": 0.75}], ASOF)
    assert res["recorded"] == 1
    lessons = journal.load_lessons("pm")
    assert lessons[0]["kind"] == "success" and lessons[0]["rule_i_keep"]


# ───────────────────────────── (c) PINNING + FALSIFIER ─────────────────────────────
def _seed_taxonomy(seat, taxonomy, dates, rule="my adopted rule", conf=0.8, asof=ASOF):
    """Draft + complete `len(dates)` mistake lessons of one taxonomy for a seat."""
    rows = _rows(*[(d, f"T{i}", 0, -0.06, "hold") for i, d in enumerate(dates)])
    journal.draft_resolutions(seat, asof, rows=rows)
    pend = journal.pending_for(seat)
    journal.complete(seat, [{"draft_id": d["id"], "why_wrong": taxonomy,
                             "rule_i_adopt": rule, "confidence_in_rule": conf} for d in pend], asof)


def test_pin_requires_min_recurrence(monkeypatch):
    _seed_taxonomy("gate", "ignored-plane", ["2026-07-01"])   # recurrence 1 < 2
    active = journal.recompute_pins("gate", ASOF)
    assert active == []
    _seed_taxonomy("gate", "ignored-plane", ["2026-07-08"])   # now recurrence 2
    active = journal.recompute_pins("gate", ASOF)
    assert len(active) == 1 and active[0]["taxonomy"] == "ignored-plane"


def test_pin_respects_confidence_floor(monkeypatch):
    _seed_taxonomy("gate", "bad-signal", ["2026-07-01", "2026-07-08"], conf=0.3)  # below 0.5 floor
    assert journal.recompute_pins("gate", ASOF) == []


def test_pin_top_k(monkeypatch):
    # 4 taxonomies each recurring twice, weights differ by grade magnitude → only top-3 pinned.
    for i, tax in enumerate(["bad-signal", "bad-timing", "ignored-plane", "crowd-follow"]):
        rows = _rows((f"2026-07-0{i+1}", f"A{i}", 0, -(0.1 * (i + 1)), "hold"),
                     (f"2026-07-1{i+1}", f"B{i}", 0, -(0.1 * (i + 1)), "hold"))
        journal.draft_resolutions("gate", ASOF, rows=rows)
        pend = [d for d in journal.pending_for("gate") if d["taxonomy_hint"] == "bad-signal"]
        journal.complete("gate", [{"draft_id": d["id"], "why_wrong": tax,
                                   "rule_i_adopt": f"rule {tax}", "confidence_in_rule": 0.8}
                                  for d in pend], ASOF)
    active = journal.recompute_pins("gate", ASOF)
    assert len(active) == 3                                   # top-K only
    # the two lowest-magnitude taxonomies (bad-signal, bad-timing) are NOT both pinned
    pinned_tax = {p["taxonomy"] for p in active}
    assert "crowd-follow" in pinned_tax and "ignored-plane" in pinned_tax


def test_falsifier_auto_unpins_on_recurrence(monkeypatch):
    # pin crowd-follow, then the SAME mistake recurs falsifier_min_n more times → auto-unpin.
    _seed_taxonomy("autonomous", "crowd-follow", ["2026-07-01", "2026-07-08"],
                   rule="STABLE RULE", asof=date(2026, 8, 1))
    journal.recompute_pins("autonomous", date(2026, 8, 1))
    pin = journal.load_pins("autonomous")[0]
    assert pin["status"] == "active" and pin["pinned_on"] == "2026-08-01"
    # 4 more crowd-follow mistakes AFTER pinned_on (same rule text → pinned_on preserved)
    _seed_taxonomy("autonomous", "crowd-follow",
                   ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"],
                   rule="STABLE RULE", asof=date(2026, 9, 1))
    journal.recompute_pins("autonomous", date(2026, 9, 1))
    pin = journal.load_pins("autonomous")[0]
    assert pin["status"] == "unpinned"
    assert pin["calls_seen"] >= 4 and pin["unpin_reason"]
    assert journal.injection_block("autonomous") == ""       # unpinned → out of the prompt


def test_injection_golden(monkeypatch):
    _seed_taxonomy("gate", "ignored-plane", ["2026-07-01", "2026-07-08"],
                   rule="never pass an all-offense book on a single plane")
    journal.recompute_pins("gate", ASOF)
    block = journal.injection_block("gate")
    assert block == (
        "--- YOUR PINNED LESSONS (journal; earned rules, auto-unpin on failure) ---\n"
        "  - [ignored-plane] never pass an all-offense book on a single plane")


# ───────────────────────────── CAP / CURATION ─────────────────────────────
def test_drafts_cap_never_drops_unresolved(monkeypatch):
    monkeypatch.setattr(journal, "_cfg",
                        lambda: {**journal._DEFAULTS, "cap_drafts_per_seat": 3})
    # 5 bad (unresolved) + 5 good (skipped); cap 3 must keep ALL 5 unresolved, prune skipped.
    rows = _rows(*[(f"2026-07-0{i}", f"BAD{i}", 0, -0.05, "hold") for i in range(1, 6)],
                 *[(f"2026-06-0{i}", f"GOOD{i}", 1, 0.05, "hold") for i in range(1, 6)])
    journal.draft_resolutions("autonomous", ASOF, rows=rows)
    drafts = journal.load_drafts("autonomous")
    assert sum(1 for d in drafts if d["status"] == "unresolved") == 5   # never dropped


def test_lessons_cap_prunes_oldest(monkeypatch):
    monkeypatch.setattr(journal, "_cfg",
                        lambda: {**journal._DEFAULTS, "cap_lessons_per_seat": 2})
    for i in range(4):
        d = f"2026-07-0{i+1}"
        journal.draft_resolutions("gate", ASOF, rows=_rows((d, f"T{i}", 0, -0.05, "veto")))
        did = journal.pending_for("gate")[0]["id"]
        journal.complete("gate", [{"draft_id": did, "why_wrong": "bad-signal",
                                   "rule_i_adopt": f"rule {i}", "confidence_in_rule": 0.8}], ASOF)
    lessons = journal.load_lessons("gate")
    assert len(lessons) == 2
    kept_dates = sorted(l["date"] for l in lessons)
    assert kept_dates == ["2026-07-03", "2026-07-04"]         # oldest two pruned


# ───────────────────────────── (d) RETROFIT ─────────────────────────────
def test_backfill_founding_incident(monkeypatch):
    res = journal.backfill_founding_incident(ASOF)
    # every seat drafted + its retroactive lesson recorded
    for seat in ("autonomous", "pm", "strategist", "gate"):
        assert res[seat]["drafted"] == 1
        assert res[seat]["lessons_recorded"] == 1
    # the pm memory is the SUCCESS (trimmed semis, held cash)
    pm_lessons = journal.load_lessons("pm")
    assert pm_lessons and pm_lessons[0]["kind"] == "success"
    assert "crowd" in pm_lessons[0]["rule_i_keep"].lower() or "crowd" in pm_lessons[0]["what_worked"].lower()
    # the autonomous memory is the crowd-follow SMH rebuy MISTAKE
    au = journal.load_lessons("autonomous")
    assert au and au[0]["why_wrong"] == "crowd-follow"
    # the gate memory is ignored-plane; strategist is label-trust
    assert journal.load_lessons("gate")[0]["why_wrong"] == "ignored-plane"
    assert journal.load_lessons("strategist")[0]["why_wrong"] == "label-trust"


def test_backfill_is_idempotent(monkeypatch):
    journal.backfill_founding_incident(ASOF)
    res2 = journal.backfill_founding_incident(ASOF)
    for seat in ("autonomous", "pm", "strategist", "gate"):
        assert res2[seat]["drafted"] == 0                     # no duplicate memories


def test_backfill_drafts_only_without_lessons(monkeypatch):
    res = journal.backfill_founding_incident(ASOF, complete_lessons=False)
    assert res["autonomous"]["drafted"] == 1
    assert journal.load_lessons("autonomous") == []           # drafts only, duty still owed
    assert journal.pending_for("autonomous")                  # the mistake awaits its lesson


# ───────────────────────────── SELF_MIRROR MERGE (P7 one seam) ─────────────────────────────
def test_self_mirror_off_is_byte_identical(monkeypatch):
    from brain import self_mirror
    monkeypatch.delenv("MASTERMIND_SELF_MIRROR", raising=False)
    _seed_taxonomy("gate", "ignored-plane", ["2026-07-01", "2026-07-08"])
    journal.recompute_pins("gate", ASOF)
    p = "GATE SYS"
    assert self_mirror.inject(p, "gate") is p                 # flag OFF → unchanged object


def test_self_mirror_injects_pins_when_on(monkeypatch):
    from brain import self_mirror
    monkeypatch.setenv("MASTERMIND_SELF_MIRROR", "1")
    monkeypatch.setattr(self_mirror._calib, "load", lambda: {"agents": {}}, raising=False)
    _seed_taxonomy("gate", "ignored-plane", ["2026-07-01", "2026-07-08"],
                   rule="never pass single-plane offense")
    journal.recompute_pins("gate", ASOF)
    out = self_mirror.inject("GATE SYS", "gate")
    assert "PINNED LESSONS" in out and "never pass single-plane offense" in out


def test_self_mirror_injects_duty_when_on(monkeypatch):
    from brain import self_mirror
    monkeypatch.setenv("MASTERMIND_SELF_MIRROR", "1")
    monkeypatch.setattr(self_mirror._calib, "load", lambda: {"agents": {}}, raising=False)
    journal.draft_resolutions("gate", ASOF, rows=_rows(("2026-07-01", "SMH", 0, -0.07, "veto")))
    out = self_mirror.inject("GATE SYS", "gate")
    assert "JOURNAL DUTY" in out and "SMH" in out

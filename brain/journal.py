"""THE JOURNAL — per-seat memory files with the conscious duty (W-L / L2, charter P3/P6).

The missing conscience. Two layers, one contract with the existing self-mirror injection seam.

  DATA LAYOUT  (data/journal/<seat>/)
  ─────────────────────────────────────────────────────────────────────────────────────────────
    drafts/drafts.json     — the auto-drafted, deterministic resolution ledger. One entry per
                             graded object (calibration label, thesis close, three-questions
                             falsifier grade). Ring buffer capped at doctrine journal.cap_drafts_per_seat.
                             Each draft:
                               {id, seat, date (decision date), resolved_on (asof of drafting),
                                call, thesis_at_entry, planes_at_entry (from the market_view
                                artifact of the decision date if available, else null), outcome
                                (0|1), grade (rel_return vs benchmark), benchmark, regime_context,
                                close_reason, taxonomy_hint, lesson_id (set once completed),
                                status: 'unresolved' (needs a conscious lesson) | 'lesson'
                                (completed) | 'skipped' (a GOOD-grade draft the seat may narrate
                                but is not REQUIRED to)}
    lessons.json           — the curated lesson file. One entry per completed conscious lesson
                             (mistakes AND successes). Capped at journal.cap_lessons_per_seat,
                             oldest pruned first. Each lesson:
                               MISTAKE  {id, seat, draft_id, date, kind:'mistake',
                                         what_i_believed, what_actually_happened,
                                         why_wrong (taxonomy), rule_i_adopt (falsifiable sentence),
                                         confidence_in_rule, grade}
                               SUCCESS  {id, seat, draft_id, date, kind:'success',
                                         what_worked, skill_or_luck, rule_i_keep,
                                         confidence_in_rule, grade}
    pins.json              — the pinned-rule registry (top-K by grade-weighted recurrence). Each
                             pin carries its own FALSIFIER — the rule's predicted effect on the
                             seat's next-N same-taxonomy calls — and AUTO-UNPINS when it fails.
                               {taxonomy, rule, kind, confidence, weight, recurrence,
                                pinned_on, calls_seen (post-pin same-taxonomy resolutions),
                                calls_wrong, status: 'active' | 'unpinned', unpin_reason,
                                unpinned_on}
  ─────────────────────────────────────────────────────────────────────────────────────────────

The two duties (mirrors the three-questions enforcement in brain.judgment_book):
  (a) AUTO-DRAFT — deterministic, no LLM. ``draft_resolutions(seat, asof)`` scans the graded rows
      the calibration/self_mirror machinery already produces for a seat and appends a draft per
      newly-resolved call. Idempotent (id = f"{seat}:{date}:{ticker}:{verb}").
  (b) CONSCIOUS DUTY — ``pending_for(seat)`` returns the last-N unresolved bad-grade drafts the seat
      MUST address on its next build; ``complete(seat, completions)`` records the seat's lessons.
      A missing completion is ACCEPTED and logged 'journal_incomplete' (add-only, never rejected —
      the same posture as three_questions_incomplete).
  (c) CURATION + PINNING — lessons cluster by taxonomy; ``recompute_pins(seat, asof)`` picks the
      top-K by grade-weighted recurrence, checks each active pin's falsifier against the seat's
      post-pin same-taxonomy grades, and auto-unpins the ones that stopped predicting.
      ``injection_block(seat)`` renders the active pins for the self_mirror seam.
  (d) RETROFIT — ``backfill_incident(...)`` drafts the 2026-07-02 incident entries for each seat
      from the incident fixtures + decisions quotes (the system's first memories).

Guardrails (charter P2 everywhere): every public entry point is best-effort and returns a no-op
value on any failure — missing data degrades to nothing, never a raise, never a fabricated grade.
Deterministic + offline (no LLM, no network). The conscious lessons are WRITTEN by the seat and
merely RECORDED here.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_JOURNAL = _ROOT / "data" / "journal"

# the eight seats the journal covers — the same agents self_mirror grades (P7: one seat vocabulary).
SEATS: tuple[str, ...] = (
    "strategist", "pm", "gate", "risk",
    "autonomous", "heavyweight", "china", "hk",
)

# the why-wrong taxonomy (design §1) — the closed vocabulary a mistake lesson must classify into.
TAXONOMY: tuple[str, ...] = (
    "bad-signal", "bad-timing", "bad-sizing", "ignored-plane",
    "crowd-follow", "label-trust", "thesis-drift", "luck-bad",
)

# ───────────────────────────── doctrine (unverified-prior) ─────────────────────────────
_DEFAULTS = {
    "last_n_drafts": 12,
    "bad_grade_max": 0.0,
    "cap_lessons_per_seat": 200,
    "cap_drafts_per_seat": 400,
    "pin_top_k": 3,
    "pin_min_recurrence": 2,
    "pin_min_confidence": 0.5,
    "falsifier_window_n": 6,
    "falsifier_min_n": 4,
    "falsifier_fail_rate": 0.5,
}


def _cfg() -> dict:
    """doctrine.yml journal block (unverified-prior), degrade-safe to _DEFAULTS."""
    out = dict(_DEFAULTS)
    try:
        from bot.doctrine_config import load_doctrine
        block = load_doctrine().get("journal") or {}
        for k in out:
            if block.get(k) is not None:
                out[k] = block[k]
    except Exception:  # noqa: BLE001 — missing doctrine degrades to the priors
        pass
    return out


# ───────────────────────────── file I/O (best-effort, add-safe) ─────────────────────────────
def _seat_dir(seat: str) -> Path:
    return _JOURNAL / str(seat)


def _read_json(p: Path, default):
    try:
        if p.exists():
            v = json.loads(p.read_text())
            return v if v is not None else default
    except Exception:  # noqa: BLE001
        pass
    return default


def _write_json(p: Path, obj) -> bool:
    """Atomic-ish write (tmp + replace). Never raises → returns False on failure."""
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, indent=2, sort_keys=False, default=str))
        tmp.replace(p)
        return True
    except Exception:  # noqa: BLE001
        return False


def _drafts_path(seat: str) -> Path:
    return _seat_dir(seat) / "drafts" / "drafts.json"


def _lessons_path(seat: str) -> Path:
    return _seat_dir(seat) / "lessons.json"


def _pins_path(seat: str) -> Path:
    return _seat_dir(seat) / "pins.json"


def load_drafts(seat: str) -> list[dict]:
    v = _read_json(_drafts_path(seat), [])
    return v if isinstance(v, list) else []


def load_lessons(seat: str) -> list[dict]:
    v = _read_json(_lessons_path(seat), [])
    return v if isinstance(v, list) else []


def load_pins(seat: str) -> list[dict]:
    v = _read_json(_pins_path(seat), [])
    return v if isinstance(v, list) else []


# ───────────────────────────── (a) AUTO-DRAFTS ─────────────────────────────
def _regime_context(asof: date | None) -> str:
    """A compact regime tag for a draft (the market_view brief's posture, else the risk_state).

    Best-effort, offline: reads the market_view artifact posture if present; degrades to "" so a
    draft is never blocked on regime lookup. The AUTO-DRAFT is deterministic given the artifacts on
    disk — it does NOT pin a live market state (charter: never pin live states in code)."""
    try:
        p = _ROOT / "data" / "market_view" / "latest.json"
        if p.exists():
            d = json.loads(p.read_text())
            brief = (d.get("brief") or {}) if isinstance(d, dict) else {}
            posture = brief.get("posture_implication") or brief.get("posture")
            if posture:
                return str(posture)[:120]
    except Exception:  # noqa: BLE001
        pass
    return ""


def _planes_at_entry(entry_iso: str) -> dict | None:
    """The market_view planes as they read on the decision date, from that date's artifact.

    L2 spec (design §1): the draft records ``planes_at_entry`` from the market_view artifact OF THAT
    DATE when available. We look for a dated snapshot data/market_view/<date>.json first; if only the
    single ``latest.json`` exists and its asof matches the entry date, we use that. Otherwise None
    (P2: absent → null, never fabricated). Returns a compact {name: direction} map to keep drafts
    small; the full artifact stays on disk."""
    try:
        base = _ROOT / "data" / "market_view"
        cand = base / f"{entry_iso}.json"
        d = None
        if cand.exists():
            d = json.loads(cand.read_text())
        else:
            latest = base / "latest.json"
            if latest.exists():
                lj = json.loads(latest.read_text())
                if isinstance(lj, dict) and str(lj.get("asof") or "")[:10] == entry_iso:
                    d = lj
        if not isinstance(d, dict):
            return None
        planes = d.get("planes") or {}
        out = {}
        for name, rec in planes.items():
            if isinstance(rec, dict) and rec.get("direction") is not None:
                out[name] = rec.get("direction")
        return out or None
    except Exception:  # noqa: BLE001
        return None


def _taxonomy_hint(row: dict) -> str:
    """A deterministic FIRST-GUESS taxonomy for a bad draft (the seat may override in its lesson).

    This is only a hint surfaced to the seat — never authoritative. Heuristics stay conservative:
      * a VETO/EXIT that graded wrong (the seat blocked/sold something that then worked) → bad-timing
      * a hold/champion/lead-call that graded wrong → bad-signal
    Everything else → "" (let the seat classify). Never raises."""
    try:
        verb = str(row.get("verb") or "").lower()
        if verb in ("veto", "exit"):
            return "bad-timing"
        if verb in ("hold", "champion", "lead-call", "rotation_call"):
            return "bad-signal"
    except Exception:  # noqa: BLE001
        pass
    return ""


def _draft_id(seat: str, row: dict) -> str:
    return f"{seat}:{row.get('date')}:{str(row.get('ticker') or '').upper()}:{row.get('verb') or 'call'}"


def draft_resolutions(seat: str, asof: date | None = None, *, rows: list[dict] | None = None) -> int:
    """Auto-draft every NEWLY-resolved graded call for ``seat`` (deterministic, no LLM). (task a)

    Reads the seat's resolved detail rows from the existing self_mirror row builders (the same graded
    resolutions calibration produces: date, ticker, outcome, conf, rel, verb, benchmark) and appends
    one draft per row not already drafted. Idempotent by draft id. Returns the number of NEW drafts.

    A GOOD-grade row (rel > bad_grade_max) is drafted with status 'skipped' — the seat MAY narrate a
    success but is not REQUIRED to. A BAD-grade row is drafted 'unresolved' — the conscious duty owes
    it a lesson. Best-effort; a failure yields 0 (P2 no-op)."""
    try:
        asof = asof or date.today()
        if rows is None:
            from brain import self_mirror
            rows = self_mirror.rows(seat, asof)
        rows = rows or []
        cfg = _cfg()
        bad_max = float(cfg["bad_grade_max"])

        existing = load_drafts(seat)
        seen = {d.get("id") for d in existing if isinstance(d, dict)}
        added = 0
        for r in rows:
            if not isinstance(r, dict):
                continue
            if r.get("rel") is None or r.get("date") is None or not r.get("ticker"):
                continue
            did = _draft_id(seat, r)
            if did in seen:
                continue
            grade = float(r["rel"])
            entry_iso = str(r.get("date"))[:10]
            outcome = int(r.get("outcome") or 0)
            is_bad = (outcome == 0) and (grade <= bad_max)
            draft = {
                "id": did,
                "seat": seat,
                "date": entry_iso,
                "resolved_on": asof.isoformat(),
                "call": f"{r.get('verb') or 'call'} {str(r.get('ticker')).upper()}",
                "thesis_at_entry": str(r.get("thesis") or r.get("summary") or "")[:400] or None,
                "planes_at_entry": _planes_at_entry(entry_iso),
                "outcome": outcome,
                "grade": round(grade, 4),
                "benchmark": r.get("benchmark") or "SPY",
                "regime_context": _regime_context(asof),
                "close_reason": str(r.get("close_reason") or r.get("verb") or "resolved")[:120],
                "taxonomy_hint": _taxonomy_hint(r) if is_bad else "",
                "lesson_id": None,
                "status": "unresolved" if is_bad else "skipped",
            }
            existing.append(draft)
            seen.add(did)
            added += 1

        if added:
            # ring-buffer cap: prune oldest COMPLETED/skipped drafts first, keep every unresolved one
            existing = _cap_drafts(existing, int(cfg["cap_drafts_per_seat"]))
            _write_json(_drafts_path(seat), existing)
        return added
    except Exception:  # noqa: BLE001 — auto-draft is best-effort; never break a build
        return 0


def _cap_drafts(drafts: list[dict], cap: int) -> list[dict]:
    """Keep at most `cap` drafts. Never drop an 'unresolved' draft (the duty still owes it a lesson);
    prune the oldest 'lesson'/'skipped' drafts by resolved_on."""
    if cap <= 0 or len(drafts) <= cap:
        return drafts
    unresolved = [d for d in drafts if d.get("status") == "unresolved"]
    others = [d for d in drafts if d.get("status") != "unresolved"]
    others.sort(key=lambda d: str(d.get("resolved_on") or ""))
    keep_others = others[-max(0, cap - len(unresolved)):] if cap > len(unresolved) else []
    # preserve original order for the survivors
    survivors = {id(d) for d in unresolved} | {id(d) for d in keep_others}
    return [d for d in drafts if id(d) in survivors]


def draft_all(asof: date | None = None) -> dict:
    """Auto-draft every seat (the nightly hook). Returns {seat: n_new_drafts}. Never raises."""
    out = {}
    for seat in SEATS:
        try:
            out[seat] = draft_resolutions(seat, asof)
        except Exception:  # noqa: BLE001
            out[seat] = 0
    return out


# ───────────────────────────── (b) CONSCIOUS DUTY ─────────────────────────────
def pending_for(seat: str, limit: int | None = None) -> list[dict]:
    """The last-N unresolved bad-grade drafts the seat MUST address on its next build. (task b)

    Ordered most-recent-first (the freshest lessons matter most). ``limit`` defaults to doctrine
    journal.last_n_drafts. Best-effort → [] on any failure (P2: no duty if we can't read it)."""
    try:
        cfg = _cfg()
        n = int(limit if limit is not None else cfg["last_n_drafts"])
        drafts = [d for d in load_drafts(seat) if d.get("status") == "unresolved"]
        drafts.sort(key=lambda d: (str(d.get("date") or ""), str(d.get("id") or "")), reverse=True)
        return drafts[:n]
    except Exception:  # noqa: BLE001
        return []


def duty_block(seat: str, limit: int | None = None) -> str:
    """A prompt-ready block listing the seat's unresolved bad-grade drafts + the required lesson
    schema (mirrors the three-questions duty text). "" when nothing is owed. Never raises."""
    try:
        pend = pending_for(seat, limit)
        if not pend:
            return ""
        lines = [
            "--- YOUR JOURNAL DUTY (complete a lesson for each below — charter P6) ---",
            "For each of your calls that graded badly, you MUST record a lesson:",
            "  {what_i_believed, what_actually_happened,",
            "   why_wrong: one of " + " | ".join(TAXONOMY) + ",",
            "   rule_i_adopt: ONE falsifiable sentence, confidence_in_rule: 0..1}",
            "Your recent calls that graded badly:",
        ]
        for d in pend:
            hint = f" (hint: {d['taxonomy_hint']})" if d.get("taxonomy_hint") else ""
            lines.append(f"  - [{d.get('id')}] {d.get('call')} on {d.get('date')} "
                         f"graded {float(d.get('grade') or 0):+.1%} vs {d.get('benchmark')}{hint}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""


def _mk_lesson_id(seat: str, draft_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{seat}:lesson:{draft_id}:{stamp}"


def _valid_mistake(c: dict) -> bool:
    tax = str(c.get("why_wrong") or "").strip()
    rule = str(c.get("rule_i_adopt") or "").strip()
    return bool(tax in TAXONOMY and rule)


def _valid_success(c: dict) -> bool:
    return bool(str(c.get("rule_i_keep") or "").strip())


def complete(seat: str, completions: list[dict], asof: date | None = None) -> dict:
    """Record the seat's conscious lessons for its pending drafts. (task b)

    ``completions`` is a list of dicts, each keyed by ``draft_id`` and carrying either a MISTAKE
    lesson {what_i_believed, what_actually_happened, why_wrong (taxonomy), rule_i_adopt,
    confidence_in_rule} or a SUCCESS lesson {what_worked, skill_or_luck, rule_i_keep,
    confidence_in_rule}. A completion for an unknown/already-resolved draft is ignored. A completion
    missing its required fields is ACCEPTED-as-incomplete: the draft stays unresolved and we log
    'journal_incomplete' (add-only, the three_questions_incomplete posture — never reject).

    Returns {recorded, incomplete, unknown}. Best-effort; never raises. Also runs the curation cap +
    triggers a pin recompute so newly-adopted rules can earn a pin."""
    try:
        asof = asof or date.today()
        drafts = load_drafts(seat)
        by_id = {d.get("id"): d for d in drafts if isinstance(d, dict)}
        lessons = load_lessons(seat)
        recorded = incomplete = unknown = 0

        for c in (completions or []):
            if not isinstance(c, dict):
                continue
            draft_id = c.get("draft_id") or c.get("id")
            draft = by_id.get(draft_id)
            if draft is None or draft.get("status") not in ("unresolved", "skipped"):
                unknown += 1
                continue

            is_success = draft.get("status") == "skipped" or bool(
                c.get("rule_i_keep") or c.get("what_worked"))
            if is_success:
                if not _valid_success(c):
                    incomplete += 1
                    _log_incomplete(seat, draft_id, "success")
                    continue
                lesson = {
                    "id": _mk_lesson_id(seat, draft_id), "seat": seat, "draft_id": draft_id,
                    "date": draft.get("date"), "kind": "success",
                    "what_worked": str(c.get("what_worked") or "")[:400],
                    "skill_or_luck": str(c.get("skill_or_luck") or "")[:200],
                    "rule_i_keep": str(c.get("rule_i_keep") or "")[:300],
                    "confidence_in_rule": _clip01(c.get("confidence_in_rule")),
                    "grade": draft.get("grade"),
                }
            else:
                if not _valid_mistake(c):
                    incomplete += 1
                    _log_incomplete(seat, draft_id, "mistake")
                    continue
                lesson = {
                    "id": _mk_lesson_id(seat, draft_id), "seat": seat, "draft_id": draft_id,
                    "date": draft.get("date"), "kind": "mistake",
                    "what_i_believed": str(c.get("what_i_believed") or "")[:400],
                    "what_actually_happened": str(c.get("what_actually_happened") or "")[:400],
                    "why_wrong": str(c.get("why_wrong")).strip(),
                    "rule_i_adopt": str(c.get("rule_i_adopt") or "")[:300],
                    "confidence_in_rule": _clip01(c.get("confidence_in_rule")),
                    "grade": draft.get("grade"),
                }
            lessons.append(lesson)
            draft["status"] = "lesson"
            draft["lesson_id"] = lesson["id"]
            recorded += 1

        if recorded:
            cfg = _cfg()
            lessons = _cap_lessons(lessons, int(cfg["cap_lessons_per_seat"]))
            _write_json(_lessons_path(seat), lessons)
            _write_json(_drafts_path(seat), drafts)
            try:
                recompute_pins(seat, asof)
            except Exception:  # noqa: BLE001
                pass
        return {"recorded": recorded, "incomplete": incomplete, "unknown": unknown}
    except Exception:  # noqa: BLE001
        return {"recorded": 0, "incomplete": 0, "unknown": 0}


def _clip01(v) -> float | None:
    try:
        return round(max(0.0, min(1.0, float(v))), 3)
    except (TypeError, ValueError):
        return None


def _cap_lessons(lessons: list[dict], cap: int) -> list[dict]:
    """Keep the most recent `cap` lessons (by date, then insertion order). Prunes oldest first."""
    if cap <= 0 or len(lessons) <= cap:
        return lessons
    # stable: newest by date wins; ties keep insertion order → sort by (date) ascending, slice tail
    idx = list(enumerate(lessons))
    idx.sort(key=lambda t: (str(t[1].get("date") or ""), t[0]))
    keep = idx[-cap:]
    keep_ids = {i for i, _ in keep}
    return [lsn for i, lsn in enumerate(lessons) if i in keep_ids]


def _log_incomplete(seat: str, draft_id, kind: str) -> None:
    """Add-only 'journal_incomplete' note (mirrors three_questions_incomplete). Never raises."""
    try:
        from bot.phase2 import _rl_log
    except Exception:  # noqa: BLE001
        _rl_log = None  # type: ignore
    if _rl_log is not None:
        try:
            _rl_log("journal", "duty", "journal_incomplete", f"seat={seat} draft={draft_id} kind={kind}")
        except Exception:  # noqa: BLE001
            pass


# ───────────────────────────── (c) CURATION + PINNING ─────────────────────────────
def _grade_weight(grade) -> float:
    """A lesson's grade-weight for pinning: the |magnitude| of its resolved rel_return.

    A lesson born from a big miss (or a big win) is more load-bearing than a marginal one. Missing
    grade → a floor weight so the lesson still counts once (recurrence carries it)."""
    try:
        return abs(float(grade))
    except (TypeError, ValueError):
        return 0.01


def cluster_by_taxonomy(seat: str) -> dict:
    """Cluster the seat's MISTAKE lessons by why_wrong taxonomy → {taxonomy: [lessons]}. (task c)

    Successes are clustered separately under a synthetic 'keep' bucket (they carry rule_i_keep, not a
    taxonomy). Best-effort → {} on failure."""
    out: dict = {}
    try:
        for lsn in load_lessons(seat):
            if not isinstance(lsn, dict):
                continue
            if lsn.get("kind") == "success":
                out.setdefault("keep", []).append(lsn)
            else:
                tax = str(lsn.get("why_wrong") or "").strip()
                if tax in TAXONOMY:
                    out.setdefault(tax, []).append(lsn)
    except Exception:  # noqa: BLE001
        pass
    return out


def _same_taxonomy_calls_since(seat: str, taxonomy: str, since_iso: str, asof: date | None) -> list[dict]:
    """The seat's drafts whose taxonomy_hint (or completed why_wrong) == taxonomy, dated AFTER
    since_iso — the falsifier evidence for a pin adopted on since_iso. Uses the deterministic
    taxonomy_hint (the pre-lesson guess) so the falsifier can grade calls the seat has NOT yet
    written a lesson for. Bad-grade share of these post-pin calls is the falsifier metric."""
    out = []
    try:
        for d in load_drafts(seat):
            if not isinstance(d, dict):
                continue
            if str(d.get("date") or "")[:10] <= since_iso:
                continue
            # a call belongs to this taxonomy if its hint matches OR its completed lesson's why_wrong
            tx = d.get("taxonomy_hint") or ""
            if tx != taxonomy and d.get("status") == "lesson":
                lsn = _lesson_for_draft(seat, d.get("lesson_id"))
                tx = (lsn or {}).get("why_wrong") or tx
            if tx == taxonomy:
                out.append(d)
    except Exception:  # noqa: BLE001
        pass
    return out


def _lesson_for_draft(seat: str, lesson_id) -> dict | None:
    if not lesson_id:
        return None
    for lsn in load_lessons(seat):
        if isinstance(lsn, dict) and lsn.get("id") == lesson_id:
            return lsn
    return None


def recompute_pins(seat: str, asof: date | None = None) -> list[dict]:
    """Recompute the seat's pinned rules: pick top-K by grade-weighted recurrence, then run each
    ACTIVE pin's falsifier and auto-unpin the ones that stopped predicting. (task c)

    Pinning arithmetic:
      * cluster mistake lessons by taxonomy; a taxonomy is PINNABLE only if it recurs
        >= pin_min_recurrence times.
      * the pinned RULE for a taxonomy = the most-recent lesson's rule_i_adopt whose
        confidence_in_rule >= pin_min_confidence (a low-confidence rule never earns prompt space).
      * weight = sum of grade-weights across the cluster × recurrence-boost.
      * top-K taxonomies by weight are pinned; ties broken by recurrence then recency.

    Falsifier / auto-unpin:
      * a pin adopted on ``pinned_on`` predicts the taxonomy will stop recurring. We count the seat's
        same-taxonomy calls dated after pinned_on; once >= falsifier_min_n have resolved, if the
        bad-grade share is STILL >= falsifier_fail_rate the pin has FAILED — it auto-unpins with a
        journal note (the rule did not fix the behaviour; even lessons earn authority — P3).

    Persists pins.json. Returns the ACTIVE pins. Best-effort → returns current pins on failure."""
    try:
        asof = asof or date.today()
        cfg = _cfg()
        clusters = cluster_by_taxonomy(seat)
        existing = load_pins(seat)
        by_tax = {p.get("taxonomy"): p for p in existing if isinstance(p, dict)}

        # 1) score every pinnable taxonomy cluster
        candidates = []
        for tax, lessons in clusters.items():
            if tax == "keep":
                continue
            recurrence = len(lessons)
            if recurrence < int(cfg["pin_min_recurrence"]):
                continue
            # the adopted rule = most-recent lesson with sufficient confidence
            rule, conf = None, None
            for lsn in sorted(lessons, key=lambda l: str(l.get("date") or ""), reverse=True):
                c = lsn.get("confidence_in_rule")
                if c is not None and float(c) >= float(cfg["pin_min_confidence"]) and lsn.get("rule_i_adopt"):
                    rule, conf = lsn.get("rule_i_adopt"), float(c)
                    break
            if not rule:
                continue
            weight = sum(_grade_weight(l.get("grade")) for l in lessons) * (1.0 + 0.1 * recurrence)
            candidates.append({"taxonomy": tax, "rule": rule, "kind": "mistake",
                               "confidence": round(conf, 3), "recurrence": recurrence,
                               "weight": round(weight, 5)})

        candidates.sort(key=lambda c: (-c["weight"], -c["recurrence"], c["taxonomy"]))
        top = candidates[: int(cfg["pin_top_k"])]
        top_tax = {c["taxonomy"] for c in top}

        # 2) build the new pin registry, preserving pinned_on + falsifier state for surviving pins
        new_pins: list[dict] = []
        for c in top:
            prev = by_tax.get(c["taxonomy"])
            if prev and prev.get("status") == "unpinned":
                # a taxonomy that reverted stays visible as unpinned history but is NOT re-pinned this
                # pass unless its rule text changed (a genuinely new lesson may re-earn the pin).
                if prev.get("rule") == c["rule"]:
                    new_pins.append(prev)
                    continue
            pinned_on = (prev.get("pinned_on") if prev and prev.get("rule") == c["rule"]
                         else asof.isoformat())
            new_pins.append({
                "taxonomy": c["taxonomy"], "rule": c["rule"], "kind": "mistake",
                "confidence": c["confidence"], "weight": c["weight"], "recurrence": c["recurrence"],
                "pinned_on": pinned_on, "calls_seen": 0, "calls_wrong": 0,
                "status": "active", "unpin_reason": None, "unpinned_on": None,
            })

        # carry forward any previously-unpinned taxonomies that dropped out of the top-K (history)
        for tax, prev in by_tax.items():
            if tax not in top_tax and prev.get("status") == "unpinned":
                new_pins.append(prev)

        # 3) falsifier pass on every ACTIVE pin
        for pin in new_pins:
            if pin.get("status") != "active":
                continue
            _apply_falsifier(seat, pin, asof, cfg)

        _write_json(_pins_path(seat), new_pins)
        return [p for p in new_pins if p.get("status") == "active"]
    except Exception:  # noqa: BLE001
        return [p for p in load_pins(seat) if p.get("status") == "active"]


def _apply_falsifier(seat: str, pin: dict, asof: date | None, cfg: dict) -> None:
    """Grade an active pin's own falsifier and auto-unpin it if it stopped predicting. In place.

    The pin PREDICTS the taxonomy will stop recurring after adoption. Conservative reading: we count
    the seat's calls DATED AFTER pinned_on that still belong to this taxonomy (their taxonomy_hint
    matches, or their completed lesson's why_wrong matches). Once falsifier_min_n such calls have
    accrued, if the mis-graded share is STILL >= falsifier_fail_rate the pin FAILED — the rule did not
    fix the behaviour, so it auto-unpins (P3: even a lesson earns its authority). A pin only unpins on
    genuine recurrence of the SAME mistake class, never on unrelated good calls."""
    try:
        calls = _same_taxonomy_calls_since(seat, pin.get("taxonomy"), str(pin.get("pinned_on") or "")[:10], asof)
        seen = len(calls)
        wrong = sum(1 for d in calls if d.get("status") in ("unresolved", "lesson"))
        pin["calls_seen"] = seen
        pin["calls_wrong"] = wrong
        if seen >= int(cfg["falsifier_min_n"]):
            fail_rate = wrong / seen if seen else 0.0
            if fail_rate >= float(cfg["falsifier_fail_rate"]):
                pin["status"] = "unpinned"
                pin["unpin_reason"] = (f"falsifier failed: {wrong}/{seen} same-taxonomy calls still "
                                       f"mis-graded post-pin (>= {cfg['falsifier_fail_rate']:.0%})")
                pin["unpinned_on"] = (asof or date.today()).isoformat()
    except Exception:  # noqa: BLE001
        pass


def injection_block(seat: str) -> str:
    """The seat's ACTIVE pinned rules, rendered for the self_mirror injection seam. (task c)

    "" when nothing is pinned (P2 no-op → the injection returns the prompt unchanged). Charter P7:
    this is the ONE contract self_mirror injects; the journal does not duplicate the injection."""
    try:
        active = [p for p in load_pins(seat) if p.get("status") == "active" and p.get("rule")]
        if not active:
            return ""
        active.sort(key=lambda p: -float(p.get("weight") or 0.0))
        lines = ["--- YOUR PINNED LESSONS (journal; earned rules, auto-unpin on failure) ---"]
        for p in active:
            lines.append(f"  - [{p.get('taxonomy')}] {p.get('rule')}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""


# ───────────────────────────── (d) RETROFIT ─────────────────────────────
def backfill_incident(entries: dict, asof: date | None = None, *, overwrite: bool = False) -> dict:
    """One-shot backfill: draft the 2026-07-02 incident entries for each seat. (task d)

    ``entries`` maps seat → list of draft dicts (each a partial draft: at minimum {date, call,
    thesis_at_entry, outcome, grade, close_reason, why_wrong?, planes_at_entry?}). This is the
    system's FIRST memory — the week that taught it everything — reconstructed from the incident
    fixtures + the decisions quotes. Deterministic, no LLM.

    Idempotent: a seat that already has a backfilled incident draft is skipped unless overwrite=True.
    Returns {seat: n_backfilled}. Best-effort; never raises."""
    try:
        asof = asof or date.today()
        cfg = _cfg()
        out = {}
        for seat, seat_entries in (entries or {}).items():
            drafts = load_drafts(seat)
            seen = {d.get("id") for d in drafts if isinstance(d, dict)}
            added = 0
            for e in (seat_entries or []):
                if not isinstance(e, dict):
                    continue
                entry_iso = str(e.get("date") or "")[:10]
                verb = str(e.get("verb") or e.get("call") or "call").split()[0].lower()
                did = e.get("id") or f"{seat}:{entry_iso}:{str(e.get('ticker') or 'INCIDENT').upper()}:{verb}"
                if did in seen and not overwrite:
                    continue
                if did in seen and overwrite:
                    drafts = [d for d in drafts if d.get("id") != did]
                grade = e.get("grade")
                try:
                    g = float(grade) if grade is not None else None
                except (TypeError, ValueError):
                    g = None
                outcome = int(e.get("outcome") if e.get("outcome") is not None
                              else (0 if (g is not None and g <= float(cfg["bad_grade_max"])) else 1))
                is_bad = outcome == 0
                draft = {
                    "id": did, "seat": seat, "date": entry_iso,
                    "resolved_on": asof.isoformat(),
                    "call": str(e.get("call") or f"{verb} {e.get('ticker') or ''}").strip(),
                    "thesis_at_entry": str(e.get("thesis_at_entry") or "")[:400] or None,
                    "planes_at_entry": e.get("planes_at_entry") or _planes_at_entry(entry_iso),
                    "outcome": outcome,
                    "grade": round(g, 4) if g is not None else None,
                    "benchmark": e.get("benchmark") or "SPY",
                    "regime_context": str(e.get("regime_context") or "")[:120],
                    "close_reason": str(e.get("close_reason") or "incident")[:120],
                    "taxonomy_hint": str(e.get("why_wrong") or "") if is_bad else "",
                    "lesson_id": None,
                    "status": "unresolved" if is_bad else "skipped",
                    "backfilled": True,
                }
                drafts.append(draft)
                seen.add(did)
                added += 1
            if added:
                drafts = _cap_drafts(drafts, int(cfg["cap_drafts_per_seat"]))
                _write_json(_drafts_path(seat), drafts)
            out[seat] = added
        return out
    except Exception:  # noqa: BLE001
        return {}


# ───────────────────────────── FOUNDING INCIDENT (the first memories) ─────────────────────────────
# The 2026-07-02 Semis/Memory/AI breakdown — the week that taught the system everything. These are
# reconstructed from the incident report (research/incidents/2026-07-02-semis-breakdown/) + the
# fixtures (tests/incident_replays/fixtures/2026-07-02-semis-breakdown/). Grades are the realized
# rel_return over the incident window from etf_closes.json (subject vs SPY): SMH -7.3% while SPY was
# roughly flat → ~-7% rel; XLV +7.8% vs SPY → ~+7.5% rel. Each carries a taxonomy hint so the
# retroactive lesson (below) can be pre-supplied — this backfill is BOTH the drafts AND the first
# conscious lessons, because the incident report already wrote them.
FOUNDING_INCIDENT_DRAFTS: dict = {
    # AUTONOMOUS — the epicenter whipsaw: sold SMH 07-01 @ $626, rebought 07-02 @ $605.70 into the
    # breakdown, reasoning from "Goldilocks + SMH #1 RS". The single most damning behavior.
    "autonomous": [
        {"date": "2026-07-02", "ticker": "SMH", "verb": "rebuy",
         "call": "rebuy SMH into the breakdown",
         "thesis_at_entry": "Goldilocks label + SMH #1 RS; intake queue 'corroborated' more semis.",
         "grade": -0.073, "outcome": 0, "benchmark": "SPY",
         "regime_context": "label=risk_on/Goldilocks; radar read CAUTION 91/100 the bot never saw",
         "close_reason": "bought 41 SMH @ $605.70 into a -7.3% week; firm caps later rejected it",
         "why_wrong": "crowd-follow"},
    ],
    # PM (flagship judgment) — the ONE bright spot: the armed dry-run PM independently trimmed semis
    # and held 29% cash. A SUCCESS worth keeping (skill vs luck checked against the regime).
    "pm": [
        {"date": "2026-07-01", "ticker": "SMH", "verb": "trim",
         "call": "trim semis and hold 29% cash",
         "thesis_at_entry": "Crowding extreme (SMH 99th pctile/+47% 60d) + short-gamma vol-hole; "
                            "did not trust the Goldilocks label.",
         "grade": 0.06, "outcome": 1, "benchmark": "SPY",
         "regime_context": "deterministic label risk_on; PM read the crowding + gamma tells instead",
         "close_reason": "held cash into the semis breakdown; beat the offensive books"},
    ],
    # STRATEGIST — ate the wrong Goldilocks label whole; every US book was 60-90% offensive on it.
    "strategist": [
        {"date": "2026-07-01", "ticker": "SMH", "verb": "lead-call",
         "call": "confirmed AI/semis leadership on the Goldilocks label",
         "thesis_at_entry": "Q1 Goldilocks (growth +0.333, inflation -0.52); AI build-out leadership.",
         "grade": -0.073, "outcome": 0, "benchmark": "SPY",
         "regime_context": "regime confidence had collapsed 0.68->0.18; the label didn't move",
         "close_reason": "leadership call rode the label into the breakdown",
         "why_wrong": "label-trust"},
    ],
    # GATE OFFICER — did not veto the offensive concentration; the disagreement plane (radar CAUTION,
    # def-RS crossover, Tech Topping/SELL) existed but was unread. ignored-plane, textbook.
    "gate": [
        {"date": "2026-07-01", "ticker": "SMH", "verb": "veto",
         "call": "did not veto the all-offense semis concentration",
         "thesis_at_entry": "No disagreement machinery; trusted the single Goldilocks label plane.",
         "grade": -0.073, "outcome": 0, "benchmark": "SPY",
         "regime_context": "radar CAUTION 91/100 + Tech Topping/SELL + def-RS crossover all unread",
         "close_reason": "let a single-plane offensive book through the gate",
         "why_wrong": "ignored-plane"},
    ],
}

# The retroactive conscious lessons — the incident report ALREADY wrote them, so the founding memory
# arrives complete (a mistake with an adopted rule; the success with its skill/luck check). Keyed by
# the (deterministic) draft id the backfill mints, so complete() can attach them.
FOUNDING_INCIDENT_LESSONS: dict = {
    "autonomous": [{
        "why_wrong": "crowd-follow",
        "what_i_believed": "SMH was the #1 RS name in a Goldilocks regime, so a dip was a rebuy.",
        "what_actually_happened": "The intake funnel samples what is already leading; I rebought "
                                  "the epicenter into a -7.3% breakdown week.",
        "rule_i_adopt": "Never rebuy a name into a fresh multi-session breakdown while it is >95th "
                        "pctile crowded, regardless of its RS rank.",
        "confidence_in_rule": 0.8,
    }],
    "pm": [{
        "what_worked": "I distrusted the Goldilocks label and read the crowding + short-gamma tells, "
                       "trimming semis and holding 29% cash before the breakdown.",
        "skill_or_luck": "skill — the crowding (99th pctile) + gamma-hole tells were live and I acted "
                         "on them; the regime label was doing the WRONG work, not carrying my call.",
        "rule_i_keep": "When crowding is >95th pctile and dealers are short gamma, size DOWN even if "
                       "the regime label says risk_on.",
        "confidence_in_rule": 0.75,
    }],
    "strategist": [{
        "why_wrong": "label-trust",
        "what_i_believed": "The Q1 Goldilocks label was valid, so AI/semis leadership was confirmable.",
        "what_actually_happened": "Regime confidence had collapsed 0.68->0.18 but the label didn't "
                                  "move; I confirmed leadership into a topping cluster.",
        "rule_i_adopt": "Do not confirm leadership on a regime label whose confidence has fallen "
                        "below 0.35 without a second, independent plane agreeing.",
        "confidence_in_rule": 0.7,
    }],
    "gate": [{
        "why_wrong": "ignored-plane",
        "what_i_believed": "The single Goldilocks label plane was enough to pass an offensive book.",
        "what_actually_happened": "The radar (CAUTION 91/100), the cycle read (Tech Topping/SELL) and "
                                  "the defensive-RS crossover all dissented and I read none of them.",
        "rule_i_adopt": "Never pass an all-offense book on a single plane when any validated plane "
                        "reads risk_off/caution (charter P1 made executable).",
        "confidence_in_rule": 0.85,
    }],
}


def backfill_founding_incident(asof: date | None = None, *, complete_lessons: bool = True,
                               overwrite: bool = False) -> dict:
    """One-shot: draft the 2026-07-02 founding-incident entries for each seat, and (by default)
    attach the retroactive conscious lessons the incident report already wrote. (task d)

    This is the system's FIRST memory. It calls ``backfill_incident`` with the canonical
    FOUNDING_INCIDENT_DRAFTS, then — when ``complete_lessons`` — feeds the matching
    FOUNDING_INCIDENT_LESSONS through ``complete`` so the mistakes arrive with adopted rules and the
    success arrives with its skill/luck check (so the very first pins can form). Idempotent unless
    overwrite=True. Returns {seat: {drafted, lessons_recorded}}. Best-effort; never raises."""
    try:
        asof = asof or date.today()
        drafted = backfill_incident(FOUNDING_INCIDENT_DRAFTS, asof, overwrite=overwrite)
        out = {seat: {"drafted": n, "lessons_recorded": 0} for seat, n in drafted.items()}
        if not complete_lessons:
            return out
        for seat, lessons in FOUNDING_INCIDENT_LESSONS.items():
            try:
                # match each canonical lesson to the seat's unresolved/skipped backfilled draft by
                # taxonomy (mistakes) or by being the sole success draft.
                pend = [d for d in load_drafts(seat)
                        if d.get("backfilled") and d.get("status") in ("unresolved", "skipped")]
                completions = []
                for lsn in lessons:
                    tgt = None
                    if lsn.get("why_wrong"):
                        tgt = next((d for d in pend if d.get("taxonomy_hint") == lsn["why_wrong"]), None)
                    if tgt is None:
                        tgt = next((d for d in pend if d.get("status") == "skipped"), None)
                    if tgt is None and pend:
                        tgt = pend[0]
                    if tgt is not None:
                        completions.append({**lsn, "draft_id": tgt.get("id")})
                        pend = [d for d in pend if d is not tgt]
                if completions:
                    res = complete(seat, completions, asof)
                    out.setdefault(seat, {"drafted": 0})["lessons_recorded"] = res.get("recorded", 0)
            except Exception:  # noqa: BLE001
                continue
        return out
    except Exception:  # noqa: BLE001
        return {}

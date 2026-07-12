"""Experiment registry — W-L / L6.

Tracks every long-running, accruing experiment the system has open, each with a
comeback date or maturity condition, current status, owner, and artifact paths. The
weekly improvement agenda consumes `matured()` to surface the highest-priority items
that are ready for review but have not yet been judged.

Data lives in ``data/experiments/registry.json``.  The file is the authoritative
source; this module is the read/write contract (never edit the JSON by hand outside
the add/update/resolve functions here).

Design invariants (charter P3/P8):
  - REGISTRY IS APPEND-ONLY for statuses — an experiment can only go OPEN →
    MATURED → JUDGED (or OPEN → CANCELLED), never backwards.
  - Gate thresholds (``gate``) are declared by the program, not by the experiment
    itself.  They are never self-modifiable.
  - ``matured()`` is the ONLY function the agenda calls — it returns items that are
    ready for a human or Opus session to judge, in priority order (earliest
    comeback_date first, then by id).
  - P2: any failure in load/save degrades to an empty list, never raises.
  - MW2 addition: ``evaluate(item, asof)`` returns a tri-state verdict
    (not_old_enough / blocked_missing_evidence / ready_for_review) driven by a
    registry of PER-EXPERIMENT mechanical evaluator functions keyed by id.  LLMs may
    only de-escalate — evaluate() NEVER promotes to matured/resolve; the date-driven
    auto-maturation path in matured() is untouched.

Experiment schema (all fields required in seed; optional fields may be absent in
the JSON and will be filled with safe defaults):

  {
    "id":               str,          # stable slug, e.g. "shadow-trim-ladder"
    "what":             str,          # one-sentence description
    "gate":             str,          # the condition that flips status → matured
    "comeback_date":    str | null,   # ISO date: when to re-check; null = condition-only
    "maturity_condition": str,        # prose description of the objective test
    "status":           str,          # open | matured | judged | cancelled
    "owner":            str,          # opus-session | fable-review | self-tune
    "artifact_paths":  [str],         # relative paths where evidence lives
    "notes":            str,          # free-form (may be "")
    "_evaluator_first_blocked": str | null  # ISO date first seen as blocked (MW2 sidecar)
  }

Tri-state evaluate() return schema:
  {
    "state":              str,          # not_old_enough | blocked_missing_evidence | ready_for_review
    "reason":             str,          # human-readable explanation
    "evidence_n":         int | null,   # current count when computable
    "required_n":         int | null,   # threshold when computable
    "expected_ready_date": str | null,  # ISO date estimate when computable
    "stuck":              bool,         # True if blocked >14 days with no comeback_date
  }
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

_ROOT = Path(__file__).resolve().parent.parent
_REGISTRY_PATH = _ROOT / "data" / "experiments" / "registry.json"

# Valid status transitions (OPEN is the initial state).
_TRANSITIONS: dict[str, set[str]] = {
    "open":     {"matured", "cancelled"},
    "matured":  {"judged", "open"},        # re-open if judged prematurely
    "judged":   set(),                     # terminal
    "cancelled": set(),                    # terminal
}


# ── load / save ───────────────────────────────────────────────────────────────

def _load() -> list[dict]:
    """Load registry.json → list[dict].  Returns [] on any failure (P2)."""
    try:
        if not _REGISTRY_PATH.exists():
            return []
        raw = _REGISTRY_PATH.read_text()
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        # support {experiments: [...]} envelope
        if isinstance(data, dict):
            return data.get("experiments") or []
        return []
    except Exception:  # noqa: BLE001
        return []


def _save(experiments: list[dict]) -> bool:
    """Persist the registry.  Returns True on success."""
    try:
        _REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        _REGISTRY_PATH.write_text(json.dumps(experiments, indent=2, default=str))
        return True
    except Exception:  # noqa: BLE001
        return False


def _defaults(exp: dict) -> dict:
    """Fill missing optional fields with safe defaults so callers never KeyError."""
    exp.setdefault("comeback_date", None)
    exp.setdefault("maturity_condition", exp.get("gate", ""))
    exp.setdefault("status", "open")
    exp.setdefault("owner", "opus-session")
    exp.setdefault("artifact_paths", [])
    exp.setdefault("notes", "")
    exp.setdefault("_evaluator_first_blocked", None)
    return exp


# ── tri-state evaluator ───────────────────────────────────────────────────────
#
# State vocabulary (the only three strings evaluate() may return):
STATE_NOT_OLD_ENOUGH = "not_old_enough"          # date-driven: comeback_date not yet reached
STATE_BLOCKED = "blocked_missing_evidence"        # condition-only: evidence absent/insufficient
STATE_READY = "ready_for_review"                  # evidence threshold met → surface for judgment
_STUCK_DAYS = 14   # items blocked longer than this without a comeback_date are flagged stuck

# Type alias: each evaluator receives (item: dict, asof: date) and returns a partial result
# dict (state + reason + optional evidence_n/required_n/expected_ready_date).  It must never
# raise and must never mutate `item`.
_EvaluatorFn = Callable[[dict, date], dict]


def _eval_result(state: str, reason: str, *,
                 evidence_n: int | None = None,
                 required_n: int | None = None,
                 expected_ready_date: str | None = None) -> dict:
    """Construct a partial evaluate() result (without the stuck flag, which is added by evaluate())."""
    return {
        "state": state,
        "reason": reason,
        "evidence_n": evidence_n,
        "required_n": required_n,
        "expected_ready_date": expected_ready_date,
    }


# ─── mechanical evaluators (one per condition-only experiment) ────────────────

def _eval_shadow_trim_ladder(item: dict, asof: date) -> dict:
    """shadow-trim-ladder: ready when >=40 graded shadow trim entries exist.

    Scans data/shadow/distribution_trims/*.json and counts rows with graded=True.
    Required: 40 graded trims (pre-registered falsifier in portfolio/distribution_tells.py).
    Expected pacing: ~5 nightly sessions/week when the book is active → ~8 weeks to threshold.
    """
    required_n = 40
    trims_dir = _ROOT / "data" / "shadow" / "distribution_trims"
    if not trims_dir.exists():
        return _eval_result(
            STATE_BLOCKED,
            f"data/shadow/distribution_trims/ does not exist yet — no graded trims (need {required_n})",
            evidence_n=0, required_n=required_n,
        )
    try:
        graded_count = 0
        for f in sorted(trims_dir.glob("*.json")):
            try:
                payload = json.loads(f.read_text())
                trims = payload.get("trims") or []
                graded_count += sum(1 for t in trims if t.get("graded") is True)
            except Exception:  # noqa: BLE001
                continue
        if graded_count >= required_n:
            return _eval_result(
                STATE_READY,
                f">=40 graded trims reached ({graded_count} graded entries in distribution_trims/)",
                evidence_n=graded_count, required_n=required_n,
            )
        remaining = required_n - graded_count
        # rough pacing: ~5 active sessions/week; 1 trim per session when distributing names exist
        weeks_est = max(1, (remaining + 4) // 5)
        ready_est = (asof + timedelta(weeks=weeks_est)).isoformat()
        return _eval_result(
            STATE_BLOCKED,
            f"graded trim count {graded_count} < required {required_n} "
            f"({remaining} more needed; ~{weeks_est}w at current pacing)",
            evidence_n=graded_count, required_n=required_n,
            expected_ready_date=ready_est,
        )
    except Exception:  # noqa: BLE001
        return _eval_result(
            STATE_BLOCKED,
            f"error reading data/shadow/distribution_trims/ — 0 graded trims confirmed (need {required_n})",
            evidence_n=0, required_n=required_n,
        )


def _eval_governor_arming(item: dict, asof: date) -> dict:
    """governor-arming: ready when effective_n>=8 weekly benchmark-ledger snapshots exist.

    Counts *.json snapshots in data/benchmark/ (excludes _series.json which is a rolling store).
    The posture_governor.guards() function requires effective_n=len(gap_series) >= 8 (its
    _MIN_EFFECTIVE_N constant); each benchmark snapshot contributes one independent weekly review.
    """
    required_n = 8
    bench_dir = _ROOT / "data" / "benchmark"
    if not bench_dir.exists():
        return _eval_result(
            STATE_BLOCKED,
            f"data/benchmark/ does not exist — 0 weekly snapshots (need {required_n})",
            evidence_n=0, required_n=required_n,
        )
    try:
        snapshots = [
            f for f in sorted(bench_dir.glob("*.json"))
            if not f.name.startswith("_")   # exclude _series.json and similar internal files
        ]
        n = len(snapshots)
        if n >= required_n:
            return _eval_result(
                STATE_READY,
                f">=8 weekly benchmark snapshots found ({n} snapshots in data/benchmark/); "
                f"HAC significance test can now run via posture_governor.guards()",
                evidence_n=n, required_n=required_n,
            )
        remaining = required_n - n
        # ~1 snapshot/week
        ready_est = (asof + timedelta(weeks=remaining)).isoformat()
        return _eval_result(
            STATE_BLOCKED,
            f"benchmark snapshot count {n} < required {required_n} "
            f"({remaining} more weekly runs needed)",
            evidence_n=n, required_n=required_n,
            expected_ready_date=ready_est,
        )
    except Exception:  # noqa: BLE001
        return _eval_result(
            STATE_BLOCKED,
            f"error reading data/benchmark/ — 0 snapshots confirmed (need {required_n})",
            evidence_n=0, required_n=required_n,
        )


def _eval_judgment_book_promotion(item: dict, asof: date) -> dict:
    """judgment-book-promotion: ready ONLY when the judgment shadow book has accrued enough
    non-overlapping forward NAV observations to clear the pre-registered forward-shadow bar.

    Gate (registry): "4wk shadow NAV >= engine in WEAKENING/CAUTION vs max(SPY,defensive)".
    The forward significance floor is NOT the calendar — it is pre-registered in
    docs/design/desk/AB_EXPERIMENT.md (§4.2 "Significance", §5 "Decision Rule", §7 "Power"):
      - effective-n >= _MIN_DATES = 8 INDEPENDENT, date-clustered, NON-OVERLAPPING 21-bday
        observations before ANY forward verdict is read; AND
      - a >= 168-calendar-day floor from the first instrumented mark (8 x 21 bdays).
    AB_EXPERIMENT.md §5 verbatim: "If neither arm has cleared its bar, the verdict is
    'INSUFFICIENT POWER — keep running'; no build decision is made and no threshold is revised."
    §7 states plainly: "The 2026-07-17 calibration deadline is BEFORE any forward verdict" — i.e.
    this experiment's comeback_date is calendar-only and MUST NOT auto-mature the promotion.

    Evidence: the judgment shadow book NAV lives at the registry artifact path
    data/shadow/books/flagship_judgment/nav_history.jsonl (one JSON row per date; same schema the
    other shadow books emit: {date, nav, cash, invested, spy_px}). The engine comparator is the
    control book data/shadow/books/prod/ (the braked-engine baseline; AB_EXPERIMENT.md §2.1).

    We count the judgment book's DISTINCT dated NAV rows and derive an effective-n by thinning to
    NON-overlapping 21-trading-day (~29 calendar-day) clusters — greedy >= _INDEP_GAP_DAYS spacing,
    mirroring the _thin_independent contract AB_EXPERIMENT.md §4.2 cites. Ready requires BOTH
    effective-n >= _MIN_DATES AND the calendar span >= _FORWARD_FLOOR_DAYS. Anything short → blocked
    (== insufficient_power). When the threshold or the data is ambiguous we default to blocked: it
    is always safe to UNDER-arm; the bug being fixed is auto-maturing on the calendar.
    """
    # Pre-registered thresholds — source: docs/design/desk/AB_EXPERIMENT.md §4.2/§5/§7.
    # DO NOT tune these to make the experiment pass; revising them voids the pre-registration.
    required_effective_n = 8      # _MIN_DATES (AB_EXPERIMENT.md §4.2 "Significance", §5)
    _FORWARD_FLOOR_DAYS = 168     # 8 x 21 bdays min runtime before ANY verdict (§4.2, §7)
    _INDEP_GAP_DAYS = 29          # ~21 trading days -> non-overlapping cluster spacing (§4.2)

    book_dir = _ROOT / "data" / "shadow" / "books" / "flagship_judgment"
    nav_path = book_dir / "nav_history.jsonl"
    if not nav_path.exists():
        return _eval_result(
            STATE_BLOCKED,
            "judgment shadow book absent — data/shadow/books/flagship_judgment/nav_history.jsonl "
            f"does not exist; 0 forward NAV observations (need effective-n>={required_effective_n} "
            f"non-overlapping 21-bday obs AND >={_FORWARD_FLOOR_DAYS}d span). INSUFFICIENT POWER — "
            "keep running (AB_EXPERIMENT.md §5); the 2026-07-17 comeback_date is calendar-only.",
            evidence_n=0, required_n=required_effective_n,
        )
    try:
        # Collect distinct, valid dated NAV rows (a row must carry a parseable 'date' and 'nav').
        dates: list[date] = []
        seen: set[str] = set()
        for line in nav_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001 — a bad line never counts, never raises
                continue
            if not isinstance(row, dict) or row.get("nav") is None:
                continue
            d_str = str(row.get("date") or "")[:10]
            if not d_str or d_str in seen:
                continue
            try:
                d = date.fromisoformat(d_str)
            except Exception:  # noqa: BLE001
                continue
            seen.add(d_str)
            dates.append(d)

        raw_n = len(dates)
        if raw_n == 0:
            return _eval_result(
                STATE_BLOCKED,
                "judgment shadow book has no parseable dated NAV rows — 0 forward observations "
                f"(need effective-n>={required_effective_n}). INSUFFICIENT POWER — keep running.",
                evidence_n=0, required_n=required_effective_n,
            )

        # Thin to NON-overlapping ~21-trading-day clusters (greedy >= _INDEP_GAP_DAYS gap), the
        # effective-n the AB_EXPERIMENT.md HAC test consumes. Overlapping daily marks are NOT
        # independent observations; counting raw rows would overstate power.
        dates.sort()
        effective_n = 0
        last_kept: date | None = None
        for d in dates:
            if last_kept is None or (d - last_kept).days >= _INDEP_GAP_DAYS:
                effective_n += 1
                last_kept = d
        span_days = (dates[-1] - dates[0]).days

        if effective_n >= required_effective_n and span_days >= _FORWARD_FLOOR_DAYS:
            return _eval_result(
                STATE_READY,
                f"judgment shadow book cleared the forward bar: effective-n={effective_n} "
                f">= {required_effective_n} non-overlapping 21-bday obs AND span {span_days}d "
                f">= {_FORWARD_FLOOR_DAYS}d floor. The 4wk WEAKENING/CAUTION NAV-vs-engine "
                "comparison (vs data/shadow/books/prod/) can now be judged (AB_EXPERIMENT.md §5).",
                evidence_n=effective_n, required_n=required_effective_n,
            )

        # Short on power on EITHER axis → insufficient_power (blocked). Never mature on the calendar.
        # Estimate readiness from whichever floor binds later.
        weeks_for_n = max(0, (required_effective_n - effective_n)) * 3   # ~3 cal-wk per indep obs
        days_for_span = max(0, _FORWARD_FLOOR_DAYS - span_days)
        ready_est = (asof + timedelta(days=max(weeks_for_n * 7, days_for_span))).isoformat()
        return _eval_result(
            STATE_BLOCKED,
            f"judgment shadow book INSUFFICIENT POWER: effective-n={effective_n} "
            f"(need {required_effective_n}), span={span_days}d (need {_FORWARD_FLOOR_DAYS}d) — "
            "keep running (AB_EXPERIMENT.md §5); 2026-07-17 is a calendar marker, NOT a data verdict.",
            evidence_n=effective_n, required_n=required_effective_n,
            expected_ready_date=ready_est,
        )
    except Exception:  # noqa: BLE001 — evaluators must not raise; ambiguity defaults to blocked
        return _eval_result(
            STATE_BLOCKED,
            "error reading data/shadow/books/flagship_judgment/nav_history.jsonl — treating as "
            f"0 forward observations (need effective-n>={required_effective_n}). INSUFFICIENT POWER.",
            evidence_n=0, required_n=required_effective_n,
        )


def _eval_posture_decider_arming(item: dict, asof: date) -> dict:
    """posture-decider-arming: ready ONLY when BOTH arming preconditions are met on disk.

    Gate (registry): "E2.2 wired + grep-gate CI + calm-tape zero-drift + 2wk shadow no-compounding
    on disagreeing tapes". The two mechanically-checkable, data-accrual preconditions are:
      (A) >= 2 CALENDAR WEEKS (14 days) of shadow posture records, no-compounding. The posture
          decider publishes a dated data/posture/<asof>.json EVERY build in shadow (flag OFF) so
          the window accrues from day one — see brain/posture_decider.py:build()/module docstring
          ("Publishes ... a dated copy EVERY BUILD ... so the shadow window accrues from day one").
          We require the span between the earliest and latest dated artifact to be >= 14 days AND
          at least _MIN_POSTURE_SNAPSHOTS distinct daily records inside that window.
      (B) >= _MIN_EFFECTIVE_N = 8 weekly benchmark snapshots in data/benchmark/ — the SAME bar
          posture_governor.guards() enforces (brain/posture_governor.py:48 `_MIN_EFFECTIVE_N = 8`,
          consumed by governor-arming above). The arming decision (E3.3) shares the governor's
          statistical floor: no arming without the governor's own evidence base.

    UNMET on EITHER precondition → insufficient_power (blocked). The E2.2-wired / grep-gate-CI /
    calm-tape-zero-drift conditions are code-review / CI facts with NO machine-readable artifact in
    this repo to verify mechanically, so a human/Fable review must still confirm them before resolve
    — this evaluator only guarantees we never AUTO-mature on the 2026-07-17 calendar with the data
    absent. Default to blocked whenever the data or the threshold is ambiguous (safe under-arming).
    """
    # Precondition (A) — shadow posture window. 14 days = the "2wk" the gate names.
    _MIN_WINDOW_DAYS = 14              # "2wk shadow no-compounding" (registry gate string)
    _MIN_POSTURE_SNAPSHOTS = 10        # >= ~10 build-day records inside the 2wk window (no-compound)
    # Precondition (B) — benchmark snapshot floor. Source: brain/posture_governor.py:48
    # `_MIN_EFFECTIVE_N = 8`, the arming guard posture_governor.guards() enforces.
    required_bench_n = 8

    posture_dir = _ROOT / "data" / "posture"
    bench_dir = _ROOT / "data" / "benchmark"

    # ── (A) count distinct dated posture artifacts (data/posture/<asof>.json) ──
    posture_dates: list[date] = []
    if posture_dir.exists():
        try:
            for f in sorted(posture_dir.glob("*.json")):
                if f.name.startswith("_") or f.name in ("latest.json", "state.json"):
                    continue
                stem = f.stem  # "2026-07-11"
                try:
                    posture_dates.append(date.fromisoformat(stem[:10]))
                except Exception:  # noqa: BLE001 — non-dated json (e.g. deviations) never counts
                    continue
        except Exception:  # noqa: BLE001
            posture_dates = []
    posture_n = len(posture_dates)
    posture_span = (max(posture_dates) - min(posture_dates)).days if posture_n >= 2 else 0
    window_ok = posture_n >= _MIN_POSTURE_SNAPSHOTS and posture_span >= _MIN_WINDOW_DAYS

    # ── (B) count weekly benchmark snapshots (data/benchmark/*.json, excluding internal files) ──
    bench_n = 0
    if bench_dir.exists():
        try:
            bench_n = len([f for f in sorted(bench_dir.glob("*.json"))
                           if not f.name.startswith("_")])
        except Exception:  # noqa: BLE001
            bench_n = 0
    bench_ok = bench_n >= required_bench_n

    if window_ok and bench_ok:
        return _eval_result(
            STATE_READY,
            f"posture arming preconditions met: {posture_n} shadow posture records spanning "
            f"{posture_span}d (>= {_MIN_WINDOW_DAYS}d 2wk window) AND {bench_n} benchmark snapshots "
            f"(>= {required_bench_n}). NOTE: E2.2-wired / grep-gate-CI / calm-tape-zero-drift are "
            "code/CI facts a human must still confirm — this evaluator gates the DATA-accrual bar only.",
            evidence_n=min(posture_n, bench_n), required_n=required_bench_n,
        )

    # ── insufficient on at least one precondition → blocked (never mature on the calendar) ──
    unmet: list[str] = []
    if not window_ok:
        if posture_n == 0:
            unmet.append(
                "data/posture/ has no dated shadow records (need >= "
                f"{_MIN_POSTURE_SNAPSHOTS} records spanning >= {_MIN_WINDOW_DAYS}d)")
        else:
            unmet.append(
                f"shadow posture window short: {posture_n} records / {posture_span}d span "
                f"(need >= {_MIN_POSTURE_SNAPSHOTS} records AND >= {_MIN_WINDOW_DAYS}d)")
    if not bench_ok:
        unmet.append(
            f"benchmark snapshots {bench_n} < required {required_bench_n} "
            "(posture_governor._MIN_EFFECTIVE_N)")
    # Estimate readiness from whichever axis binds later (~1 posture record/build-day; ~1 bench/wk).
    days_for_window = max(0, _MIN_WINDOW_DAYS - posture_span, _MIN_POSTURE_SNAPSHOTS - posture_n)
    days_for_bench = max(0, required_bench_n - bench_n) * 7
    ready_est = (asof + timedelta(days=max(days_for_window, days_for_bench))).isoformat()
    return _eval_result(
        STATE_BLOCKED,
        "posture arming INSUFFICIENT POWER — " + "; ".join(unmet)
        + ". Keep running; the 2026-07-17 comeback_date is a calendar marker, NOT a data verdict.",
        evidence_n=min(posture_n, bench_n), required_n=required_bench_n,
        expected_ready_date=ready_est,
    )


def _eval_bubble_formation_grading(item: dict, asof: date) -> dict:
    """bubble-formation-grading: blocked on H4 handoff from the dashboard side.

    The maturity_condition is 'H4 handoff lands' — this refers to a Macro Dashboard
    deliverable (a forward-graded crowding log) that is tracked outside this repo.
    No machine-readable artifact exists in this codebase to verify the handoff has
    landed; this cannot be evaluated mechanically.
    """
    return _eval_result(
        STATE_BLOCKED,
        "no mechanical evaluator — needs Fable review to define one "
        "(maturity_condition='H4 handoff lands' refers to an external Macro Dashboard "
        "deliverable; no artifact exists in this repo to verify the handoff)",
    )


def _eval_deploy_lag_sla(item: dict, asof: date) -> dict:
    """deploy-lag-sla: continuous operational SLA, not a single-promotion experiment.

    The maturity_condition is 'continuous' — this experiment has no threshold-crossing
    event that promotes it from open to matured.  It is a standing operational check
    (check_deploy_lag.py runs on every build and writes data/deploy_lag.json).
    No mechanical evaluator can define a 'ready_for_review' state for a continuous SLA;
    current health is readable from data/deploy_lag.json but that is not a maturity gate.
    """
    # Surface current deploy health as context even though we can't evaluate maturity.
    lag_path = _ROOT / "data" / "deploy_lag.json"
    health_note = ""
    try:
        d = json.loads(lag_path.read_text())
        if d.get("warn"):
            health_note = (f"; CURRENT LAG ALERT: {d.get('behind_by_commits')} commits behind, "
                           f"{d.get('lag_hours')}h lag")
        else:
            health_note = "; current deploy lag: OK (no warn flag)"
    except Exception:  # noqa: BLE001
        health_note = "; data/deploy_lag.json absent or unreadable (nightly run may not have executed)"
    return _eval_result(
        STATE_BLOCKED,
        "no mechanical evaluator — needs Fable review to define one "
        "(maturity_condition='continuous'; this is a standing operational SLA with no single "
        "promotion event; check_deploy_lag.py runs on every build)"
        + health_note,
    )


# ─── date-driven fallback evaluator (covers all items with a comeback_date) ──

def _eval_date_driven(item: dict, asof: date) -> dict:
    """Default evaluator for items with a comeback_date.

    Mirrors the existing matured() logic exactly so the tri-state evaluator agrees with the
    auto-promotion path.  Items whose comeback_date has not yet been reached return
    not_old_enough; items whose date has arrived return ready_for_review.
    """
    cd = item.get("comeback_date")
    if not cd:
        # No comeback_date and no registered evaluator → honest blocked
        return _eval_result(
            STATE_BLOCKED,
            "no mechanical evaluator — needs Fable review to define one "
            "(no comeback_date and no per-experiment evaluator registered)",
        )
    try:
        cd_date = date.fromisoformat(str(cd)[:10])
    except Exception:  # noqa: BLE001
        return _eval_result(
            STATE_BLOCKED,
            f"comeback_date {cd!r} is not a valid ISO date — cannot evaluate",
        )
    if asof >= cd_date:
        return _eval_result(
            STATE_READY,
            f"comeback_date {cd} has been reached (as_of={asof.isoformat()})",
            expected_ready_date=cd,
        )
    days_left = (cd_date - asof).days
    return _eval_result(
        STATE_NOT_OLD_ENOUGH,
        f"comeback_date {cd} not yet reached ({days_left} days remaining)",
        expected_ready_date=cd,
    )


# ─── evaluator registry (keyed by experiment id) ─────────────────────────────

_EVALUATORS: dict[str, _EvaluatorFn] = {
    "shadow-trim-ladder":        _eval_shadow_trim_ladder,
    "governor-arming":           _eval_governor_arming,
    "judgment-book-promotion":   _eval_judgment_book_promotion,
    "posture-decider-arming":    _eval_posture_decider_arming,
    "bubble-formation-grading":  _eval_bubble_formation_grading,
    "deploy-lag-sla":            _eval_deploy_lag_sla,
}
"""Per-experiment mechanical evaluators.  Keyed by experiment id.  Items without a
registered evaluator fall through to _eval_date_driven (which handles comeback_date
items and returns blocked_missing_evidence with an honest reason for anything else).

To register a new evaluator:
    _EVALUATORS["my-experiment-id"] = my_evaluator_fn

The evaluator function receives (item: dict, asof: date) and must return a dict from
_eval_result().  It must NEVER raise, NEVER mutate the item, and NEVER promote status.
"""


def register_evaluator(experiment_id: str, fn: _EvaluatorFn) -> None:
    """Register (or replace) a mechanical evaluator for an experiment.

    Intended for tests and future extensions.  Registered evaluators take precedence
    over the date-driven fallback for all items with that id.
    """
    _EVALUATORS[experiment_id] = fn


# ─── public evaluate() ────────────────────────────────────────────────────────

def evaluate(item: dict, asof: date | None = None) -> dict:
    """Tri-state evaluator for a single experiment item.

    Returns:
      {
        "state":               str,          # not_old_enough | blocked_missing_evidence | ready_for_review
        "reason":              str,          # human-readable explanation
        "evidence_n":          int | null,   # current count when computable
        "required_n":          int | null,   # threshold when computable
        "expected_ready_date": str | null,   # ISO date estimate when computable
        "stuck":               bool,         # True if blocked >14 days with no comeback_date
      }

    SAFETY INVARIANTS (enforced here, not by the evaluator fns):
      - evaluate() NEVER promotes status to matured/judged — that is the sole domain of
        matured() (date-driven) and resolve() (human judgment).
      - A condition-only item with no registered evaluator returns blocked_missing_evidence
        with an honest explanation.
      - The stuck flag is set when state==blocked_missing_evidence AND comeback_date is None
        AND the item has been in that state for >_STUCK_DAYS days (tracked via
        _evaluator_first_blocked in the item itself).
    """
    asof = asof or date.today()
    item = _defaults(dict(item))   # safe copy with defaults; never mutates caller's dict
    eid = item.get("id") or ""

    # Skip terminal items — nothing to evaluate
    status = item.get("status", "open")
    if status in ("judged", "cancelled"):
        return {
            "state": STATE_BLOCKED,
            "reason": f"experiment is terminal (status={status}); no evaluation applicable",
            "evidence_n": None, "required_n": None, "expected_ready_date": None, "stuck": False,
        }

    # Already promoted to matured by the date-driven path → always ready
    if status == "matured":
        cd = item.get("comeback_date")
        return {
            "state": STATE_READY,
            "reason": f"experiment status is already matured (comeback_date={cd})",
            "evidence_n": None, "required_n": None,
            "expected_ready_date": cd,
            "stuck": False,
        }

    # Dispatch to per-experiment evaluator, or fall through to date-driven default
    fn = _EVALUATORS.get(eid)
    if fn is None:
        fn = _eval_date_driven
    try:
        result = fn(item, asof)
    except Exception:  # noqa: BLE001 — evaluators must not raise; if one does, report blocked
        result = _eval_result(
            STATE_BLOCKED,
            f"evaluator for '{eid}' raised an unexpected exception — treating as blocked",
        )

    # Attach stuck flag: blocked + no comeback_date + first blocked >STUCK_DAYS ago
    state = result.get("state", STATE_BLOCKED)
    stuck = False
    if state == STATE_BLOCKED and not item.get("comeback_date"):
        first_blocked_str = item.get("_evaluator_first_blocked")
        if first_blocked_str:
            try:
                first_blocked = date.fromisoformat(str(first_blocked_str)[:10])
                stuck = (asof - first_blocked).days > _STUCK_DAYS
            except Exception:  # noqa: BLE001
                pass
        # Note: _evaluator_first_blocked is a sidecar tracking field.  It is written
        # by update_evaluator_tracking() — a separate call that callers invoke after
        # evaluate() if they want to persist the first-blocked date.

    result["stuck"] = stuck
    return result


def update_evaluator_tracking(experiment_id: str, state: str, asof: date | None = None) -> bool:
    """Persist the _evaluator_first_blocked sidecar field based on the current evaluate() result.

    Call this after evaluate() when you want stuck-flag tracking to persist across sessions.
    - If state == blocked_missing_evidence and _evaluator_first_blocked is not set, stamps today.
    - If state != blocked_missing_evidence, clears the field (evidence appeared or condition changed).
    Returns True on successful save, False otherwise (P2: never raises).
    """
    asof = asof or date.today()
    try:
        all_exps = _load()
        for i, exp in enumerate(all_exps):
            if exp.get("id") != experiment_id:
                continue
            if state == STATE_BLOCKED:
                if not exp.get("_evaluator_first_blocked"):
                    all_exps[i]["_evaluator_first_blocked"] = asof.isoformat()
            else:
                all_exps[i]["_evaluator_first_blocked"] = None
            return _save(all_exps)
        return False
    except Exception:  # noqa: BLE001
        return False


# ── public API ────────────────────────────────────────────────────────────────

def load() -> list[dict]:
    """Return all experiments (with safe defaults filled in)."""
    return [_defaults(dict(e)) for e in _load()]


def get(experiment_id: str) -> dict | None:
    """Return one experiment by id, or None if not found."""
    for exp in load():
        if exp.get("id") == experiment_id:
            return exp
    return None


def add(experiment: dict) -> bool:
    """Append a new experiment.  Returns False if id already exists or save fails."""
    try:
        eid = experiment.get("id")
        if not eid:
            return False
        all_exps = _load()
        if any(e.get("id") == eid for e in all_exps):
            return False
        all_exps.append(_defaults(dict(experiment)))
        return _save(all_exps)
    except Exception:  # noqa: BLE001
        return False


def update(experiment_id: str, **fields: Any) -> bool:
    """Update allowed fields on an existing experiment.  Status field is validated.

    NEVER allowed to update: id, gate, maturity_condition (these are set at creation
    time and are effectively immutable — only Fable-review sessions may change them
    by editing the seed directly).
    """
    _IMMUTABLE = {"id", "gate", "maturity_condition"}
    try:
        all_exps = _load()
        for i, exp in enumerate(all_exps):
            if exp.get("id") == experiment_id:
                for k, v in fields.items():
                    if k in _IMMUTABLE:
                        continue
                    if k == "status":
                        current = exp.get("status", "open")
                        allowed = _TRANSITIONS.get(current, set())
                        if v not in allowed:
                            return False          # invalid transition
                    exp[k] = v
                all_exps[i] = exp
                return _save(all_exps)
        return False                              # not found
    except Exception:  # noqa: BLE001
        return False


def resolve(experiment_id: str, verdict: str, notes: str = "") -> bool:
    """Mark an experiment JUDGED with a verdict note.

    ``verdict`` is free-form prose stored in the experiment's ``notes`` field
    (prepended with the date).  This is the official closure call; after this
    the experiment is terminal and cannot be re-opened by the registry API.
    """
    try:
        dated_note = f"[{date.today().isoformat()} JUDGED] {verdict}"
        all_exps = _load()
        for i, exp in enumerate(all_exps):
            if exp.get("id") == experiment_id:
                current = exp.get("status", "open")
                if "judged" not in _TRANSITIONS.get(current, set()):
                    return False                  # already terminal or wrong path
                exp["status"] = "judged"
                prior = (exp.get("notes") or "").strip()
                exp["notes"] = f"{prior}\n{dated_note}".strip()
                all_exps[i] = exp
                return _save(all_exps)
        return False
    except Exception:  # noqa: BLE001
        return False


def matured(as_of: date | None = None) -> list[dict]:
    """Return all experiments that are ready for review, in priority order.

    An experiment is *matured* when:
      (a) its status == "matured" already (previously flagged), OR
      (b) its status == "open" AND its comeback_date is not null AND the date
          has been reached (as_of >= comeback_date).

    Items where (b) applies are promoted to status="matured" and saved before
    being returned, so the agenda never re-surface a newly-matured item twice
    without human acknowledgement.

    Priority order: matured items first, sorted by comeback_date ASC (earliest
    deadline first); then by id as a tiebreaker.  Items with no comeback_date
    sort last within their status tier.
    """
    as_of = as_of or date.today()
    all_exps = _load()
    changed = False
    # promote open items whose comeback_date has arrived
    for i, exp in enumerate(all_exps):
        if exp.get("status") != "open":
            continue
        cd = exp.get("comeback_date")
        if not cd:
            continue
        try:
            cd_date = date.fromisoformat(str(cd)[:10])
        except Exception:  # noqa: BLE001
            continue
        if as_of >= cd_date:
            all_exps[i]["status"] = "matured"
            changed = True
    if changed:
        _save(all_exps)
    # collect all matured (not yet judged/cancelled)
    results = [_defaults(dict(e)) for e in all_exps if e.get("status") == "matured"]
    # sort: by comeback_date ASC (None sorts last), then by id
    def _sort_key(e: dict) -> tuple:
        cd = e.get("comeback_date")
        if cd:
            try:
                return (0, str(cd)[:10], e.get("id") or "")
            except Exception:  # noqa: BLE001
                pass
        return (1, "", e.get("id") or "")
    return sorted(results, key=_sort_key)


def open_with_tristate(as_of: date | None = None) -> list[dict]:
    """Return all OPEN (non-terminal) experiments with their tri-state evaluation attached.

    Each item in the returned list is the experiment dict enriched with an ``evaluation``
    sub-dict (the full result of evaluate()).  Items are sorted by priority:
      1. ready_for_review first (these need immediate human attention)
      2. stuck items second (blocked + no comeback_date + >14 days)
      3. blocked_missing_evidence without stuck
      4. not_old_enough last (sorted by expected_ready_date ASC within this tier)

    This is the source of truth for the agenda's experiment section and the web pane.
    Never raises (P2).
    """
    as_of = as_of or date.today()
    try:
        all_exps = load()
        results = []
        for exp in all_exps:
            if exp.get("status") in ("judged", "cancelled"):
                continue
            try:
                ev = evaluate(exp, as_of)
            except Exception:  # noqa: BLE001
                ev = {"state": STATE_BLOCKED, "reason": "evaluator error",
                      "evidence_n": None, "required_n": None,
                      "expected_ready_date": None, "stuck": False}
            enriched = dict(exp)
            enriched["evaluation"] = ev
            results.append(enriched)

        def _sort_key(e: dict) -> tuple:
            ev = e.get("evaluation") or {}
            state = ev.get("state", STATE_BLOCKED)
            stuck = bool(ev.get("stuck"))
            erd = str(ev.get("expected_ready_date") or "9999-99-99")
            # tier 0=ready, 1=stuck, 2=blocked, 3=not_old_enough
            tier = (0 if state == STATE_READY else
                    1 if stuck else
                    2 if state == STATE_BLOCKED else 3)
            return (tier, erd, e.get("id") or "")

        results.sort(key=_sort_key)
        return results
    except Exception:  # noqa: BLE001
        return []


def summary() -> dict:
    """A compact summary suitable for the agenda and the /api/experiments endpoint.

    Returns {total, open, matured, judged, cancelled, matured_items: [...],
             open_tristate: [...], as_of: str}.  Never raises.
    """
    try:
        all_exps = load()
        by_status: dict[str, int] = {}
        for e in all_exps:
            s = e.get("status", "open")
            by_status[s] = by_status.get(s, 0) + 1
        mat = matured()
        tristate = open_with_tristate()
        return {
            "as_of": date.today().isoformat(),
            "total": len(all_exps),
            "open": by_status.get("open", 0),
            "matured": by_status.get("matured", 0),
            "judged": by_status.get("judged", 0),
            "cancelled": by_status.get("cancelled", 0),
            "matured_items": mat,
            "open_tristate": tristate,
        }
    except Exception:  # noqa: BLE001
        return {"as_of": date.today().isoformat(), "total": 0, "open": 0,
                "matured": 0, "judged": 0, "cancelled": 0, "matured_items": [],
                "open_tristate": []}

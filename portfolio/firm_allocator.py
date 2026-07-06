"""Shadow Firm Allocator — MW5 Lane A (docket M3).

DISPLAY-ONLY FOREVER until a future Fable ruling promotes it to authority.
This module docstring is the binding statement of that constraint.  The ruling
will be recorded in research/MASTERMIND_CONTROL_PLANE_MASTERPLAN.md §2 before
any promotion.

Nothing imports this module except:
  - app/web.py  (the /api/firm_allocator endpoint)
  - tests/test_firm_allocator.py

A grep-provable shadow guarantee: nothing in any book sizing path imports
portfolio.firm_allocator.  Tests assert this invariant (ratchet-style).

Architecture §2 MW5 / charter P8 — shadow-earned autonomy
----------------------------------------------------------
We pre-commit the allocation formula HERE, before any binding authority is even
proposed, so the design can be evaluated against live paper history.  The formula
is documented below and tested exhaustively.  When (and if) a Fable ruling
promotes the allocator, the formula does not change — only the call site does.

Pre-committed formula (documented; every step is tested)
---------------------------------------------------------
Inputs:
  lifecycle_artifact  — latest data/lifecycle/*.json  (brain.book_lifecycle output)
  benchmark_ledger    — latest data/benchmark/*.json  (brain.benchmark_ledger output)
  mandate_packets     — latest data/portfolios/<id>/mandate_packet.json per book
  firm_exposure_data  — portfolio.firm_exposure.summary() (best-effort)

Step 1 — ACTIVE BOOKS
  Books in state "active" per the lifecycle artifact are eligible.
  Books in state "probation" receive a half-weight penalty (see step 3).
  Books in state "retired-recommendation" receive weight 0.
  self_directed is CONSTITUTIONALLY EXCLUDED from the allocator (it is the
  yardstick, not a capital receiver; charter P6 / brain.book_lifecycle EXEMPT_BOOKS).

Step 2 — BASE WEIGHTS
  Equal-weight over eligible books (active + probation; retired-rec excluded):
    base_w[i] = 1 / N_eligible

Step 3 — ACTIVE-RETURN TILT
  For each book, compute z-score of rolling active_return_vs_spy from the latest
  lifecycle grade (grade["active_vs_spy"]).  The z is clipped to [-1, +1] to
  avoid extreme tilts at paper-N.  Tilt factor:
    tilt[i] = 1 + ALPHA * clip(z[i], -1, 1)   where ALPHA = 0.3
  Books in probation have tilt factor halved before normalization:
    tilt[i] *= 0.5   (probation penalty)
  SD is excluded (step 1); it is set tilt=0 explicitly.

Step 4 — CORRELATION PENALTY
  If the lifecycle artifact contains an orthogonality matrix and the pairwise
  active-return correlation between two eligible books is > NOISY_MIRROR_CORR
  (default 0.80), the LOWER-graded book in the pair receives an additional
  penalty factor of 0.70.  Applied multiplicatively before normalization.

Step 5 — MANDATE BREACH PENALTY
  If the book's latest mandate packet has len(breaches) > 0, apply a penalty
  factor of BREACH_PENALTY = 0.80 per breach (capped at 0.50 total).

Step 6 — REGIONAL CAVEAT FLAG
  china and hk books are included with a "proxy_bogey" flag because their active
  returns are measured against FXI (a proxy, not the canonical index), so the
  tilt computed for them carries additional uncertainty.  This is noted in the
  output flags but does not change the arithmetic.

Step 7 — NORMALIZE
  weights = tilt / sum(tilt); guaranteed to sum to 1.0 over eligible books.
  Retired-rec books get shadow_weight = 0.

Step 8 — RISK BUDGET
  risk_budget[i] = shadow_weight[i] * 1.0   (one-for-one with weight; placeholder
  for a future volatility-scaled risk budget once book-level vol is tracked).

Output artifact:  data/allocator/<asof>.json
  {
    "asof": str,
    "formula_version": "v1",
    "books": {
      "<book_id>": {
        "lifecycle_state": str | null,
        "shadow_weight": float,
        "risk_budget": float,
        "flags": [str],
      },
      ...
    },
    "firm": {
      "n_eligible": int,
      "n_books": int,
      "correlation_pairs_penalized": [[str, str]],
      "mandate_breach_books": [str],
      "rationale": str,
    },
    "advisory_only": True,
  }

Integration point for the orchestrator (lane B / app/scheduler.py)
-------------------------------------------------------------------
Call ``build_latest()`` from the weekly CIO path when lane B owns the scheduler
wire.  Do NOT wire the scheduler in this wave (lane B owns it).

    from portfolio.firm_allocator import build_latest
    artifact = build_latest()          # returns the output dict; also persists to disk
"""
from __future__ import annotations

import json
import statistics
from datetime import date
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_OUT_DIR = _ROOT / "data" / "allocator"

# ── formula constants (pre-committed; change requires a new formula_version) ──
_FORMULA_VERSION = "v1"
_ALPHA = 0.3              # tilt amplitude (clipped z-score weighting)
_CORR_PENALTY = 0.70      # lower-graded book in a noisy-mirror pair loses this factor
_NOISY_MIRROR_CORR = 0.80 # correlation threshold above which the penalty fires
_PROBATION_PENALTY = 0.50 # tilt factor multiplier for books on probation
_BREACH_PENALTY_PER = 0.80  # penalty per mandate breach
_BREACH_PENALTY_FLOOR = 0.50  # total mandate penalty floor (cannot go below this)

# Books that are constitutionally excluded from the allocator.
# Must match brain.book_lifecycle.EXEMPT_BOOKS.
_EXEMPT = frozenset({"self_directed"})

# Books excluded as YARDSTICK / comparison-only in the lifecycle SD designation.
# SD is never allocated to — it is the defensive basket bogey.
_SD_ID = "self_directed"

# Regional books get a proxy-bogey caveat flag.
_REGIONAL = frozenset({"china", "hk"})


# ---------------------------------------------------------------------------
# Artifact readers — best-effort, degrade to empty on any failure
# ---------------------------------------------------------------------------

def _latest_lifecycle() -> dict[str, Any]:
    """Read the most recent lifecycle artifact.  Returns {} on any failure."""
    try:
        lc_dir = _ROOT / "data" / "lifecycle"
        files = sorted(lc_dir.glob("*.json")) if lc_dir.exists() else []
        if not files:
            return {}
        return json.loads(files[-1].read_text())
    except Exception:  # noqa: BLE001
        return {}


def _latest_benchmark() -> dict[str, Any]:
    """Read the most recent benchmark ledger artifact.  Returns {} on any failure."""
    try:
        bench_dir = _ROOT / "data" / "benchmark"
        files = sorted(bench_dir.glob("*.json")) if bench_dir.exists() else []
        if not files:
            return {}
        return json.loads(files[-1].read_text())
    except Exception:  # noqa: BLE001
        return {}


def _latest_mandate_packet(book_id: str) -> dict[str, Any]:
    """Read the latest mandate packet for a book.  Returns {} on failure."""
    try:
        from portfolio import registry
        p = registry.data_dir(book_id) / "mandate_packet.json"
        if not p.exists():
            return {}
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _firm_exposure_data() -> dict[str, Any]:
    """portfolio.firm_exposure.summary() — best-effort; returns {} on any failure."""
    try:
        from portfolio import firm_exposure
        return firm_exposure.summary()
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Grade helpers
# ---------------------------------------------------------------------------

def _grade_for_book(book_id: str, lifecycle: dict) -> dict[str, Any]:
    """Extract the grade dict for a book from a lifecycle artifact.  Returns {}."""
    grades = lifecycle.get("grades") or {}
    return grades.get(book_id) or {}


def _lifecycle_state(book_id: str, lifecycle: dict) -> str | None:
    """The lifecycle state for a book from the artifact, or None if absent."""
    states = lifecycle.get("states") or {}
    s = states.get(book_id)
    if isinstance(s, dict):
        return s.get("state")
    if isinstance(s, str):
        return s
    return None


def _active_return_z(active_returns: list[float]) -> float:
    """Z-score of the active_return_vs_spy series, clipped to [-1, +1].
    Returns 0.0 when there is insufficient data (< 2 observations)."""
    vals = [v for v in active_returns if v is not None]
    if len(vals) < 2:
        return 0.0
    try:
        mu = statistics.mean(vals)
        sd = statistics.stdev(vals)
        if sd == 0.0:
            return 0.0
        # Use the most recent value as the representative point
        z = (vals[-1] - mu) / sd
        return max(-1.0, min(1.0, z))
    except Exception:  # noqa: BLE001
        return 0.0


# ---------------------------------------------------------------------------
# Core allocation formula
# ---------------------------------------------------------------------------

def _compute(
    lifecycle: dict,
    benchmark: dict,
    mandate_packets: dict[str, dict],
    *,
    book_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Pure allocation computation — all inputs injected for testability.

    Returns the full output artifact dict (without the disk-write metadata).
    Never raises.
    """
    from portfolio import registry

    asof = (lifecycle.get("asof") or benchmark.get("asof") or date.today().isoformat())

    # The books we consider (default: all registry books minus exemptions)
    if book_ids is None:
        book_ids = [p["id"] for p in registry.all_portfolios() if p["id"] not in _EXEMPT]

    # ── Step 1: classify lifecycle state ──────────────────────────────────
    STATE_ACTIVE = "active"
    STATE_PROBATION = "probation"
    STATE_RETIRE_REC = "retired-recommendation"

    book_states: dict[str, str | None] = {}
    for bid in book_ids:
        book_states[bid] = _lifecycle_state(bid, lifecycle)

    # Eligible = active or probation (retired-rec and None treated as active for equal-weight
    # equal-weight degradation when lifecycle artifact is missing entirely)
    lifecycle_missing = not lifecycle
    if lifecycle_missing:
        # Degrade: treat every book as active, flag all
        eligible = list(book_ids)
    else:
        eligible = [
            bid for bid in book_ids
            if book_states.get(bid) in (STATE_ACTIVE, STATE_PROBATION, None)
            and book_states.get(bid) != STATE_RETIRE_REC
        ]

    n_eligible = len(eligible)
    if n_eligible == 0:
        # Pathological — no eligible books; return equal-weight over all
        eligible = list(book_ids)
        n_eligible = len(eligible)

    # ── Step 2: base equal-weights ─────────────────────────────────────────
    base_w = 1.0 / n_eligible if n_eligible > 0 else 0.0

    # ── Step 3: active-return tilt ─────────────────────────────────────────
    tilt: dict[str, float] = {}
    for bid in book_ids:
        if bid not in eligible:
            tilt[bid] = 0.0
            continue
        grade = _grade_for_book(bid, lifecycle)
        # Try to get a series from the lifecycle; fall back to the scalar if only one value
        ar_series = grade.get("active_vs_spy_series") or []
        if not ar_series:
            ar_scalar = grade.get("active_vs_spy")
            if ar_scalar is not None:
                ar_series = [ar_scalar]
        z = _active_return_z(ar_series)
        tf = 1.0 + _ALPHA * z
        # Probation half-penalty
        if book_states.get(bid) == STATE_PROBATION:
            tf *= _PROBATION_PENALTY
        tilt[bid] = max(0.0, tf)

    # ── Step 4: correlation penalty ────────────────────────────────────────
    # Read the orthogonality matrix from the lifecycle artifact
    ortho = lifecycle.get("orthogonality") or {}
    pairs_penalized: list[list[str]] = []
    corr_penalized_books: set[str] = set()  # track which specific books were penalized

    for i, bid_a in enumerate(eligible):
        for bid_b in eligible[i + 1:]:
            key = f"{bid_a}:{bid_b}"
            alt_key = f"{bid_b}:{bid_a}"
            corr = ortho.get(key) or ortho.get(alt_key)
            if corr is None or corr == "insufficient-n":
                continue
            try:
                corr_f = float(corr)
            except (TypeError, ValueError):
                continue
            if corr_f > _NOISY_MIRROR_CORR:
                # Penalise the lower-graded book
                grade_a = (_grade_for_book(bid_a, lifecycle).get("active_vs_spy") or 0.0)
                grade_b = (_grade_for_book(bid_b, lifecycle).get("active_vs_spy") or 0.0)
                loser = bid_b if grade_a >= grade_b else bid_a
                tilt[loser] = tilt.get(loser, 1.0) * _CORR_PENALTY
                corr_penalized_books.add(loser)
                pairs_penalized.append([bid_a, bid_b])

    # ── Step 5: mandate breach penalty ────────────────────────────────────
    breach_books: list[str] = []
    for bid in eligible:
        pkt = mandate_packets.get(bid) or {}
        breaches = pkt.get("breaches") or []
        if breaches:
            breach_books.append(bid)
            penalty = _BREACH_PENALTY_PER ** len(breaches)
            penalty = max(_BREACH_PENALTY_FLOOR, penalty)
            tilt[bid] = tilt.get(bid, 1.0) * penalty

    # ── Step 6 is informational (regional flag added to output below) ──────

    # ── Step 7: normalize ─────────────────────────────────────────────────
    total_tilt = sum(tilt[bid] for bid in eligible)
    shadow_weights: dict[str, float] = {}
    for bid in book_ids:
        if bid not in eligible or total_tilt == 0:
            shadow_weights[bid] = 0.0
        else:
            shadow_weights[bid] = tilt[bid] / total_tilt

    # Verify sum-to-1 invariant (floating-point safe)
    weight_sum = sum(shadow_weights[bid] for bid in eligible)
    if weight_sum > 0 and abs(weight_sum - 1.0) > 1e-9:
        # renormalize for float safety
        for bid in eligible:
            shadow_weights[bid] /= weight_sum

    # ── Step 8: risk budget ────────────────────────────────────────────────
    risk_budgets = {bid: shadow_weights[bid] for bid in book_ids}

    # ── assemble per-book output ──────────────────────────────────────────
    books_out: dict[str, Any] = {}
    for bid in book_ids:
        flags: list[str] = []
        if lifecycle_missing:
            flags.append("lifecycle_artifact_missing:equal_weight_fallback")
        if bid in _REGIONAL:
            flags.append("proxy_bogey:active_return_z_uncertain")
        if book_states.get(bid) == STATE_PROBATION:
            flags.append("probation:half_weight_penalty")
        if book_states.get(bid) == STATE_RETIRE_REC:
            flags.append("retired_recommendation:weight_zero")
        if bid in breach_books:
            flags.append("mandate_breach_penalty")
        if bid in corr_penalized_books:
            flags.append("correlation_penalty_applied")
        books_out[bid] = {
            "lifecycle_state": book_states.get(bid),
            "shadow_weight": round(shadow_weights[bid], 6),
            "risk_budget": round(risk_budgets[bid], 6),
            "flags": flags,
        }

    rationale_parts = [
        f"equal-weight base over {n_eligible} eligible book(s)",
        f"active-return tilt alpha={_ALPHA} (clipped z)",
    ]
    if lifecycle_missing:
        rationale_parts.append("LIFECYCLE MISSING — equal-weight fallback applied")
    if pairs_penalized:
        rationale_parts.append(
            f"{len(pairs_penalized)} noisy-mirror pair(s) penalized "
            f"(corr > {_NOISY_MIRROR_CORR}): {pairs_penalized}"
        )
    if breach_books:
        rationale_parts.append(
            f"mandate breach penalty on: {breach_books}"
        )

    return {
        "asof": asof,
        "formula_version": _FORMULA_VERSION,
        "advisory_only": True,
        "books": books_out,
        "firm": {
            "n_eligible": n_eligible,
            "n_books": len(book_ids),
            "correlation_pairs_penalized": pairs_penalized,
            "mandate_breach_books": breach_books,
            "rationale": "; ".join(rationale_parts),
        },
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_latest(
    *,
    lifecycle: dict | None = None,
    benchmark: dict | None = None,
    mandate_packets: dict[str, dict] | None = None,
    book_ids: list[str] | None = None,
) -> dict:
    """Compute the shadow allocation from the latest on-disk artifacts and persist it.

    Parameters
    ----------
    lifecycle, benchmark, mandate_packets : optional injection for tests / CIO path.
    book_ids : optional override of eligible books.

    Returns
    -------
    The artifact dict (also written to data/allocator/<asof>.json).
    Never raises.

    Integration point for the weekly CIO orchestrator (lane B)
    -----------------------------------------------------------
    Call this from the CIO step AFTER lifecycle + benchmark + mandate packets are
    current.  Do NOT call from app/scheduler.py in this wave — lane B owns that wire.
    """
    try:
        lc = lifecycle if lifecycle is not None else _latest_lifecycle()
        bm = benchmark if benchmark is not None else _latest_benchmark()
        if mandate_packets is None:
            from portfolio import registry
            all_ids = [p["id"] for p in registry.all_portfolios()]
            pkts = {bid: _latest_mandate_packet(bid) for bid in all_ids}
        else:
            pkts = mandate_packets

        artifact = _compute(lc, bm, pkts, book_ids=book_ids)
        _persist(artifact)
        return artifact
    except Exception as exc:  # noqa: BLE001
        return {
            "asof": date.today().isoformat(),
            "formula_version": _FORMULA_VERSION,
            "advisory_only": True,
            "computed": False,
            "reason": f"firm_allocator.build_latest crashed: {exc!r}"[:400],
            "books": {},
            "firm": {},
        }


def latest_artifact() -> dict | None:
    """Read the most recent persisted allocator artifact.  Returns None if absent."""
    try:
        files = sorted(_OUT_DIR.glob("*.json")) if _OUT_DIR.exists() else []
        if not files:
            return None
        return json.loads(files[-1].read_text())
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _persist(artifact: dict) -> None:
    """Write artifact to data/allocator/<asof>.json.  Never raises."""
    try:
        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        asof = artifact.get("asof") or date.today().isoformat()
        p = _OUT_DIR / f"{asof}.json"
        p.write_text(json.dumps(artifact, indent=2))
    except Exception:  # noqa: BLE001
        pass

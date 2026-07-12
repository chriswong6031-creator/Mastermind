"""brain/board_learning.py — the LEARNING LOOP over the buy-board's PROVEN forward edge.

WHAT THIS DOES (and why it exists)
----------------------------------
The macro buy board forward-grades every surfacing into a persistent ledger (running / stopped /
flat + forward return). ``brain/board_track_record.py`` is the sole fail-soft reader of that ledger.
This module is the thin LEARNING LAYER on top of it: it collapses the ledger's aggregate stats into a
single verdict about whether the board has DEMONSTRATED a forward edge, and turns that verdict into a
SHRINK-ONLY trust multiplier the intake funnel may apply to the standout (buy-board) candidacy source.

THE CARDINAL INVARIANTS (mirror board_track_record + neural_web_context)
-----------------------------------------------------------------------
* SHRINK-ONLY. ``standout_trust_multiplier`` lives in [0.5, 1.0]. It can NEVER exceed 1.0 — a board
  with a proven edge is trusted at par (1.0), never boosted. Only an UNproven / negative board is
  shrunk. This is the anti-compounding law: the learning loop can only ever REMOVE trust, never add.
* NEUTRAL COLD-START. No data / n below the minimum → verdict 'insufficient' → multiplier 1.0. We
  never penalize the source for a board that hasn't published enough forward grades yet — absence of
  evidence is not evidence of no edge (the board_track_record reader is empty until macro publishes).
* FAIL-SOFT EVERYWHERE. Any error → the neutral read ('insufficient' / 1.0), never a raise. Reads only
  through ``board_track_record`` (never the macro engine, never a raw file); that reader is itself
  process-safe and fail-soft, so this module inherits its safety.

DOCTRINE (unverified priors — config/doctrine.yml board_learning:, declaration-only this wave)
----------------------------------------------------------------------------------------------
The thresholds/multipliers below are the module's own constants this wave (doctrine.yml carries a
mirror ``board_learning:`` block tagged unverified-prior for a future consumer; the module does NOT
read it yet — one source of truth per wave, exactly like the neural_web / rotation blocks).

PUBLIC API (all fail-soft)
--------------------------
* board_edge(min_n=BOARD_MIN_N)               -> dict  {as_of, n, win_rate, avg_return, running,
                                                        stopped, flat, edge_verdict}
* standout_trust_multiplier(min_n=BOARD_MIN_N)-> float shrink-only in [0.5, 1.0]
* audit_row()                                 -> dict  {status, as_of, n, edge_verdict, multiplier}
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# doctrine-tunable priors — ALL UNVERIFIED (config/doctrine.yml board_learning: mirrors these)
# --------------------------------------------------------------------------- #
# Minimum forward-graded rows before the board's edge is judged at all. Below it the verdict is
# 'insufficient' and the multiplier is a neutral 1.0 (cold-start — never penalize on thin data).
BOARD_MIN_N: int = 12

# SHRINK-ONLY trust multipliers by verdict. STRONG (or insufficient) → 1.0 (par, never a boost);
# WEAK → 0.75; NEGATIVE → 0.50. These can only ever reduce the standout source's contribution.
WEAK_MULT: float = 0.75
NEGATIVE_MULT: float = 0.5

# edge thresholds (applied only once n >= min_n):
#   STRONG   — win_rate >= 0.55 AND avg_return > 0 (a demonstrated, profitable forward edge)
#   NEGATIVE — avg_return <= 0 OR win_rate < 0.40 (the board is actively mis-picking)
#   WEAK     — everything in between (a real sample, but no proven edge)
STRONG_WIN_RATE: float = 0.55
NEGATIVE_WIN_RATE: float = 0.40

# multiplier hard bounds — the shrink-only clamp. The ceiling is 1.0 BY LAW (never boost).
_MULT_FLOOR: float = 0.5
_MULT_CEIL: float = 1.0


# --------------------------------------------------------------------------- #
# board_edge() — the verdict
# --------------------------------------------------------------------------- #

def board_edge(min_n: int = BOARD_MIN_N) -> dict[str, Any]:
    """Return the board's aggregate forward-edge read + a single ``edge_verdict``.

    Shape::

        {
          "as_of": <str|None>,          # the track-record ledger's as_of (via audit_row)
          "n": <int>,                   # total forward-graded rows
          "win_rate": <float|None>,     # running / (running + stopped); None when undecided
          "avg_return": <float|None>,   # mean forward return_pct; None when no numeric returns
          "running": <int>,
          "stopped": <int>,
          "flat": <int>,
          "edge_verdict": "insufficient" | "strong" | "negative" | "weak",
        }

    edge_verdict:
      * 'insufficient' — no data OR n < min_n (neutral cold-start; the multiplier stays 1.0).
      * 'strong'       — win_rate >= STRONG_WIN_RATE AND avg_return > 0.
      * 'negative'     — avg_return <= 0 OR win_rate < NEGATIVE_WIN_RATE.
      * 'weak'         — a real sample that is neither strong nor negative (no proven edge).

    The strong/negative order matters: a board can only be 'strong' if it is NOT 'negative' first —
    negative is checked (and returned) before strong, so a mixed read (e.g. good win_rate but a
    negative avg_return) resolves to 'negative', the safer verdict. Fail-soft → the insufficient read.
    """
    try:
        from brain import board_track_record as btr
    except Exception as e:  # noqa: BLE001 — a missing reader collapses to the neutral read
        log.debug("board_learning: board_track_record import failed (%s)", e)
        return _insufficient_edge(None)

    try:
        stats = btr.board_stats() or {}
    except Exception as e:  # noqa: BLE001 — fail-soft: never raise
        log.debug("board_learning: board_stats failed (%s)", e)
        return _insufficient_edge(None)

    # as_of via the reader's audit_row (fail-soft → None); purely advisory / display.
    as_of: Optional[str] = None
    try:
        as_of = (btr.audit_row() or {}).get("as_of")
    except Exception:  # noqa: BLE001
        as_of = None

    n = _int(stats.get("n"))
    running = _int(stats.get("running"))
    stopped = _int(stats.get("stopped"))
    flat = _int(stats.get("flat"))
    win_rate = _float(stats.get("win_rate"))
    avg_return = _float(stats.get("avg_return"))

    base = {
        "as_of": as_of,
        "n": n,
        "win_rate": win_rate,
        "avg_return": avg_return,
        "running": running,
        "stopped": stopped,
        "flat": flat,
    }

    # cold-start: no data or below the sample floor → insufficient (neutral).
    try:
        floor = int(min_n)
    except Exception:  # noqa: BLE001
        floor = BOARD_MIN_N
    if n <= 0 or n < floor:
        return {**base, "edge_verdict": "insufficient"}

    # NEGATIVE first (the safer verdict wins a mixed read): a non-positive avg_return, or a win_rate
    # that has decisively broken below the floor. A None win_rate (all-flat sample) does NOT trip the
    # win_rate leg (we never fabricate a bad rate from absence), but a non-positive avg_return still can.
    neg_by_return = avg_return is not None and avg_return <= 0.0
    neg_by_winrate = win_rate is not None and win_rate < NEGATIVE_WIN_RATE
    if neg_by_return or neg_by_winrate:
        return {**base, "edge_verdict": "negative"}

    # STRONG: a demonstrated, profitable forward edge — both legs must hold.
    if win_rate is not None and win_rate >= STRONG_WIN_RATE and avg_return is not None and avg_return > 0.0:
        return {**base, "edge_verdict": "strong"}

    # everything else at/above the sample floor is a real-but-unproven sample → weak.
    return {**base, "edge_verdict": "weak"}


def _insufficient_edge(as_of: Optional[str]) -> dict[str, Any]:
    """The neutral cold-start edge block (no data / reader unavailable)."""
    return {
        "as_of": as_of,
        "n": 0,
        "win_rate": None,
        "avg_return": None,
        "running": 0,
        "stopped": 0,
        "flat": 0,
        "edge_verdict": "insufficient",
    }


# --------------------------------------------------------------------------- #
# standout_trust_multiplier() — the SHRINK-ONLY learning lever
# --------------------------------------------------------------------------- #

def standout_trust_multiplier(min_n: int = BOARD_MIN_N) -> float:
    """Return the SHRINK-ONLY trust multiplier for the standout (buy-board) candidacy source.

    Mapping (verdict → multiplier), all in [0.5, 1.0]:
      * insufficient → 1.0   (neutral cold-start — NEVER penalize a board with no/thin forward data)
      * strong       → 1.0   (a proven edge is trusted at par — trust is never BOOSTED above 1.0)
      * weak         → 0.75  (a real sample with no proven edge earns a mild trim)
      * negative     → 0.50  (a board that is actively mis-picking is trusted at half)

    The result is HARD-CLAMPED to [0.5, 1.0] as a belt-and-suspenders guarantee that no future
    verdict/mapping change can ever produce a value that BOOSTS the source above par. Fail-soft: any
    error → 1.0 (a broken learning loop never SHRINKS a candidate — absence of a verdict is neutral).
    """
    try:
        verdict = board_edge(min_n=min_n).get("edge_verdict")
    except Exception as e:  # noqa: BLE001 — fail-soft: a broken loop is neutral, never a shrink
        log.debug("board_learning: multiplier failed (%s)", e)
        return _MULT_CEIL

    if verdict == "negative":
        mult = NEGATIVE_MULT
    elif verdict == "weak":
        mult = WEAK_MULT
    else:
        # 'strong' and 'insufficient' (and any unrecognized value) → par. Never a boost.
        mult = _MULT_CEIL

    # SHRINK-ONLY clamp — structurally impossible to exceed 1.0 (or drop below the floor).
    return max(_MULT_FLOOR, min(_MULT_CEIL, float(mult)))


# --------------------------------------------------------------------------- #
# audit_row() — perception runlog
# --------------------------------------------------------------------------- #

def audit_row() -> dict[str, Any]:
    """Return {status, as_of, n, edge_verdict, multiplier} for the perception runlog.

    status mirrors the edge_verdict semantics for the runlog:
      * 'insufficient' — cold-start (no/thin forward data); multiplier 1.0.
      * 'strong' | 'weak' | 'negative' — a scored board.

    Flag-independent — always safe to run. Fail-soft → the insufficient block with a 1.0 multiplier.
    """
    try:
        edge = board_edge()
        verdict = edge.get("edge_verdict", "insufficient")
        return {
            "status": verdict,
            "as_of": edge.get("as_of"),
            "n": edge.get("n", 0),
            "edge_verdict": verdict,
            "multiplier": standout_trust_multiplier(),
        }
    except Exception as e:  # noqa: BLE001 — fail-soft
        log.debug("board_learning: audit_row failed (%s)", e)
        return {"status": "insufficient", "as_of": None, "n": 0,
                "edge_verdict": "insufficient", "multiplier": _MULT_CEIL}


# --------------------------------------------------------------------------- #
# internal coercers (never fabricate a number)
# --------------------------------------------------------------------------- #

def _int(x: Any) -> int:
    """Coerce to int, or 0. Rejects bool. Fail-soft — never raises."""
    if isinstance(x, bool) or x is None:
        return 0
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def _float(x: Any) -> Optional[float]:
    """Coerce to float, or None. Rejects bool / dict / list (never a fabricated number)."""
    if x is None or isinstance(x, (dict, list, bool)):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

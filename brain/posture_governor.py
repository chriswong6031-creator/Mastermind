"""THE POSTURE GOVERNOR — the smallest safe self-adaptive corrective (W-L / L5c; architecture Stage 10).

The benchmark ledger now MEASURES the fact the whole program started from: the user's Self-Directed
(defensive) book has, in the incident window, beaten the Brain's posture. The governor is the loop that
would eventually ACT on that — nudging the Brain's leadership-budget multiplier toward defense when the
gap is real and persistent, and back toward 1.0 when it closes. It is deliberately the most conservative
thing in the program:

  * DEFAULT OFF. ``MASTERMIND_POSTURE_ADAPT`` unset ⇒ ``multiplier()`` returns exactly 1.0. The loop
    still OBSERVES (computes the gap, the guards, the would-be step) and journals its state, but it
    NEVER moves a live budget. This is the honest near-term deliverable: *see and credit the gap, with
    the smallest safe corrective armed and waiting* (architecture §risk-4).
  * Charter P8 statistical guards — ALL must hold before the multiplier may leave 1.0:
      - ``effective_n >= min_effective_n`` INDEPENDENT gap observations (not raw session count);
      - the rolling brain-minus-defensive gap is HAC-significant (Newey-West |t| >= ``hac_t_min``);
      - ``hysteresis_reviews`` consecutive SAME-SIGN reviews (a one-off print never moves it).
  * Motion is a ±``step`` (0.05) nudge, HARD-CLAMPED inside the doctrine ``[floor, ceil]`` band — the
    governor can only trim WITHIN the band the budget already lives in, never widen it. Shrink-toward-
    defense may step immediately once armed (shrink-fast); restore toward 1.0 EMA-decays (restore-slow),
    and on a sign flip the multiplier decays toward 1.0 rather than jumping.
  * NONE of the governor's own constants are self-tunable (doctrine denylist entry ``governor``): the
    adapter never adapts its own guards (P8 — never self-modify the validation machinery).

Signs: the gap is ``brain_return − defensive_return`` per review. A PERSISTENTLY NEGATIVE gap (the Brain
trailing defense) pushes the multiplier DOWN toward the floor (trim leadership, lean defensive). A
positive gap (the Brain beating defense) restores it toward 1.0 (SLOWLY). The multiplier is only ever
<= ceil (1.0) — the governor trims, it never boosts leadership above the un-governed budget.

State persists in ``data/posture_governor/state.json``: the review streak, the current multiplier, and
the revert bookkeeping. Everything is best-effort — a missing ledger degrades to a no-op (multiplier 1.0)
and never raises (P2).
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_STATE = _ROOT / "data" / "posture_governor" / "state.json"
_BENCH_DIR = _ROOT / "data" / "benchmark"

# ── doctrine fallbacks (mirror config/doctrine.yml posture_governor:) — all (unverified-prior) ──
_STEP = 0.05
_FLOOR = 0.40
_CEIL = 1.00
_MIN_EFFECTIVE_N = 8
_HAC_T_MIN = 2.0
_HAC_LAG = 3
_HYSTERESIS = 3
_EMA_ALPHA = 0.34

_ADAPT_ENV = "MASTERMIND_POSTURE_ADAPT"


# ─────────────────────────────────────────────────────────────────────────────
# doctrine + flag
# ─────────────────────────────────────────────────────────────────────────────
def _cfg() -> dict:
    try:
        from bot.doctrine_config import load_doctrine
        b = load_doctrine().get("posture_governor")
        return b if isinstance(b, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _cf(key: str, default: float) -> float:
    v = _cfg().get(key)
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _ci(key: str, default: int) -> int:
    v = _cfg().get(key)
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def armed() -> bool:
    """True iff the governor is ARMED to move a live budget (MASTERMIND_POSTURE_ADAPT on). Default OFF."""
    return os.environ.get(_ADAPT_ENV, "0").strip().lower() in ("1", "true", "yes", "on")


# ─────────────────────────────────────────────────────────────────────────────
# state IO (never raises)
# ─────────────────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        d = json.loads(_STATE.read_text())
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict) -> None:
    try:
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        _STATE.write_text(json.dumps(state, indent=2, default=str))
    except Exception:  # noqa: BLE001
        pass


def _default_state() -> dict:
    return {"multiplier": 1.0, "streak_sign": 0, "streak_len": 0, "last_review": None,
            "reverts": 0, "locked": False, "last_armed": None}


# ─────────────────────────────────────────────────────────────────────────────
# MW2 emitter (d): posture_governor armed/disarmed on TRANSITION only
# ─────────────────────────────────────────────────────────────────────────────

def _check_emit_armed_transition(root: str | Path | None = None) -> bool | None:
    """MW2 emitter (d): check the MASTERMIND_POSTURE_ADAPT flag against the last-recorded
    armed state in state.json; emit ``posture_governor_armed`` or ``posture_governor_disarmed``
    on a TRANSITION only.  Persists the new armed state so subsequent calls do not re-fire.

    Returns the resolved current armed state (None on failure) so callers that
    save their own state snapshot afterwards can carry ``last_armed`` forward —
    review()'s _save_state(out_state) would otherwise clobber the write made here.

    Called at the point where arming is checked (persist path of ``review()`` + a direct
    ``check_armed_transition()`` export).  Never raises."""
    try:
        current = armed()
        st = _load_state() or _default_state()
        last = st.get("last_armed")
        if last is None:
            # First time: record the current state without emitting (no prior to diff against)
            st["last_armed"] = current
            _save_state(st)
            return current
        if bool(last) == current:
            return current  # no transition
        # Transition detected
        event_type = "posture_governor_armed" if current else "posture_governor_disarmed"
        reason = (
            f"MASTERMIND_POSTURE_ADAPT {'set' if current else 'unset'}: posture governor "
            f"{'armed to move live budget' if current else 'disarmed; multiplier pinned to 1.0'}"
        )
        try:
            from control_plane import governance as _gov
            _gov.append({
                "event_type": event_type,
                "target": _ADAPT_ENV,
                "actor": "system",
                "reason": reason,
                "before": bool(last),
                "after": current,
                "rollback": (
                    f"{'unset' if current else 'set'} MASTERMIND_POSTURE_ADAPT env var"
                ),
                "source_artifact": "brain.posture_governor",
            }, root=root)
        except Exception:  # noqa: BLE001
            pass
        st["last_armed"] = current
        _save_state(st)
        return current
    except Exception:  # noqa: BLE001 — never interfere with the governor
        return None


def check_armed_transition(root: str | Path | None = None) -> bool | None:
    """Public entry-point for MW2 emitter (d): call at startup to detect arming transitions.
    Safe to call multiple times — only emits on state CHANGE.  Never raises."""
    return _check_emit_armed_transition(root=root)


# ─────────────────────────────────────────────────────────────────────────────
# the gap series — INPUT wired to the benchmark ledger (brain minus defensive)
# ─────────────────────────────────────────────────────────────────────────────
def _bench_history() -> list[dict]:
    """Every persisted benchmark ledger, oldest-first. Best-effort ([] on any miss)."""
    try:
        out = []
        for f in sorted(_BENCH_DIR.glob("*.json")):
            try:
                out.append(json.loads(f.read_text()))
            except Exception:  # noqa: BLE001
                continue
        return out
    except Exception:  # noqa: BLE001
        return []


def _gap_from_ledger(ledger: dict, book: str) -> float | None:
    """brain_return − defensive_return for one ledger snapshot (percent → fraction). None if either
    side is missing. `book` is the Brain book whose return stands for 'the Brain' (default autonomous)."""
    if not isinstance(ledger, dict):
        return None
    lb = {r.get("id"): r for r in (ledger.get("leaderboard") or [])}
    dfn = (lb.get("defensive") or {}).get("return_pct")
    brain = (lb.get(book) or {}).get("return_pct")
    if dfn is None or brain is None:
        return None
    try:
        return (float(brain) - float(dfn)) / 100.0
    except (TypeError, ValueError):
        return None


def gap_series(book: str = "autonomous", *, ledgers: list[dict] | None = None) -> list[float]:
    """The rolling brain-minus-defensive gap series over the benchmark-ledger history (fractions). A
    persistently NEGATIVE series = the Brain trailing defense (the governor's arming condition). Reads
    the persisted ledgers unless `ledgers` is injected (fixture path). Best-effort; [] on no data."""
    hist = ledgers if ledgers is not None else _bench_history()
    out = []
    for lg in hist:
        g = _gap_from_ledger(lg, book)
        if g is not None:
            out.append(g)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# statistical guards
# ─────────────────────────────────────────────────────────────────────────────
def _hac(series: list[float], lags: int) -> dict:
    """Newey-West mean/t over the gap series, reusing the macro engine's validated helper. Degrades to
    {n, t:0} when the helper or data is unavailable (guard then fails closed → no arming)."""
    if not series or len(series) < 2:
        return {"n": len(series or []), "mean": (series[0] if series else None), "t": 0.0, "se": None}
    try:
        from engine.validation import newey_west_tstat
        return newey_west_tstat(list(series), lags=lags)
    except Exception:  # noqa: BLE001
        n = len(series)
        mean = sum(series) / n
        return {"n": n, "mean": round(mean, 6), "t": 0.0, "se": None}


def guards(series: list[float]) -> dict:
    """Evaluate the P8 arming guards over a gap series WITHOUT touching state. Returns every guard's
    value + a single `pass_all`. Pure; never raises. effective_n = len(series) (each ledger snapshot is
    one independent review; the caller is responsible for feeding non-overlapping snapshots)."""
    min_n = _ci("min_effective_n", _MIN_EFFECTIVE_N)
    t_min = _cf("hac_t_min", _HAC_T_MIN)
    lags = _ci("hac_lag", _HAC_LAG)
    stat = _hac(series, lags)
    eff_n = len(series)
    n_ok = eff_n >= min_n
    t = float(stat.get("t") or 0.0)
    sig_ok = abs(t) >= t_min
    return {"effective_n": eff_n, "min_effective_n": min_n,
            "hac_t": round(t, 4), "hac_t_min": t_min, "mean_gap": stat.get("mean"),
            "n_ok": n_ok, "significant": sig_ok, "pass_all": bool(n_ok and sig_ok)}


# ─────────────────────────────────────────────────────────────────────────────
# the review — the one state transition
# ─────────────────────────────────────────────────────────────────────────────
def _clamp(x: float) -> float:
    return max(_cf("floor", _FLOOR), min(_cf("ceil", _CEIL), x))


def review(series: list[float], *, asof: date | None = None, state: dict | None = None,
           persist: bool = False) -> dict:
    """One weekly governor review over the gap series. Updates the hysteresis streak, and — ONLY when
    the guards pass AND the streak is mature AND the governor is ARMED — steps the multiplier. When
    disarmed (default) the multiplier is pinned at 1.0 but the streak/guards are still tracked and
    reported (the observe-only near-term deliverable).

    Motion (armed): a persistently NEGATIVE gap (Brain trailing defense) shrinks the multiplier toward
    the floor by `step` (shrink-fast). A positive gap restores toward the ceiling via an EMA (restore-
    slow). A sign flip vs the tracked streak EMA-decays the multiplier toward 1.0. Always clamped inside
    the doctrine band. Best-effort; never raises."""
    asof = asof or date.today()
    st = dict(state if state is not None else _load_state() or _default_state())
    st = {**_default_state(), **st}

    g = guards(series)
    mean_gap = g.get("mean_gap")
    sign = 0 if mean_gap is None else (1 if mean_gap > 0 else (-1 if mean_gap < 0 else 0))

    # hysteresis streak: same-sign reviews accumulate; a sign change resets the streak to 1.
    if sign == 0:
        streak_sign, streak_len = st.get("streak_sign", 0), 0
    elif sign == st.get("streak_sign", 0):
        streak_sign, streak_len = sign, int(st.get("streak_len", 0)) + 1
    else:
        streak_sign, streak_len = sign, 1

    hysteresis = _ci("hysteresis_reviews", _HYSTERESIS)
    step = _cf("step", _STEP)
    alpha = _cf("ema_alpha", _EMA_ALPHA)
    mult = float(st.get("multiplier", 1.0))
    locked = bool(st.get("locked", False))

    streak_mature = streak_len >= hysteresis
    would_move = bool(g["pass_all"] and streak_mature and sign != 0)
    moved = False
    action = "observe"

    if would_move and armed() and not locked:
        prev_sign = st.get("streak_sign", 0)
        if sign < 0:                                   # Brain trailing defense → shrink (shrink-fast)
            new = _clamp(mult - step)
            action = "shrink"
        elif prev_sign is not None and prev_sign > 0 and sign > 0:
            new = _clamp(mult + alpha * (_cf("ceil", _CEIL) - mult))   # restore-slow toward ceiling
            action = "restore"
        else:                                          # sign flip → EMA-decay toward neutral 1.0
            new = _clamp(mult + alpha * (1.0 - mult))
            action = "decay"
        moved = abs(new - mult) > 1e-12
        mult = new
    elif not armed():
        mult = 1.0                                     # disarmed: pinned neutral, but streak still tracks
        action = "observe_disarmed"

    out_state = {**st, "multiplier": round(mult, 6), "streak_sign": streak_sign,
                 "streak_len": streak_len, "last_review": asof.isoformat()}
    if persist:
        # MW2 emitter (d): check for armed/disarmed TRANSITION before persisting new state,
        # then carry the reconciled last_armed INTO out_state — otherwise this
        # _save_state(out_state) clobbers the write the transition check just made
        # (out_state was built from the state loaded at review() start).
        armed_now = _check_emit_armed_transition()
        if armed_now is not None:
            out_state["last_armed"] = armed_now
        _save_state(out_state)

    return {"as_of": asof.isoformat(), "armed": armed(), "multiplier": round(mult, 6),
            "action": action, "moved": moved, "would_move_if_armed": would_move,
            "streak_sign": streak_sign, "streak_len": streak_len,
            "hysteresis_reviews": hysteresis, "guards": g, "state": out_state}


# ─────────────────────────────────────────────────────────────────────────────
# public read — what a budget consumer multiplies by
# ─────────────────────────────────────────────────────────────────────────────
def multiplier() -> float:
    """The leadership-budget multiplier a consumer applies. DISARMED (default) → exactly 1.0 (no-op).
    ARMED → the persisted, band-clamped governor multiplier (<= ceil). 1.0 on any miss (P2)."""
    if not armed():
        return 1.0
    try:
        m = _load_state().get("multiplier")
        return _clamp(float(m)) if m is not None else 1.0
    except Exception:  # noqa: BLE001
        return 1.0


def status() -> dict:
    """A display-only snapshot for cio / the agenda: armed flag, current multiplier, streak, and the
    latest guard readout over the persisted ledger history. Never raises."""
    try:
        st = _load_state() or _default_state()
    except Exception:  # noqa: BLE001
        st = _default_state()
    try:
        g = guards(gap_series())
    except Exception:  # noqa: BLE001
        g = {}
    return {"armed": armed(), "multiplier": st.get("multiplier", 1.0),
            "streak_sign": st.get("streak_sign", 0), "streak_len": st.get("streak_len", 0),
            "locked": st.get("locked", False), "guards": g}

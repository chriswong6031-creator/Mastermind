"""ENTRY ENGINE — the binding is-NOW-a-good-price assessor (Flagship v2, W8 "the entry era").

Extends the W0 advisory annotator (``portfolio/entry_quality.py``, absorbed per
``research/FLAGSHIP_V2_DECISION_CORE.md`` §2.1) into the desk's first-class ENTRY axis. Where the
confluence gate asks *"is this a real leader?"* and the vetoes ask *"is it broken?"*, this module
owns the question the 2026-07-13..16 buys proved nobody owned: *"is NOW a good price/technical
moment to START?"* — on the fast timescale a chase actually happens (range position, off-low
run-up, 20dma stretch, rollover-off-the-boil), joined with the dashboard's VALIDATED per-name
entry tiers (MACD-2D × StochRSI-3D cascade in ``signal_gate.json``), Weinstein stage / NW
bottom-state evidence, and — when a US Prophet plan exists for the name — the plan's own
entry/invalidation geometry (never chase past entry + 0.5R; never buy a missed move).

CONTRACT (inherits the W0 contract, now with binding consumers):
  * PURE + INJECTABLE. Every external read can be passed in; production defaults read the
    vendored artifacts. Tests/replays run fully offline.
  * FAIL-OPEN per input (charter P2): a missing signal informs nothing — it can never manufacture
    a withhold. ``verdict='unknown'`` (no usable sources) withholds NOTHING; the existing W0
    fail-closed stockdata floor + data-health breaker own true outages upstream.
  * SUBTRACT-ONLY at the consumer: a non-buyable verdict PARKS a would-be NEW buy on the
    watchlist with explicit promotion triggers (patience, not forfeit). It never exits, resizes,
    or downgrades a HELD name — entry is an entry brake, standing invariant.
  * Thresholds are (unverified-prior) starter values, tuned on the outcome ledger; every verdict
    is stamped on decision records so the §5 kill criteria stay computable.

Verdict vocabulary (BUYABLE = {clean, pullback_in_trend, base_turn}; everything else parks):
    clean             — no adverse tell; a reasonable place to start.
    pullback_in_trend — uptrend intact, price back in the lower part of its recent range (the
                        AVGO-dip class — buy weakness in strength).
    base_turn         — a base/bottom turning up with validated tier evidence (the XLE class —
                        the positive path the strength-biased gate never had).
    extended          — stretched vs fast/slow MA or pinned at the 52w high; wait for a reset.
    chase             — at the top of its own recent range on a fresh rip (the buy-high failure).
    late_leg          — deep INTO a move off the recent low (the AAPL +21%-in-3-weeks class).
    rollover          — just came off the boil: was at the range top days ago, now fading (the
                        ANET / QUAL "StochRSI 90 rolling over" class).
    knife             — acute multi-day collapse (existing falling-knife thresholds, reused).
    missed_move / extended_vs_plan — price beyond the Prophet plan's chase room (plan-geometry
                        discipline; only when a plan exists).
    unknown           — not enough data to judge; withholds nothing (fail-open).
"""
from __future__ import annotations

import logging

from portfolio.entry_quality import (  # the absorbed W0 annotator's pure helpers
    _fast_metrics,
    _pulse_heat_for,
)

log = logging.getLogger(__name__)

BUYABLE = ("clean", "pullback_in_trend", "base_turn")

# ── thresholds — (unverified-prior), tuned on the ledger; W0 values kept where they existed ──
_RANGE_HIGH_PCTILE = 90.0     # top of the 60d range
_RANGE_RESET_PCTILE = 80.0    # a top-decile name fading back below this = coming off the boil
_RANGE_PULLBACK_PCTILE = 40.0  # lower band = a pullback entry, not a chase
_RANGE_LOW_PCTILE = 15.0
_HOT_PCT_VS_20DMA = 12.0
_RIP_RET_10D = 0.18
_EXTENDED_PCT_VS_200DMA = 30.0
_NEAR_HIGH_OFF_52W = -6.0
_FALL_5D = -0.09              # falling-knife thresholds — identical to lenses.py (reused semantics)
_FALL_10D = -0.14
_LATE_LEG_RET_63D = 0.20      # >= +20% off the 63d low ...
_LATE_LEG_RANGE = 85.0        # ... while still near the top of the range ...
_LATE_LEG_MAX_DAYS = 45       # ... within the same leg → too deep into the move to START
_ROLLOVER_PEAK_LOOKBACK = 10  # sessions in which the name must have printed the range top
_ROLLOVER_RET_5D = -0.03      # a -3% 5d fade off the boil reads as rolling over
_BASE_TURN_MAX_RANGE = 60.0   # a base turn must not already be at the top of its range
_BASE_TURN_MAX_WEEKS = 8      # stage-2 freshness bound (weeks_in_stage)
_PLAN_CHASE_ROOM_R = 0.5      # never chase a Prophet plan past entry + 0.5R

_TIER_FRESH = ("T1", "T2")    # the dashboard's validated buy tiers (T3 buyable but not "fresh-clean")


# ─────────────────────────────────────────────────────────────────────────────
# default loaders — every one optional + fail-open; tests inject instead
# ─────────────────────────────────────────────────────────────────────────────
def _default_series(ticker: str):
    try:
        from portfolio import lenses
        return lenses._closes(ticker)
    except Exception:  # noqa: BLE001
        return None


def _default_stockdata(ticker: str):
    try:
        from portfolio import lenses
        return lenses._load(f"site/stockdata/{ticker}.json")
    except Exception:  # noqa: BLE001
        return None


def _default_signal_gate_row(ticker: str):
    """Per-name row from the dashboard's validated entry-tier cascade
    (``site/factordata/signal_gate.json`` → ``{as_of, verdicts:{T:{eligible, tier_cascade,…}}}``)."""
    try:
        from portfolio import lenses
        d = lenses._load("site/factordata/signal_gate.json") or {}
        row = (d.get("verdicts") or {}).get((ticker or "").upper())
        return row if isinstance(row, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _default_stage_row(ticker: str):
    """Per-name Weinstein stage row from the vendored stage-analysis context roster
    (``data/stage_analysis/context/latest.json`` → ``roster{T:{stage_flag, weeks_in_stage,…}}``).
    Absent until the sparse checkout ships it — fail-open."""
    try:
        from portfolio import lenses
        d = lenses._load("data/stage_analysis/context/latest.json") or {}
        row = (d.get("roster") or {}).get((ticker or "").upper())
        return row if isinstance(row, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _default_bottom_state(ticker: str):
    """NW bottom-sensor state for the name (mastermind_context candidate row) — parallel
    base-turn evidence when the stage roster is absent. Fail-soft to None."""
    try:
        from brain import neural_web_context as nwc
        row = nwc.candidate(ticker) or {}
        bottom = row.get("bottom") or {}
        st = bottom.get("bottom_state") or bottom.get("state")
        return st if isinstance(st, str) else None
    except Exception:  # noqa: BLE001
        return None


def _default_prophet_plan(ticker: str):
    try:
        from portfolio import prophet_feed
        return prophet_feed.plan_for(ticker)
    except Exception:  # noqa: BLE001
        return None


# ─────────────────────────────────────────────────────────────────────────────
# metric helpers (pure)
# ─────────────────────────────────────────────────────────────────────────────
def _leg_metrics(series) -> dict:
    """Off-low leg metrics from an ascending close Series: how far INTO the current move a
    would-be entry is. {ret_from_21d_low, ret_from_63d_low, days_since_63d_low,
    peak_range_pctile_10d, ret_5d} — all nullable; never raises."""
    out = {"ret_from_21d_low": None, "ret_from_63d_low": None, "days_since_63d_low": None,
           "peak_range_pctile_10d": None, "ret_5d": None}
    try:
        if series is None or len(series) < 22:
            return out
        last = float(series.iloc[-1])
        w21 = series.iloc[-21:]
        lo21 = float(w21.min())
        if lo21 > 0:
            out["ret_from_21d_low"] = round(last / lo21 - 1.0, 4)
        w63 = series.iloc[-63:] if len(series) >= 63 else series
        lo63 = float(w63.min())
        if lo63 > 0:
            out["ret_from_63d_low"] = round(last / lo63 - 1.0, 4)
        try:
            # LAST touch of the low, not the first (argmin) — on a flat base the low is held for
            # weeks, and "how far into the leg is this entry" is measured from when price LEFT it.
            vals = w63.values
            idx_low = max(i for i in range(len(vals)) if float(vals[i]) == float(vals.min()))
            out["days_since_63d_low"] = int(len(w63) - 1 - idx_low)
        except Exception:  # noqa: BLE001
            pass
        # where the RECENT PEAK sat in the 60d range — the "was at the boil" read for rollover.
        w60 = series.iloc[-60:] if len(series) >= 60 else series
        hi60, lo60 = float(w60.max()), float(w60.min())
        if hi60 > lo60:
            peak10 = float(series.iloc[-_ROLLOVER_PEAK_LOOKBACK:].max())
            out["peak_range_pctile_10d"] = round(
                max(0.0, min(1.0, (peak10 - lo60) / (hi60 - lo60))) * 100.0, 1)
        if len(series) > 5:
            base5 = float(series.iloc[-6])
            if base5 > 0:
                out["ret_5d"] = round(last / base5 - 1.0, 4)
    except Exception:  # noqa: BLE001
        return out
    return out


def _tier_read(sg_row: dict | None) -> dict:
    """{tier, eligible, fresh} from a signal_gate verdicts row — the dashboard's validated
    MACD-2D × StochRSI-3D entry cascade. {}-safe."""
    if not isinstance(sg_row, dict):
        return {"tier": None, "eligible": None, "fresh": False}
    tier = sg_row.get("tier_cascade")
    eligible = sg_row.get("eligible")
    fresh = isinstance(tier, str) and tier in _TIER_FRESH and eligible is True
    return {"tier": tier if isinstance(tier, str) else None,
            "eligible": eligible if isinstance(eligible, bool) else None,
            "fresh": bool(fresh)}


def _stage_read(stage_row: dict | None, bottom_state: str | None) -> dict:
    """Base/turn evidence: Weinstein stage (roster row) and/or NW bottom-sensor state.
    turn_evidence=True when EITHER says a base is turning (stage 2 fresh, or stage 1 with a
    BOTTOMING/CONFIRMED sensor)."""
    stage = weeks = None
    if isinstance(stage_row, dict):
        try:
            s = stage_row.get("stage_flag") or stage_row.get("stage")
            stage = int(s) if s is not None else None
        except Exception:  # noqa: BLE001
            stage = None
        try:
            w = stage_row.get("weeks_in_stage")
            weeks = float(w) if w is not None else None
        except Exception:  # noqa: BLE001
            weeks = None
    bs = bottom_state if isinstance(bottom_state, str) else None
    stage2_fresh = stage == 2 and (weeks is None or weeks <= _BASE_TURN_MAX_WEEKS)
    sensor_turn = bs in ("BOTTOMING", "CONFIRMED")
    stage3_warn = stage == 3
    return {"stage": stage, "weeks_in_stage": weeks, "bottom_state": bs,
            "turn_evidence": bool(stage2_fresh or (stage in (None, 1, 2) and sensor_turn)),
            "stage3_warn": bool(stage3_warn)}


def _plan_read(plan: dict | None, last: float | None) -> dict:
    """Prophet plan-geometry discipline. {} when no plan/price. status ∈
    within_zone|pre_trigger|extended_vs_plan|missed_move|invalidated."""
    if not isinstance(plan, dict) or not isinstance(last, (int, float)):
        return {}
    try:
        entry = plan.get("entry")
        inval = plan.get("invalidation")
        t1 = plan.get("t1")
        trigger = plan.get("trigger")
        if not isinstance(entry, (int, float)) or not isinstance(inval, (int, float)):
            return {}
        r = entry - inval
        if r <= 0:
            return {}
        if last < inval:
            status = "invalidated"
        elif isinstance(t1, (int, float)) and last > t1:
            status = "missed_move"
        elif last > entry + _PLAN_CHASE_ROOM_R * r:
            status = "extended_vs_plan"
        elif plan.get("phase") == "pre_trigger" and isinstance(trigger, (int, float)) and last < trigger:
            status = "pre_trigger"
        else:
            status = "within_zone"
        return {"status": status, "plan_id": plan.get("plan_id"), "r": round(r, 4),
                "room_r": round((last - entry) / r, 2), "conviction": plan.get("conviction")}
    except Exception:  # noqa: BLE001
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# the assessor
# ─────────────────────────────────────────────────────────────────────────────
def assess(ticker: str, *, series=None, stockdata: dict | None = None,
           signal_gate_row: dict | None = None, stage_row: dict | None = None,
           bottom_state: str | None = None, pulse: dict | None = None,
           theme_id: str | None = None, prophet_plan: dict | None = None,
           as_of: str | None = None, _use_defaults: bool = True) -> dict:
    """The binding entry read for a would-be NEW buy. Returns
    ``{ticker, verdict, buyable, entry_score, metrics, notes, park_triggers, sources, as_of}``.

    FAIL-OPEN: absent inputs degrade toward 'unknown', which withholds NOTHING (the caller treats
    only explicit adverse verdicts as park-worthy). Never raises. ``_use_defaults=False`` keeps the
    call fully offline (tests) even where an injected input is None."""
    t = (ticker or "").upper().strip()
    notes: list[str] = []
    sources: list[str] = []

    if series is None and _use_defaults:
        series = _default_series(t)
    if stockdata is None and _use_defaults:
        stockdata = _default_stockdata(t)
    if signal_gate_row is None and _use_defaults:
        signal_gate_row = _default_signal_gate_row(t)
    if stage_row is None and _use_defaults:
        stage_row = _default_stage_row(t)
    if bottom_state is None and _use_defaults:
        bottom_state = _default_bottom_state(t)
    if prophet_plan is None and _use_defaults:
        prophet_plan = _default_prophet_plan(t)

    fm = _fast_metrics(series)
    lm = _leg_metrics(series)
    if fm.get("last") is not None:
        sources.append("price_series")

    tech = (stockdata or {}).get("tech") or {}
    p200 = tech.get("pct_vs_200dma")
    off52 = tech.get("off_52w_high_pct")
    above200 = tech.get("above200")
    rsi14 = tech.get("rsi14")
    macd_pos = tech.get("macd_pos")
    if isinstance(stockdata, dict) and tech:
        sources.append("stockdata")

    tier = _tier_read(signal_gate_row)
    if tier.get("tier") is not None or tier.get("eligible") is not None:
        sources.append("signal_gate")
    stg = _stage_read(stage_row, bottom_state)
    if stg.get("stage") is not None or stg.get("bottom_state") is not None:
        sources.append("stage_or_sensor")
    if theme_id is None and isinstance(stockdata, dict):
        sp = stockdata.get("sector_pulse")
        if isinstance(sp, dict):
            theme_id = sp.get("theme_id")
    heat = _pulse_heat_for(theme_id, pulse) if pulse is not None else {}
    if heat:
        sources.append("sector_pulse")
    plan = _plan_read(prophet_plan, fm.get("last"))
    if plan:
        sources.append("prophet_plan")

    rp = fm.get("range_pctile_60d")
    p20 = fm.get("pct_vs_20dma")
    r10 = fm.get("ret_10d")
    r5 = lm.get("ret_5d")
    peak10 = lm.get("peak_range_pctile_10d")
    off_low63 = lm.get("ret_from_63d_low")
    d_low63 = lm.get("days_since_63d_low")

    _n = lambda x: isinstance(x, (int, float))          # noqa: E731 — numeric-guard shorthand
    at_top = _n(rp) and rp >= _RANGE_HIGH_PCTILE
    at_low = _n(rp) and rp <= _RANGE_LOW_PCTILE
    pullback_band = _n(rp) and rp <= _RANGE_PULLBACK_PCTILE
    fast_stretch = _n(p20) and p20 >= _HOT_PCT_VS_20DMA
    fresh_rip = _n(r10) and r10 >= _RIP_RET_10D
    slow_ext = _n(p200) and p200 >= _EXTENDED_PCT_VS_200DMA
    pinned_high = _n(off52) and off52 >= _NEAR_HIGH_OFF_52W
    knife = (_n(r5) and r5 <= _FALL_5D) or (_n(r10) and r10 <= _FALL_10D)
    was_at_boil = _n(peak10) and peak10 >= _RANGE_HIGH_PCTILE
    fading = (_n(rp) and rp <= _RANGE_RESET_PCTILE) or (_n(r5) and r5 <= _ROLLOVER_RET_5D)
    rollover = was_at_boil and fading and not at_low and not knife
    # strengtheners make the rollover read louder but never create it from nothing
    if rollover and (macd_pos is False or tier.get("eligible") is False or
                     (_n(rsi14) and rsi14 >= 70)):
        notes.append("rollover confirmed by MACD/tier/RSI state")
    chase = at_top and (fresh_rip or fast_stretch) and not rollover
    late_leg = (not chase and not rollover
                and _n(off_low63) and off_low63 >= _LATE_LEG_RET_63D
                and _n(rp) and rp >= _LATE_LEG_RANGE
                and (d_low63 is None or d_low63 <= _LATE_LEG_MAX_DAYS))
    extended = (fast_stretch or slow_ext or pinned_high) and not (chase or rollover or late_leg)
    base_turn = (stg.get("turn_evidence") and (tier.get("fresh") or tier.get("eligible") is True)
                 and (_n(rp) and rp <= _BASE_TURN_MAX_RANGE) and not knife)
    pullback = (pullback_band and above200 is True and not knife and not rollover)

    # ── verdict (most-adverse-first; buyable classes only when nothing adverse fired) ──
    have_price = fm.get("last") is not None
    if not sources:
        verdict = "unknown"
    elif plan.get("status") == "missed_move":
        verdict = "missed_move"
        notes.append(f"beyond Prophet T1 (room {plan.get('room_r')}R) — the move already paid")
    elif plan.get("status") == "extended_vs_plan":
        verdict = "extended_vs_plan"
        notes.append(f"+{plan.get('room_r')}R past Prophet entry — no chase room left")
    elif knife:
        verdict = "knife"
        notes.append("acute multi-day collapse — do not catch the knife")
    elif rollover:
        verdict = "rollover"
        notes.append(f"came off the boil — peak {peak10:.0f}th pctile within "
                     f"{_ROLLOVER_PEAK_LOOKBACK}d, now {rp:.0f}th" if _n(peak10) and _n(rp)
                     else "came off the boil")
    elif chase:
        bits = [f"{rp:.0f}th pctile of 60d range"]
        if fresh_rip:
            bits.append(f"+{r10 * 100:.0f}% in 10d")
        if fast_stretch:
            bits.append(f"+{p20:.0f}% vs 20dma")
        verdict = "chase"
        notes.append("buying the top of the range on a fresh rip (" + ", ".join(bits) + ")")
    elif late_leg:
        verdict = "late_leg"
        notes.append(f"+{off_low63 * 100:.0f}% off the 63d low in {d_low63 or '?'} sessions — "
                     "too deep into the leg to START")
    elif extended:
        verdict = "extended"
        if pinned_high:
            notes.append(f"pinned near the 52w high ({off52:.0f}% off)")
        if slow_ext:
            notes.append(f"+{p200:.0f}% vs 200dma")
        if fast_stretch and not slow_ext:
            notes.append(f"+{p20:.0f}% vs 20dma")
    elif base_turn:
        verdict = "base_turn"
        notes.append("base turning up with validated tier evidence"
                     + (f" (stage {stg.get('stage')}, {stg.get('bottom_state') or 'no sensor'},"
                        f" tier {tier.get('tier')})"))
    elif pullback:
        verdict = "pullback_in_trend"
        notes.append(f"uptrend intact, {rp:.0f}th pctile of the 60d range — weakness in strength")
    elif have_price:
        verdict = "clean"
    else:
        verdict = "unknown"

    if stg.get("stage3_warn") and verdict in BUYABLE:
        # a Stage-3 (topping) read does not veto alone, but it must be visible on the record
        notes.append("caution: Weinstein stage 3 (topping) — size accordingly")
    if plan.get("status") == "within_zone":
        notes.append("within Prophet entry zone")
    if plan.get("status") == "pre_trigger":
        notes.append("Prophet setup not yet triggered — a watch, not a chase")

    # ── entry score (0-100; sizing tilt + grading substrate, never the gate itself) ──
    score = 50.0
    if verdict in ("chase", "missed_move"):
        score = 5.0
    elif verdict in ("rollover", "knife", "extended_vs_plan"):
        score = 10.0
    elif verdict == "late_leg":
        score = 15.0
    elif verdict == "extended":
        score = 25.0
    elif verdict == "base_turn":
        score = 80.0
    elif verdict == "pullback_in_trend":
        score = 75.0
    elif verdict == "clean":
        score = 60.0
    if tier.get("fresh"):
        score += 10.0
    if plan.get("status") == "within_zone":
        score += 10.0
    if heat.get("clean_entry_flag") is True:
        score += 5.0
    score = max(0.0, min(100.0, score))

    park_triggers = None
    if verdict not in BUYABLE and verdict != "unknown":
        park_triggers = {
            "range_pctile_lte": _RANGE_PULLBACK_PCTILE if verdict in ("chase", "late_leg")
            else _RANGE_RESET_PCTILE,
            "tier_in": list(_TIER_FRESH),
            "not_verdicts": ["chase", "late_leg", "rollover", "extended", "knife",
                             "missed_move", "extended_vs_plan"],
        }

    return {
        "ticker": t, "verdict": verdict, "buyable": verdict in BUYABLE,
        "entry_score": round(score, 1),
        "metrics": {
            "range_pctile_60d": rp, "pct_vs_20dma": p20, "ret_10d": r10, "ret_5d": r5,
            "pct_vs_200dma": p200, "off_52w_high_pct": off52, "above200": above200,
            "rsi14": rsi14, "macd_pos": macd_pos,
            "ret_from_21d_low": lm.get("ret_from_21d_low"),
            "ret_from_63d_low": off_low63, "days_since_63d_low": d_low63,
            "peak_range_pctile_10d": peak10,
            "tier": tier.get("tier"), "tier_eligible": tier.get("eligible"),
            "tier_fresh": tier.get("fresh"),
            "stage": stg.get("stage"), "bottom_state": stg.get("bottom_state"),
            "plan": plan or None,
        },
        "notes": notes, "park_triggers": park_triggers,
        "sources": sorted(set(sources)), "as_of": as_of,
    }


def still_withheld_reason(ticker: str) -> str | None:
    """Watchlist re-review predicate leg: None when the name's entry has healed (buyable or
    unknown — fail-open), else a short reason string. Never raises."""
    try:
        rep = assess(ticker)
        if rep.get("buyable") or rep.get("verdict") == "unknown":
            return None
        return f"entry {rep.get('verdict')}" + (
            f" — {rep['notes'][0]}" if rep.get("notes") else "")
    except Exception:  # noqa: BLE001 — a predicate failure must never block promotion
        return None

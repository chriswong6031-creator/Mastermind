"""brain/anticipation.py — the ANTICIPATION BATTERY (W-E.0 task E0.2, the "look-ahead" perception organ).

WHY THIS MODULE EXISTS
----------------------
The 2026-07-02 semis-breakdown post-mortem: the bot had size-brakes (W2/W3 caps, W1 tripwire) but no
organ that *anticipated* the trouble — no forward-looking classifier that said "this sector is topping",
"this pile is a crowding bubble", "market crash risk is rising" BEFORE the drawdown, from the planes the
dashboard already publishes. The judged design (research/eyes/judged_anticipation.md, winner = Design 1
MINIMAL-COMPOSABLE + Design 2 grafts) is this module: three graded alarms assembled from VENDORED data
only in v1, each carrying an honest authority tier, published to data/anticipation/<asof>.json.

This is a PERCEPTION organ, not a sizer. In this wave it changes ZERO behavior — it publishes an
artifact and enriches the PM payload (E1.1), read-only. The severity notch (crash-risk → derisk) arms
ONLY after the E1.4 AUC gate; until then every alarm ships ``notch_eligible=False`` (the seam is dark).

THE THREE ALARMS (each WATCH / ELEVATED / CRITICAL, "calm" below WATCH)
----------------------------------------------------------------------
  * SECTOR-TOP    (per sector): a sector distributing at the top of its cycle. Legs —
        distribution aggregate (REUSES portfolio/distribution_tells primitives — the 3D/weekly MACD
        bearish STATE + crowding percentile; NEVER forks that math), the sector_cycles ``now`` block
        (Peak/Topping phase + negative osc_slope + extended pos), RS deceleration (rs_63d vs rs_126d
        where the cycles product carries them; degrades to absent otherwise), and — as the magnitude
        payload the user asked for — the def-vs-offense ``rs_diff`` from distribution_tells' shared
        helper. Advisory forever-until-gated; barred from severity.
  * BUBBLE-FORMATION (per sector/cluster): a crowding bubble forming. Legs — crowding percentile +
        parabolic-extension share + the froth_fragility embedded quadrant read. ``cold_start=True``,
        ADVISORY FOREVER until a forward-graded crowding artifact exists (no gate available yet — the
        schema is honest about this).
  * CRASH-RISK    (market): rising market-wide crash risk. Legs — vol structure (vol_shock band/score),
        dealer gamma (READ-ONLY classification of market_gamma; bot/derisk.py OWNS gex for SEVERITY —
        this module only READS the regime to classify, it never notches off gex), a credit /
        liquidity_quality label, and auction stress when vendored. The severity-notch seam is present
        but DARK: ``notch_eligible=False`` until the E1.4 AUC>0.55 gate arms it.

AUTHORITY TIERS AS CODE (the graft — charter P3)
------------------------------------------------
Every alarm carries ``status`` ∈ {validated, advisory}, ``cold_start`` bool, and ``notch_eligible``
bool. ``notch_eligible`` is HARD-RESTRICTED to forward-graded legs — NONE exist in v1, so it is False
on every alarm this wave. The crash-risk severity notch arms only after its AUC gate in E1.4; we ship
the seam dark. No alarm may sign a posture tilt or release a cap; advisory can only annotate.

THE INVARIANT (governs every path)
----------------------------------
Missing / stale / thin data COARSENS or DROPS a leg — it never fabricates an alarm level, never raises
authority, never flips ``notch_eligible`` True. Absent legs simply do not fire; an alarm with zero
determinable legs sits at ``calm``. Every function degrades to a legal, low-alarm result and NEVER
raises. Pure / deterministic given the vendored artifacts + injected series fn.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Callable, Sequence

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_V = _ROOT / "vendor" / "macro"
_ARTIFACTS = _ROOT / "data" / "anticipation"

# ---- level lattice (ordered; "calm" is the below-WATCH null level) ----
_CALM = "calm"
_WATCH = "watch"
_ELEVATED = "elevated"
_CRITICAL = "critical"
_LEVEL_ORDER: dict[str, int] = {_CALM: 0, _WATCH: 1, _ELEVATED: 2, _CRITICAL: 3}

# The offensive baskets whose sector ETFs get a SECTOR-TOP alarm by default. The def/off baskets that
# feed the shared rs_diff magnitude live in distribution_tells (ONE definition) — we import that helper,
# never re-derive the baskets. The alarm universe is the 11 GICS SPDRs + the semis block that the
# incident turned on; degrade-safe if a sector's cycles row is absent.
_SECTOR_UNIVERSE: tuple[str, ...] = (
    "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY", "SMH",
)


# ---------------------------------------------------------------------------
# config (mirrors doctrine.yml `anticipation:` block; these are the degrade-safe fallbacks)
# ---------------------------------------------------------------------------

def _cfg() -> dict:
    """The anticipation doctrine block, or {} on any miss (→ all fallbacks apply)."""
    try:
        from bot.doctrine_config import load_doctrine
        block = load_doctrine().get("anticipation")
        return block if isinstance(block, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _cfg_f(key: str, default: float) -> float:
    v = _cfg().get(key)
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _cfg_list(key: str, default: Sequence[str]) -> tuple[str, ...]:
    v = _cfg().get(key)
    if isinstance(v, (list, tuple)) and v:
        return tuple(str(x).lower().strip() for x in v)
    return tuple(default)


# ---- default thresholds. All (unverified-prior). ----
_STOP_OSC_SLOPE_MAX = -5.0     # sector_cycles osc_slope <= this (rolling over) counts as a topping leg
_STOP_POS_MIN = 70.0           # cycle position >= this (extended into the top) counts
_STOP_CROWD_PCTILE = 0.90      # sector-ETF crowding percentile >= this counts (reuse of DT primitive)
_STOP_RS_DECEL_MIN = 0.0       # rs_63d - rs_126d < this (deceleration) counts, where the fields exist
_STOP_ELEVATED_LEGS = 2        # >= this many topping legs → ELEVATED
_STOP_CRITICAL_LEGS = 3        # >= this many topping legs (incl. a SELL/Topping phase) → CRITICAL

_BUB_CROWD_PCTILE = 0.90       # bubble crowding percentile floor
_BUB_PARABOLIC_PCTILE = 0.97   # parabolic-extension share floor (very-extended)

_CRASH_VOL_ELEVATED = 50.0     # vol_shock score >= this → an elevated vol leg
_CRASH_VOL_CRITICAL = 65.0     # vol_shock score >= this → a critical vol leg
_CRASH_GAMMA_FLIP_PCT = 0.0    # spot_vs_flip_pct <= this (below the gamma flip, dealers short) counts
_LIQ_STRESS_LABELS = ("stress-expansion", "neutral-hollow", "contracting")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _u(t: Any) -> str:
    return (str(t) if t is not None else "").upper().strip()


def _f(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _lc(x: Any) -> str:
    return (str(x) if x is not None else "").lower().strip()


def _max_level(*levels: str) -> str:
    """The highest (most-severe) level among the args; ``calm`` when none supplied/valid."""
    best = _CALM
    for lv in levels:
        if _LEVEL_ORDER.get(lv, 0) > _LEVEL_ORDER[best]:
            best = lv
    return best


def _level_from_legs(n_legs: int, *, elevated_at: int, critical_at: int, critical_bonus: bool = False) -> str:
    """Map a fired-leg count to a level. ``critical_bonus`` (a hard topping/sell confirmer) lets the
    alarm reach CRITICAL one leg early. Degrade-safe: 0 legs → calm."""
    if n_legs <= 0:
        return _CALM
    if n_legs >= critical_at or (critical_bonus and n_legs >= critical_at - 1):
        return _CRITICAL
    if n_legs >= elevated_at:
        return _ELEVATED
    return _WATCH


def _regime(regime: dict | None) -> dict:
    """The vendored regime/latest.json (already loaded by the caller when possible). None → read the
    vendored copy; {} on any miss. Never raises."""
    if isinstance(regime, dict):
        return regime
    try:
        p = _V / "data" / "regime" / "latest.json"
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


def _sector_cycles(sector_cycles: dict | None) -> dict:
    """The sector_cycles.json product (per-sector cycle ``now`` blocks). None → vendored site copy;
    {} on any miss. Never raises."""
    if isinstance(sector_cycles, dict):
        return sector_cycles
    for cand in (
        _V / "site" / "sectordata" / "sector_cycles.json",
        _V / "data" / "sector_cycles.json",
    ):
        try:
            if cand.exists():
                return json.loads(cand.read_text())
        except Exception:  # noqa: BLE001
            continue
    return {}


def _cycles_now_by_ticker(sc: dict) -> dict[str, dict]:
    """Flatten sector_cycles into ticker -> ``now`` block, over both the ``sectors`` and ``baskets``
    lists (the semis block SMH lives under baskets). {} on any miss."""
    out: dict[str, dict] = {}
    for key in ("sectors", "baskets"):
        rows = sc.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            tk = _u(row.get("ticker"))
            now = row.get("now")
            if tk and isinstance(now, dict):
                out.setdefault(tk, now)
    return out


def _sector_rs_by_ticker(regime: dict) -> dict[str, dict]:
    """Flatten the embedded ``sector_rs`` list into ticker -> row (carries pctile_252d, mom_20d_pct,
    mom_60d_pct, above_200d_trend, and rs_63d/rs_126d where the product publishes them)."""
    out: dict[str, dict] = {}
    for row in (regime.get("sector_rs") or []):
        if isinstance(row, dict):
            tk = _u(row.get("ticker"))
            if tk:
                out.setdefault(tk, row)
    return out


# ---------------------------------------------------------------------------
# the alarm-record constructor (one shape for all three alarms — schema honesty)
# ---------------------------------------------------------------------------

def _alarm(
    kind: str,
    scope: str,
    level: str,
    *,
    status: str,
    cold_start: bool,
    legs: dict[str, Any],
    magnitude: dict[str, Any] | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """Build ONE alarm record. ``notch_eligible`` is HARD-RESTRICTED to forward-graded legs — none in
    v1 — so it is ALWAYS False here (the severity-notch seam ships dark; E1.4 arms it after the AUC
    gate). Advisory alarms can annotate but never size or release a cap."""
    return {
        "kind": kind,                 # sector_top | bubble_formation | crash_risk
        "scope": scope,               # a sector ticker, a cluster id, or "market"
        "level": level,               # calm | watch | elevated | critical
        "status": status,             # validated | advisory  (all advisory in v1)
        "cold_start": bool(cold_start),
        # notch_eligible: the DARK SEAM. Forward-graded legs only may ever set this True (E1.4). In v1
        # every leg is un-graded, so this is always False — the alarm cannot notch severity.
        "notch_eligible": False,
        "legs": legs,                 # the per-leg tri-state evidence (True / False / None-absent)
        "magnitude": magnitude or {},
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# SECTOR-TOP — per sector, distribution + cycle + RS-deceleration (advisory forever-until-gated)
# ---------------------------------------------------------------------------

def sector_top(
    sectors: Sequence[str] | None = None,
    *,
    regime: dict | None = None,
    sector_cycles: dict | None = None,
    prices_fn: Callable[[str], Any] | None = None,
    rs_diff: dict | None = None,
) -> list[dict[str, Any]]:
    """Per-sector SECTOR-TOP alarms: a sector distributing at the top of its cycle.

    Legs (each degrade-safe → absent when the input is missing; absent NEVER fires):
      * cycle_topping   — sector_cycles ``now``: Topping/Peak phase, SELL signal, negative osc_slope,
                          or an extended cycle position. The hard confirmer that unlocks early CRITICAL.
      * distribution    — REUSES distribution_tells' 3D/weekly MACD bearish STATE + crowding percentile
                          for the sector ETF (the primitives — we NEVER fork that MACD/crowding math).
      * rs_decel        — rs_63d < rs_126d (RS decelerating) where the cycles/sector_rs product carries
                          those fields; degrades to absent when it does not (the common case today).

    Carries the def-vs-offense ``rs_diff`` magnitude (from distribution_tells' shared helper — ONE
    definition) so the PM brief can say "how they are rotating and by how much". Advisory forever-until-
    gated; barred from severity. Never raises."""
    reg = _regime(regime)
    sc = _sector_cycles(sector_cycles)
    now_by_tk = _cycles_now_by_ticker(sc)
    rs_by_tk = _sector_rs_by_ticker(reg)
    board_pctile = _board_pctile(reg)

    universe = tuple(_u(s) for s in (sectors or _SECTOR_UNIVERSE) if _u(s))

    # the shared def-vs-offense differential (computed ONCE; the magnitude payload on every alarm)
    if rs_diff is None:
        try:
            from portfolio import distribution_tells as DT
            rs_diff = DT.defensive_offensive_rs_diff(series_fn=prices_fn)
        except Exception:  # noqa: BLE001
            rs_diff = {"diff": None, "crossed": None}

    osc_max = _cfg_f("stop_osc_slope_max", _STOP_OSC_SLOPE_MAX)
    pos_min = _cfg_f("stop_pos_min", _STOP_POS_MIN)
    crowd_min = _cfg_f("stop_crowd_pctile", _STOP_CROWD_PCTILE)
    decel_min = _cfg_f("stop_rs_decel_min", _STOP_RS_DECEL_MIN)
    elevated_at = int(_cfg_f("stop_elevated_legs", _STOP_ELEVATED_LEGS))
    critical_at = int(_cfg_f("stop_critical_legs", _STOP_CRITICAL_LEGS))

    out: list[dict[str, Any]] = []
    for tk in universe:
        now = now_by_tk.get(tk) or {}
        rs_row = rs_by_tk.get(tk) or {}

        cyc = _sector_cycle_legs(now, osc_max=osc_max, pos_min=pos_min)
        dist = _sector_distribution_leg(tk, prices_fn, board_pctile, crowd_min)
        rs_decel = _sector_rs_decel_leg(now, rs_row, decel_min)

        legs = {
            # the cycle read decomposed into two independent sub-signals so a name that is BOTH in a
            # Topping/SELL phase AND rolling-over/extended registers the multi-signal top it actually is
            "cycle_phase": cyc["phase"],          # Topping/Peak phase OR SELL signal
            "cycle_extended": cyc["extended"],    # rolling-over oscillator OR extended cycle position
            "distribution": dist["fired"],        # 3D-MACD bear + crowded (distribution_tells primitive)
            "rs_decel": rs_decel,                 # rs_63d < rs_126d (RS decelerating)
        }
        fired = [k for k, v in legs.items() if v is True]
        if not fired:
            continue  # a sector with zero determinable topping legs is not an alarm (stays calm)

        # a hard Topping/Peak+SELL phase lets the alarm reach CRITICAL one leg early
        hard = bool(cyc["phase"] is True and _lc(now.get("signal")) == "sell")
        level = _level_from_legs(len(fired), elevated_at=elevated_at, critical_at=critical_at,
                                 critical_bonus=hard)

        # per-sector magnitude: the cycle position / osc_slope + the shared def/off rs_diff
        magnitude = {
            "cycle_pos": _f(now.get("pos")),
            "osc_slope": _f(now.get("osc_slope")),
            "crowd_pctile": dist["crowd_pctile"],
            "rs_diff": rs_diff.get("diff") if isinstance(rs_diff, dict) else None,
            "rs_diff_crossed": rs_diff.get("crossed") if isinstance(rs_diff, dict) else None,
        }
        phase = now.get("phaseLabel") or now.get("phase")
        reason = (f"{tk} {level}: {'+'.join(fired)}"
                  + (f" ({phase}, pos {magnitude['cycle_pos']:.0f})"
                     if magnitude["cycle_pos"] is not None else ""))
        out.append(_alarm(
            "sector_top", tk, level,
            status="advisory", cold_start=False,   # sector_cycles carries a (weak) walk-forward record
            legs=legs, magnitude=magnitude, reason=reason,
        ))
    # highest-alarm sector first (stable), so the PM brief leads with the hottest top
    out.sort(key=lambda a: _LEVEL_ORDER.get(a["level"], 0), reverse=True)
    return out


def _sector_cycle_legs(now: dict, *, osc_max: float, pos_min: float) -> dict[str, bool | None]:
    """Decompose a sector_cycles ``now`` block into two independent topping sub-signals:

      * phase    — the sector is in a Peak/Topping phase OR carries a SELL signal (a discrete
                   'the cycle has rolled to a top' read).
      * extended — the oscillator is rolling over (osc_slope <= osc_max) OR the cycle position is
                   extended into the top (pos >= pos_min) — a 'stretched at the highs' read.

    Each is True/False when the block is present, None when it is absent (leg absent, never fires).
    Splitting them lets a name that is BOTH topping AND extended (the incident's SMH/XLK: Topping+SELL
    + osc rolling + pos>=80) register the multi-signal top it is, rather than one collapsed boolean."""
    if not isinstance(now, dict) or not now:
        return {"phase": None, "extended": None}
    phase = _lc(now.get("phase"))
    phase_label = _lc(now.get("phaseLabel"))
    signal = _lc(now.get("signal"))
    osc = _f(now.get("osc_slope"))
    pos = _f(now.get("pos"))

    phase_hit = bool(phase in ("peak", "topping") or "top" in phase_label or signal == "sell")
    # 'extended' is only determinable when at least one of osc/pos is present
    extended: bool | None
    if osc is None and pos is None:
        extended = None
    else:
        extended = bool((osc is not None and osc <= osc_max) or (pos is not None and pos >= pos_min))
    return {"phase": phase_hit, "extended": extended}


def _sector_distribution_leg(
    ticker: str, prices_fn: Callable[[str], Any] | None, board_pctile: dict[str, float], crowd_min: float,
) -> dict[str, Any]:
    """The distribution leg for a sector ETF, REUSING distribution_tells' primitives (the 3D/weekly
    MACD bearish STATE + the crowding percentile). We import those functions — we NEVER re-implement
    the MACD/crowding math. Returns {fired: True/False/None, crowd_pctile}. A distribution fires when
    the sector ETF is crowded AND its 3D-MACD is in a bearish state (the SMH tell). Degrade-safe."""
    try:
        from portfolio import distribution_tells as DT
    except Exception:  # noqa: BLE001
        return {"fired": None, "crowd_pctile": None}
    try:
        series = (prices_fn or DT._default_series_fn)(ticker)
    except Exception:  # noqa: BLE001
        series = None
    crowd_pct = DT._crowding_tell(ticker, series, board_pctile)
    macd3d = DT._macd_bear_state(series, "3d")
    # nothing determinable → leg absent (None), never a fabricated False-that-reads-as-benign
    if crowd_pct is None and macd3d is None:
        return {"fired": None, "crowd_pctile": None}
    crowded = crowd_pct is not None and crowd_pct >= crowd_min
    fired = bool(crowded and macd3d is True)
    return {"fired": fired, "crowd_pctile": (round(crowd_pct, 4) if crowd_pct is not None else None)}


def _sector_rs_decel_leg(now: dict, rs_row: dict, decel_min: float) -> bool | None:
    """RS deceleration: rs_63d - rs_126d < decel_min (medium-term RS decelerating vs long-term). Reads
    the fields from the cycles ``now`` block first, then the embedded sector_rs row. None when NEITHER
    product carries rs_63d/rs_126d (the common case today) — degrades to absent, never fabricated."""
    for src in (now, rs_row):
        if not isinstance(src, dict):
            continue
        rs63 = _f(src.get("rs_63d"))
        rs126 = _f(src.get("rs_126d"))
        if rs63 is not None and rs126 is not None:
            return bool((rs63 - rs126) < decel_min)
    return None


# ---------------------------------------------------------------------------
# BUBBLE-FORMATION — per sector/cluster crowding bubble (cold_start, ADVISORY FOREVER)
# ---------------------------------------------------------------------------

def bubble_formation(
    sectors: Sequence[str] | None = None,
    *,
    regime: dict | None = None,
    prices_fn: Callable[[str], Any] | None = None,
) -> list[dict[str, Any]]:
    """Per-sector/cluster BUBBLE-FORMATION alarms: a crowding bubble forming.

    Legs:
      * crowding    — the sector ETF's crowding percentile (board pctile_252d preferred; own-history
                      fallback) at/above the bubble floor.
      * parabolic   — a parabolic-extension read: a very-high crowding percentile (>= parabolic floor)
                      OR the froth_fragility ``face_a`` parabolic leg firing market-wide.
      * froth_alert — the embedded froth_fragility read: alert=True with a distributing/narrowing-top
                      quadrant (the euphoric "leaders distributing under a held index" quadrant).

    ``cold_start=True`` and ``status='advisory'`` FOREVER in v1: there is no forward-graded crowding
    artifact yet (needs the H4 froth/radar manifest handoff), so this alarm is honestly labeled as
    ungradeable — it annotates, it never sizes. Never raises."""
    reg = _regime(regime)
    board_pctile = _board_pctile(reg)
    froth = reg.get("froth_fragility") if isinstance(reg.get("froth_fragility"), dict) else {}

    universe = tuple(_u(s) for s in (sectors or _SECTOR_UNIVERSE) if _u(s))

    crowd_min = _cfg_f("bub_crowd_pctile", _BUB_CROWD_PCTILE)
    parab_min = _cfg_f("bub_parabolic_pctile", _BUB_PARABOLIC_PCTILE)

    # market-wide froth read (same for every sector; a corroborating leg, not a per-name one)
    froth_alert = _froth_alert_leg(froth)
    froth_parabolic = _froth_parabolic_leg(froth)

    try:
        from portfolio import distribution_tells as DT
    except Exception:  # noqa: BLE001
        DT = None  # type: ignore

    out: list[dict[str, Any]] = []
    for tk in universe:
        crowd_pct = None
        if DT is not None:
            try:
                series = (prices_fn or DT._default_series_fn)(tk)
            except Exception:  # noqa: BLE001
                series = None
            crowd_pct = DT._crowding_tell(tk, series, board_pctile)

        crowded = (crowd_pct is not None and crowd_pct >= crowd_min)
        parabolic = (
            (crowd_pct is not None and crowd_pct >= parab_min)
            or (froth_parabolic is True)
        )
        legs = {
            "crowding": (crowded if crowd_pct is not None else None),
            "parabolic": (parabolic if (crowd_pct is not None or froth_parabolic is not None) else None),
            "froth_alert": froth_alert,   # True / False / None (market-wide corroborator)
        }
        fired = [k for k, v in legs.items() if v is True]
        # a bubble needs its OWN crowding leg to fire — the market froth alert alone is only a
        # corroborator (it must not manufacture a per-sector bubble on a name that isn't crowded)
        if not crowded:
            continue

        # level scales with how many legs fire; crowding+parabolic+froth = the full euphoric read
        level = _level_from_legs(len(fired), elevated_at=2, critical_at=3)
        magnitude = {
            "crowd_pctile": (round(crowd_pct, 4) if crowd_pct is not None else None),
            "froth_headline": _f(froth.get("headline")),
            "froth_quadrant": froth.get("quadrant"),
        }
        reason = (f"{tk} {level}: {'+'.join(fired)}"
                  + (f" (crowd {crowd_pct*100:.0f}%ile)" if crowd_pct is not None else ""))
        out.append(_alarm(
            "bubble_formation", tk, level,
            status="advisory", cold_start=True,   # ADVISORY FOREVER — no forward-graded crowding artifact
            legs=legs, magnitude=magnitude, reason=reason,
        ))
    out.sort(key=lambda a: _LEVEL_ORDER.get(a["level"], 0), reverse=True)
    return out


def _froth_alert_leg(froth: dict) -> bool | None:
    """The froth_fragility distribution/narrowing-top read. True when alert=True AND the quadrant is a
    distributing/euphoric one; False when the block is present but benign; None when absent."""
    if not isinstance(froth, dict) or not froth:
        return None
    alert = froth.get("alert")
    quad = _lc(froth.get("quadrant"))
    if alert is None:
        return None
    hot_quadrant = ("narrowing" in quad or "euphoric" in quad or "distribut" in quad)
    return bool(alert is True and hot_quadrant)


def _froth_parabolic_leg(froth: dict) -> bool | None:
    """The parabolic-extension corroborator from froth_fragility ``face_a`` (its A4_parab leg). True
    when the parabolic face scores hot; None when the block/legs are absent."""
    if not isinstance(froth, dict) or not froth:
        return None
    face_a = froth.get("face_a")
    if not isinstance(face_a, dict):
        return None
    score = _f(face_a.get("score"))
    if score is None:
        return None
    # face_a is the parabolic/extension face; a score at/above its midpoint corroborates parabolic risk
    return bool(score >= 50.0)


# ---------------------------------------------------------------------------
# CRASH-RISK — market vol structure + dealer gamma (READ-ONLY) + credit/liquidity + auction stress
# ---------------------------------------------------------------------------

def crash_risk(
    *,
    regime: dict | None = None,
    liquidity_label: str | None = None,
    liquidity_fn: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """The market-wide CRASH-RISK alarm.

    Legs (each degrade-safe → absent when the input is missing):
      * drawdown_scare — the risk_radar forward drawdown-probability read: drawdown_prob.h21 rising
                        above its base rate under a growth/credit dominant scare. This is the plane the
                        incident post-mortem names as the smoking gun (h21 0.16→0.19, growth scare) and
                        the leg the 06-26 replay bar turns on. Forward-log-calibrated on the dashboard,
                        but ADVISORY here (the notch stays dark until the E1.4 gate).
      * vol_structure — vol_shock band/score (forward vol-shock probability).
      * dealer_gamma  — a READ-ONLY CLASSIFICATION of market_gamma: spot below the gamma flip with a
                        short-gamma regime = dealers amplify moves. THIS MODULE NEVER NOTCHES OFF GEX —
                        bot/derisk.py OWNS gex for severity. We only read the regime to CLASSIFY the
                        crash-risk level; the alarm's authority to touch severity is gated separately
                        (E1.4) and stays DARK (notch_eligible=False) regardless.
      * credit_liquidity — the liquidity_quality label (stress/hollow/contracting = a stress leg).
      * auction_stress   — Treasury-auction tail stress when vendored; absent otherwise.

    ``cold_start=True``, ``status='advisory'``: the crash-risk severity notch arms ONLY after its
    E1.4 AUC>0.55 gate — until then ``notch_eligible=False`` (the seam is shipped dark). Never raises."""
    reg = _regime(regime)
    vs = reg.get("vol_shock") if isinstance(reg.get("vol_shock"), dict) else {}
    mg = reg.get("market_gamma") if isinstance(reg.get("market_gamma"), dict) else {}
    rr = reg.get("risk_radar") if isinstance(reg.get("risk_radar"), dict) else {}

    radar_leg = _crash_radar_leg(rr)             # (fired, level_hint)  drawdown-scare read
    vol_leg = _crash_vol_leg(vs)                 # (fired, level_hint)
    gamma_leg = _crash_gamma_leg(mg)             # True / False / None  (classification-only)
    credit_leg = _crash_credit_leg(reg, liquidity_label, liquidity_fn)
    auction_leg = _crash_auction_leg(reg)

    legs = {
        "drawdown_scare": radar_leg["fired"],    # True / False / None (risk_radar drawdown_prob)
        "vol_structure": vol_leg["fired"],       # True / False / None
        "dealer_gamma": gamma_leg,               # True / False / None (READ-ONLY classification)
        "credit_liquidity": credit_leg["fired"], # True / False / None
        "auction_stress": auction_leg,           # True / False / None (absent unless vendored)
    }
    fired = [k for k, v in legs.items() if v is True]

    # a hot vol band OR a rising drawdown-scare is a hard confirmer that lets the alarm reach CRITICAL
    # one leg early (mirrors sector_top's hard-phase leg)
    hard = (vol_leg["level_hint"] == _CRITICAL) or (radar_leg["level_hint"] == _CRITICAL)
    level = _level_from_legs(len(fired), elevated_at=2, critical_at=3, critical_bonus=hard)
    if not fired:
        level = _CALM

    magnitude = {
        "drawdown_prob_h21": radar_leg["h21"],
        "dominant_scare": rr.get("dominant_scare"),
        "vol_shock_score": _f(vs.get("score")),
        "vol_shock_band": vs.get("band"),
        "gamma_regime": mg.get("regime"),
        "spot_vs_flip_pct": _f(mg.get("spot_vs_flip_pct")),
        "liquidity_label": credit_leg["label"],
    }
    parts = []
    if radar_leg["fired"] is True and radar_leg["h21"] is not None:
        parts.append(f"drawdown-prob {radar_leg['h21']:.2f}"
                     + (f" ({rr.get('dominant_scare')} scare)" if rr.get("dominant_scare") else ""))
    if vol_leg["fired"] is True and magnitude["vol_shock_score"] is not None:
        parts.append(f"vol {magnitude['vol_shock_score']:.0f}")
    if gamma_leg is True:
        parts.append(f"dealers short-gamma ({magnitude['spot_vs_flip_pct']:.1f}% vs flip)"
                     if magnitude["spot_vs_flip_pct"] is not None else "dealers short-gamma")
    if credit_leg["fired"] is True and credit_leg["label"]:
        parts.append(f"liquidity {credit_leg['label']}")
    reason = (f"market {level}: " + ", ".join(parts)) if parts else f"market {level}"

    return _alarm(
        "crash_risk", "market", level,
        status="advisory", cold_start=True,   # notch arms only after the E1.4 AUC gate; seam stays dark
        legs=legs, magnitude=magnitude, reason=reason,
    )


def _crash_radar_leg(rr: dict) -> dict[str, Any]:
    """The risk_radar drawdown-scare leg — the incident's smoking-gun plane. Returns {fired, h21,
    level_hint}. Fires when the 21-day forward drawdown probability is ELEVATED above its base rate
    (lift > 1) under a growth/credit/liquidity dominant scare, OR the radar itself alerts. A very-high
    lift (or an outright radar alert) sets a CRITICAL hint. Degrades to {None, ...} when the radar
    block is absent. READ-ONLY: forward-log-calibrated on the dashboard, but ADVISORY here."""
    if not isinstance(rr, dict) or not rr:
        return {"fired": None, "h21": None, "level_hint": _CALM}
    ddp = rr.get("drawdown_prob") if isinstance(rr.get("drawdown_prob"), dict) else {}
    h21 = _f(ddp.get("h21"))
    base_h21 = _f(ddp.get("base_h21"))
    lift = _f(ddp.get("lift_h21"))
    scare = _lc(rr.get("dominant_scare"))
    alert = rr.get("alert")
    # derive lift when only h21+base are present
    if lift is None and h21 is not None and base_h21 not in (None, 0):
        lift = h21 / base_h21
    hot_scare = scare in ("growth", "credit", "liquidity", "rates", "geopolitical")
    if h21 is None and alert is None:
        return {"fired": None, "h21": None, "level_hint": _CALM}
    # a rising forward drawdown prob (lift>1) under a real scare, OR an outright radar alert, fires
    rising = (lift is not None and lift >= 1.0) or (h21 is not None and base_h21 is not None and h21 > base_h21)
    fired = bool((rising and hot_scare) or alert is True)
    # CRITICAL hint on a strong lift or an alert; else elevated hint when it fires
    if alert is True or (lift is not None and lift >= 1.3):
        hint = _CRITICAL
    elif fired:
        hint = _ELEVATED
    else:
        hint = _CALM
    return {"fired": fired, "h21": h21, "level_hint": hint}


def _crash_vol_leg(vs: dict) -> dict[str, Any]:
    """The vol-structure leg from vol_shock. Returns {fired, level_hint}. A vol_shock score/band at
    'elevated' fires the leg; 'critical'/'high' bands (or a very-high score) set a CRITICAL hint that
    lets crash_risk reach CRITICAL one leg early. Degrades to {None, calm} when absent."""
    if not isinstance(vs, dict) or not vs:
        return {"fired": None, "level_hint": _CALM}
    score = _f(vs.get("score"))
    band = _lc(vs.get("band"))
    elevated = _cfg_f("crash_vol_elevated", _CRASH_VOL_ELEVATED)
    critical = _cfg_f("crash_vol_critical", _CRASH_VOL_CRITICAL)
    hint = _CALM
    fired: bool | None = None
    if band or score is not None:
        is_crit = band in ("critical", "high", "severe") or (score is not None and score >= critical)
        is_elev = band in ("elevated", "warning") or (score is not None and score >= elevated)
        fired = bool(is_crit or is_elev)
        hint = _CRITICAL if is_crit else (_ELEVATED if is_elev else _CALM)
    return {"fired": fired, "level_hint": hint}


def _crash_gamma_leg(mg: dict) -> bool | None:
    """READ-ONLY dealer-gamma CLASSIFICATION. True when the market is in a short-gamma regime with spot
    below the gamma flip (dealers amplify moves); False when present but benign (long gamma / spot above
    flip); None when absent. NOTE: this is CLASSIFICATION-ONLY — bot/derisk.py owns gex for severity;
    this leg never itself notches (crash_risk.notch_eligible stays False in v1)."""
    if not isinstance(mg, dict) or not mg:
        return None
    regime = _lc(mg.get("regime"))
    spot_vs_flip = _f(mg.get("spot_vs_flip_pct"))
    flip_max = _cfg_f("crash_gamma_flip_pct", _CRASH_GAMMA_FLIP_PCT)
    if not regime and spot_vs_flip is None:
        return None
    short_gamma = regime in ("short", "negative", "short_gamma")
    below_flip = spot_vs_flip is not None and spot_vs_flip <= flip_max
    return bool(short_gamma and below_flip)


def _crash_credit_leg(
    reg: dict, liquidity_label: str | None, liquidity_fn: Callable[[str], Any] | None,
) -> dict[str, Any]:
    """The credit / liquidity-quality leg. Prefers an explicit ``liquidity_label`` (test injection);
    else calls brain.liquidity_quality.classify with an injected series fn when one is supplied; else
    absent. Returns {fired, label}. A stress/hollow/contracting label fires; benign/unknown do NOT
    (unknown never counts — the invariant)."""
    label = _lc(liquidity_label) if liquidity_label is not None else None
    if label is None and liquidity_fn is not None:
        try:
            from brain import liquidity_quality as LQ
            res = LQ.classify(liquidity_fn)
            label = _lc(res.get("label")) if isinstance(res, dict) else None
        except Exception:  # noqa: BLE001
            label = None
    if label is None:
        # last resort: an embedded liquidity_overlay label if the regime carries one
        overlay = reg.get("liquidity_overlay")
        if isinstance(overlay, dict):
            label = _lc(overlay.get("label"))
    if not label or label == "unknown":
        return {"fired": None, "label": (label or None)}
    stress_labels = _cfg_list("crash_liquidity_stress_labels", _LIQ_STRESS_LABELS)
    fired = label in stress_labels
    return {"fired": fired, "label": label}


def _crash_auction_leg(reg: dict) -> bool | None:
    """Treasury-auction tail-stress leg — absent unless the regime vendors an auction-stress read. In
    v1 this is None (not vendored into the bot's regime file); the seam is here for the H4 handoff."""
    aux = reg.get("auction_stress") or reg.get("treasury_auctions")
    if not isinstance(aux, dict) or not aux:
        return None
    stressed = aux.get("stressed")
    if stressed is None:
        band = _lc(aux.get("band"))
        if not band:
            return None
        return bool(band in ("elevated", "critical", "high", "stress"))
    return bool(stressed)


# ---------------------------------------------------------------------------
# board-published crowding (preferred so bot & board agree; reuses the DT pattern)
# ---------------------------------------------------------------------------

def _board_pctile(regime: dict | None = None) -> dict[str, float]:
    """Map ticker -> published crowding percentile (0-1) from the embedded ``sector_rs`` table.

    When an injected ``regime`` dict is supplied (the test / caller path), the percentiles are read
    from THAT regime's ``sector_rs`` — NEVER the live vendored file. This is the fixture-injection
    contract: a test's calm/incident regime must not leak the live board's crowding reads. Only when
    ``regime is None`` (a bare production call with no injected frame) does it fall back to the DT
    helper's live read. {} on any miss → the crowding leg falls back to own-history percentile."""
    reg = regime if isinstance(regime, dict) else None
    if reg is None:
        # no injected frame → the production path: prefer the DT helper's live read
        try:
            from portfolio import distribution_tells as DT
            board = DT._board_pctile_252d()
            if board:
                return board
        except Exception:  # noqa: BLE001
            pass
        reg = _regime(None)
    # read pctile_252d off the (injected or live) sector_rs list
    out: dict[str, float] = {}
    try:
        for row in (reg.get("sector_rs") or []):
            if not isinstance(row, dict):
                continue
            tk = _u(row.get("ticker"))
            pc = _f(row.get("pctile_252d"))
            if tk and pc is not None:
                out[tk] = pc / 100.0 if pc > 1.0 else pc
    except Exception:  # noqa: BLE001
        return {}
    return out


# ---------------------------------------------------------------------------
# the BATTERY — assemble all three alarms into one artifact
# ---------------------------------------------------------------------------

def battery(
    *,
    regime: dict | None = None,
    sector_cycles: dict | None = None,
    prices_fn: Callable[[str], Any] | None = None,
    liquidity_label: str | None = None,
    liquidity_fn: Callable[[str], Any] | None = None,
    asof: str | None = None,
) -> dict[str, Any]:
    """Assemble the full anticipation battery — all three alarms — into ONE artifact dict.

    The battery is a PERCEPTION organ: it publishes what it sees and stamps every alarm's authority
    (status / cold_start / notch_eligible). It changes NO behavior in this wave. ``top_level`` is the
    highest alarm level across the whole battery (for a quick PM-brief read). Never raises."""
    reg = _regime(regime)
    # TRUE data date = the regime block's own asof, NOT build time (charter §6.5). Degrade to the
    # explicit asof arg, then today, on any miss.
    data_asof = asof or _regime_asof(reg) or date.today().isoformat()

    tops = sector_top(regime=reg, sector_cycles=sector_cycles, prices_fn=prices_fn)
    bubbles = bubble_formation(regime=reg, prices_fn=prices_fn)
    crash = crash_risk(regime=reg, liquidity_label=liquidity_label, liquidity_fn=liquidity_fn)

    all_levels = [a["level"] for a in tops] + [a["level"] for a in bubbles] + [crash["level"]]
    top_level = _max_level(*all_levels) if all_levels else _CALM

    return {
        "schema_version": 1,
        "asof": str(data_asof)[:10],
        "built_at": date.today().isoformat(),
        "top_level": top_level,
        "sector_top": tops,
        "bubble_formation": bubbles,
        "crash_risk": crash,
        # honest program-level authority stamp: NOTHING here is notch-eligible in v1 (all seams dark).
        "authority": {
            "notch_eligible_alarms": [],
            "note": ("v1: all alarms advisory; no forward-graded legs exist, so notch_eligible is False "
                     "on every alarm. CRASH-RISK severity notch arms only after the E1.4 AUC>0.55 gate."),
        },
    }


def _regime_asof(reg: dict) -> str | None:
    """The regime file's own data date (``date`` or a block asof). None on any miss."""
    if not isinstance(reg, dict):
        return None
    for key in ("date", "asof", "as_of"):
        v = reg.get(key)
        if v:
            return str(v)[:10]
    # else the freshest block asof we can find
    for block in ("risk_radar", "froth_fragility", "vol_shock", "market_gamma"):
        b = reg.get(block)
        if isinstance(b, dict) and b.get("asof"):
            return str(b["asof"])[:10]
    return None


# ---------------------------------------------------------------------------
# artifact writer — data/anticipation/<asof>.json (+ latest.json), atomic
# ---------------------------------------------------------------------------

def write_battery(
    *,
    regime: dict | None = None,
    sector_cycles: dict | None = None,
    prices_fn: Callable[[str], Any] | None = None,
    liquidity_label: str | None = None,
    liquidity_fn: Callable[[str], Any] | None = None,
    asof: str | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Build the battery and persist it to data/anticipation/<asof>.json AND latest.json (atomic
    tmp+os.replace). SHADOW/perception artifact only — no sizing is touched. Returns the payload even
    when the write no-ops (degrade-never-fabricate). Never raises."""
    payload = battery(
        regime=regime, sector_cycles=sector_cycles, prices_fn=prices_fn,
        liquidity_label=liquidity_label, liquidity_fn=liquidity_fn, asof=asof,
    )
    d = out_dir if out_dir is not None else _ARTIFACTS
    stamp = payload.get("asof") or date.today().isoformat()
    try:
        d.mkdir(parents=True, exist_ok=True)
        for name in (f"{stamp}.json", "latest.json"):
            _atomic_write(d / name, payload)
    except Exception:  # noqa: BLE001
        pass
    return payload


def _atomic_write(path: Path, payload: dict) -> None:
    """Write ``payload`` to ``path`` atomically (tmp file + os.replace). Never leaves a partial file."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        try:
            os.unlink(tmp)
        except Exception:  # noqa: BLE001
            pass
        raise

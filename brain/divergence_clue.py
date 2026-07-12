"""brain/divergence_clue.py — the single-stock EARLY-CLUE detector (roadmap §1 B5).

WHY THIS MODULE EXISTS
----------------------
The AAPL-Jul-1 pattern. On 2026-07-01 the standout system flagged AAPL as a
safe-haven tech name while semis/memory sold off and the risk radar flared; it
netted ~+9%. Nothing in the candidacy funnel *originated* that read as an early
additive clue — the momentum funnel only surfaces a name AFTER its sector confirms.
This module is the lane that surfaces ONE name diverging BULLISHLY from a
hesitant/selling sector, BEFORE the sector confirms, with enough discipline that
it isn't noise.

WHAT IT IS (and is NOT)
-----------------------
This is an ADDITIVE-AT-CANDIDACY organ: it decides WHAT to look at, never how much
to size (roadmap §0 constitution). A surviving clue is a starter-grade candidacy
prior for the gate to filter — never authority. Consumption is gated later by the
flag ``MASTERMIND_DIVERGENCE_CLUE`` (default OFF); the ``scan()`` computation itself
is always safe to run (it only reads + measures, never sizes, never executes).

THE CONJUNCTION (a name is a clue ONLY on all three)
----------------------------------------------------
  TRIGGER    : (name on the buy-board MEMBERSHIP set) OR (radar POSITIVE_DIVERGENCE)

BOARD MEMBERSHIP — LIVE vs POINT-IN-TIME (the AAPL-Jul-1 re-grounding)
---------------------------------------------------------------------
The buy-board trigger has TWO membership sources, unioned by ``_default_board_membership(asof)``:

  (1) the VOLATILE board (``us_standouts.json``, overwritten each build, no history). It RESPECTS its
      own ``gate_go`` — an explicit ``gate_go=False`` means the LIVE board is not validated, so it
      contributes NOTHING to today's membership. This is the live/today read.
  (2) the PERSISTENT track-record ledger (``brain/board_track_record.surfaced_on(asof)``), the
      immutable append-only record of every name ever SURFACED on the board on a given date. A past
      surfacing is a HISTORICAL FACT, so this source is NOT gated by gate_go — for a replay/backtest
      ``asof`` it answers "was this name surfaced that day" from the retained ledger, which the
      volatile board (having been overwritten) can no longer answer.

This re-grounds the module's central AAPL-Jul-1 example on the REAL, retained surface: on a replay of
``asof='2026-07-01'`` AAPL is a valid trigger via ``surfaced_on('2026-07-01')`` even though the live
board today reads ``gate_go=False``. IMPORTANT — this only broadens the historical/point-in-time
TRIGGER membership (what to LOOK at); it never weakens the live ``gate_go`` discipline for SIZING.
Consumption is still gated by ``MASTERMIND_DIVERGENCE_CLUE`` (default OFF); this change is byte-
identical for a default-OFF consumer.
  ≥2 CORROBORATORS of:
      S1 down-day alpha : member out-performs its sector on the sector's DOWN days by
                          ≥ +50 bps/day over ~63 sessions, with a decent recent hit-rate.
                          (DOCTRINE scorecard dim 1b — "holds up on down days".)
      S2 RS-velocity gap: name RS-velocity minus sector RS-velocity ≥ +3 bps/day.
      S3 flow rotation  : name rvol_z > +1 while the sector is DISTRIBUTING.
  ≥1 SECTOR-STRESS of:
      - cycles()[sector] phase in {Peak, Downturn/Topping} (late_cycle), OR
      - rotation_tensor sector accel < 0 / distribution, OR
      - macro_risk state != risk_on, OR
      - the sector's own radar is flaring.
  GUARDS (reject) : parabolic (name > +12% vs 50dma OR RSI14 > 78); 10-session
                    cooldown per name; cap ≤5 clues per build.

THE INVARIANT (governs every path — mirrors brain/neural_web_context.py)
------------------------------------------------------------------------
Fail-soft EVERYWHERE. Absent / malformed / stale data → an EMPTY result, never a
raise, never a fabricated clue. Every reader is optional and injectable; a single
failing source degrades that leg to "absent" (which can only ever REMOVE a clue,
never manufacture one). Pure + deterministic given the injected readers.

SHARED MATH LEGS
----------------
``down_day_alpha()`` and ``single_rs_velocity()`` are defined here as PURE functions
for now. They may later be hoisted to ``portfolio/distribution_tells.py`` (down_day
alpha, DOCTRINE dim 1b) and ``brain/rotation_tensor.py`` (single_rs_velocity, the
single-name analogue of the tensor's sector RS-velocity) once a second consumer
appears — kept local until then to avoid a one-caller abstraction across modules.

PUBLIC API
----------
* scan(asof=None, *, ...readers...)  -> list[dict]   — the detector (never raises)
* down_day_alpha(name_rets, sector_rets)             -> float bps/day | None (pure)
* single_rs_velocity(rets)                           -> float bps/day | None (pure)
* write_latest(rows, asof)  -> Path|None             — data/brain/divergence_clue_latest.json
* append_ledger(rows)       -> int                   — data/brain/divergence_clue.jsonl (idempotent)
* by_sector(rows)           -> dict                   — clue density per sector
* clue_flag_enabled()       -> bool                   — reads MASTERMIND_DIVERGENCE_CLUE (default OFF)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_V = _ROOT / "vendor" / "macro"
_OUT_DIR = _ROOT / "data" / "brain"
_LATEST_PATH = _OUT_DIR / "divergence_clue_latest.json"
_LEDGER_PATH = _OUT_DIR / "divergence_clue.jsonl"

# --------------------------------------------------------------------------- #
# thresholds — ALL UNVERIFIED PRIORS (roadmap §4: run a 2025-26 replay before arming).
# Conservative starter weights; only ever ADD a candidate for the gate to filter.
# --------------------------------------------------------------------------- #
_S1_DOWN_DAY_ALPHA_MIN_BPS = 50.0     # ≥ +50 bps/day member alpha on sector down-days
_S1_LOOKBACK = 63                     # ~63 sessions (~1 quarter)
_S1_MIN_DOWN_DAYS = 8                 # need this many sector down-days for a meaningful read
_S1_HIT_RATE_MIN = 0.55              # "decent recent hit-rate" — > half the down-days beat sector

_S2_RS_VELOCITY_GAP_MIN_BPS = 3.0     # ≥ +3 bps/day name-minus-sector RS-velocity
_S2_RSM_WINDOW = 20                   # 20d relative-momentum window (mirrors rotation_tensor)
_S2_VEL_WINDOW = 5                    # 5d acceleration window (mirrors rotation_tensor)

_S3_RVOL_Z_MIN = 1.0                  # name rvol_z above this AND sector distributing

_MIN_CORROBORATORS = 2                # ≥2 of {S1, S2, S3}
_MIN_SECTOR_STRESS = 1                # ≥1 sector-stress condition

# guards
_PARABOLIC_VS_50DMA = 0.12            # > +12% above the 50-day MA → parabolic reject
_PARABOLIC_RSI14 = 78.0               # RSI14 above this → parabolic reject
_COOLDOWN_SESSIONS = 10               # 10-session cooldown per name
_MAX_CLUES = 5                        # ≤ 5 clues per build

# score band: 0.45 (2 corroborators) → 0.65 (all 3). Starter-grade prior, never authority.
_SCORE_BASE = 0.45
_SCORE_PER_EXTRA_CORROB = 0.10        # 2→0.45, 3→0.55 ... capped at 0.65 below
_SCORE_MAX = 0.65

# cycle phases that count as sector stress (Peak/Downturn/Topping late-cycle).
_STRESS_PHASES = frozenset({"Peak", "Downturn", "Topping"})

# radar states that read as a sector "flare" (a negative/stress divergence on the sector ETF).
_RADAR_FLARE_STATES = frozenset({"NEGATIVE_DIVERGENCE", "CONFIRMED_DOWN"})

# radar state that is the POSITIVE_DIVERGENCE trigger.
_RADAR_POS_STATE = "POSITIVE_DIVERGENCE"


# --------------------------------------------------------------------------- #
# flag — consumption is gated; the scan computation itself is always safe.
# --------------------------------------------------------------------------- #
def clue_flag_enabled() -> bool:
    """True iff MASTERMIND_DIVERGENCE_CLUE is set (default OFF).

    NOTE: this flag gates DOWNSTREAM CONSUMPTION of the clues (candidacy injection).
    The ``scan()`` computation is safe to run regardless — it only reads + measures.
    """
    try:
        return os.environ.get("MASTERMIND_DIVERGENCE_CLUE", "0").strip().lower() in (
            "1", "true", "yes", "on")
    except Exception:  # noqa: BLE001 — fail-soft
        return False


# --------------------------------------------------------------------------- #
# small helpers (mirror intake._u / _f)
# --------------------------------------------------------------------------- #
def _u(t: Any) -> str:
    return (str(t) if t is not None else "").upper().strip()


def _f(x: Any) -> float | None:
    if x is None or isinstance(x, (dict, list, bool)):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _read_json(rel: str) -> Any:
    """Read a JSON artifact from the vendored macro site/data tree. None on any miss.
    Mirrors intake._read (checks both site/ and data/)."""
    for base in ("site", "data"):
        p = _V / base / rel
        try:
            if p.exists():
                return json.loads(p.read_text())
        except Exception as e:  # noqa: BLE001
            log.debug("divergence_clue: read %s failed (%s)", p, e)
    return None


# =========================================================================== #
# PURE MATH LEGS (may later hoist to distribution_tells / rotation_tensor)
# =========================================================================== #

def down_day_alpha(
    name_rets: Sequence[float] | Any,
    sector_rets: Sequence[float] | Any,
    *,
    lookback: int = _S1_LOOKBACK,
    min_down_days: int = _S1_MIN_DOWN_DAYS,
) -> dict[str, Any] | None:
    """DOCTRINE dim 1b — the member's alpha on the SECTOR's DOWN days.

    Given two aligned daily-return series (name vs its sector ETF), restrict to the
    sessions where the SECTOR return was negative, and measure the member's average
    EXCESS return over the sector on exactly those sessions, in bps/day. This is the
    "holds up on down days" tell — a safe-haven diverger out-performs precisely when
    its sector is being sold.

    Parameters
    ----------
    name_rets, sector_rets : aligned per-session simple returns (lists / np arrays /
        pandas Series). They are truncated to their common length (last ``lookback``).
    lookback : trailing sessions to consider (~63 ≈ 1 quarter).
    min_down_days : minimum sector down-days required for a meaningful read.

    Returns
    -------
    {"alpha_bps_day": float, "n_down": int, "hit_rate": float} on a determinable read,
    or ``None`` on insufficient history / bad input (the leg is ABSENT, never a
    fabricated value). PURE — no I/O, never raises.
    """
    try:
        nr = _to_float_list(name_rets)
        sr = _to_float_list(sector_rets)
    except Exception:  # noqa: BLE001
        return None
    if nr is None or sr is None:
        return None
    n = min(len(nr), len(sr))
    if n < min_down_days:
        return None
    # align on the trailing common window
    nr = nr[-n:]
    sr = sr[-n:]
    if lookback and n > lookback:
        nr = nr[-lookback:]
        sr = sr[-lookback:]

    excess_on_down: list[float] = []
    for rn, rs in zip(nr, sr):
        if rs is None or rn is None:
            continue
        if rs < 0.0:                       # a SECTOR down-day
            excess_on_down.append(rn - rs)  # member excess over sector that day
    n_down = len(excess_on_down)
    if n_down < min_down_days:
        return None
    mean_excess = sum(excess_on_down) / n_down
    hit_rate = sum(1 for e in excess_on_down if e > 0.0) / n_down
    return {
        "alpha_bps_day": round(mean_excess * 1e4, 3),   # fraction → bps/day
        "n_down": n_down,
        "hit_rate": round(hit_rate, 4),
    }


def single_rs_velocity(
    rets: Sequence[float] | Any,
    *,
    rsm_window: int = _S2_RSM_WINDOW,
    vel_window: int = _S2_VEL_WINDOW,
) -> float | None:
    """The single-name RS-velocity (acceleration of relative momentum), in bps/day.

    Mirrors the sector-level construction in rotation_tensor._compute_rs_velocity, but
    for a single series of RELATIVE returns (the name minus its sector ETF, per session):

        rsm(t)  = Σ of the last ``rsm_window`` relative returns  (cumulative rel-momentum)
        vel(t)  = (rsm(t) - rsm(t - vel_window)) / vel_window     (rate the RS is changing)

    Returned in bps/day (× 1e4). A POSITIVE value means the name is *accelerating* its
    out-performance of its sector — the honest early tell of a divergence turning up.

    ``rets`` is the per-session RELATIVE return series (name_ret - sector_ret). Returns
    ``None`` on insufficient history / bad input. PURE — no I/O, never raises.
    """
    try:
        rr = _to_float_list(rets)
    except Exception:  # noqa: BLE001
        return None
    if rr is None:
        return None
    need = rsm_window + vel_window + 1
    if len(rr) < need:
        return None
    # cumulative rolling sum over rsm_window → the rsm level at t and at t-vel_window
    rsm_now = sum(rr[-rsm_window:])
    rsm_prev = sum(rr[-(rsm_window + vel_window):-vel_window])
    vel = (rsm_now - rsm_prev) / vel_window
    return round(vel * 1e4, 4)


def _to_float_list(seq: Any) -> list[float] | None:
    """Coerce a list / np array / pandas Series to a clean list of floats (dropna).
    Returns None on total failure. Never raises."""
    if seq is None:
        return None
    # pandas Series / numpy array → list via tolist() if present
    try:
        if hasattr(seq, "dropna") and hasattr(seq, "tolist"):
            seq = seq.dropna().tolist()
        elif hasattr(seq, "tolist"):
            seq = seq.tolist()
    except Exception:  # noqa: BLE001
        pass
    out: list[float] = []
    try:
        for x in seq:
            v = _f(x)
            if v is not None:
                out.append(v)
    except TypeError:
        return None
    return out


def _returns_from_series(series: Any) -> list[float] | None:
    """Simple per-session returns from a date-indexed close Series (or list of closes).
    Returns None if fewer than 2 points. Never raises."""
    closes = _to_float_list(series)
    if closes is None or len(closes) < 2:
        return None
    rets: list[float] = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev and prev != 0.0:
            rets.append(closes[i] / prev - 1.0)
    return rets or None


def _aligned_returns(name_series: Any, sector_series: Any) -> tuple[list[float], list[float]] | None:
    """Build aligned per-session return series for name + sector.

    When BOTH are pandas Series (date-indexed) they are aligned on their common index
    so the down-day pairing is honest. Otherwise both are coerced to plain return lists
    and trailing-aligned. Returns (name_rets, sector_rets) or None. Never raises.
    """
    # pandas path — align on common dates before differencing
    try:
        import pandas as pd  # noqa: F401
        if hasattr(name_series, "index") and hasattr(sector_series, "index") \
                and hasattr(name_series, "align"):
            a = name_series.astype(float).dropna().sort_index()
            b = sector_series.astype(float).dropna().sort_index()
            a2, b2 = a.align(b, join="inner")
            a2 = a2.dropna()
            b2 = b2.dropna()
            common = a2.index.intersection(b2.index)
            if len(common) < 3:
                return None
            a2 = a2.reindex(common)
            b2 = b2.reindex(common)
            nr = _returns_from_series(a2)
            sr = _returns_from_series(b2)
            if nr is None or sr is None:
                return None
            m = min(len(nr), len(sr))
            return nr[-m:], sr[-m:]
    except Exception:  # noqa: BLE001
        pass
    # plain path — trailing-align two return lists
    nr = _returns_from_series(name_series)
    sr = _returns_from_series(sector_series)
    if nr is None or sr is None:
        return None
    m = min(len(nr), len(sr))
    if m < 3:
        return None
    return nr[-m:], sr[-m:]


def _rsi14(series: Any, period: int = 14) -> float | None:
    """Wilder RSI-14 on a close Series/list. None on insufficient history. Never raises."""
    closes = _to_float_list(series)
    if closes is None or len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    # Wilder smoothing seeded on the first `period`
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 3)


def _pct_vs_50dma(series: Any) -> float | None:
    """Latest close percent above/below its 50-day MA. None on insufficient history."""
    closes = _to_float_list(series)
    if closes is None or len(closes) < 50:
        return None
    ma = sum(closes[-50:]) / 50.0
    if ma == 0.0:
        return None
    return closes[-1] / ma - 1.0


# =========================================================================== #
# default readers (production) — every one is INJECTABLE + fail-soft
# =========================================================================== #

def _default_standouts() -> set[str]:
    """The names on the standout BUY board. Respects the board's own gate_go (P-NEW-2):
    an explicit gate_go=False → the board is not validated → empty set. {} on any miss.

    This is the VOLATILE / LIVE membership source (us_standouts.json is overwritten each build).
    The gate_go discipline is preserved here on purpose — the live board must be validated to
    contribute to TODAY's membership."""
    d = _read_json("factordata/us_standouts.json") or {}
    if d.get("gate_go") is False:
        return set()
    out: set[str] = set()
    for s in (d.get("buy") or d.get("standouts") or []):
        t = _u(s.get("ticker") if isinstance(s, dict) else s)
        if t:
            out.add(t)
    return out


def _default_board_membership(asof: str | None) -> set[str]:
    """The buy-board TRIGGER membership set for ``asof`` — the UNION of two sources:

      (1) the VOLATILE live board (``_default_standouts``, gate_go-respecting) — the today read; and
      (2) the PERSISTENT track-record ledger's point-in-time board-ENTRY set for ``asof``
          (``board_track_record.surfaced_on(asof)``) — the immutable historical surfacing, NOT gated
          by gate_go (a past surfacing is a fact the overwritten live board can no longer answer).

    This is what re-grounds the AAPL-Jul-1 replay: for ``asof='2026-07-01'`` the ledger answers
    "AAPL was surfaced that day" even though the live board reads gate_go=False. Fail-soft: any source
    failure degrades that leg to empty (which can only ever REMOVE a trigger, never fabricate one);
    never raises. Broadens the point-in-time TRIGGER membership only — NOT the live sizing gate."""
    out: set[str] = set()
    # (1) volatile live board (gate_go-respecting).
    try:
        live = _default_standouts()
        if isinstance(live, set):
            out |= {_u(t) for t in live if _u(t)}
    except Exception:  # noqa: BLE001 — a live-board failure just drops that leg
        pass
    # (2) persistent ledger point-in-time surfacing (NOT gated by gate_go).
    try:
        from brain import board_track_record
        surfaced = board_track_record.surfaced_on(str(asof)[:10]) if asof else set()
        if isinstance(surfaced, set):
            out |= {_u(t) for t in surfaced if _u(t)}
    except Exception:  # noqa: BLE001 — a ledger failure just drops that leg
        pass
    return out


def _default_radar() -> dict[str, str]:
    """Map TICKER -> radar state from radar_ticker.json. {} on any miss.
    Tolerates either a list or a dict under 'tickers' (mirrors intake._from_radar)."""
    d = _read_json("basketdata/radar_ticker.json") or {}
    rows = d.get("tickers") or []
    if isinstance(rows, dict):
        rows = list(rows.values())
    out: dict[str, str] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        t = _u(r.get("ticker"))
        st = r.get("state")
        if t and st:
            out[t] = str(st)
    return out


def _default_sector_etf(ticker: str) -> str | None:
    """Resolve a single name → its sector ETF (AAPL → XLK).

    Mirrors conviction._sector_of: read site/stockdata/<T>.json → GICS `sector`
    name → lenses._SECTOR_ETF. None on any miss (the name is then un-sectored →
    it can never be a clue, which is the safe degrade). Never raises."""
    t = _u(ticker)
    if not t:
        return None
    d = _read_json(f"stockdata/{t}.json") or {}
    sec = d.get("sector")
    if not sec:
        return None
    try:
        from portfolio import lenses
        etf = lenses._SECTOR_ETF.get(sec)
        return etf
    except Exception:  # noqa: BLE001
        return None


def _default_series_fn(ticker: str) -> Any:
    """Default single-name close-Series reader — the engine price store.
    Mirrors distribution_tells._default_series_fn. None on any miss / cold store."""
    try:
        from portfolio import paper_account
        return paper_account._fetch_price_series(ticker)
    except Exception:  # noqa: BLE001
        return None


def _default_cycles() -> dict[str, dict]:
    """The per-sector cycle read keyed by ETF (regime_frame.cycles is the SOLE reader,
    stale-gated). {} on any miss."""
    try:
        from brain import regime_frame
        return regime_frame.cycles() or {}
    except Exception:  # noqa: BLE001
        return {}


def _default_tensor(asof: str | None) -> dict[str, Any]:
    """The rotation-tensor measurement (rotation_tensor.assemble). {} on any miss."""
    try:
        from brain import rotation_tensor
        return rotation_tensor.assemble(asof=asof) or {}
    except Exception:  # noqa: BLE001
        return {}


def _default_risk_state(asof: str | None) -> dict[str, Any]:
    """The deterministic macro risk state (macro_risk.risk_state → {state}). {} on any miss.
    Reads the US regime frame for the fuse; degrades to {} on any failure."""
    try:
        from brain import macro_risk, regime_frame
        regime = regime_frame.frame("us")
        return macro_risk.risk_state(asof or date.today().isoformat(), regime) or {}
    except Exception:  # noqa: BLE001
        return {}


def _default_cooldown() -> dict[str, str]:
    """Map TICKER -> asof of its last surfaced clue, from the ledger tail (for the
    10-session cooldown). {} on any miss. Read-only; never raises."""
    out: dict[str, str] = {}
    try:
        if not _LEDGER_PATH.exists():
            return {}
        for ln in _LEDGER_PATH.read_text().splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                row = json.loads(ln)
            except Exception:  # noqa: BLE001
                continue
            t = _u(row.get("ticker"))
            a = row.get("asof")
            if t and a:
                # keep the most-recent asof per ticker (ledger is append-order chronological)
                if t not in out or str(a) > str(out[t]):
                    out[t] = str(a)
    except Exception:  # noqa: BLE001
        return {}
    return out


# =========================================================================== #
# sector-stress evaluation
# =========================================================================== #

def _sector_stress(sector_etf: str, *, cycles: dict, tensor: dict, risk_state: dict,
                   radar: dict) -> list[str]:
    """Return the list of sector-stress conditions that fire for ``sector_etf``.
    Each condition is degrade-safe (absent data simply doesn't fire it)."""
    stress: list[str] = []
    etf = _u(sector_etf)

    # (1) cycle phase Peak/Downturn/Topping (late_cycle) on the sector
    row = (cycles or {}).get(etf) or {}
    if isinstance(row, dict):
        phase = row.get("phase")
        phase_label = row.get("phaseLabel")
        late = bool(row.get("late_cycle"))
        if late or (isinstance(phase, str) and phase in _STRESS_PHASES) \
                or (isinstance(phase_label, str) and phase_label in _STRESS_PHASES):
            stress.append(f"cycle:{phase or phase_label}"
                          + ("/late" if late else ""))

    # (2) rotation_tensor: sector accel < 0 OR distribution flag
    rsv = ((tensor or {}).get("rs_velocity") or {})
    accel_map = rsv.get("accel_bps_per_day") or {}
    accel = _f(accel_map.get(etf)) if isinstance(accel_map, dict) else None
    dist_map = ((tensor or {}).get("flow") or {}).get("distribution_flag") or {}
    distributing = bool(dist_map.get(etf)) if isinstance(dist_map, dict) else False
    if accel is not None and accel < 0.0:
        stress.append(f"tensor_accel<0 ({accel:+.1f}bps/d)")
    if distributing:
        stress.append("tensor_distribution")

    # (3) macro_risk != risk_on
    st = (risk_state or {}).get("state")
    if st is not None and str(st) != "risk_on":
        stress.append(f"macro_risk:{st}")

    # (4) the sector's own radar is flaring
    r_state = (radar or {}).get(etf)
    if isinstance(r_state, str) and r_state in _RADAR_FLARE_STATES:
        stress.append(f"sector_radar:{r_state}")

    return stress


def _sector_distributing(sector_etf: str, tensor: dict) -> bool:
    """Is the sector ETF distributing per the rotation tensor? (used by S3)."""
    etf = _u(sector_etf)
    dist_map = ((tensor or {}).get("flow") or {}).get("distribution_flag") or {}
    if isinstance(dist_map, dict) and dist_map.get(etf):
        return True
    # secondary tell: negative accel is a soft-distribution read
    rsv = ((tensor or {}).get("rs_velocity") or {})
    accel_map = rsv.get("accel_bps_per_day") or {}
    accel = _f(accel_map.get(etf)) if isinstance(accel_map, dict) else None
    return accel is not None and accel < 0.0


def _name_rvol_z(ticker: str, tensor: dict, rvol_z_fn: Optional[Callable[[str], float | None]]) -> float | None:
    """The single-name rvol_z. Prefers an injected reader; else the tensor's rvol_z map
    (which only carries the 12 rotation instruments, so single names usually miss → None)."""
    if rvol_z_fn is not None:
        try:
            return _f(rvol_z_fn(ticker))
        except Exception:  # noqa: BLE001
            return None
    rvz = ((tensor or {}).get("flow") or {}).get("rvol_z") or {}
    if isinstance(rvz, dict):
        return _f(rvz.get(_u(ticker)))
    return None


# =========================================================================== #
# the detector
# =========================================================================== #

def scan(
    asof: str | None = None,
    *,
    standouts_fn: Optional[Callable[[], set[str]]] = None,
    board_membership_fn: Optional[Callable[[str | None], set[str]]] = None,
    radar_fn: Optional[Callable[[], dict[str, str]]] = None,
    sector_etf_fn: Optional[Callable[[str], str | None]] = None,
    series_fn: Optional[Callable[[str], Any]] = None,
    cycles_fn: Optional[Callable[[], dict]] = None,
    tensor_fn: Optional[Callable[[str | None], dict]] = None,
    risk_state_fn: Optional[Callable[[str | None], dict]] = None,
    rvol_z_fn: Optional[Callable[[str], float | None]] = None,
    cooldown_fn: Optional[Callable[[], dict[str, str]]] = None,
) -> list[dict[str, Any]]:
    """Scan for single-stock EARLY divergence clues (the AAPL-Jul-1 pattern).

    Every reader is injectable (production defaults read the vendored artifacts /
    price store); this is how the synthetic acceptance test reproduces the pattern
    without touching the live store, exactly as distribution_tells / rotation_tensor
    are tested. FAIL-SOFT: any absent / malformed source degrades that leg to absent;
    the function NEVER raises and returns ``[]`` when nothing qualifies (or on total
    data outage).

    BOARD MEMBERSHIP (the point-in-time re-grounding)
    -------------------------------------------------
    The buy-board trigger reads its membership from ``board_membership_fn(asof)``. Its production
    default (``_default_board_membership``) UNIONS the volatile live board (gate_go-respecting) with
    the persistent track-record ledger's ``surfaced_on(asof)`` (NOT gated — a past surfacing is a
    fact). This is what lets a replay of ``asof='2026-07-01'`` surface AAPL from the retained ledger
    even though the live board reads gate_go=False.

    For BACKWARD COMPATIBILITY: if a caller injects ``standouts_fn`` but NOT ``board_membership_fn``
    (as the synthetic tests do), the membership set is taken verbatim from that injected
    ``standouts_fn`` — the injected board is treated as the point-in-time membership, so no test needs
    to supply the ledger. ``board_membership_fn``, when injected, takes precedence.

    This broadens only the point-in-time TRIGGER membership (what to LOOK at); it never touches the
    live gate_go SIZING discipline. Consumption stays gated by MASTERMIND_DIVERGENCE_CLUE (default OFF).

    Returns a list of clue rows (≤ _MAX_CLUES), each::

        {ticker, asof, sector, sector_etf, trigger, corroborators:[...],
         sector_stress:[...], score (0.45-0.65), safe_haven:bool,
         falsifier:{kind:'rel_return', subject, benchmark, horizon_bdays:21, op:'>', value:0}}
    """
    try:
        # Resolve the board-membership reader:
        #  * an explicit board_membership_fn always wins;
        #  * else if standouts_fn was injected (tests), treat that injected board as the point-in-time
        #    membership verbatim (backward compat — no ledger needed in a fixture-driven test);
        #  * else the production default that unions the volatile board + the ledger surfaced_on(asof).
        if board_membership_fn is not None:
            membership_fn = board_membership_fn
        elif standouts_fn is not None:
            membership_fn = lambda a, _s=standouts_fn: _s()  # noqa: E731
        else:
            membership_fn = _default_board_membership
        return _scan_impl(
            asof,
            board_membership_fn=membership_fn,
            radar_fn=radar_fn or _default_radar,
            sector_etf_fn=sector_etf_fn or _default_sector_etf,
            series_fn=series_fn or _default_series_fn,
            cycles_fn=cycles_fn or _default_cycles,
            tensor_fn=tensor_fn or _default_tensor,
            risk_state_fn=risk_state_fn or _default_risk_state,
            rvol_z_fn=rvol_z_fn,
            cooldown_fn=cooldown_fn or _default_cooldown,
        )
    except Exception as e:  # noqa: BLE001 — the master invariant: never raise into a build
        log.warning("divergence_clue: scan failed, returning [] (%s)", e)
        return []


def _scan_impl(asof, *, board_membership_fn, radar_fn, sector_etf_fn, series_fn,
               cycles_fn, tensor_fn, risk_state_fn, rvol_z_fn, cooldown_fn) -> list[dict[str, Any]]:
    asof = str(asof)[:10] if asof else datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    board = _safe_call(lambda: board_membership_fn(asof), set())
    radar = _safe_call(radar_fn, {})
    cycles = _safe_call(cycles_fn, {})
    tensor = _safe_call(lambda: tensor_fn(asof), {})
    risk = _safe_call(lambda: risk_state_fn(asof), {})
    cooldown = _safe_call(cooldown_fn, {})

    if not isinstance(board, set):
        board = set(board or [])
    radar = radar if isinstance(radar, dict) else {}

    # candidate universe = every name that fires the TRIGGER
    triggers: dict[str, str] = {}
    for t in board:
        tu = _u(t)
        if tu:
            triggers[tu] = "standout_buy_board"
    for t, st in radar.items():
        tu = _u(t)
        if tu and st == _RADAR_POS_STATE and tu not in triggers:
            triggers[tu] = "radar_positive_divergence"

    clues: list[dict[str, Any]] = []
    for ticker, trigger in sorted(triggers.items()):
        clue = _evaluate_candidate(
            ticker, trigger, asof,
            sector_etf_fn=sector_etf_fn, series_fn=series_fn,
            cycles=cycles, tensor=tensor, risk=risk, radar=radar,
            rvol_z_fn=rvol_z_fn, cooldown=cooldown,
        )
        if clue is not None:
            clues.append(clue)

    # rank by score (desc), then ticker for determinism; cap ≤ _MAX_CLUES
    clues.sort(key=lambda c: (-c["score"], c["ticker"]))
    return clues[:_MAX_CLUES]


def _evaluate_candidate(ticker, trigger, asof, *, sector_etf_fn, series_fn,
                        cycles, tensor, risk, radar, rvol_z_fn, cooldown) -> dict[str, Any] | None:
    """Evaluate one triggered name against the full conjunction. None → not a clue."""
    # cooldown guard — a name surfaced within the last _COOLDOWN_SESSIONS is suppressed
    last = (cooldown or {}).get(ticker)
    if last and _sessions_between(str(last), asof) is not None \
            and _sessions_between(str(last), asof) < _COOLDOWN_SESSIONS:
        return None

    sector_etf = _safe_call(lambda: sector_etf_fn(ticker), None)
    if not sector_etf:
        return None  # un-sectored → cannot judge divergence → not a clue (safe degrade)
    sector_etf = _u(sector_etf)

    name_series = _safe_call(lambda: series_fn(ticker), None)
    sector_series = _safe_call(lambda: series_fn(sector_etf), None)

    # ---- GUARD: parabolic reject (needs the name's own price series) ----
    pct50 = _pct_vs_50dma(name_series)
    rsi = _rsi14(name_series)
    if (pct50 is not None and pct50 > _PARABOLIC_VS_50DMA) \
            or (rsi is not None and rsi > _PARABOLIC_RSI14):
        return None

    # ---- CORROBORATORS ----
    corroborators: list[str] = []
    aligned = _aligned_returns(name_series, sector_series)

    # S1 — down-day alpha
    if aligned is not None:
        dda = down_day_alpha(aligned[0], aligned[1])
        if dda is not None and dda["alpha_bps_day"] >= _S1_DOWN_DAY_ALPHA_MIN_BPS \
                and dda["hit_rate"] >= _S1_HIT_RATE_MIN:
            corroborators.append(
                f"S1_down_day_alpha({dda['alpha_bps_day']:+.0f}bps/d,"
                f"hit={dda['hit_rate']:.0%},n={dda['n_down']})")

    # S2 — RS-velocity gap (name RS-velocity vs sector RS-velocity)
    if aligned is not None:
        rel = [rn - rs for rn, rs in zip(aligned[0], aligned[1])]
        name_rsvel = single_rs_velocity(rel)
        sector_rsvel = _sector_rs_velocity_from_tensor(sector_etf, tensor)
        if name_rsvel is not None:
            gap = name_rsvel - (sector_rsvel if sector_rsvel is not None else 0.0)
            if gap >= _S2_RS_VELOCITY_GAP_MIN_BPS:
                corroborators.append(f"S2_rs_velocity_gap({gap:+.1f}bps/d)")

    # S3 — flow rotation (name rvol_z > +1 while the sector distributes)
    rvol_z = _name_rvol_z(ticker, tensor, rvol_z_fn)
    if rvol_z is not None and rvol_z > _S3_RVOL_Z_MIN and _sector_distributing(sector_etf, tensor):
        corroborators.append(f"S3_flow_rotation(rvol_z={rvol_z:+.2f})")

    if len(corroborators) < _MIN_CORROBORATORS:
        return None

    # ---- SECTOR STRESS (≥1) ----
    stress = _sector_stress(sector_etf, cycles=cycles, tensor=tensor, risk_state=risk, radar=radar)
    if len(stress) < _MIN_SECTOR_STRESS:
        return None

    # ---- score by corroborator count (0.45 → 0.65) ----
    n_corr = len(corroborators)
    score = min(_SCORE_BASE + _SCORE_PER_EXTRA_CORROB * (n_corr - _MIN_CORROBORATORS), _SCORE_MAX)
    score = round(score, 4)

    # safe-haven = macro is not risk_on AND the name still triggered bullishly (the AAPL read)
    safe_haven = str((risk or {}).get("state") or "") not in ("", "risk_on")

    return {
        "ticker": ticker,
        "asof": asof,
        "sector": sector_etf,          # sector identity == the sector ETF here (no separate GICS carry)
        "sector_etf": sector_etf,
        "trigger": trigger,
        "corroborators": corroborators,
        "sector_stress": stress,
        "score": score,
        "safe_haven": bool(safe_haven),
        "falsifier": {
            "kind": "rel_return",
            "subject": ticker,
            "benchmark": sector_etf,
            "horizon_bdays": 21,
            "op": ">",
            "value": 0,
        },
    }


def _sector_rs_velocity_from_tensor(sector_etf: str, tensor: dict) -> float | None:
    """The sector's RS-velocity (accel_bps_per_day) from the rotation tensor, if present."""
    rsv = ((tensor or {}).get("rs_velocity") or {})
    accel_map = rsv.get("accel_bps_per_day") or {}
    if isinstance(accel_map, dict):
        return _f(accel_map.get(_u(sector_etf)))
    return None


def _safe_call(fn: Callable[[], Any], default: Any) -> Any:
    """Call a zero-arg fn, returning ``default`` on any failure. Never raises."""
    try:
        v = fn()
        return v if v is not None else default
    except Exception as e:  # noqa: BLE001
        log.debug("divergence_clue: reader failed (%s)", e)
        return default


def _sessions_between(a: str, b: str) -> int | None:
    """Calendar-day gap between two YYYY-MM-DD strings as a cheap session proxy for the
    cooldown (weekends make this slightly conservative — a 10-calendar-day gap covers the
    10-session intent with margin). None on unparseable input."""
    try:
        da = date.fromisoformat(str(a)[:10])
        db = date.fromisoformat(str(b)[:10])
        return abs((db - da).days)
    except Exception:  # noqa: BLE001
        return None


# =========================================================================== #
# output helpers (fail-soft)
# =========================================================================== #

def write_latest(rows: list[dict], asof: str | None = None) -> Path | None:
    """Write the current clue set to data/brain/divergence_clue_latest.json (atomic-ish).
    Returns the path, or None on any write failure. Never raises."""
    asof = str(asof)[:10] if asof else datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    payload = {
        "schema": "divergence_clue.v1",
        "asof": asof,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "n_clues": len(rows or []),
        "clues": rows or [],
        "by_sector": by_sector(rows or []),
        "flag_enabled": clue_flag_enabled(),
        "note": ("Single-stock early-divergence clues (roadmap B5). ADDITIVE at candidacy only "
                 "— a starter-grade prior for the gate to filter, never authority to size. "
                 "Consumption gated by MASTERMIND_DIVERGENCE_CLUE (default OFF)."),
    }
    try:
        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _LATEST_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str))
        os.replace(tmp, _LATEST_PATH)
        return _LATEST_PATH
    except Exception as e:  # noqa: BLE001
        log.warning("divergence_clue: write_latest failed (%s)", e)
        return None


def append_ledger(rows: list[dict]) -> int:
    """Append clue rows to the append-only data/brain/divergence_clue.jsonl.

    IDEMPOTENT per (ticker, asof): a row whose (ticker, asof) already exists in the
    ledger is skipped, so re-running a build on the same date never double-writes.
    Returns the number of rows actually appended. Never raises.
    """
    if not rows:
        return 0
    try:
        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        existing: set[tuple[str, str]] = set()
        if _LEDGER_PATH.exists():
            for ln in _LEDGER_PATH.read_text().splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:  # noqa: BLE001
                    continue
                key = (_u(r.get("ticker")), str(r.get("asof")))
                if key[0]:
                    existing.add(key)
        written = 0
        with _LEDGER_PATH.open("a") as fh:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                key = (_u(row.get("ticker")), str(row.get("asof")))
                if not key[0] or key in existing:
                    continue
                fh.write(json.dumps(row, default=str) + "\n")
                existing.add(key)
                written += 1
        return written
    except Exception as e:  # noqa: BLE001
        log.warning("divergence_clue: append_ledger failed (%s)", e)
        return 0


def by_sector(rows: list[dict]) -> dict[str, dict[str, Any]]:
    """Clue density per sector — 2-3 clean divergers in a topping sector is itself a signal.

    Returns {sector_etf: {n, tickers:[...], mean_score, safe_haven_n}}. Never raises.
    """
    out: dict[str, dict[str, Any]] = {}
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        sec = _u(r.get("sector_etf") or r.get("sector"))
        if not sec:
            continue
        bucket = out.setdefault(sec, {"n": 0, "tickers": [], "_scores": [], "safe_haven_n": 0})
        bucket["n"] += 1
        bucket["tickers"].append(_u(r.get("ticker")))
        sc = _f(r.get("score"))
        if sc is not None:
            bucket["_scores"].append(sc)
        if r.get("safe_haven"):
            bucket["safe_haven_n"] += 1
    for sec, b in out.items():
        scores = b.pop("_scores")
        b["mean_score"] = round(sum(scores) / len(scores), 4) if scores else None
    return out


# =========================================================================== #
# CLI entry point
# =========================================================================== #

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    asof_arg = sys.argv[1] if len(sys.argv) > 1 else None
    _rows = scan(asof=asof_arg)
    _p = write_latest(_rows, asof_arg)
    _n = append_ledger(_rows)
    print(f"divergence_clue: {len(_rows)} clue(s) asof={asof_arg or 'today'} "
          f"(appended {_n}) → {_p}")
    for _r in _rows:
        print(f"  {_r['ticker']:6s} score={_r['score']} sector={_r['sector_etf']} "
              f"trigger={_r['trigger']} corrob={len(_r['corroborators'])} "
              f"stress={len(_r['sector_stress'])} safe_haven={_r['safe_haven']}")

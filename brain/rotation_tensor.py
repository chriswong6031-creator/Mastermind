"""brain/rotation_tensor.py — Rotation Tensor perception organ (W-E.0, E0.1).

WHY THIS MODULE EXISTS
----------------------
The 2026-07-02 incident: the bot's regime label read Goldilocks/STABLE while the tape rotated
defensively for 6+ sessions.  The tensor is the *magnitude organ* — the difference between
"healthcare is leading" (a label) and "healthcare is gaining 34 bps/day on semis, 6 sessions
running, a 0.83-percentile episode, with breadth + churn + flow confirming" (a measurement).

This module ONLY measures.  It has zero sizing authority (advisory:true until the §3 gate
passes), zero LLM calls, and never blocks or raises.  Stale inputs → degrade, never fabricate.

FIVE MEASUREMENT BLOCKS (per rotation_spec.md §1)
---------------------------------------------------
  (a) Pairwise 12×12 RS-velocity matrix: R[i][j] = avg bps/day i out-gained j over 20d;
      dR[i][j] = rate the gap is currently widening (bps/day); plus top_pairs extract.
  (b) Breadth migration: sector breadth participation shift; degrades to 'unavailable'.
  (c) Leadership churn index: rank-order turnover of top-RS, 10d.
  (d) Flow proxies: relative-volume z + ETF shares-outstanding delta z (SMH → rvol_only).
  (e) Episode detection: headline DEF/OFF bloc + per top-pair; rarity percentile vs 2015+.

CAUSAL CONTRACT
---------------
All computation uses ONLY close[:T] (no forward fill of future data), ewm / rolling with
min_periods set.  The plane is therefore replayable on any historical date for P3 validation.

OUTPUT
------
Artifact: data/market_view/rotation_tensor.json  (atomic tmp→replace, P2).
Schema: rotation_spec.md §2 contract.

SERIES ACCESS
-------------
Production default reads data/yahoo/<ticker>.parquet (shared with distribution_tells conventions).
Tests inject a `series_fn(ticker) -> pd.Series | None` (close series, date-indexed) so the
shared live store is never live-read inside the computation.  Same pattern as distribution_tells.

INVARIANT (governs every path)
------------------------------
Missing / stale / corrupt data may coarsen identity or degrade a block to 'unavailable'.
It may NEVER fabricate a measurement, un-cap, raise authority, or flip direction.
advisory:true until walk-forward gate passes (P3).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Universe definition (rotation_spec.md §1)
# ---------------------------------------------------------------------------

# 11 GICS SPDRs + semis bloc (SMH primary, SOXX fallback).
UNIVERSE: list[str] = [
    "XLB", "XLC", "XLE", "XLF", "XLI",
    "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
    "SMH",
]
_BENCHMARK: str = "SPY"
_SEMIS_PRIMARY: str = "SMH"
_SEMIS_FALLBACK: str = "SOXX"

# Defensive / offensive blocs for the headline episode axis (rotation_spec.md §1e).
_DEF_BLOC: tuple[str, ...] = ("XLV", "XLU", "XLP")
_OFF_BLOC: tuple[str, ...] = ("SMH", "XLK", "XLY")

# Minimum history for the computation to be meaningful.
_MIN_HISTORY: int = 260  # sessions

# Minimum history for 2015+ episode calibration (the spec requires 2011+; we use 2015).
_EPISODE_HISTORY_START: str = "2015-01-01"

# Freshness-gate: flows are allowed up to this many calendar days stale.
_FLOWS_MAX_AGE_TD: int = 7  # calendar days

# Top-N pairs to extract (largest |R| with same-signed dR).
_TOP_PAIRS_N: int = 6

# ---------------------------------------------------------------------------
# Doctrine thresholds (mirrored here as fallbacks; doctrine.yml is authoritative)
# ---------------------------------------------------------------------------
_RSM_WINDOW: int = 20       # 20d relative momentum window
_VEL_WINDOW: int = 5        # 5d acceleration window
_CHURN_K: int = 4           # top-K sectors for churn
_CHURN_LOOKBACK: int = 10   # sessions for churn measurement
_RVOL_SMA: int = 20         # SMA window for relative-volume z
_EPISODE_MIN_SESSIONS: int = 3  # min consecutive same-sign sessions to open an episode
_EPISODE_PCTILE_MIN: float = 0.70  # advisory only — not a gate here

_YAHOO_DIR: Path = _ROOT / "vendor" / "macro" / "data" / "yahoo"
_FLOWS_DIR: Path = _ROOT / "vendor" / "macro" / "data" / "flows"
_ARTIFACT_DIR: Path = _ROOT / "data" / "market_view"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_doctrine() -> dict:
    try:
        import yaml
        p = _ROOT / "config" / "doctrine.yml"
        if p.exists():
            return yaml.safe_load(p.read_text()) or {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _as_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _default_series_fn(ticker: str) -> pd.Series | None:
    """Default production reader: data/yahoo/<ticker>.parquet → close Series.

    Tries SMH first for the semis bloc; falls back to SOXX if SMH absent.
    Never raises — returns None on any miss so the block degrades gracefully.
    """
    effective = ticker
    if ticker == _SEMIS_PRIMARY:
        # Fall back to SOXX if SMH is absent.
        for candidate in (_SEMIS_PRIMARY, _SEMIS_FALLBACK):
            p = _YAHOO_DIR / f"{candidate}.parquet"
            if p.exists():
                effective = candidate
                break
    else:
        p = _YAHOO_DIR / f"{ticker}.parquet"
        if not p.exists():
            return None
    try:
        df = pd.read_parquet(p, columns=["close"])
        return df["close"].sort_index()
    except Exception:  # noqa: BLE001
        return None


def _default_volume_fn(ticker: str) -> pd.Series | None:
    """Production reader for volume: data/yahoo/<ticker>.parquet → volume Series."""
    effective = ticker
    if ticker == _SEMIS_PRIMARY:
        for candidate in (_SEMIS_PRIMARY, _SEMIS_FALLBACK):
            p = _YAHOO_DIR / f"{candidate}.parquet"
            if p.exists():
                effective = candidate
                break
    else:
        p = _YAHOO_DIR / f"{ticker}.parquet"
        if not p.exists():
            return None
    try:
        df = pd.read_parquet(p, columns=["volume"])
        return df["volume"].sort_index()
    except Exception:  # noqa: BLE001
        return None


def _default_flows_fn(ticker: str) -> pd.DataFrame | None:
    """Production reader for ETF flows: data/flows/<ticker>.parquet → {nav, aum_mn, so_mn}."""
    # Only 11 sector ETFs have flows; SMH deliberately has no flows file (rotation_spec.md §1d).
    if ticker in (_SEMIS_PRIMARY, _SEMIS_FALLBACK):
        return None
    p = _FLOWS_DIR / f"{ticker}.parquet"
    if not p.exists():
        return None
    try:
        return pd.read_parquet(p).sort_index()
    except Exception:  # noqa: BLE001
        return None


def _nan_matrix(n: int) -> list[list[float | None]]:
    return [[None] * n for _ in range(n)]


# ---------------------------------------------------------------------------
# Block (a) — pairwise RS-velocity matrix
# ---------------------------------------------------------------------------

def _compute_rs_velocity(
    tickers: list[str],
    benchmark: str,
    series_fn: Callable[[str], pd.Series | None],
    asof: Optional[str] = None,
) -> dict[str, Any]:
    """Compute the 12×12 R / dR matrices and top_pairs extract.

    Returns
    -------
    {
        "level_bps_per_day": {ticker: float|None},  # rsm20/20 in bps/day
        "accel_bps_per_day": {ticker: float|None},  # vel (5d change of rsm20 / 5) in bps/day
        "pair_R": [[…12×12…]],                     # bps/day i gains on j over 20d
        "pair_dR": [[…12×12…]],                    # how fast the gap is widening, bps/day
        "top_pairs": [{lead, lag, R_bps_day, dR_bps_day, accelerating}…],
        "as_of": str | None,   # true data date = last common trading date
        "n_instruments": int,
    }
    All None on degrade — never raises.
    """
    n = len(tickers)
    bench = series_fn(benchmark)
    if bench is None or len(bench) < _MIN_HISTORY:
        return {
            "level_bps_per_day": {t: None for t in tickers},
            "accel_bps_per_day": {t: None for t in tickers},
            "pair_R": _nan_matrix(n),
            "pair_dR": _nan_matrix(n),
            "top_pairs": [],
            "as_of": None,
            "n_instruments": 0,
        }

    bench_s = bench.sort_index()
    if asof:
        bench_s = bench_s[:asof]

    # Build per-instrument rs-line + rsm20 + vel, aligned on benchmark index.
    rsm20: dict[str, pd.Series | None] = {}
    vel: dict[str, pd.Series | None] = {}
    level: dict[str, float | None] = {}
    accel: dict[str, float | None] = {}

    for tk in tickers:
        s = series_fn(tk)
        if s is None:
            rsm20[tk] = None
            vel[tk] = None
            level[tk] = None
            accel[tk] = None
            continue
        s = s.sort_index()
        if asof:
            s = s[:asof]
        # Align to benchmark
        aligned, bench_al = s.align(bench_s, join="inner")
        if len(aligned) < _MIN_HISTORY:
            rsm20[tk] = None
            vel[tk] = None
            level[tk] = None
            accel[tk] = None
            continue
        # rs_i(t) = log(close_i(t) / close_SPY(t))
        rs = np.log(aligned / bench_al)
        # rsm20_i(t) = rs_i(t) - rs_i(t-20)  — causal: no forward fill
        rm = rs.diff(_RSM_WINDOW)
        # vel_i(t) = (rsm20_i(t) - rsm20_i(t-5)) / 5
        v = rm.diff(_VEL_WINDOW) / _VEL_WINDOW
        rsm20[tk] = rm
        vel[tk] = v
        last_rm = rm.dropna().iloc[-1] if not rm.dropna().empty else None
        last_v = v.dropna().iloc[-1] if not v.dropna().empty else None
        level[tk] = float(1e4 * last_rm / _RSM_WINDOW) if last_rm is not None else None
        accel[tk] = float(1e4 * last_v) if last_v is not None else None

    # Determine true as_of: last common date across all available instruments + benchmark.
    last_dates: list[pd.Timestamp] = []
    for tk in tickers:
        if rsm20[tk] is not None:
            s_raw = series_fn(tk)
            if s_raw is not None:
                s_raw = s_raw.sort_index()
                if asof:
                    s_raw = s_raw[:asof]
                if not s_raw.empty:
                    last_dates.append(s_raw.index[-1])
    if bench_s is not None and not bench_s.empty:
        last_dates.append(bench_s.index[-1])
    true_asof = min(last_dates).strftime("%Y-%m-%d") if last_dates else None

    # Build the 12×12 matrices.
    pair_R: list[list[float | None]] = _nan_matrix(n)
    pair_dR: list[list[float | None]] = _nan_matrix(n)

    # We need the last-value rsm20 and vel per instrument (already computed as level/accel).
    # R[i][j] = 1e4 * (rsm20_i(t) - rsm20_j(t)) / 20
    # dR[i][j] = 1e4 * (vel_i(t) - vel_j(t))

    # Get the raw last rsm20 (not bps/day form) and last vel for matrix arithmetic.
    last_rsm20: dict[str, float | None] = {}
    last_vel: dict[str, float | None] = {}
    for tk in tickers:
        if rsm20[tk] is not None:
            rm = rsm20[tk].dropna()
            last_rsm20[tk] = float(rm.iloc[-1]) if not rm.empty else None
        else:
            last_rsm20[tk] = None
        if vel[tk] is not None:
            v = vel[tk].dropna()
            last_vel[tk] = float(v.iloc[-1]) if not v.empty else None
        else:
            last_vel[tk] = None

    for i, ti in enumerate(tickers):
        for j, tj in enumerate(tickers):
            rm_i = last_rsm20.get(ti)
            rm_j = last_rsm20.get(tj)
            v_i = last_vel.get(ti)
            v_j = last_vel.get(tj)
            if rm_i is not None and rm_j is not None:
                pair_R[i][j] = round(float(1e4 * (rm_i - rm_j) / _RSM_WINDOW), 4)
            if v_i is not None and v_j is not None:
                pair_dR[i][j] = round(float(1e4 * (v_i - v_j)), 4)

    # Top pairs: largest |R| where dR is same-signed as R (accelerating divergences).
    pairs: list[dict] = []
    for i, ti in enumerate(tickers):
        for j, tj in enumerate(tickers):
            if i >= j:
                continue  # upper triangle only; avoid duplicates
            r_val = pair_R[i][j]
            dr_val = pair_dR[i][j]
            if r_val is None:
                continue
            # Only include if |R| significant and dR same-signed (confirming acceleration)
            if abs(r_val) < 0.01:
                continue
            accelerating = (dr_val is not None and r_val * dr_val > 0)
            # For top_pairs ordering: lead is the instrument that is gaining
            if r_val >= 0:
                lead, lag = ti, tj
                r_out = r_val
                dr_out = dr_val if dr_val is not None else 0.0
            else:
                lead, lag = tj, ti
                r_out = abs(r_val)
                dr_out = abs(dr_val) if dr_val is not None else 0.0
            pairs.append({
                "lead": lead,
                "lag": lag,
                "R_bps_day": round(r_out, 3),
                "dR_bps_day": round(dr_out, 3),
                "accelerating": accelerating,
            })

    # Sort by R magnitude (largest divergence first); take top N with same-signed dR.
    pairs_accel = sorted([p for p in pairs if p["accelerating"]], key=lambda x: -x["R_bps_day"])
    pairs_all = sorted(pairs, key=lambda x: -x["R_bps_day"])
    top_pairs = (pairs_accel + [p for p in pairs_all if not p["accelerating"]])[:_TOP_PAIRS_N]

    n_instruments = sum(1 for tk in tickers if last_rsm20.get(tk) is not None)

    return {
        "level_bps_per_day": {tk: round(v, 4) if v is not None else None for tk, v in level.items()},
        "accel_bps_per_day": {tk: round(v, 4) if v is not None else None for tk, v in accel.items()},
        "pair_R": pair_R,
        "pair_dR": pair_dR,
        "top_pairs": top_pairs,
        "as_of": true_asof,
        "n_instruments": n_instruments,
    }


# ---------------------------------------------------------------------------
# Block (b) — breadth migration
# ---------------------------------------------------------------------------

_SECTOR_GICS_MAP: dict[str, str] = {
    "XLB": "Materials",
    "XLC": "Communication Services",
    "XLE": "Energy",
    "XLF": "Financials",
    "XLI": "Industrials",
    "XLK": "Information Technology",
    "XLP": "Consumer Staples",
    "XLRE": "Real Estate",
    "XLU": "Utilities",
    "XLV": "Health Care",
    "XLY": "Consumer Discretionary",
}


def _compute_breadth_migration(
    tickers: list[str],
    asof: Optional[str] = None,
) -> dict[str, Any]:
    """Per-sector breadth participation shift (5d delta in pct_above_50 and nh_share).

    Uses the breadth/constituents.parquet + _closes_cache.parquet for per-sector computation.
    Degrades to 'unavailable' when data is missing — never fabricates.

    Returns {"status": "ok"|"unavailable", "pct50_5d_delta": {...}, "nh_share_5d_delta": {...}}
    """
    breadth_dir = _ROOT / "vendor" / "macro" / "data" / "breadth"
    closes_path = breadth_dir / "_closes_cache.parquet"
    constituents_path = breadth_dir / "constituents.parquet"

    if not closes_path.exists() or not constituents_path.exists():
        return {"status": "unavailable", "pct50_5d_delta": None, "nh_share_5d_delta": None,
                "note": "breadth cache files absent"}

    try:
        closes = pd.read_parquet(closes_path).sort_index()
        constituents = pd.read_parquet(constituents_path)
    except Exception as exc:
        log.debug("breadth_migration: read error: %s", exc)
        return {"status": "unavailable", "pct50_5d_delta": None, "nh_share_5d_delta": None,
                "note": "breadth read error"}

    if asof:
        closes = closes[:asof]

    if len(closes) < 6:  # need at least 5d delta
        return {"status": "unavailable", "pct50_5d_delta": None, "nh_share_5d_delta": None,
                "note": "insufficient breadth history"}

    # Build per-sector pct_above_50 and nh_share.
    # GICS sector name in constituents → ticker in UNIVERSE.
    gics_to_ticker = {v: k for k, v in _SECTOR_GICS_MAP.items()}

    # Check if 'sector' column exists.
    if "sector" not in constituents.columns:
        return {"status": "unavailable", "pct50_5d_delta": None, "nh_share_5d_delta": None,
                "note": "constituents missing sector column"}

    pct50_5d: dict[str, float | None] = {}
    nh_5d: dict[str, float | None] = {}

    now_close = closes.iloc[-1].dropna()
    prev5_close = closes.iloc[-6].dropna() if len(closes) >= 6 else None

    # Also need the 50d SMA for each stock.
    sma50_now: pd.Series | None = None
    if len(closes) >= 50:
        sma50_now = closes.iloc[-50:].mean()

    for sector_name, etf_ticker in gics_to_ticker.items():
        if etf_ticker not in tickers:
            continue
        # Filter constituents to this sector.
        sec_syms = constituents[constituents["sector"] == sector_name].index.tolist()
        if not sec_syms:
            pct50_5d[etf_ticker] = None
            nh_5d[etf_ticker] = None
            continue

        # Intersection with available closes columns.
        available = [s for s in sec_syms if s in closes.columns]
        if not available:
            pct50_5d[etf_ticker] = None
            nh_5d[etf_ticker] = None
            continue

        # pct_above_50: % of sector stocks trading above 50d SMA today vs 5d ago.
        if sma50_now is not None and prev5_close is not None:
            # Today's pct_above_50.
            sma_vals_now = sma50_now.reindex(available)
            close_vals_now = now_close.reindex(available)
            valid_now = (sma_vals_now.notna() & close_vals_now.notna())
            if valid_now.sum() > 0:
                p50_now = float((close_vals_now[valid_now] > sma_vals_now[valid_now]).mean() * 100)
            else:
                p50_now = None

            # 5d-ago pct_above_50: use 50d SMA up to 5d ago.
            if len(closes) >= 55:
                sma50_prev = closes.iloc[-55:-5].mean()
                sma_vals_prev = sma50_prev.reindex(available)
                close_vals_prev = prev5_close.reindex(available)
                valid_prev = (sma_vals_prev.notna() & close_vals_prev.notna())
                if valid_prev.sum() > 0:
                    p50_prev = float(
                        (close_vals_prev[valid_prev] > sma_vals_prev[valid_prev]).mean() * 100
                    )
                else:
                    p50_prev = None
            else:
                p50_prev = None

            if p50_now is not None and p50_prev is not None:
                pct50_5d[etf_ticker] = round(p50_now - p50_prev, 2)
            else:
                pct50_5d[etf_ticker] = None
        else:
            pct50_5d[etf_ticker] = None

        # New-highs share delta (proxy: 252d high share today vs 5d ago).
        if len(closes) >= 253:
            high252 = closes.iloc[-252:].max()  # causal: uses closes up to T
            close_vals_now = now_close.reindex(available)
            high_now = high252.reindex(available)
            valid = (high_now.notna() & close_vals_now.notna())
            if valid.sum() > 0:
                nh_now = float((close_vals_now[valid] >= high_now[valid] * 0.99).mean())
            else:
                nh_now = None

            if len(closes) >= 258:
                high252_prev = closes.iloc[-257:-5].max()
                close_vals_prev = prev5_close.reindex(available)
                high_prev = high252_prev.reindex(available)
                valid_prev = (high_prev.notna() & close_vals_prev.notna())
                if valid_prev.sum() > 0:
                    nh_prev = float(
                        (close_vals_prev[valid_prev] >= high_prev[valid_prev] * 0.99).mean()
                    )
                else:
                    nh_prev = None
            else:
                nh_prev = None

            if nh_now is not None and nh_prev is not None:
                nh_5d[etf_ticker] = round(nh_now - nh_prev, 4)
            else:
                nh_5d[etf_ticker] = None
        else:
            nh_5d[etf_ticker] = None

    any_ok = any(v is not None for v in pct50_5d.values())
    return {
        "status": "ok" if any_ok else "unavailable",
        "pct50_5d_delta": pct50_5d,
        "nh_share_5d_delta": nh_5d,
    }


# ---------------------------------------------------------------------------
# Block (c) — leadership churn index
# ---------------------------------------------------------------------------

def _compute_leadership_churn(
    tickers: list[str],
    series_fn: Callable[[str], pd.Series | None],
    benchmark: str,
    asof: Optional[str] = None,
) -> dict[str, Any]:
    """Rank-order turnover of top-RS sectors, 10d.

    Returns
    -------
    {
        "churn10": float | None,    # 1 - Jaccard(top_now, top_prev), in [0,1]
        "rank_dist": int | None,    # Σ |rank_i(t) - rank_i(t-10)|
        "entered_top4": [str],
        "exited_top4": [str],
    }
    """
    bench = series_fn(benchmark)
    if bench is None:
        return {"churn10": None, "rank_dist": None, "entered_top4": [], "exited_top4": []}

    bench_s = bench.sort_index()
    if asof:
        bench_s = bench_s[:asof]

    # Compute rsm20 per sector ticker (not SMH — SMH is a bloc instrument, not a sector).
    sector_tickers = [t for t in tickers if t in _SECTOR_GICS_MAP]
    rsm20_map: dict[str, pd.Series] = {}
    for tk in sector_tickers:
        s = series_fn(tk)
        if s is None:
            continue
        s = s.sort_index()
        if asof:
            s = s[:asof]
        aligned, bench_al = s.align(bench_s, join="inner")
        if len(aligned) < _RSM_WINDOW + _CHURN_LOOKBACK + 5:
            continue
        rs = np.log(aligned / bench_al)
        rm = rs.diff(_RSM_WINDOW)
        rsm20_map[tk] = rm

    if len(rsm20_map) < 4:
        return {"churn10": None, "rank_dist": None, "entered_top4": [], "exited_top4": []}

    # Align all rsm20 series on common index.
    df = pd.DataFrame(rsm20_map).sort_index().dropna(how="all")
    if len(df) < _CHURN_LOOKBACK + 1:
        return {"churn10": None, "rank_dist": None, "entered_top4": [], "exited_top4": []}

    # Current ranks (by rsm20 at T).
    row_now = df.iloc[-1].dropna()
    row_prev = df.iloc[-1 - _CHURN_LOOKBACK].dropna() if len(df) >= _CHURN_LOOKBACK + 1 else pd.Series(dtype=float)

    if row_now.empty or row_prev.empty:
        return {"churn10": None, "rank_dist": None, "entered_top4": [], "exited_top4": []}

    # Top-K sectors by rsm20.
    k = min(_CHURN_K, len(row_now), len(row_prev))
    top_now = set(row_now.nlargest(k).index.tolist())
    top_prev = set(row_prev.nlargest(k).index.tolist())

    intersection = top_now & top_prev
    union = top_now | top_prev
    churn10 = float(1.0 - len(intersection) / len(union)) if union else 0.0

    # Rank displacement: Σ |rank_i(t) - rank_i(t-10)|.
    common_tickers = sorted(set(row_now.index) & set(row_prev.index))
    if common_tickers:
        ranks_now = pd.Series(range(1, len(row_now) + 1), index=row_now.sort_values(ascending=False).index)
        ranks_prev = pd.Series(range(1, len(row_prev) + 1), index=row_prev.sort_values(ascending=False).index)
        rank_dist = int(sum(abs(int(ranks_now.get(t, 0)) - int(ranks_prev.get(t, 0)))
                           for t in common_tickers))
    else:
        rank_dist = None

    entered = sorted(top_now - top_prev)
    exited = sorted(top_prev - top_now)

    return {
        "churn10": round(churn10, 4),
        "rank_dist": rank_dist,
        "entered_top4": entered,
        "exited_top4": exited,
    }


# ---------------------------------------------------------------------------
# Block (d) — flow proxies
# ---------------------------------------------------------------------------

def _compute_flow_proxies(
    tickers: list[str],
    volume_fn: Callable[[str], pd.Series | None],
    flows_fn: Callable[[str], pd.DataFrame | None],
    asof: Optional[str] = None,
) -> dict[str, Any]:
    """Relative-volume z (all 12) + ETF shares-outstanding delta z (11 sectors).

    SMH has no flows/ file → flow_source='rvol_only' for SMH.
    Returns
    -------
    {
        "flow_plane": "etf_so_delta",
        "rvol_z": {ticker: float|None},
        "netflow_z": {ticker: float|None},   # null for SMH
        "distribution_flag": {ticker: bool}, # high rvol_z + negative accel
        "asof_flows": str | None,
        "flow_source": str,
    }
    """
    # --- rvol_z: relative-volume z-score (all 12) ---
    rvol: dict[str, float | None] = {}
    for tk in tickers:
        vol = volume_fn(tk)
        if vol is None:
            rvol[tk] = None
            continue
        vol = vol.sort_index()
        if asof:
            vol = vol[:asof]
        vol_clean = vol.dropna()
        if len(vol_clean) < _RVOL_SMA + 1:
            rvol[tk] = None
            continue
        sma = vol_clean.rolling(_RVOL_SMA, min_periods=_RVOL_SMA).mean()
        rv = vol_clean / sma.where(sma > 0)
        rv_clean = rv.dropna()
        if rv_clean.empty:
            rvol[tk] = None
        else:
            rvol[tk] = float(rv_clean.iloc[-1])

    # Cross-sectional z-score of rvol.
    rvol_vals = {tk: v for tk, v in rvol.items() if v is not None}
    rvol_z: dict[str, float | None] = {tk: None for tk in tickers}
    if len(rvol_vals) >= 2:
        vals = list(rvol_vals.values())
        mu = np.mean(vals)
        std = np.std(vals, ddof=1)
        if std > 0:
            for tk, v in rvol_vals.items():
                rvol_z[tk] = round(float((v - mu) / std), 4)
        else:
            for tk in rvol_vals:
                rvol_z[tk] = 0.0

    # --- netflow_z: ETF shares-outstanding delta (11 sector ETFs only) ---
    netflow_5d: dict[str, float | None] = {}
    flows_asofs: list[pd.Timestamp] = []
    for tk in tickers:
        fdf = flows_fn(tk)
        if fdf is None:
            netflow_5d[tk] = None
            continue
        fdf = fdf.sort_index()
        if asof:
            fdf = fdf[:asof]
        if fdf.empty or "so_mn" not in fdf.columns or "nav" not in fdf.columns:
            netflow_5d[tk] = None
            continue
        flows_asofs.append(fdf.index[-1])
        # netflow_i(t) = (so_mn_i(t) - so_mn_i(t-1)) * nav_i(t)
        so = fdf["so_mn"].dropna()
        nav = fdf["nav"].dropna()
        aligned_so, aligned_nav = so.align(nav, join="inner")
        if len(aligned_so) < 2:
            netflow_5d[tk] = None
            continue
        daily_flow = aligned_so.diff() * aligned_nav  # $mn
        # 5d sum of netflow (rolling)
        flow5 = daily_flow.rolling(5, min_periods=1).sum()
        flow5_clean = flow5.dropna()
        if flow5_clean.empty:
            netflow_5d[tk] = None
        else:
            netflow_5d[tk] = float(flow5_clean.iloc[-1])

    # Cross-sectional z-score of netflow_5d.
    flow_vals = {tk: v for tk, v in netflow_5d.items() if v is not None}
    netflow_z: dict[str, float | None] = {tk: None for tk in tickers}
    if len(flow_vals) >= 2:
        vals = list(flow_vals.values())
        mu = np.mean(vals)
        std = np.std(vals, ddof=1)
        if std > 0:
            for tk, v in flow_vals.items():
                netflow_z[tk] = round(float((v - mu) / std), 4)
        else:
            for tk in flow_vals:
                netflow_z[tk] = 0.0

    # Distribution flag: high rvol_z + accel_bps_per_day would be negative → computed caller-side.
    # Here we flag: rvol_z > 1.0 (above cross-sectional mean) — the acceleration check happens
    # in assemble() where we have accel_bps_per_day.
    distribution_flag: dict[str, bool] = {}
    for tk in tickers:
        rz = rvol_z.get(tk)
        if rz is not None and rz > 1.0:
            distribution_flag[tk] = True
        else:
            distribution_flag[tk] = False

    asof_flows_str: str | None = None
    if flows_asofs:
        asof_flows_str = min(flows_asofs).strftime("%Y-%m-%d")

    return {
        "flow_plane": "etf_so_delta",
        "rvol_z": rvol_z,
        "netflow_z": netflow_z,
        "distribution_flag": distribution_flag,
        "asof_flows": asof_flows_str,
        "flow_source": "so_mn+rvol",
    }


# ---------------------------------------------------------------------------
# Block (e) — episode detection + magnitude + percentile
# ---------------------------------------------------------------------------

def _compute_episodes(
    tickers: list[str],
    series_fn: Callable[[str], pd.Series | None],
    benchmark: str,
    top_pairs: list[dict],
    asof: Optional[str] = None,
) -> dict[str, Any]:
    """Headline DEF/OFF episode + per top-pair episodes.

    Episode detection (deterministic hysteresis per rotation_spec.md §1e):
    - spread = 1e4 * (DEF_rsm20_mean - OFF_rsm20_mean)
    - vel_spread = daily change of spread
    - episode starts when sign(vel_spread) flips and holds >= _EPISODE_MIN_SESSIONS
    - magnitude = cumulative Δspread from start→now
    - percentile = fraction of historical (2015+) sessions with a LARGER same-direction episode rate

    Returns {"headline_episode": {...}, "episodes": [...]}
    """
    bench = series_fn(benchmark)
    if bench is None:
        return {"headline_episode": None, "episodes": []}

    bench_s = bench.sort_index()
    if asof:
        bench_s = bench_s[:asof]

    def _rsm20_series(tk: str) -> pd.Series | None:
        s = series_fn(tk)
        if s is None:
            return None
        s = s.sort_index()
        if asof:
            s = s[:asof]
        aligned, bench_al = s.align(bench_s, join="inner")
        if len(aligned) < _MIN_HISTORY:
            return None
        rs = np.log(aligned / bench_al)
        return rs.diff(_RSM_WINDOW)

    # --- Headline episode: DEF over OFF bloc ---
    def_series_list = [_rsm20_series(tk) for tk in _DEF_BLOC]
    off_series_list = [_rsm20_series(tk) for tk in _OFF_BLOC]
    def_series_list = [s for s in def_series_list if s is not None]
    off_series_list = [s for s in off_series_list if s is not None]

    headline_episode = None
    if def_series_list and off_series_list:
        # Align all on common index.
        common_idx = def_series_list[0].index
        for s in def_series_list[1:] + off_series_list:
            common_idx = common_idx.intersection(s.index)
        def_rsm = pd.concat([s.reindex(common_idx) for s in def_series_list], axis=1).mean(axis=1)
        off_rsm = pd.concat([s.reindex(common_idx) for s in off_series_list], axis=1).mean(axis=1)
        spread = 1e4 * (def_rsm - off_rsm)
        headline_episode = _detect_episode(spread, axis="DEF_over_OFF",
                                           history_start=_EPISODE_HISTORY_START)

    # --- Per top-pair episodes ---
    episodes: list[dict] = []
    if headline_episode:
        episodes.append(headline_episode)

    for pair in top_pairs[:4]:
        lead = pair.get("lead")
        lag = pair.get("lag")
        if not lead or not lag or lead == lag:
            continue
        s_lead = _rsm20_series(lead)
        s_lag = _rsm20_series(lag)
        if s_lead is None or s_lag is None:
            continue
        common_idx = s_lead.index.intersection(s_lag.index)
        spread_pair = 1e4 * (s_lead.reindex(common_idx) - s_lag.reindex(common_idx))
        ep = _detect_episode(spread_pair,
                             axis=f"{lead}_over_{lag}",
                             history_start=_EPISODE_HISTORY_START)
        if ep:
            episodes.append(ep)

    return {
        "headline_episode": headline_episode,
        "episodes": episodes,
    }


def _detect_episode(
    spread: pd.Series,
    axis: str,
    history_start: str = _EPISODE_HISTORY_START,
) -> dict | None:
    """Detect the current episode in a spread series.

    Returns a dict or None if no episode is currently active.
    """
    spread_clean = spread.dropna().sort_index()
    if len(spread_clean) < _EPISODE_MIN_SESSIONS + 2:
        return None

    vel = spread_clean.diff()

    # Find the most recent sign-consistent run of vel (>= _EPISODE_MIN_SESSIONS).
    # Walk backwards from the end.
    vel_arr = vel.dropna().values
    vel_idx = vel.dropna().index

    if len(vel_arr) < _EPISODE_MIN_SESSIONS:
        return None

    # Determine current direction: sign of the last velocity that is non-zero.
    last_sign = 0
    for v in reversed(vel_arr):
        if v > 0:
            last_sign = 1
            break
        elif v < 0:
            last_sign = -1
            break
    if last_sign == 0:
        return None

    # Count how many consecutive same-sign sessions at the end.
    n_consec = 0
    for v in reversed(vel_arr):
        if v == 0:
            continue  # skip zeros
        if np.sign(v) == last_sign:
            n_consec += 1
        else:
            break

    if n_consec < _EPISODE_MIN_SESSIONS:
        return None

    # Find episode start: go back n_consec sessions.
    n_back = min(n_consec, len(vel_idx))
    start_idx = max(0, len(vel_idx) - n_back)
    episode_start_date = vel_idx[start_idx]

    # Magnitude: cumulative Δspread from start to now.
    ep_spread = spread_clean[episode_start_date:]
    if len(ep_spread) < 2:
        return None
    magnitude_bps = float(ep_spread.iloc[-1] - ep_spread.iloc[0])
    n_sessions = int(len(ep_spread) - 1)  # number of daily changes
    rate_bps_day = magnitude_bps / n_sessions if n_sessions > 0 else 0.0

    direction = "defensive" if last_sign > 0 else "offensive"

    # Percentile: among all historical sessions (2015+) where an episode of this axis
    # is active with the same direction, what fraction have a SMALLER |rate|?
    # Computed as: fraction of ALL sessions (2015+) where |vel| > |current_rate| in same direction
    # This gives the "rarity" - what fraction of days have this extreme a movement.
    hist_spread = spread_clean[history_start:]
    hist_vel = hist_spread.diff().dropna()
    same_dir = hist_vel[np.sign(hist_vel) == last_sign]
    if len(same_dir) > 0:
        abs_rate = abs(rate_bps_day)
        # Percentile = fraction of same-direction sessions with |vel| <= current episode rate
        # (1 - this = fraction of sessions more extreme than us)
        pctile = float((abs(same_dir) <= abs_rate).mean())
    else:
        pctile = 0.5

    return {
        "axis": axis,
        "start": episode_start_date.strftime("%Y-%m-%d"),
        "n_sessions": n_sessions,
        "magnitude_bps": round(magnitude_bps, 3),
        "rate_bps_day": round(rate_bps_day, 3),
        "percentile": round(pctile, 4),
        "direction": direction,
        "agreement": "unscored",  # updated in assemble() after all blocks complete
    }


# ---------------------------------------------------------------------------
# Confidence + freshness scoring
# ---------------------------------------------------------------------------

def _compute_confidence(
    n_instruments: int,
    breadth_ok: bool,
    flows_asof: str | None,
    n_episode_blocks: int,
) -> float:
    """Composite confidence 0→1 based on plane coverage and freshness.

    Shrink-only: each missing or stale plane reduces confidence from 1.0.
    """
    base = 0.0
    # Instruments: 0.4 weight — core block
    base += 0.4 * min(n_instruments / len(UNIVERSE), 1.0)
    # Breadth: 0.2 weight
    base += 0.2 if breadth_ok else 0.0
    # Flows freshness: 0.2 weight
    if flows_asof:
        try:
            flows_date = date.fromisoformat(flows_asof)
            today = datetime.now(tz=timezone.utc).date()
            age_td = (today - flows_date).days
            base += 0.2 * max(0.0, 1.0 - age_td / _FLOWS_MAX_AGE_TD)
        except Exception:  # noqa: BLE001
            pass
    else:
        base += 0.0
    # Episodes: 0.2 weight
    base += 0.2 * min(n_episode_blocks / 2, 1.0)
    return round(min(max(base, 0.0), 1.0), 4)


# ---------------------------------------------------------------------------
# Public API — assemble()
# ---------------------------------------------------------------------------

def assemble(
    *,
    series_fn: Callable[[str], pd.Series | None] | None = None,
    volume_fn: Callable[[str], pd.Series | None] | None = None,
    flows_fn: Callable[[str], pd.DataFrame | None] | None = None,
    asof: str | None = None,
) -> dict[str, Any]:
    """Assemble the full rotation tensor measurement.

    Parameters
    ----------
    series_fn : injected close-price reader (default: production yahoo parquet reader)
    volume_fn : injected volume reader (default: production yahoo parquet reader)
    flows_fn  : injected ETF-flows reader (default: production flows parquet reader)
    asof      : if given, all series are truncated to this date (YYYY-MM-DD); enables replay.

    Returns the full artifact dict (schema per rotation_spec.md §2).
    Never raises — degrades to minimal artifact on any unexpected error.
    """
    sfn = series_fn if series_fn is not None else _default_series_fn
    vfn = volume_fn if volume_fn is not None else _default_volume_fn
    ffn = flows_fn if flows_fn is not None else _default_flows_fn

    tickers = list(UNIVERSE)

    # (a) RS-velocity
    rs_vel = _compute_rs_velocity(tickers, _BENCHMARK, sfn, asof=asof)

    # (b) Breadth migration
    breadth = _compute_breadth_migration(tickers, asof=asof)

    # (c) Leadership churn
    churn = _compute_leadership_churn(tickers, sfn, _BENCHMARK, asof=asof)

    # (d) Flow proxies
    flow = _compute_flow_proxies(tickers, vfn, ffn, asof=asof)

    # Refine distribution_flag with acceleration context (high rvol + negative accel = distribution).
    accel = rs_vel["accel_bps_per_day"]
    dist_flag: dict[str, bool] = {}
    for tk in tickers:
        rvol_z_val = flow["rvol_z"].get(tk)
        accel_val = accel.get(tk) if isinstance(accel, dict) else None
        if rvol_z_val is not None and rvol_z_val > 1.0 and accel_val is not None and accel_val < 0:
            dist_flag[tk] = True
        else:
            dist_flag[tk] = False
    flow["distribution_flag"] = dist_flag

    # (e) Episodes (needs rs_vel for top_pairs)
    top_pairs = rs_vel.get("top_pairs") or []
    ep = _compute_episodes(tickers, sfn, _BENCHMARK, top_pairs, asof=asof)
    headline_ep = ep.get("headline_episode")
    all_episodes = ep.get("episodes") or []

    # Compute agreement score for headline episode (how many blocks confirm).
    if headline_ep:
        n_agree = 0
        # Block (a): does top_pairs contain the headline axis direction?
        if top_pairs and headline_ep.get("direction") == "defensive":
            # Check if a defensive instrument leads in top_pairs.
            for tp in top_pairs[:3]:
                if tp.get("lead") in _DEF_BLOC:
                    n_agree += 1
                    break
        elif top_pairs and headline_ep.get("direction") == "offensive":
            for tp in top_pairs[:3]:
                if tp.get("lead") in _OFF_BLOC + ("SMH",):
                    n_agree += 1
                    break
        # Block (b): breadth confirms if pct50_5d_delta for DEF sectors is positive.
        if breadth.get("status") == "ok":
            pct50 = breadth.get("pct50_5d_delta") or {}
            if headline_ep.get("direction") == "defensive":
                def_vals = [pct50.get(tk) for tk in _DEF_BLOC if pct50.get(tk) is not None]
                if def_vals and np.mean(def_vals) > 0:
                    n_agree += 1
            elif headline_ep.get("direction") == "offensive":
                off_vals = [pct50.get(tk) for tk in _OFF_BLOC if pct50.get(tk) is not None]
                if off_vals and np.mean(off_vals) > 0:
                    n_agree += 1
        # Block (c): churn confirms if direction == "defensive" and churn10 > 0.25.
        if churn.get("churn10") is not None and churn["churn10"] > 0.25:
            n_agree += 1
        # Block (d): distribution_flag fires on offensive instruments.
        if headline_ep.get("direction") == "defensive":
            off_dist = [dist_flag.get(tk) for tk in _OFF_BLOC]
            if any(off_dist):
                n_agree += 1
        headline_ep["agreement"] = f"{n_agree}of5_blocks"

    # Determine as_of (true data date = min(asOf) of every input plane).
    asof_sources: list[str] = []
    if rs_vel.get("as_of"):
        asof_sources.append(rs_vel["as_of"])
    if flow.get("asof_flows"):
        asof_sources.append(flow["asof_flows"])

    # Determine breadth asof from closes cache.
    breadth_asof_str: str | None = None
    breadth_dir = _ROOT / "vendor" / "macro" / "data" / "breadth"
    closes_path = breadth_dir / "_closes_cache.parquet"
    if closes_path.exists():
        try:
            closes_idx = pd.read_parquet(closes_path, columns=[]).index.sort_values()
            if asof:
                closes_idx = closes_idx[:asof]
            if not closes_idx.empty:
                breadth_asof_str = closes_idx[-1].strftime("%Y-%m-%d")
                asof_sources.append(breadth_asof_str)
        except Exception:  # noqa: BLE001
            pass

    true_asof = min(asof_sources) if asof_sources else (asof or datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"))

    # Staleness: if true_asof more than 5 trading days behind today.
    stale = False
    try:
        ta_date = date.fromisoformat(true_asof)
        today = datetime.now(tz=timezone.utc).date()
        # Simple calendar-day check (5 business days ≈ 7 calendar days).
        if (today - ta_date).days > 7:
            stale = True
    except Exception:  # noqa: BLE001
        pass

    # Flows age.
    flows_age_td: int | None = None
    degraded_planes: list[str] = []
    if flow.get("asof_flows"):
        try:
            fdate = date.fromisoformat(flow["asof_flows"])
            today = datetime.now(tz=timezone.utc).date()
            flows_age_td = (today - fdate).days
            if flows_age_td > _FLOWS_MAX_AGE_TD:
                degraded_planes.append(f"flows({flows_age_td}td)")
        except Exception:  # noqa: BLE001
            pass
    if breadth.get("status") == "unavailable":
        degraded_planes.append("breadth")

    # Confidence.
    confidence = _compute_confidence(
        n_instruments=rs_vel.get("n_instruments", 0),
        breadth_ok=(breadth.get("status") == "ok"),
        flows_asof=flow.get("asof_flows"),
        n_episode_blocks=len([e for e in all_episodes if e is not None]),
    )

    asof_by_plane: dict[str, str] = {"yahoo": rs_vel.get("as_of") or "unknown"}
    if flow.get("asof_flows"):
        asof_by_plane["flows"] = flow["asof_flows"]
    if breadth_asof_str:
        asof_by_plane["breadth"] = breadth_asof_str

    artifact = {
        "schema_version": 1,
        "as_of": true_asof,
        "asof_by_plane": asof_by_plane,
        "freshness": {
            "stale": stale,
            "max_age_td": flows_age_td,
            "degraded_planes": degraded_planes,
        },
        "confidence": confidence,
        "universe": tickers,
        "rs_velocity": {
            "level_bps_per_day": rs_vel["level_bps_per_day"],
            "accel_bps_per_day": rs_vel["accel_bps_per_day"],
            "pair_R": rs_vel["pair_R"],
            "pair_dR": rs_vel["pair_dR"],
            "top_pairs": rs_vel["top_pairs"],
        },
        "breadth_migration": breadth,
        "leadership_churn": churn,
        "flow": flow,
        "headline_episode": headline_ep,
        "episodes": all_episodes,
        "advisory": True,  # flips to validated:true ONLY after §3 gate passes (P3)
    }

    return artifact


# ---------------------------------------------------------------------------
# write_artifact() — atomic output
# ---------------------------------------------------------------------------

def write_artifact(artifact: dict[str, Any]) -> Path:
    """Atomically write the artifact to data/market_view/rotation_tensor.json.

    Uses tmp-file + os.replace for atomicity (P2 degrade-never-corrupt).
    Returns the artifact path.
    """
    _ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _ARTIFACT_DIR / "rotation_tensor.json"
    payload = json.dumps(artifact, indent=2, default=str)
    # Write to a sibling tmp file and atomically replace.
    fd, tmp_path = tempfile.mkstemp(dir=_ARTIFACT_DIR, prefix=".rotation_tensor_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
        os.replace(tmp_path, out_path)
    except Exception:  # noqa: BLE001
        try:
            os.unlink(tmp_path)
        except Exception:  # noqa: BLE001
            pass
        raise
    return out_path


# ---------------------------------------------------------------------------
# CLI entry point (for the 22:40 build pipeline)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    asof_arg = sys.argv[1] if len(sys.argv) > 1 else None
    art = assemble(asof=asof_arg)
    out = write_artifact(art)
    print(f"rotation_tensor written: {out}  as_of={art['as_of']}  confidence={art['confidence']}")

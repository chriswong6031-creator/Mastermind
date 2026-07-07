"""Portfolio held-risk lane composer — W2 build.

Compose existing nightly Macro Dashboard artifacts into per-lane risk states
(ok / watch / elevated / coverage_missing / stale) and a deterministic role
ladder for each held position.

PRD laws enforced:
  PRD-R2: no fused score — aggregates are elevated_lanes count + role only.
  PRD-R3: pure deterministic functions, no LLM anywhere.
  PRD-R4: review language only; no advice verbs; "validated" never appears.
  PRD-R6: coverage degraded to price_only / partial when artifacts absent.
  PRD-R7: positions/prices never logged or committed.
  PRD-R11: MFE/giveback thresholds are heuristic v0.

Public API:
  compose(positions, *, vendor_root, data_root, now, price_loader) -> dict
  write_state(state, path) -> None

Field-name deviations from spec (real data vs brief):
  - No `ext` block → extension computed from OHLCV or view.evidence.extension.value
  - No `event_windows` → earnings from stockdata.earnings.next_date; no FOMC calendar
  - No `expectation_state` → sue_z from earnings.sue_z; streak from earnings.summary.streak
  - No `leverage_ratios` → interest_coverage derived from financials.raw (ni/debt_lt proxy)
  - No `capital_allocation` → repurchases flag from financials.raw
  - No `moat_falsifiers` → absent (counted as 0)
  - No `beneficial_ownership` → smart_money block used for 13D info-flag context
  - No `personality` → archetype from profile.archetype.key
  - No `sector_rs` dict by sector name → list of ETF tickers; mapped via SECTOR_ETF_MAP
  - No `short_ratio_z` → factors.legs.short_interest z-score used instead
  - No dilution_events.parquet → skip with coverage_missing for that sub-check
  - market_state not a separate file → risk_state inside regime/latest.json
  - OHLCV parquet at data/yahoo/<TICKER>.parquet (columns: close_price, close, volume)
  - insider cluster from positioning.insider (n_buyers, n_sellers, net_usd_mn)
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_VENDOR_MACRO = _REPO_ROOT / "vendor" / "macro"
_VENDOR_MACRO_SRC = _REPO_ROOT / "vendor" / "macro_src"
VENDOR_DEFAULT = _VENDOR_MACRO if _VENDOR_MACRO.exists() else _VENDOR_MACRO_SRC
DATA_DEFAULT = _REPO_ROOT / "data"
_OUT_DEFAULT = DATA_DEFAULT / "portfolio_watch" / "risk_state.json"

# ---------------------------------------------------------------------------
# Sector → sector-RS ETF mapping (GICS sectors to ETF tickers in regime data)
# ---------------------------------------------------------------------------
SECTOR_ETF_MAP: dict[str, str] = {
    "Information Technology": "XLK",
    "Health Care": "XLV",
    "Financials": "XLF",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
    "Energy": "XLE",
    "Communication Services": "XLC",
}

# ---------------------------------------------------------------------------
# Lane thresholds (heuristic v0 — PRD-R11; first-match role ladder)
# ---------------------------------------------------------------------------
_STALE_SESSIONS = 3      # sessions; artifact older than this → stale
_EXT_PARABOLIC_GRADE = "parabolic"
_EXT_STRETCHED_GRADE = "stretched"
_MFE_GIVEBACK_ELEVATED_MFE = 0.20      # 20%
_MFE_GIVEBACK_ELEVATED_GB = 0.35       # 35% of MFE
_MFE_GIVEBACK_CRITICAL_MFE = 0.25
_MFE_GIVEBACK_CRITICAL_GB = 0.60
_EARNINGS_ELEVATED_TDAYS = 3
_EARNINGS_WATCH_TDAYS = 7
_FOMC_WATCH_TDAYS = 2
_SUE_STREAK_ELEVATED = -2              # consecutive miss count ≤ this
_PEAD_DRIFT_ELEVATED = -0.03           # −3%
_SUE_Z_SINGLE_MISS = 0.0              # sue_z < 0 counts as miss
_INTEREST_COVERAGE_WATCH = 2.0
_INTEREST_COVERAGE_CRITICAL = 1.0
_NET_DEBT_EBITDA_FLAG = 4.0
_ALTMAN_Z_THRESHOLD = 1.8
_PIOTROSKI_FLAG = 3
_SHORT_INT_Z_FLAG = 2.0
_INSIDER_SELLER_MIN = 3
_RATE_BETA_HIGH_ABS = 0.3
_SECTOR_RS_BOTTOM_N = 4               # bottom N ETFs in the ranked list

# Role ladder labels (PRD-R4)
_ROLE_LABELS = {
    "exit_review": "Exit review",
    "trim_review": "Take-profit review",
    "tighten": "Tighten",
    "review": "Review",
    "monitor": "Monitor",
    "info": "Ok",
    "ok": "Ok",
}

_ALL_LANE_NAMES = [
    "price_trend", "extension_giveback", "event_window", "earnings_expectation",
    "solvency_dilution", "ownership_flow", "macro_sensitivity", "sector_rotation",
    "market_regime", "data_quality",
]
_EVIDENCE_LANES = [
    "price_trend", "extension_giveback", "event_window", "earnings_expectation",
    "solvency_dilution", "ownership_flow", "macro_sensitivity", "sector_rotation",
]


# ===========================================================================
# Utility helpers
# ===========================================================================

def _trading_days_between(start: date, end: date) -> int:
    """Approximate count of NYSE trading sessions in (start, end] exclusive of start."""
    from portfolio.market_calendar import is_trading_day
    count = 0
    cur = start + timedelta(days=1)
    while cur <= end:
        if is_trading_day(cur):
            count += 1
        cur += timedelta(days=1)
    return count


def _is_stale(asof_str: str | None, run_date: date) -> bool:
    """Return True if asof is more than _STALE_SESSIONS trading sessions before run_date."""
    if not asof_str:
        return True
    try:
        asof = date.fromisoformat(asof_str[:10])
    except ValueError:
        return True
    return _trading_days_between(asof, run_date) > _STALE_SESSIONS


def _lane(
    state: str,
    flags: list[str] | None = None,
    reasons: list[str] | None = None,
    asof: str | None = None,
) -> dict:
    return {
        "state": state,
        "flags": flags or [],
        "reasons": reasons or [],
        "asof": asof,
    }


def _missing_lane(name: str) -> dict:
    return _lane("coverage_missing", reasons=[f"{name} source block absent"])


def _stale_lane(asof: str | None) -> dict:
    return _lane("stale", reasons=["artifact asof > 3 sessions old"], asof=asof)


# ===========================================================================
# Data loaders
# ===========================================================================

def _load_stockdata(ticker: str, vendor_root: Path) -> dict | None:
    p = vendor_root / "site" / "stockdata" / f"{ticker}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _load_ohlcv(
    ticker: str,
    vendor_root: Path,
    price_loader=None,
    data_root: Path | None = None,
) -> pd.DataFrame | None:
    """Load OHLCV DataFrame with DatetimeIndex and 'close' column.

    Three-tier loader:
      Tier 1: vendor data/stocks/<T>.parquet
      Tier 2: vendor data/yahoo/<T>.parquet
      Tier 3: yfinance fetch (~420d), cached at data/portfolio_watch/ohlcv/<T>.parquet
               (refresh when >1 session old, ~28h)

    price_loader signature: (ticker) -> pd.DataFrame | None (used in tests to skip network).
    """
    if price_loader is not None:
        return price_loader(ticker)

    # Tier 1 + 2: vendor parquets
    for parquet_path in [
        vendor_root / "data" / "stocks" / f"{ticker}.parquet",
        vendor_root / "data" / "yahoo" / f"{ticker}.parquet",
    ]:
        if parquet_path.exists():
            try:
                df = pd.read_parquet(parquet_path)
                close_col = "close" if "close" in df.columns else (
                    "close_price" if "close_price" in df.columns else None
                )
                if close_col:
                    df = df[[close_col]].rename(columns={close_col: "close"})
                    return df
            except Exception:
                pass

    # Tier 3: yfinance with local cache
    cache_dir = (data_root / "portfolio_watch" / "ohlcv") if data_root else None
    cache_path = (cache_dir / f"{ticker}.parquet") if cache_dir else None

    if cache_path and cache_path.exists():
        try:
            from datetime import datetime as _dt
            age_hours = (_dt.now().timestamp() - cache_path.stat().st_mtime) / 3600
            if age_hours < 28:  # < 28h = fresh enough (1 session)
                df = pd.read_parquet(cache_path)
                if "close" in df.columns:
                    return df
        except Exception:
            pass

    try:
        import yfinance as yf  # type: ignore[import]
        tk = yf.Ticker(ticker)
        raw = tk.history(period="420d", auto_adjust=True)
        if raw.empty:
            return None
        df = raw[["Close"]].rename(columns={"Close": "close"})
        df.index = pd.to_datetime(df.index).tz_localize(None)
        if cache_path:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                df.to_parquet(cache_path)
            except Exception:
                pass
        return df
    except Exception:
        return None


def _load_regime(vendor_root: Path) -> dict:
    p = vendor_root / "data" / "regime" / "latest.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _load_insider_signals(vendor_root: Path) -> dict:
    p = vendor_root / "site" / "factordata" / "insider_signals.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


# ===========================================================================
# OHLCV-derived metrics
# ===========================================================================

def _compute_price_metrics(df: pd.DataFrame, spy_df: pd.DataFrame | None = None) -> dict:
    """Return dict with ma20, ma50, ma200, atr14, rs20_spy_z, high_52w, close, close_date."""
    if df is None or len(df) < 20:
        return {}

    closes = df["close"].dropna()
    if len(closes) < 20:
        return {}

    close = float(closes.iloc[-1])
    close_date = closes.index[-1].date() if hasattr(closes.index[-1], "date") else None

    ma20 = float(closes.iloc[-20:].mean()) if len(closes) >= 20 else None
    ma50 = float(closes.iloc[-50:].mean()) if len(closes) >= 50 else None
    ma200 = float(closes.iloc[-200:].mean()) if len(closes) >= 200 else None

    # ATR14 (high/low if available, else close-to-close proxy)
    atr14 = None
    if "high" in df.columns and "low" in df.columns:
        hi = df["high"].dropna()
        lo = df["low"].dropna()
        if len(hi) >= 14 and len(lo) >= 14:
            tr = (hi - lo).abs().iloc[-14:]
            atr14 = float(tr.mean())
    else:
        # close-to-close approximation
        if len(closes) >= 15:
            day_ranges = closes.iloc[-15:].diff().abs().dropna()
            atr14 = float(day_ranges.mean()) if len(day_ranges) >= 14 else None

    # 52-week high
    high_52w = float(closes.iloc[-252:].max()) if len(closes) >= 1 else None

    # 20-day lower-low: last close below every prior close in window
    lower_low_20d = False
    if len(closes) >= 20:
        window = closes.iloc[-20:]
        lower_low_20d = bool(close < float(window.iloc[:-1].min()))

    # RS20 vs SPY z-score
    rs20_spy_z = None
    if spy_df is not None and len(spy_df) >= 252:
        spy_closes = spy_df["close"].dropna()
        # Align
        combined = pd.concat(
            [closes.rename("stk"), spy_closes.rename("spy")], axis=1
        ).dropna()
        if len(combined) >= 252:
            rs_series = combined["stk"] / combined["spy"]
            rs20_vals = rs_series.rolling(20).mean().dropna()
            if len(rs20_vals) >= 252:
                mu = float(rs20_vals.iloc[-252:].mean())
                sigma = float(rs20_vals.iloc[-252:].std())
                if sigma > 0:
                    rs20_spy_z = float((rs20_vals.iloc[-1] - mu) / sigma)

    # ext_z: (close/ma200 - 1) z-score vs own 252d
    ext_z = None
    if ma200 and len(closes) >= 252:
        ma200_series = closes.rolling(200).mean()
        ratio = (closes / ma200_series - 1).dropna()
        if len(ratio) >= 252:
            mu = float(ratio.iloc[-252:].mean())
            sigma = float(ratio.iloc[-252:].std())
            if sigma > 0:
                ext_z = float((ratio.iloc[-1] - mu) / sigma)

    return {
        "close": close,
        "close_date": str(close_date) if close_date else None,
        "ma20": ma20,
        "ma50": ma50,
        "ma200": ma200,
        "atr14": atr14,
        "high_52w": high_52w,
        "rs20_spy_z": rs20_spy_z,
        "ext_z": ext_z,
        "lower_low_20d": lower_low_20d,
    }


# ===========================================================================
# Extension grade helpers
# ===========================================================================

def _ext_grade_from_stockdata(sd: dict) -> str | None:
    """Derive extension grade from stockdata view.evidence.extension or tech fields."""
    # Try view.evidence.extension.value ("In-trend", "Stretched", "Parabolic", etc.)
    try:
        val = sd["view"]["evidence"]["extension"]["value"].lower()
        if "parabolic" in val:
            return "parabolic"
        if "stretched" in val or "extended" in val:
            return "stretched"
        if "in-trend" in val or "in trend" in val:
            return "intrend"
        if "steady" in val:
            return "steady"
    except (KeyError, TypeError, AttributeError):
        pass
    # Fallback to pct_vs_200dma from tech block
    try:
        pct = sd["tech"]["pct_vs_200dma"]
        if pct is not None:
            if pct >= 40:
                return "parabolic"
            if pct >= 20:
                return "stretched"
            return "intrend"
    except (KeyError, TypeError):
        pass
    return None


def _ext_grade_from_ext_z(ext_z: float | None) -> str | None:
    if ext_z is None:
        return None
    if ext_z >= 2.0:
        return "parabolic"
    if ext_z >= 1.0:
        return "stretched"
    return "intrend"


# ===========================================================================
# LANE 1: price_trend
# ===========================================================================

def _lane_price_trend(
    metrics: dict,
    sd: dict | None,
    run_date: date,
) -> dict:
    """Elevated: close < MA50 AND (rs20_spy_z <= -1 OR close < MA200)."""
    if not metrics:
        return _missing_lane("price_trend")

    close = metrics.get("close")
    ma20 = metrics.get("ma20")
    ma50 = metrics.get("ma50")
    ma200 = metrics.get("ma200")
    rs20_z = metrics.get("rs20_spy_z")
    high_52w = metrics.get("high_52w")
    atr14 = metrics.get("atr14")
    lower_low_20d = metrics.get("lower_low_20d", False)
    close_date = metrics.get("close_date")

    if close is None:
        return _missing_lane("price_trend")

    flags: list[str] = []
    reasons: list[str] = []

    # MA breaks
    if ma20 and close < ma20:
        flags.append("below_ma20")
        reasons.append(f"close {close:.2f} below MA20 {ma20:.2f}")
    if ma50 and close < ma50:
        flags.append("below_ma50")
        reasons.append(f"close {close:.2f} below MA50 {ma50:.2f}")
    if ma200 and close < ma200:
        flags.append("below_ma200")
        reasons.append(f"close {close:.2f} below MA200 {ma200:.2f}")

    # RS z
    if rs20_z is not None and rs20_z <= -1.0:
        flags.append("rs_weak")
        reasons.append(f"RS-vs-SPY 20d z {rs20_z:.2f} <= -1.0")

    # ATR z (relative to atr14 being large vs typical — simple proxy: atr/close > 4%)
    if atr14 and close and atr14 / close > 0.04:
        flags.append("atr_elevated")
        reasons.append(f"ATR14 {atr14:.2f} ({atr14/close*100:.1f}% of close)")

    # 20d lower-low
    if lower_low_20d:
        flags.append("lower_low_20d")
        reasons.append("close below 20d prior-low")

    # Drawdown from 52w high
    if high_52w and close:
        dd = (close - high_52w) / high_52w
        if dd <= -0.15:
            flags.append(f"drawdown_52w_{abs(int(dd*100))}pct")
            reasons.append(f"drawdown from 52w-high {dd*100:.1f}%")

    # Elevated: close < MA50 AND (rs20_z <= -1 OR close < MA200)
    below_ma50 = bool(ma50 and close < ma50)
    rs_weak = rs20_z is not None and rs20_z <= -1.0
    below_ma200 = bool(ma200 and close < ma200)

    if below_ma50 and (rs_weak or below_ma200):
        state = "elevated"
    elif flags:
        state = "watch"
    else:
        state = "ok"

    return _lane(state, flags, reasons, asof=close_date)


# ===========================================================================
# LANE 2: extension_giveback
# ===========================================================================

def _lane_extension_giveback(
    metrics: dict,
    sd: dict | None,
    position: dict,
    *,
    ohlcv_df: pd.DataFrame | None = None,
) -> tuple[dict, dict]:
    """Returns (lane_dict, path_dict).

    Elevated: ext grade parabolic OR (mfe >= 20% AND giveback >= 35% of mfe).
    Critical flag: giveback >= 60% of mfe >= 25%.

    When position["post_entry_high"] is absent, derives it from ohlcv_df
    between entry_date and now.
    """
    close = metrics.get("close")
    close_date = metrics.get("close_date")
    ma20 = metrics.get("ma20")
    ma50 = metrics.get("ma50")
    ma200 = metrics.get("ma200")
    atr14 = metrics.get("atr14")
    high_52w = metrics.get("high_52w")
    rs20_z = metrics.get("rs20_spy_z")
    ext_z = metrics.get("ext_z")

    # Determine extension grade
    ext_grade = None
    if sd is not None:
        ext_grade = _ext_grade_from_stockdata(sd)
    if ext_grade is None:
        ext_grade = _ext_grade_from_ext_z(ext_z)

    # Entry-path metrics
    entry_price = position.get("entry_price")
    entry_date_str = position.get("entry_date")

    post_entry_high = None
    mfe_pct = None
    mae_pct = None
    giveback_pct = None

    if entry_price and close:
        entry_price = float(entry_price)
        if "post_entry_high" in position and position["post_entry_high"]:
            post_entry_high = float(position["post_entry_high"])
            mfe_pct = (post_entry_high - entry_price) / entry_price if entry_price > 0 else None
            mae_pct = None
            if mfe_pct and mfe_pct > 0:
                giveback_pct = (post_entry_high - close) / (post_entry_high - entry_price)
        elif ohlcv_df is not None and entry_date_str:
            # Derive from price history since entry_date
            try:
                ed = pd.Timestamp(str(entry_date_str)[:10])
                window = ohlcv_df[ohlcv_df.index >= ed]["close"].dropna()
                if len(window) > 0:
                    post_entry_high = float(window.max())
                    mae_low = float(window.min())
                    mfe_pct = (post_entry_high - entry_price) / entry_price if entry_price > 0 else None
                    mae_pct = (mae_low - entry_price) / entry_price if entry_price > 0 else None
                    if mfe_pct and mfe_pct > 0 and post_entry_high > entry_price:
                        giveback_pct = (post_entry_high - close) / (post_entry_high - entry_price)
            except Exception:
                pass

    flags: list[str] = []
    reasons: list[str] = []

    parabolic = ext_grade == _EXT_PARABOLIC_GRADE
    stretched = ext_grade in (_EXT_PARABOLIC_GRADE, _EXT_STRETCHED_GRADE)

    if parabolic:
        flags.append("parabolic")
        reasons.append(f"extension grade: parabolic")

    elif stretched and ext_grade != _EXT_PARABOLIC_GRADE:
        flags.append("stretched")
        reasons.append("extension grade: stretched")

    # Giveback flags
    critical_giveback = False
    if mfe_pct is not None and giveback_pct is not None:
        if mfe_pct >= _MFE_GIVEBACK_ELEVATED_MFE and giveback_pct >= _MFE_GIVEBACK_ELEVATED_GB:
            flags.append("giveback_elevated")
            reasons.append(
                f"MFE {mfe_pct*100:.1f}% >= 20%, giveback {giveback_pct*100:.1f}% >= 35% of MFE"
            )
        if mfe_pct >= _MFE_GIVEBACK_CRITICAL_MFE and giveback_pct >= _MFE_GIVEBACK_CRITICAL_GB:
            flags.append("critical_giveback")
            reasons.append(
                f"critical: MFE {mfe_pct*100:.1f}% >= 25%, giveback {giveback_pct*100:.1f}% >= 60% of MFE"
            )
            critical_giveback = True

    # State
    if parabolic or (mfe_pct and mfe_pct >= _MFE_GIVEBACK_ELEVATED_MFE
                     and giveback_pct and giveback_pct >= _MFE_GIVEBACK_ELEVATED_GB):
        state = "elevated"
    elif flags:
        state = "watch"
    elif sd is None and metrics:
        state = "ok"
    else:
        state = "ok"

    path = {
        "entry_date": entry_date_str,
        "entry_price": float(entry_price) if entry_price else None,
        "ref_close": float(close) if close else None,
        "ref_date": close_date,
        "post_entry_high": post_entry_high,
        "mfe_pct": round(mfe_pct, 4) if mfe_pct is not None else None,
        "mae_pct": mae_pct,
        "giveback_pct_of_mfe": round(giveback_pct, 4) if giveback_pct is not None else None,
        "ma20": round(ma20, 4) if ma20 else None,
        "ma50": round(ma50, 4) if ma50 else None,
        "ma200": round(ma200, 4) if ma200 else None,
        "atr14": round(atr14, 4) if atr14 else None,
        "high_52w": round(high_52w, 4) if high_52w else None,
        "rs20_spy_z": round(rs20_z, 4) if rs20_z is not None else None,
        "ext_grade": ext_grade,
    }

    return _lane(state, flags, reasons, asof=close_date), path


# ===========================================================================
# LANE 3: event_window
# ===========================================================================

def _lane_event_window(
    sd: dict | None,
    run_date: date,
) -> tuple[dict, dict]:
    """Returns (lane_dict, events_dict).

    Elevated: earnings tdays <= 3.
    Watch: tdays <= 7.
    Flags: fomc <= 2d (no FOMC calendar available — always omit), debt_pressure.
    """
    if sd is None:
        return _missing_lane("event_window"), {"earnings_tdays": None, "earnings_date": None, "fomc_tdays": None}

    flags: list[str] = []
    reasons: list[str] = []

    # Earnings countdown — prefer fresh R2 schema event_windows.earnings.tdays
    earnings_tdays = None
    earnings_date_str = None
    fomc_tdays = None

    # Fresh R2 schema: event_windows block
    try:
        ew_block = sd.get("event_windows") or {}
        earn_block = ew_block.get("earnings") or {}
        if earn_block.get("tdays") is not None:
            earnings_tdays = int(earn_block["tdays"])
            earnings_date_str = earn_block.get("date")
        # fomc_within
        fomc_block = ew_block.get("fomc_within") or {}
        fomc_tdays_val = fomc_block.get("tdays")
        if fomc_tdays_val is not None:
            fomc_tdays = int(fomc_tdays_val)
            if fomc_tdays <= _FOMC_WATCH_TDAYS:
                flags.append("fomc_within_2d")
                reasons.append(f"FOMC within {fomc_tdays} trading session(s)")
        # debt pressure from fresh schema (current_debt > cash)
        debt_block = ew_block.get("debt") or {}
        cd_val = debt_block.get("current_debt")
        cash_val = debt_block.get("cash")
        if cd_val is not None and cash_val is not None:
            try:
                if float(cd_val) > float(cash_val):
                    flags.append("debt_pressure")
                    reasons.append(
                        f"current_debt {float(cd_val)/1e9:.1f}B > cash {float(cash_val)/1e9:.1f}B"
                    )
            except (TypeError, ValueError):
                pass
    except (KeyError, TypeError, ValueError):
        pass

    # Fallback: legacy schema earnings.next_date
    if earnings_tdays is None:
        try:
            next_date_str = sd["earnings"]["next_date"]
            if next_date_str:
                earnings_date_str = next_date_str
                next_date = date.fromisoformat(next_date_str[:10])
                if next_date >= run_date:
                    earnings_tdays = _trading_days_between(run_date, next_date)
                else:
                    earnings_tdays = None  # in the past
        except (KeyError, TypeError, ValueError):
            pass

    if earnings_tdays is not None:
        if earnings_tdays <= _EARNINGS_ELEVATED_TDAYS:
            flags.append(f"earnings_t{earnings_tdays}")
            reasons.append(f"earnings {earnings_tdays} trading session(s) away ({earnings_date_str})")
        elif earnings_tdays <= _EARNINGS_WATCH_TDAYS:
            flags.append(f"earnings_t{earnings_tdays}")
            reasons.append(f"earnings {earnings_tdays} trading sessions away ({earnings_date_str})")

    # Debt pressure fallback: use financials.raw proxy (when event_windows.debt absent)
    if "debt_pressure" not in flags:
        try:
            raw = sd["financials"]["raw"]
            debt_lt = raw.get("debt_lt")
            cfo = raw.get("cfo")
            if debt_lt and cfo and debt_lt > 0 and cfo < debt_lt * 0.15:
                flags.append("debt_pressure")
                reasons.append(
                    f"long-term debt {debt_lt/1e9:.1f}B > CFO-coverage threshold ({cfo/1e9:.1f}B CFO)"
                )
        except (KeyError, TypeError):
            pass

    # State
    if earnings_tdays is not None and earnings_tdays <= _EARNINGS_ELEVATED_TDAYS:
        state = "elevated"
    elif flags:
        state = "watch"
    else:
        state = "ok"

    sd_asof = sd.get("asof")
    if _is_stale(sd_asof, run_date):
        return _stale_lane(sd_asof), {"earnings_tdays": earnings_tdays, "earnings_date": earnings_date_str, "fomc_tdays": fomc_tdays}

    return (
        _lane(state, flags, reasons, asof=sd_asof),
        {"earnings_tdays": earnings_tdays, "earnings_date": earnings_date_str, "fomc_tdays": fomc_tdays},
    )


# ===========================================================================
# LANE 4: earnings_expectation
# ===========================================================================

def _lane_earnings_expectation(sd: dict | None, run_date: date) -> dict:
    """Elevated: sue_streak <= -2 (consecutive misses) OR (sue_z < 0 AND pead_drift proxy negative).

    Real data mapping:
      - earnings.sue_z (float, positive=beat, negative=miss)
      - earnings.summary.streak = consecutive beats count (positive = beats, not misses)
      - earnings.summary.beats / total for deriving miss streak
      - revisions.est_chg_30d for revision direction
    """
    if sd is None:
        return _missing_lane("earnings_expectation")

    sd_asof = sd.get("asof")
    if _is_stale(sd_asof, run_date):
        return _stale_lane(sd_asof)

    flags: list[str] = []
    reasons: list[str] = []

    sue_z = None
    sue_streak = None  # consecutive miss count (negative = misses)

    # Fresh R2 schema: expectation_state block
    try:
        es = sd.get("expectation_state") or {}
        if es:
            sue_streak_fresh = es.get("sue_streak")
            sue_latest = es.get("sue_latest")
            pead_drift = es.get("pead_drift_20d")
            if sue_streak_fresh is not None:
                sue_streak = int(sue_streak_fresh)
            if sue_latest is not None:
                sue_z = float(sue_latest)
            # Direct PEAD miss flag from fresh schema
            if (sue_latest is not None and pead_drift is not None
                    and float(sue_latest) < 0 and float(pead_drift) <= _PEAD_DRIFT_ELEVATED):
                flags.append("sue_miss_pead_drift")
                reasons.append(
                    f"sue_latest {float(sue_latest):.2f} < 0 AND pead_drift_20d "
                    f"{float(pead_drift)*100:.1f}% <= -3%"
                )
    except (KeyError, TypeError, ValueError):
        pass

    # Fallback: legacy earnings block
    if sue_streak is None and sue_z is None:
        try:
            sue_z = sd["earnings"].get("sue_z")
            summ = sd["earnings"].get("summary", {})
            beat_streak = summ.get("streak", 0)
            total = summ.get("total", 0)
            beats = summ.get("beats", 0)
            misses = total - beats if total else 0
            if beat_streak == 0 and misses >= 2:
                sue_streak = -misses
            elif beat_streak > 0:
                sue_streak = beat_streak
        except (KeyError, TypeError):
            pass

    # Revisions direction
    rev_down = False
    try:
        est_chg_30d = sd["revisions"].get("est_chg_30d")
        net_up_30d = sd["revisions"].get("net_up_30d")
        breadth = sd["revisions"].get("breadth")
        if est_chg_30d is not None and est_chg_30d < 0:
            rev_down = True
            flags.append("revisions_down")
            reasons.append(f"estimate change 30d: {est_chg_30d:+.2f}%")
        elif net_up_30d is not None and net_up_30d < 0:
            rev_down = True
            flags.append("revisions_down")
            reasons.append(f"net upgrades 30d: {net_up_30d:.0f}")
    except (KeyError, TypeError):
        pass

    elevated = False

    # Elevated: fresh schema pead_drift already set the flag above
    if "sue_miss_pead_drift" in flags:
        elevated = True

    # Elevated: sue_streak <= -2 (consecutive misses >= 2)
    elif sue_streak is not None and sue_streak <= _SUE_STREAK_ELEVATED:
        flags.append(f"sue_streak_{sue_streak}")
        reasons.append(f"earnings miss streak: {abs(sue_streak)} consecutive misses")
        elevated = True

    # Elevated: sue_z < 0 AND revisions are down (proxy for pead_drift_20d negative)
    elif sue_z is not None and sue_z < _SUE_Z_SINGLE_MISS and rev_down:
        flags.append("sue_miss_revisions_down")
        reasons.append(
            f"SUE z {sue_z:.2f} < 0 (recent miss) and revisions deteriorating"
        )
        elevated = True

    # Watch: single flag (sue_z < 0 alone, or revisions down alone)
    elif sue_z is not None and sue_z < _SUE_Z_SINGLE_MISS:
        flags.append(f"sue_z_{sue_z:.2f}")
        reasons.append(f"SUE z {sue_z:.2f} indicates recent miss")

    state = "elevated" if elevated else ("watch" if flags else "ok")
    return _lane(state, flags, reasons, asof=sd_asof)


# ===========================================================================
# LANE 5: solvency_dilution
# ===========================================================================

def _lane_solvency_dilution(sd: dict | None, run_date: date) -> dict:
    """Flags: {interest_coverage<2, net_debt/EBITDA>4, altman_z<1.8, piotroski<=3,
    dilutive capital allocation, dilution filing within 90d, >=2 moat falsifiers}.
    Elevated: >= 2 flags.
    Critical: coverage < 1, or (altman < 1.8 AND dilutive).

    Real data mapping:
      - financials.multiyear.altman.z  (Altman Z)
      - financials.multiyear.piotroski.score  (Piotroski F)
      - financials.raw: {ni, debt_lt, cfo} — derive interest_coverage proxy, net_debt
      - financials.raw.repurchases: if repurchases > 0 while debt_lt high → dilutive proxy
      - profile.sector: skip altman for Financials
      - moat_falsifiers: not in data → 0
      - dilution_events.parquet: not found → coverage_missing for that check
    """
    if sd is None:
        return _missing_lane("solvency_dilution")

    sd_asof = sd.get("asof")
    if _is_stale(sd_asof, run_date):
        return _stale_lane(sd_asof)

    flags: list[str] = []
    reasons: list[str] = []
    flag_count = 0
    critical = False

    sector = None
    try:
        sector = sd["profile"]["sector"]
    except (KeyError, TypeError):
        pass

    # Altman Z (skip for Financials) — prefer fresh R2 accounting_quality.altman_z
    altman_z = None
    altman_below = False
    try:
        aq = sd.get("accounting_quality") or {}
        altman_z_fresh = aq.get("altman_z")
        if altman_z_fresh is not None and sector != "Financials" and float(altman_z_fresh) < _ALTMAN_Z_THRESHOLD:
            altman_z = float(altman_z_fresh)
            altman_below = True
            flags.append(f"altman_z_{altman_z:.1f}")
            reasons.append(f"Altman Z {altman_z:.1f} < {_ALTMAN_Z_THRESHOLD} (distress zone)")
            flag_count += 1
    except (KeyError, TypeError, ValueError):
        pass

    # Altman Z fallback: financials.multiyear.altman.z
    if not altman_below:
        try:
            altman_z = sd["financials"]["multiyear"]["altman"]["z"]
            if sector != "Financials" and altman_z is not None and altman_z < _ALTMAN_Z_THRESHOLD:
                flags.append(f"altman_z_{altman_z:.1f}")
                reasons.append(f"Altman Z {altman_z:.1f} < {_ALTMAN_Z_THRESHOLD} (distress zone)")
                flag_count += 1
                altman_below = True
        except (KeyError, TypeError):
            pass

    # Piotroski F — prefer fresh R2 accounting_quality.piotroski
    piotroski = None
    try:
        aq = sd.get("accounting_quality") or {}
        piotroski_fresh = aq.get("piotroski")
        if piotroski_fresh is not None and int(piotroski_fresh) <= _PIOTROSKI_FLAG:
            piotroski = int(piotroski_fresh)
            flags.append(f"piotroski_{piotroski}")
            reasons.append(f"Piotroski F-score {piotroski} <= {_PIOTROSKI_FLAG} (weak fundamentals)")
            flag_count += 1
    except (KeyError, TypeError, ValueError):
        pass

    # Piotroski F fallback: financials.multiyear.piotroski.score
    if piotroski is None:
        try:
            piotroski = sd["financials"]["multiyear"]["piotroski"]["score"]
            if piotroski is not None and piotroski <= _PIOTROSKI_FLAG:
                flags.append(f"piotroski_{piotroski}")
                reasons.append(f"Piotroski F-score {piotroski} <= {_PIOTROSKI_FLAG} (weak fundamentals)")
                flag_count += 1
        except (KeyError, TypeError):
            pass

    # Fresh R2: leverage_ratios.net_debt_to_ebitda and current_ratio
    try:
        lr = sd.get("leverage_ratios") or {}
        if lr:
            net_debt_ebitda_fresh = lr.get("net_debt_to_ebitda")
            current_ratio_fresh = lr.get("current_ratio")
            if net_debt_ebitda_fresh is not None and float(net_debt_ebitda_fresh) > _NET_DEBT_EBITDA_FLAG:
                nde = float(net_debt_ebitda_fresh)
                flags.append(f"net_debt_ebitda_{nde:.1f}")
                reasons.append(f"net_debt/EBITDA {nde:.1f} > {_NET_DEBT_EBITDA_FLAG}")
                flag_count += 1
                if nde > 6.0:
                    flags.append("net_debt_ebitda_critical")
            if current_ratio_fresh is not None and float(current_ratio_fresh) < 1.0:
                cr = float(current_ratio_fresh)
                flags.append(f"current_ratio_{cr:.2f}")
                reasons.append(f"current_ratio {cr:.2f} < 1.0 (liquidity concern)")
                flag_count += 1
    except (KeyError, TypeError, ValueError):
        pass

    # Interest coverage proxy (fallback when leverage_ratios not present)
    interest_coverage = None
    try:
        raw = sd["financials"]["raw"]
        debt_lt = raw.get("debt_lt") or 0
        ni = raw.get("ni") or 0
        cfo = raw.get("cfo") or 0
        if debt_lt and debt_lt > 0:
            assumed_interest = debt_lt * 0.05
            if assumed_interest > 0:
                interest_coverage = cfo / assumed_interest
                if interest_coverage < _INTEREST_COVERAGE_WATCH:
                    flags.append(f"interest_coverage_{interest_coverage:.1f}")
                    reasons.append(
                        f"interest coverage proxy {interest_coverage:.1f} < {_INTEREST_COVERAGE_WATCH}"
                        " (CFO vs 5% of LT-debt)"
                    )
                    flag_count += 1
                    if interest_coverage < _INTEREST_COVERAGE_CRITICAL:
                        critical = True
                        flags.append("critical_coverage")
                        reasons.append(
                            f"critical: interest coverage {interest_coverage:.1f} < {_INTEREST_COVERAGE_CRITICAL}"
                        )
    except (KeyError, TypeError):
        pass

    # Capital allocation: prefer fresh R2 capital_allocation.delta
    dilutive = False
    try:
        ca = sd.get("capital_allocation") or {}
        if ca.get("delta") == "dilutive":
            dilutive = True
            flags.append("dilutive_allocation")
            reasons.append("capital_allocation.delta = dilutive")
            flag_count += 1
    except (KeyError, TypeError):
        pass

    # Capital allocation fallback: debt-funded buybacks proxy
    if not dilutive:
        try:
            raw = sd["financials"]["raw"]
            debt_lt = raw.get("debt_lt") or 0
            repurchases = raw.get("repurchases") or 0
            ni = raw.get("ni") or 0
            equity = raw.get("equity") or 0
            if equity < 0 or (debt_lt > 0 and repurchases > (ni * 1.5) and debt_lt > ni * 2):
                dilutive = True
                flags.append("dilutive_allocation")
                reasons.append(
                    f"debt-funded buybacks proxy: repurchases {repurchases/1e9:.1f}B vs NI {ni/1e9:.1f}B, debt {debt_lt/1e9:.1f}B"
                )
                flag_count += 1
        except (KeyError, TypeError):
            pass

    # Fresh R2: moat_falsifiers (list of {firing: bool, ...})
    try:
        mf = sd.get("moat_falsifiers") or []
        firing = [m for m in mf if isinstance(m, dict) and m.get("firing")]
        if len(firing) >= 2:
            flags.append(f"moat_falsifiers_{len(firing)}")
            reasons.append(f"{len(firing)} moat falsifiers firing")
            flag_count += 1
    except (KeyError, TypeError):
        pass

    # Critical: altman < 1.8 AND dilutive
    if altman_below and dilutive:
        critical = True
        flags.append("critical_altman_dilutive")
        reasons.append("critical: Altman Z distressed AND dilutive capital allocation")

    # Critical: net_debt_ebitda > 6 AND dilutive
    if any("net_debt_ebitda_critical" in f for f in flags) and dilutive:
        critical = True
        flags.append("critical_high_leverage_dilutive")
        reasons.append("critical: net_debt/EBITDA > 6 AND dilutive capital allocation")

    # Dilution filings: parquet not found → note as coverage gap
    flags.append("dilution_filing_coverage_missing")
    reasons.append("dilution_events.parquet not available (coverage gap — no S-1/S-3 check)")

    # Net debt / EBITDA proxy from financials.raw (fallback when leverage_ratios absent)
    if not any("net_debt_ebitda" in f for f in flags):
        try:
            raw = sd["financials"]["raw"]
            debt_lt = raw.get("debt_lt") or 0
            cfo = raw.get("cfo") or 0
            if debt_lt > 0 and cfo > 0:
                ebitda_proxy = cfo * 1.2
                net_debt_ebitda = debt_lt / ebitda_proxy
                if net_debt_ebitda > _NET_DEBT_EBITDA_FLAG:
                    flags.append(f"net_debt_ebitda_{net_debt_ebitda:.1f}")
                    reasons.append(
                        f"net-debt/EBITDA proxy {net_debt_ebitda:.1f} > {_NET_DEBT_EBITDA_FLAG}"
                    )
                    flag_count += 1
        except (KeyError, TypeError):
            pass

    state = "elevated" if (flag_count >= 2 or critical) else ("watch" if flag_count >= 1 else "ok")
    return _lane(state, flags, reasons, asof=sd_asof)


# ===========================================================================
# LANE 6: ownership_flow
# ===========================================================================

def _lane_ownership_flow(
    sd: dict | None,
    insider_signals: dict,
    ticker: str,
    run_date: date,
) -> dict:
    """Elevated: short z >= 2 AND insider sell cluster (sellers>=3, buyers==0, net<0).
    Watch: either alone.
    13D activist: info flag only.
    """
    if sd is None:
        return _missing_lane("ownership_flow")

    sd_asof = sd.get("asof")
    if _is_stale(sd_asof, run_date):
        return _stale_lane(sd_asof)

    flags: list[str] = []
    reasons: list[str] = []

    # Short interest z — from factors.legs.short_interest z-score
    short_z = None
    short_elevated = False
    try:
        short_z = sd["factors"]["legs"]["short_interest"]
        if short_z is not None and short_z >= _SHORT_INT_Z_FLAG:
            flags.append(f"short_pressure_z_{short_z:.2f}")
            reasons.append(f"short-interest factor z {short_z:.2f} >= {_SHORT_INT_Z_FLAG}")
            short_elevated = True
    except (KeyError, TypeError):
        pass

    # Insider sell cluster — from positioning.insider
    insider_sell = False
    try:
        ins = sd["positioning"]["insider"]
        n_buyers = ins.get("n_buyers", 0) or 0
        n_sellers = ins.get("n_sellers", 0) or 0
        net_mn = ins.get("net_usd_mn", 0) or ins.get("net_mn", 0) or 0
        if n_sellers >= _INSIDER_SELLER_MIN and n_buyers == 0 and net_mn < 0:
            flags.append(f"insider_sell_cluster_{n_sellers}sellers")
            reasons.append(
                f"insider sell cluster: {n_sellers} sellers, {n_buyers} buyers, "
                f"net {net_mn:.1f}M"
            )
            insider_sell = True
    except (KeyError, TypeError):
        pass

    # Fallback to insider_signals.json (site/factordata)
    if not insider_sell and ticker in insider_signals:
        sig = insider_signals[ticker]
        n_buyers = sig.get("buyers", 0) or 0
        n_sellers = sig.get("sellers", 0) or 0
        net_mn = sig.get("net_mn", 0) or 0
        if n_sellers >= _INSIDER_SELLER_MIN and n_buyers == 0 and net_mn < 0:
            flags.append(f"insider_sell_cluster_{n_sellers}sellers")
            reasons.append(
                f"insider sell cluster (signals): {n_sellers} sellers, {n_buyers} buyers, "
                f"net {net_mn:.1f}M"
            )
            insider_sell = True

    # smart_money for 13D activist info-flag
    try:
        sm = sd.get("smart_money", {})
        trend = sm.get("trend", {})
        if trend.get("direction") == "selling" and sm.get("is_vip"):
            flags.append("smart_money_vip_selling")
            reasons.append("VIP smart-money holder selling (13F trend)")
    except (KeyError, TypeError):
        pass

    if short_elevated and insider_sell:
        state = "elevated"
    elif short_elevated or insider_sell:
        state = "watch"
    else:
        state = "ok"

    return _lane(state, flags, reasons, asof=sd_asof)


# ===========================================================================
# LANE 7: macro_sensitivity
# ===========================================================================

def _lane_macro_sensitivity(sd: dict | None, regime: dict, run_date: date) -> dict:
    """Elevated: rate_beta_tier HIGH AND regime_read = headwind.
    Watch: HIGH tier alone or in adverse rate trend.

    Real data mapping:
      - macro_sensitivity.tier: 'high'/'medium'/'low'
      - macro_sensitivity.regime: 'neutral'/'headwind'/'tailwind'
      - macro_sensitivity.rate_state.direction: 'rising'/'falling'/'steady'
    """
    if sd is None:
        return _missing_lane("macro_sensitivity")

    sd_asof = sd.get("asof")
    if _is_stale(sd_asof, run_date):
        return _stale_lane(sd_asof)

    flags: list[str] = []
    reasons: list[str] = []

    ms = sd.get("macro_sensitivity", {})
    if not ms:
        return _lane("coverage_missing", reasons=["macro_sensitivity block absent"], asof=sd_asof)

    tier = ms.get("tier", "").lower()
    regime_read = ms.get("regime", "").lower()
    rate_state = ms.get("rate_state", {})
    rate_direction = rate_state.get("direction", "") if rate_state else ""
    rate_beta = ms.get("rate_beta")

    # Also check by abs(rate_beta) >= 0.3 as HIGH proxy
    high_by_beta = rate_beta is not None and abs(rate_beta) >= _RATE_BETA_HIGH_ABS
    is_high = tier in ("high",) or high_by_beta

    headwind = regime_read in ("headwind",)
    # Adverse rate trend: rates rising and negative rate_beta (bond-proxy stocks hurt)
    adverse_rate = rate_direction == "rising" and rate_beta is not None and rate_beta < -0.2

    if is_high:
        flags.append(f"rate_sensitivity_high")
        reasons.append(
            f"rate beta {rate_beta:.3f} (tier={tier})" if rate_beta is not None
            else f"rate sensitivity tier: {tier}"
        )
        if headwind:
            flags.append("regime_headwind")
            reasons.append(f"regime read: {regime_read}")
    if adverse_rate and not headwind:
        flags.append("adverse_rate_trend")
        reasons.append(f"rising rates with negative rate beta {rate_beta:.3f}")

    if is_high and headwind:
        state = "elevated"
    elif is_high or adverse_rate:
        state = "watch"
    else:
        state = "ok"

    return _lane(state, flags, reasons, asof=sd_asof)


# ===========================================================================
# LANE 8: sector_rotation
# ===========================================================================

def _lane_sector_rotation(sd: dict | None, regime: dict, run_date: date) -> dict:
    """Elevated: >= 2 of {sector RS bottom quartile, sector_pulse fading/deteriorating,
    subsector quadrant weakening/lagging}.
    Watch: = 1.

    sector_rs in regime is a list of {ticker, rank, rs, mom_20d_pct, pctile_252d, ...}
    We map profile.sector → ETF ticker, then check rank vs total.
    sector_pulse from stockdata.sector_pulse.label.
    subsector quadrant: not found in data (counted as absent).
    """
    if sd is None:
        return _missing_lane("sector_rotation")

    sd_asof = sd.get("asof")
    if _is_stale(sd_asof, run_date):
        return _stale_lane(sd_asof)

    flags: list[str] = []
    reasons: list[str] = []
    signal_count = 0

    # Sector RS: bottom quartile check
    sector = None
    try:
        sector = sd["profile"]["sector"]
    except (KeyError, TypeError):
        pass

    sector_rs_list = regime.get("sector_rs", []) or []
    if sector and sector_rs_list:
        etf = SECTOR_ETF_MAP.get(sector)
        if etf:
            etf_rows = [r for r in sector_rs_list if r.get("ticker") == etf]
            if etf_rows:
                row = etf_rows[0]
                total = len(sector_rs_list)
                rank = row.get("rank", 0)
                # Bottom quartile: rank > 75% of total
                if rank and total and rank > total * 0.75:
                    flags.append(f"sector_rs_bottom_q_{etf}")
                    reasons.append(
                        f"sector ETF {etf} rank {rank}/{total} (bottom quartile of regime sector_rs)"
                    )
                    signal_count += 1

    # sector_pulse from stockdata
    try:
        sp = sd.get("sector_pulse", {})
        label = (sp.get("label") or "").lower()
        if label in ("fading", "deteriorating"):
            flags.append(f"sector_pulse_{label}")
            reasons.append(f"sector pulse label: {label}")
            signal_count += 1
    except (KeyError, TypeError):
        pass

    # Subsector quadrant: not available in current data
    # (counted as 0, flagged as coverage gap)
    flags.append("subsector_quadrant_coverage_missing")
    reasons.append("subsector_rotation/state.json not found — quadrant check skipped")

    state = "elevated" if signal_count >= 2 else ("watch" if signal_count >= 1 else "ok")
    return _lane(state, flags, reasons, asof=sd_asof)


# ===========================================================================
# LANE 9: market_regime (shared across all positions)
# ===========================================================================

def _lane_market_regime(regime: dict, run_date: date) -> dict:
    """Elevated: risk_radar.state in {caution, risk-off} (ORANGE/EXTREME in site banner).
    Watch: risk_state.state 'caution' (YELLOW proxy — lower-severity composite).

    Vocabulary in actual data (vendor/macro_src/data/regime/latest.json):
      risk_radar.state: 'caution' | 'risk-off' | 'calm'
      risk_state.state: 'risk-on' | 'caution'

    Site-banner color mapping:
      risk_radar 'caution'  → ORANGE (§7 "market_regime ORANGE" triggers tighten/trim)
      risk_radar 'risk-off' → EXTREME
      risk_state 'caution'  → YELLOW (watch only)
      risk_radar 'calm'     → GREEN

    The role-ladder tighten/trim checks use elevated(market_regime); 'caution' must
    produce elevated so ORANGE regime triggers those roles.
    """
    flags: list[str] = []
    reasons: list[str] = []

    rr = regime.get("risk_radar", {}) or {}
    rs = regime.get("risk_state", {}) or {}

    radar_state = (rr.get("state") or "").lower()
    risk_state_val = (rs.get("state") or "").lower()
    dominant_scare = rr.get("dominant_scare")

    regime_asof = regime.get("asof")

    # risk_radar 'caution' (ORANGE) or 'risk-off' (EXTREME) → elevated
    # risk_state 'caution' alone (YELLOW, lower-confidence composite) → watch
    elevated = radar_state in ("caution", "risk-off")
    watch = not elevated and risk_state_val in ("caution",)

    if elevated:
        flags.append(f"radar_{radar_state}")
        reasons.append(f"risk radar: {radar_state} (dominant: {dominant_scare})")
    elif watch:
        flags.append(f"risk_state_{risk_state_val}")
        reasons.append(f"risk state: {risk_state_val} (composite caution)")

    state = "elevated" if elevated else ("watch" if watch else "ok")
    return _lane(state, flags, reasons, asof=regime_asof)


# ===========================================================================
# LANE 10: data_quality
# ===========================================================================

def _lane_data_quality(lanes: dict, run_date: date) -> dict:
    """Summarise coverage/staleness across evidence lanes."""
    missing = [k for k, v in lanes.items() if v["state"] == "coverage_missing"]
    stale = [k for k, v in lanes.items() if v["state"] == "stale"]

    flags: list[str] = []
    reasons: list[str] = []
    if missing:
        flags.append(f"coverage_missing_{len(missing)}_lanes")
        reasons.append(f"coverage missing: {', '.join(missing)}")
    if stale:
        flags.append(f"stale_{len(stale)}_lanes")
        reasons.append(f"stale artifacts: {', '.join(stale)}")

    state = "ok" if not missing and not stale else "watch"
    return _lane(state, flags, reasons, asof=str(run_date))


# ===========================================================================
# Role ladder (deterministic, first match from top, §7)
# ===========================================================================

def _assign_role(lanes: dict, path: dict, position: dict) -> tuple[str, list[str]]:
    """Return (role, role_reasons) per masterplan §7 first-match ladder."""
    pt = lanes.get("price_trend", {})
    eg = lanes.get("extension_giveback", {})
    ew = lanes.get("event_window", {})
    ee = lanes.get("earnings_expectation", {})
    sd = lanes.get("solvency_dilution", {})
    of_ = lanes.get("ownership_flow", {})
    ms = lanes.get("macro_sensitivity", {})
    sr = lanes.get("sector_rotation", {})
    mr = lanes.get("market_regime", {})

    def elevated(lane): return lane.get("state") == "elevated"
    def watch(lane): return lane.get("state") in ("elevated", "watch")

    # --- exit_review ---
    # any of:
    #   solvency critical flag AND price_trend elevated
    #   earnings_expectation elevated AND price_trend elevated AND close<MA200
    #   giveback >= 60% of mfe >= 25%
    #   dilution filing <= 30d AND price_trend elevated
    exit_reasons: list[str] = []

    solvency_critical = any("critical" in f for f in sd.get("flags", []))
    if solvency_critical and elevated(pt):
        exit_reasons.append("solvency critical flag with price trend elevated")

    close = path.get("ref_close")
    ma200 = path.get("ma200")
    if elevated(ee) and elevated(pt) and close and ma200 and close < ma200:
        exit_reasons.append(
            "earnings expectation elevated + price trend elevated + close below MA200"
        )

    giveback = path.get("giveback_pct_of_mfe")
    mfe = path.get("mfe_pct")
    if giveback is not None and mfe is not None and giveback >= 0.60 and mfe >= 0.25:
        exit_reasons.append(
            f"giveback {giveback*100:.0f}% of MFE {mfe*100:.0f}% (>= 60% of >= 25%)"
        )

    # Dilution filing <= 30d: not available from parquet; skip

    if exit_reasons:
        return "exit_review", exit_reasons

    # --- trim_review ---
    # extension_giveback elevated AND (earnings <= 7 sessions OR sector_rotation elevated
    #   OR ownership_flow elevated OR market_regime ORANGE+)
    trim_reasons: list[str] = []
    if elevated(eg):
        ew_flags = ew.get("flags", [])
        earn_soon = any("earnings_t" in f for f in ew_flags)
        if earn_soon or elevated(sr) or elevated(of_) or elevated(mr):
            trim_reasons.append(
                "extension elevated with: "
                + "; ".join(filter(None, [
                    "earnings within 7 sessions" if earn_soon else "",
                    "sector rotation elevated" if elevated(sr) else "",
                    "ownership flow elevated" if elevated(of_) else "",
                    "market regime elevated" if elevated(mr) else "",
                ]))
            )
    if trim_reasons:
        return "trim_review", trim_reasons

    # --- tighten ---
    # price_trend elevated AND (event_window elevated OR macro_sensitivity elevated
    #   OR market_regime ORANGE/EXTREME)
    tighten_reasons: list[str] = []
    if elevated(pt) and (elevated(ew) or elevated(ms) or elevated(mr)):
        tighten_reasons.append(
            "price trend elevated with: "
            + "; ".join(filter(None, [
                "event window elevated" if elevated(ew) else "",
                "macro sensitivity elevated" if elevated(ms) else "",
                "market regime elevated" if elevated(mr) else "",
            ]))
        )
    if tighten_reasons:
        return "tighten", tighten_reasons

    # --- review ---
    # >= 2 elevated lanes across independent groups
    # Groups: {price_trend+extension} | {event} | {expectation+solvency} | {ownership} | {macro+sector}
    groups = {
        "price_ext": elevated(pt) or elevated(eg),
        "event": elevated(ew),
        "expectation_solvency": elevated(ee) or elevated(sd),
        "ownership": elevated(of_),
        "macro_sector": elevated(ms) or elevated(sr),
    }
    fired = [g for g, v in groups.items() if v]
    if len(fired) >= 2:
        review_reasons = [f"independent groups elevated: {', '.join(fired)}"]
        return "review", review_reasons

    # --- monitor ---
    # exactly 1 elevated lane, or >= 2 watch lanes
    evidence_lanes_vals = [pt, eg, ew, ee, sd, of_, ms, sr]
    n_elevated = sum(1 for l in evidence_lanes_vals if elevated(l))
    n_watch = sum(1 for l in evidence_lanes_vals if watch(l))

    if n_elevated == 1 or n_watch >= 2:
        monitor_reasons: list[str] = []
        elev_names = [
            n for n, l in zip(_EVIDENCE_LANES, evidence_lanes_vals) if elevated(l)
        ]
        watch_names = [
            n for n, l in zip(_EVIDENCE_LANES, evidence_lanes_vals)
            if l.get("state") == "watch"
        ]
        if elev_names:
            monitor_reasons.append(f"1 elevated lane: {elev_names[0]}")
        if len(watch_names) >= 2:
            monitor_reasons.append(f"{len(watch_names)} watch lanes: {', '.join(watch_names)}")
        return "monitor", monitor_reasons

    return "ok", []


# ===========================================================================
# Coverage classification
# ===========================================================================

def _coverage_class(sd: dict | None, lanes: dict) -> str:
    if sd is None:
        return "price_only"
    missing_count = sum(
        1 for k in _EVIDENCE_LANES if lanes.get(k, {}).get("state") == "coverage_missing"
    )
    return "partial" if missing_count >= 3 else "full"


# ===========================================================================
# Single-position composer
# ===========================================================================

def _compose_position(
    position: dict,
    regime: dict,
    insider_signals: dict,
    market_regime_lane: dict,
    vendor_root: Path,
    run_date: date,
    price_loader=None,
    spy_df: pd.DataFrame | None = None,
    data_root: Path | None = None,
) -> dict:
    ticker = position.get("ticker", "UNKNOWN").upper()
    position_id = str(position.get("id", ticker))

    sd = _load_stockdata(ticker, vendor_root)
    ohlcv = _load_ohlcv(ticker, vendor_root, price_loader=price_loader, data_root=data_root)
    metrics = _compute_price_metrics(ohlcv, spy_df=spy_df) if ohlcv is not None else {}

    # Build lanes
    l_price = _lane_price_trend(metrics, sd, run_date)
    l_ext, path = _lane_extension_giveback(metrics, sd, position, ohlcv_df=ohlcv)
    l_event, events = _lane_event_window(sd, run_date)
    l_earn = _lane_earnings_expectation(sd, run_date)
    l_solv = _lane_solvency_dilution(sd, run_date)
    l_own = _lane_ownership_flow(sd, insider_signals, ticker, run_date)
    l_macro = _lane_macro_sensitivity(sd, regime, run_date)
    l_sector = _lane_sector_rotation(sd, regime, run_date)
    # market_regime is shared (pre-computed)
    mr_lane = dict(market_regime_lane)  # copy

    evidence_lanes = {
        "price_trend": l_price,
        "extension_giveback": l_ext,
        "event_window": l_event,
        "earnings_expectation": l_earn,
        "solvency_dilution": l_solv,
        "ownership_flow": l_own,
        "macro_sensitivity": l_macro,
        "sector_rotation": l_sector,
    }
    all_lanes = dict(evidence_lanes)
    all_lanes["market_regime"] = mr_lane
    dq_lane = _lane_data_quality(evidence_lanes, run_date)
    all_lanes["data_quality"] = dq_lane

    # Elevated count: evidence lanes 1–8 only
    elevated_count = sum(
        1 for k in _EVIDENCE_LANES
        if all_lanes.get(k, {}).get("state") == "elevated"
    )

    # Role ladder
    role, role_reasons = _assign_role(all_lanes, path, position)
    role_label = _ROLE_LABELS.get(role, role)

    # Coverage
    coverage = _coverage_class(sd, all_lanes)

    # Personality — prefer fresh R2 personality block
    archetype = None
    dna_class = None
    current_mode = None
    chart_labels: list[str] = []
    if sd:
        # Fresh R2: personality.base.archetype / dna_class / current_mode
        try:
            personality_block = sd.get("personality") or {}
            if personality_block:
                base = personality_block.get("base") or {}
                archetype = base.get("archetype") or archetype
                dna_class = base.get("dna_class") or dna_class
                current_mode = personality_block.get("current_mode")
        except (KeyError, TypeError):
            pass

        # Fallback: profile.archetype.key
        if archetype is None:
            try:
                archetype = sd["profile"]["archetype"]["key"]
            except (KeyError, TypeError):
                pass
        # Fallback: mktcap_tier as dna_class proxy
        if dna_class is None:
            try:
                dna_class = sd["profile"]["mktcap_tier"]["key"]
            except (KeyError, TypeError):
                pass
        # chart labels from conviction.drivers (descriptive)
        try:
            for d in sd["conviction"].get("drivers", []):
                if isinstance(d, dict) and "en" in d:
                    chart_labels.append(d["en"])
        except (KeyError, TypeError):
            pass

    return {
        "position_id": position_id,
        "ticker": ticker,
        "coverage": coverage,
        "role": role,
        "role_label": role_label,
        "role_reasons": role_reasons,
        "elevated_lanes": elevated_count,
        "lane_total": 8,
        "lanes": all_lanes,
        "path": path,
        "personality": {
            "archetype": archetype,
            "dna_class": dna_class,
            "current_mode": current_mode,
            "chart_labels": chart_labels,
        },
        "events": events,
    }


# ===========================================================================
# Market block builder
# ===========================================================================

def _build_market_block(regime: dict) -> dict:
    rr = regime.get("risk_radar", {}) or {}
    rs = regime.get("risk_state", {}) or {}
    vr = regime.get("vol_regime", {}) or {}

    radar_verdict = rr.get("state")
    radar_score = rr.get("top_score")
    dominant_scare = rr.get("dominant_scare")

    ms_verdict = rs.get("state")
    ms_score = rs.get("score")

    vol_regime = vr.get("regime")
    quad = regime.get("quad")
    quad_name = regime.get("quad_name")
    asof = regime.get("asof")

    return {
        "risk_radar": {
            "verdict": radar_verdict,
            "score": float(radar_score) if radar_score is not None else None,
            "dominant_scare": dominant_scare,
        },
        "market_state": {
            "verdict": ms_verdict,
            "score": float(ms_score) if ms_score is not None else None,
        },
        "vol_regime": vol_regime,
        "quad": quad,
        "quad_name": quad_name,
        "asof": asof,
    }


# ===========================================================================
# Public API
# ===========================================================================

def compose(
    positions: list[dict],
    *,
    vendor_root: Path = VENDOR_DEFAULT,
    data_root: Path = DATA_DEFAULT,
    now: date | None = None,
    price_loader=None,
) -> dict:
    """Compose held-risk state for all positions.

    Args:
        positions: list of position rows shaped like Supabase rows
                   {id, ticker, shares, entry_price, entry_date, status, ...}.
        vendor_root: path to vendor/macro_src (the Macro Dashboard artifact tree).
        data_root: path to Mastermind data/ directory.
        now: run date (defaults to today); injectable for tests.
        price_loader: callable(ticker) -> pd.DataFrame | None; overrides OHLCV loading
                      for tests (eliminates network calls).

    Returns:
        portfolio_risk_state.v1 dict per output contract.
    """
    run_date: date = now or date.today()

    regime = _load_regime(vendor_root)
    insider_signals = _load_insider_signals(vendor_root)

    # Load SPY once for RS z computation
    spy_df: pd.DataFrame | None = None
    if price_loader is not None:
        spy_df = price_loader("SPY")
    else:
        spy_df = _load_ohlcv("SPY", vendor_root, data_root=data_root)

    # Market regime is shared across all positions
    market_regime_lane = _lane_market_regime(regime, run_date)

    market_block = _build_market_block(regime)

    # Determine asof from regime artifact
    asof_str = regime.get("asof") or str(run_date)

    composed_positions: list[dict] = []
    for pos in positions:
        try:
            result = _compose_position(
                pos,
                regime=regime,
                insider_signals=insider_signals,
                market_regime_lane=market_regime_lane,
                vendor_root=vendor_root,
                run_date=run_date,
                price_loader=price_loader,
                spy_df=spy_df,
                data_root=data_root,
            )
        except Exception as exc:
            # Fail-soft: per-ticker failure → coverage_missing lanes
            ticker = str(pos.get("ticker", "UNKNOWN")).upper()
            coverage_lanes = {
                k: _lane("coverage_missing", reasons=[f"compose error: {exc}"])
                for k in _ALL_LANE_NAMES
            }
            result = {
                "position_id": str(pos.get("id", ticker)),
                "ticker": ticker,
                "coverage": "price_only",
                "role": "ok",
                "role_label": "Ok",
                "role_reasons": [f"coverage missing — error: {exc}"],
                "elevated_lanes": 0,
                "lane_total": 8,
                "lanes": coverage_lanes,
                "path": {
                    "entry_date": pos.get("entry_date"),
                    "entry_price": pos.get("entry_price"),
                    "ref_close": None,
                    "ref_date": None,
                    "post_entry_high": None,
                    "mfe_pct": None,
                    "mae_pct": None,
                    "giveback_pct_of_mfe": None,
                    "ma20": None,
                    "ma50": None,
                    "ma200": None,
                    "atr14": None,
                    "high_52w": None,
                    "rs20_spy_z": None,
                    "ext_grade": None,
                },
                "personality": {"archetype": None, "dna_class": None, "current_mode": None, "chart_labels": []},
                "events": {"earnings_tdays": None, "earnings_date": None, "fomc_tdays": None},
            }
        composed_positions.append(result)

    return {
        "schema": "portfolio_risk_state.v1",
        "asof": asof_str,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market": market_block,
        "positions": composed_positions,
    }


def write_state(state: dict, path: Path | str | None = None) -> None:
    """Atomically write state dict to path (tmp → os.replace)."""
    target = Path(path) if path else _OUT_DEFAULT
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

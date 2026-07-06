"""Hermetic stub of engine.canon — contains ONLY the functions consumed by
portfolio.distribution_tells (resample_sessions, _resample_weekly, rsi_macd,
plus their primitives: rma, ema, rsi, _as_series, and the required constants).

This is a SNAPSHOT of the functions as they existed in vendor/macro_src on
2026-07-06.  It is NOT the full canon module.  It exists so that
tests/test_distribution_tells.py runs hermetically in environments where
vendor/macro_src is absent (e.g. fresh CI worktrees).

MAINTENANCE: if the canon API used by distribution_tells changes, update this
stub to match.  The canonical source remains vendor/macro_src/engine/canon.py.

DO NOT add functions from the full canon.py here — keep this stub minimal so
divergences are obvious and easy to spot.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# ── confluence params ───────────────────────────────────────────────────────
RSI_LEN, FAST_LEN, BASE_LEN, SIG_LEN = 14, 14, 60, 5


# ── internal helpers ────────────────────────────────────────────────────────

def _as_series(x) -> pd.Series:
    if isinstance(x, pd.Series):
        return x
    return pd.Series(x)


def rma(series: pd.Series, n: int) -> pd.Series:
    """Wilder's RMA == Pine ta.rma: SMA-seeded, then recursive adjust=False."""
    s = pd.to_numeric(_as_series(series), errors="coerce").astype(float)
    vals = s.to_numpy(dtype=float)
    out = np.full(len(vals), np.nan)
    alpha = 1.0 / n
    finite = np.isfinite(vals)
    prev = np.nan
    seeded = False
    for i in range(len(vals)):
        if not seeded:
            if i >= n - 1 and finite[i - n + 1:i + 1].all():
                prev = float(np.mean(vals[i - n + 1:i + 1]))
                out[i] = prev
                seeded = True
            continue
        v = vals[i]
        if not np.isfinite(v):
            out[i] = prev
            continue
        prev = alpha * v + (1.0 - alpha) * prev
        out[i] = prev
    return pd.Series(out, index=s.index)


def ema(series: pd.Series, span: int) -> pd.Series:
    """Recursive EMA == Pine ta.ema: ewm(span, adjust=False) with n-bar warm-up."""
    s = pd.to_numeric(_as_series(series), errors="coerce").astype(float)
    return s.ewm(span=span, adjust=False, min_periods=span).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder RSI on the SMA-seeded RMA (== Pine ta.rsi)."""
    s = pd.to_numeric(_as_series(close), errors="coerce").astype(float)
    d = s.diff()
    up = rma(d.clip(lower=0), n)
    dn = rma((-d).clip(lower=0), n)
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


# ── public API consumed by distribution_tells ───────────────────────────────

def resample_sessions(daily: pd.Series, n: int) -> tuple[pd.Series, pd.Series]:
    """Session-GROUPED resample: bucket by session ordinal, take last close of each group."""
    s = pd.to_numeric(_as_series(daily), errors="coerce").dropna()
    if s.empty:
        return s, s
    grp = np.arange(len(s)) // n
    df = pd.DataFrame({"v": s.to_numpy(), "d": s.index}, index=s.index)
    df["g"] = grp
    last = df.groupby("g").last()
    bucketed = pd.Series(last["v"].to_numpy(), index=pd.DatetimeIndex(last["d"]))
    known = pd.Series(pd.DatetimeIndex(last["d"]).to_numpy(),
                      index=pd.DatetimeIndex(last["d"]))
    return bucketed, known


def _resample_weekly(daily: pd.Series, rule: str) -> tuple[pd.Series, pd.Series]:
    """Calendar resample for higher confirm timeframes (weekly/2-weekly/monthly)."""
    s = pd.to_numeric(_as_series(daily), errors="coerce").dropna()
    b = s.resample(rule).last().dropna()
    return b, pd.Series(b.index, index=b.index)


def rsi_macd(close: pd.Series):
    """Pine RSI-based MACD: EMA(RSI) - slower EMA(RSI), signal = EMA(macd)."""
    r = rsi(close, RSI_LEN)
    macd = ema(r, FAST_LEN) - ema(r, BASE_LEN)
    return macd, ema(macd, SIG_LEN)

"""loop/single_name_panel.py — survivorship-safe, PIT-clean single-name price panel.

Productionizes the panel construction validated in loop/factor_experiment.py. The panel
is the INPUT contract the frozen harness consumes: a daily `closes` DataFrame whose
column 0 is the benchmark (SPY) and column 1 is a real bond (IEF), so that
  - harness.score's beats_spy compares vs closes.iloc[:, 0]  (SPY), and
  - harness.sixty_forty_sharpe finds a REAL "IEF" column (not the last single name),
making the 60/40 hurdle honest rather than an accident of column order.

Survivorship safety
-------------------
The universe is UNION(_closes_deep, _closes_delisted): delisted names keep their real
final prices, so a long decile cannot silently drop its losers. Membership is enforced
POINT-IN-TIME at rank time (loop/single_name_candidates.materialize), never here — this
module only assembles the price grid and ships the as-of membership table alongside it.

Data hygiene (CAUSAL, uniform, reported)
---------------------------------------
The raw breadth caches carry corrupted ticks (zeros, sub-cent placeholders, an unadjusted
split that manufactures a 30,000x one-day "return"). Left raw, a single contaminated name
in a long decile fabricates an enormous CAGR — a pure DATA-BUG false positive. We rebuild
a clean price path from clean returns: sub-$1/non-positive -> NaN, gap/halt days earn 0
(no teleport), |daily ret| > RET_CAP neutralized. Applied uniformly to every name with no
forward information; it removes artifacts, it does not cherry-pick winners.

PURE-ish: numpy/pandas + lib.store (read-only parquet reads). No writes, no DB.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# wire the import path to the vendored macro stack (read-only)
# --------------------------------------------------------------------------- #
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MACRO = os.path.join(_REPO, "vendor", "macro")
for _p in (_REPO, _MACRO):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib import store  # noqa: E402  (vendored macro parquet store)

# --------------------------------------------------------------------------- #
# constants — frozen to match the validated experiment
# --------------------------------------------------------------------------- #
PANEL_START = pd.Timestamp("2002-01-01")     # the loop's "real 2002-2026" window
HOLDOUT_START = pd.Timestamp("2022-01-01")   # LOCKED before any scoring (mirrors iterate.py)

MIN_PRICE = 1.0      # sub-$1 / non-positive ticks -> NaN (placeholder bad data)
RET_CAP = 0.60       # neutralize |daily ret| > 60% (unadjusted split / residual bad tick)
SELECT_FLOOR = 5.0   # a name must trade >= $5 at rebalance to be RANKED (penny-stock screen)

_BREADTH = os.path.join(_MACRO, "data", "breadth")

BENCH = "SPY"        # harness benchmark = closes.iloc[:, 0]
BOND = "IEF"         # 60/40 bond leg = harness.sixty_forty_sharpe's "IEF" column


# --------------------------------------------------------------------------- #
# data hygiene — clean-return reconstruction (causal, uniform)
# --------------------------------------------------------------------------- #
def sanitize_panel(panel: pd.DataFrame) -> pd.DataFrame:
    """Rebuild each name's price path from CLEAN daily returns.

    For every column independently: mask sub-$1/non-positive to NaN; compute returns
    only across adjacent valid rows (a gap/halt day earns 0, never a teleport);
    clamp |ret| > RET_CAP; re-integrate from the first valid price. No cross-name and
    no forward information is used. `out.attrs['n_clamped']` records how many cells were
    neutralized (reported in the self-audit).
    """
    px = panel.where((panel >= MIN_PRICE) & (panel > 0))
    out = pd.DataFrame(index=panel.index, columns=panel.columns, dtype=float)
    n_clamped = 0
    for c in px.columns:
        s = px[c]
        f = s.first_valid_index()
        l = s.last_valid_index()
        if f is None:
            continue
        seg = s.loc[f:l]
        valid = seg.notna()
        prev = seg.shift(1)
        r = seg / prev - 1.0
        adjacent = valid & prev.notna()          # both ends valid & adjacent rows
        r = r.where(adjacent, 0.0)                # gap/halt day -> 0 return (flat, no teleport)
        r = r.replace([np.inf, -np.inf], 0.0)
        n_clamped += int((r.abs() > RET_CAP).sum())
        r = r.clip(-RET_CAP, RET_CAP)             # neutralize absurd jumps
        r.iloc[0] = 0.0
        p0 = float(seg.loc[seg.first_valid_index()])
        out.loc[f:l, c] = (p0 * (1.0 + r).cumprod()).values
    out.attrs["n_clamped"] = n_clamped
    return out


# --------------------------------------------------------------------------- #
# survivorship-safe panel assembly
# --------------------------------------------------------------------------- #
def _read_membership() -> pd.DataFrame:
    """PIT S&P 1500 membership, with the S&P 500 spans folded in (a 500 name is by
    construction a 1500 name). Columns: ticker, start_date, end_date (NaT = active)."""
    m1500 = pd.read_parquet(os.path.join(_BREADTH, "sp1500_pit_membership.parquet"))
    frames = [m1500[["ticker", "start_date", "end_date"]]]
    p500 = os.path.join(_BREADTH, "sp500_pit_membership.parquet")
    if os.path.exists(p500):
        m500 = pd.read_parquet(p500)
        frames.append(m500[["ticker", "start_date", "end_date"]])
    mem = pd.concat(frames, ignore_index=True)
    mem["start_date"] = pd.to_datetime(mem["start_date"])
    mem["end_date"] = pd.to_datetime(mem["end_date"])
    mem = mem.drop_duplicates().reset_index(drop=True)
    return mem


def members_asof(mem: pd.DataFrame, t: pd.Timestamp) -> set:
    """PIT membership as-of t — checks ALL spans for each ticker (a ticker can have
    several disjoint membership intervals). A span is active at t iff
    start_date <= t < end_date (end_date NaT = still active)."""
    t = pd.Timestamp(t)
    active = mem[(mem["start_date"] <= t) & (mem["end_date"].isna() | (mem["end_date"] > t))]
    return set(active["ticker"].unique())


def load_panel(panel_start: pd.Timestamp = PANEL_START) -> dict:
    """Assemble the survivorship-safe, PIT-clean panel the harness consumes.

    Returns a dict:
      closes  : DataFrame [SPY, IEF, <single names...>] on the shared trading-day index,
                cleaned. Column 0 = SPY (benchmark), column 1 = IEF (60/40 bond leg).
      mem     : PIT membership table (for materialize's as-of filter).
      bill    : the short bill-yield series (cash-leg rate) from engine.equity_alloc.
      index   : the shared DatetimeIndex.
      hygiene : {raw_max_abs_ret, n_clamped} for the self-audit.

    SPY and IEF are NOT sanitized through the single-name reconstruction: they are clean
    adjusted ETF closes from the yahoo store, and they must remain byte-exact so the
    benchmark / 60-40 hurdle are the genuine series, not a re-integrated approximation.
    """
    from engine.equity_alloc import bill_yield  # local import: engine path wired above

    deep = pd.read_parquet(os.path.join(_BREADTH, "_closes_deep.parquet"))
    deli = pd.read_parquet(os.path.join(_BREADTH, "_closes_delisted.parquet"))
    # disjoint columns (verified) -> horizontal union keeps delisted names with their
    # REAL final prices; dropping them is the classic survivorship lie.
    panel = pd.concat([deep, deli], axis=1)
    panel = panel.loc[:, ~panel.columns.duplicated()]
    panel.index = pd.to_datetime(panel.index)
    panel = panel.sort_index()
    panel = panel[panel.index >= panel_start]
    # drop any accidental SPY/IEF columns in the single-name caches (benchmarks come
    # from the yahoo store, not the breadth grid)
    panel = panel.drop(columns=[c for c in (BENCH, BOND) if c in panel.columns], errors="ignore")

    raw_max = float(np.nanmax(np.abs(
        panel.pct_change().replace([np.inf, -np.inf], np.nan).values)))
    panel = sanitize_panel(panel)                 # <-- causal data hygiene
    n_clamped = int(panel.attrs.get("n_clamped", 0))

    spy = store.read("yahoo", BENCH)["close"].astype(float).copy()
    spy.index = pd.to_datetime(spy.index)
    ief = store.read("yahoo", BOND)["close"].astype(float).copy()
    ief.index = pd.to_datetime(ief.index)

    # shared trading days: panel ∩ SPY ∩ IEF, >= panel_start. IEF starts 2002-07-30, so
    # the honest panel begins where the real bond leg begins (no synthetic backfill).
    idx = panel.index.intersection(spy.index).intersection(ief.index)
    idx = idx[idx >= panel_start]
    panel = panel.reindex(idx)
    spy = spy.reindex(idx)
    ief = ief.reindex(idx)

    names = [c for c in panel.columns if c not in (BENCH, BOND)]
    # column order is load-bearing: SPY first (harness benchmark), IEF second (60/40 leg)
    closes = pd.concat([spy.rename(BENCH), ief.rename(BOND), panel[names]], axis=1)

    mem = _read_membership()
    bill = bill_yield()

    return {
        "closes": closes,
        "mem": mem,
        "bill": bill,
        "index": idx,
        "hygiene": {"raw_max_abs_ret": raw_max, "n_clamped": n_clamped},
    }


# --------------------------------------------------------------------------- #
# in-sample / holdout split (the locked 2022 boundary)
# --------------------------------------------------------------------------- #
def split(closes: pd.DataFrame, holdout_start: pd.Timestamp = HOLDOUT_START):
    """(in_sample, holdout) by the LOCKED boundary. in_sample strictly before
    holdout_start; holdout on/after. Mirrors loop.holdout.split_index semantics."""
    cutoff = pd.Timestamp(holdout_start)
    return closes[closes.index < cutoff], closes[closes.index >= cutoff]

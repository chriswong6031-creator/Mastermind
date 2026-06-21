"""loop/single_name_candidates.py — cross-sectional single-name factor candidates.

Candidate-compatible (.spec, .spec_hash, .materialize(closes)->W) so the FROZEN harness
(loop.harness.score) judges them byte-identically to the ETF-sleeve candidates. This is
the PROPOSER only — it never re-implements any statistic.

The pool is the validated low-vol factor (60d & 120d lowest-vol quintile) PLUS a few
economically-distinct variants (momentum 12-1, momentum 6-1 long & long/short, short-term
reversal, 52w/26w-high proximity) so the DSR/PBO/BH multiple-testing pool is HONEST: the
low-vol survivor is judged against a real family of competing factors, and the near-
duplicate lookback variants give effective-N clustering something to collapse.

NO-LOOKAHEAD CONTRACT (every materialize obeys it):
  - rebalance monthly on the last trading day of each month;
  - on rebalance date t, rank ONLY names that are PIT S&P-1500 members as-of t, have a
    valid price at t (>= $5 screen) AND a NaN-free lookback window;
  - the signal uses prices THROUGH t (inclusive) only;
  - the position is set at t and held; backtest_portfolio applies its OWN .shift(1), so
    weights set at t earn t+1.. returns. We must NOT pre-shift.
  - equal weight; long-only -> gross 1.0; long/short -> 0.5 long / 0.5 short;
  - SPY and IEF weights are ALWAYS 0 (benchmark + bond leg, never holdings).
"""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

from loop.single_name_panel import SELECT_FLOOR, members_asof, BENCH, BOND

_NON_HOLDINGS = (BENCH, BOND)   # never take a position in the benchmark or the bond leg
_MIN_XSECTION = 20              # need a meaningful cross-section to sort


def _rebalance_dates(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Last trading day of each (year, month) present in idx — monthly rebalance."""
    s = pd.Series(idx, index=idx)
    grp = s.groupby([idx.year, idx.month]).last()
    return pd.DatetimeIndex(sorted(grp.values))


class FactorCandidate:
    """One cross-sectional single-name factor. Higher signal = more attractive; we long
    the TOP of the score (a long/short variant also shorts the bottom).

    Parameters
    ----------
    mem_df : the PIT membership table from single_name_panel.load_panel()['mem'].
             Carried on the instance so .materialize is self-contained and PIT-correct.
    """

    def __init__(self, name, kind, lookback, *, skip=0, side="long", frac=0.10,
                 vol_win=60, mem_df=None):
        self.name = name
        self.spec = {
            "name": name, "kind": kind, "lookback": int(lookback), "skip": int(skip),
            "side": side, "frac": float(frac), "vol_win": int(vol_win),
            "rebalance": "monthly", "weight": "equal", "universe": "sp1500_pit",
            "cost_bps": 3.0,
        }
        # spec_hash excludes mem_df (data, not spec) -> stable, holdout-lock-compatible.
        self.spec_hash = hashlib.sha256(
            json.dumps(self.spec, sort_keys=True).encode()).hexdigest()
        if mem_df is None:
            raise ValueError("FactorCandidate requires a PIT membership table (mem_df)")
        self._mem = mem_df

    # ---- signal: data THROUGH t only (hist.iloc[-1] is t); higher = long ----
    def _signal(self, hist: pd.DataFrame) -> pd.Series:
        kind = self.spec["kind"]
        lb, sk, vw = self.spec["lookback"], self.spec["skip"], self.spec["vol_win"]
        px = hist
        if kind == "momentum":
            # 12-1 style: return from t-lb to t-sk (skip the most recent `sk` days)
            p_now = px.iloc[-1 - sk] if sk > 0 else px.iloc[-1]
            p_then = px.iloc[-1 - lb]
            return p_now / p_then - 1.0
        if kind == "reversal":
            # short-term reversal: LONG the worst recent performers -> negate last-lb ret
            return -(px.iloc[-1] / px.iloc[-1 - lb] - 1.0)
        if kind == "lowvol":
            # LONG lowest trailing vol -> negate vol so high score = low vol
            rets = px.iloc[-(vw + 1):].pct_change()
            return -rets.iloc[-vw:].std()
        if kind == "high52w":
            # proximity to trailing-lb high: price / rolling-max (higher = nearer high)
            return px.iloc[-1] / px.iloc[-lb:].max()
        raise ValueError(kind)

    def materialize(self, closes: pd.DataFrame) -> pd.DataFrame:
        idx = closes.index
        names = [c for c in closes.columns if c not in _NON_HOLDINGS]
        px = closes[names]
        lb, sk, vw = self.spec["lookback"], self.spec["skip"], self.spec["vol_win"]
        need = max(lb + sk + 1, vw + 1)          # rows of history a valid signal needs
        frac, side = self.spec["frac"], self.spec["side"]

        rebs = _rebalance_dates(idx)
        if len(idx) > need:
            rebs = rebs[rebs >= idx[need]]        # only once enough history exists

        W = pd.DataFrame(0.0, index=idx, columns=closes.columns)
        rows = {}
        for t in rebs:
            loc = idx.get_loc(t)
            hist = px.iloc[: loc + 1]            # data THROUGH t (inclusive) -> no look-ahead
            if len(hist) < need:
                continue
            mem_set = members_asof(self._mem, t)
            window = hist.iloc[-need:]
            valid = window.notna().all(axis=0)               # NaN-free lookback
            last = hist.iloc[-1]
            price_ok = last.notna() & (last >= SELECT_FLOOR)  # $5 penny-stock screen
            eligible = [c for c in names
                        if c in mem_set
                        and bool(valid.get(c, False))
                        and bool(price_ok.get(c, False))]
            if len(eligible) < _MIN_XSECTION:
                continue
            sig = self._signal(hist[eligible]).dropna()
            sig = sig[np.isfinite(sig)]
            if len(sig) < _MIN_XSECTION:
                continue
            n = len(sig)
            k = max(1, int(round(frac * n)))
            ranked = sig.sort_values(ascending=False)
            longs = ranked.index[:k]
            row = pd.Series(0.0, index=closes.columns)
            if side == "long":
                row[longs] = 1.0 / len(longs)                 # gross 1.0
            else:  # long/short, gross ~1.0 (0.5 long / 0.5 short)
                shorts = ranked.index[-k:]
                row[longs] = 0.5 / len(longs)
                row[shorts] = -0.5 / len(shorts)
            for nh in _NON_HOLDINGS:
                row[nh] = 0.0
            rows[t] = row
        if not rows:
            return W
        Wr = pd.DataFrame(rows).T.reindex(columns=closes.columns).sort_index()
        # forward-fill monthly target weights across the daily index (held between rebs)
        W = Wr.reindex(idx).ffill().fillna(0.0)
        for nh in _NON_HOLDINGS:
            W[nh] = 0.0
        return W


def generate(mem_df: pd.DataFrame):
    """The honest multiple-testing pool: the validated low-vol factor + economically
    distinct competitors + near-duplicate lookback variants (for effective-N/PBO)."""
    C = []
    # --- the VALIDATED survivor: low volatility (60d & 120d lowest-vol quintile) ---
    C.append(FactorCandidate("lowvol_60d_botquintile",  "lowvol", 0, side="long", frac=0.20, vol_win=60,  mem_df=mem_df))
    C.append(FactorCandidate("lowvol_120d_botquintile", "lowvol", 0, side="long", frac=0.20, vol_win=120, mem_df=mem_df))
    # --- momentum family (12-1, 6-1, and a long/short — near-duplicates by design) ---
    C.append(FactorCandidate("mom_12_1_topdecile", "momentum", 252, skip=21, side="long", frac=0.10, mem_df=mem_df))
    C.append(FactorCandidate("mom_12_1_longshort", "momentum", 252, skip=21, side="ls",   frac=0.10, mem_df=mem_df))
    C.append(FactorCandidate("mom_6_1_topdecile",  "momentum", 126, skip=21, side="long", frac=0.10, mem_df=mem_df))
    # --- short-term reversal ---
    C.append(FactorCandidate("reversal_1m_botdecile", "reversal", 21, side="long", frac=0.10, mem_df=mem_df))
    # --- 52-week / 26-week high proximity (near-duplicate lookback variants) ---
    C.append(FactorCandidate("high52w_proximity_topdecile", "high52w", 252, side="long", frac=0.10, mem_df=mem_df))
    C.append(FactorCandidate("high26w_proximity_topdecile", "high52w", 126, side="long", frac=0.10, mem_df=mem_df))
    return C

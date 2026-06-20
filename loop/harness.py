"""The IMMUTABLE scoring harness — the frozen judge.

Wraps engine.active_alloc.backtest_portfolio + engine.validation.* and adds ZERO new
statistics. The loop may mutate the strategy spec; it may NEVER edit how a candidate is
judged. Grounded invariants: cost_bps=3.0 (one-way), trading_year=252 (equities), the
purged-fold embargo >= the longest forward horizon.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import bot  # noqa: F401
from engine import active_alloc as aa, validation as V

COST_BPS = 3.0
TRADING_YEAR = 252
FOLDS = 5
EMBARGO = 63

# leave-one-crisis-out windows (the doctrine's robustness check)
CRISES = [("2008-08-01", "2009-03-31"), ("2020-02-15", "2020-04-30"), ("2022-01-01", "2022-10-31")]


def _crisis_pass(net: pd.Series, spy_ret: pd.Series) -> bool:
    """In every crisis window present in the data, the strategy must not badly
    underperform SPY (cumulative return gap >= -5pp)."""
    for a, b in CRISES:
        seg = net.loc[(net.index >= a) & (net.index <= b)]
        spy = spy_ret.loc[(spy_ret.index >= a) & (spy_ret.index <= b)]
        if len(seg) < 10:
            continue
        gap = float((1 + seg).prod() - 1) - float((1 + spy).prod() - 1)
        if gap < -0.05:
            return False
    return True


def score(candidate, closes: pd.DataFrame, bill: pd.Series, n_eff: int,
          sixty40_sharpe: float | None = None) -> dict | None:
    """Backtest one candidate through the frozen pipeline. Returns a metrics dict or None."""
    W = candidate.materialize(closes)
    bt = aa.backtest_portfolio(W, closes, bill, cost_bps=COST_BPS)
    net = bt["net"].dropna()
    m = V.ret_moments(net)
    if not m:
        return None
    sr_d, skew, kurt, T = m
    dsr = V.deflated_sharpe(sr_d, skew, kurt, T, n_trials=max(1, int(n_eff)),
                            trading_year=TRADING_YEAR) or {"dsr": 0.0}

    folds = V.purged_folds(net.index, k=FOLDS, embargo=EMBARGO)
    fold_signs = []
    for ix in folds.values():
        fm = V.ret_moments(net.loc[ix])
        fold_signs.append(1 if (fm and fm[0] > 0) else -1)
    fr = bool(V.fold_robust(1 if sr_d > 0 else -1, fold_signs, want=1))

    nw = V.newey_west_tstat(net)
    pval = nw.get("p")
    pval = float(pval) if pval is not None else 1.0

    spy_ret = closes.iloc[:, 0].pct_change().fillna(0)
    s6040 = sixty40_sharpe if sixty40_sharpe is not None else bt["hodl_sharpe"]
    return {
        "dsr": float(dsr["dsr"]), "dsr_verdict": V.dsr_verdict(dsr["dsr"]),
        "sharpe": float(bt["sharpe"]), "maxdd": float(bt["maxdd"]), "cagr": float(bt["cagr"]),
        "beats_spy": bool(bt["cagr"] > bt["hodl_cagr"] and bt["sharpe"] > bt["hodl_sharpe"]),
        "beats_6040": bool(bt["sharpe"] > s6040),
        "crisis_pass": _crisis_pass(net, spy_ret),
        "fold_robust": fr, "p_value": pval, "net": net,
    }


def sixty_forty_sharpe(closes: pd.DataFrame, bill: pd.Series) -> float:
    """The 60/40 SPY/bond baseline Sharpe (the second hurdle a candidate must beat)."""
    bond = "IEF" if "IEF" in closes.columns else closes.columns[-1]
    W = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    W[closes.columns[0]] = 0.6
    W[bond] = 0.4
    return float(aa.backtest_portfolio(W, closes, bill, cost_bps=COST_BPS)["sharpe"])

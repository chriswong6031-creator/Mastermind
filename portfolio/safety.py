"""Portfolio safety engine — an automatic, deterministic risk backtest of the book.

This answers a single blunt question: *how safe is the portfolio we are actually
holding, right now?* It takes the live book's weights and runs a **static-weight
historical simulation** of that exact basket over a multi-year window, then turns
the result into a falsifiable safety scorecard:

  * **max historical drawdown** of the WHOLE portfolio (peak-to-trough of the
    cash-included book, not the worst single name) — with a block-bootstrap CI,
  * **diversification** — effective number of names (1/HHI), sector concentration,
    mean pairwise correlation, and the Kritzman-Page absorption ratio (the share of
    variance trapped in one factor — the early tell of a fragile one-bet book),
  * **ticker correlations** — the pairwise correlation structure + the most
    correlated pairs (the hidden doubled-up bets),
  * **beta** — the book's market sensitivity vs SPY (whole-book, from the simulated
    return series, and the gross weighted-name beta as a cross-check),
  * **other factors** — annualized volatility, historical Sharpe / Sortino, downside
    deviation, 1-day 95% VaR / CVaR, per-name risk contribution, and the cash buffer.

It is read-only and **never an order**. Every number is a measurement, not a forecast.

HONESTY (this matters, per DOCTRINE.md):
  * This is a COUNTERFACTUAL. It asks "if you had held *today's* weights, statically,
    over the lookback, what would the risk have looked like?" — it is NOT how the book
    actually traded and NOT a forward guarantee.
  * The current names are SURVIVORS. A drawdown measured on today's winners understates
    the true risk of the selection process. We say so, in `caveats`.
  * Coverage is reported. A name with no price history contributes nothing and is listed
    as an uncovered gap; a low-coverage read is flagged low-confidence.

Math is REUSED from the vendored, already-validated macro engine (no scipy):
  validation._maxdd / _sharpe / block_bootstrap_ci, portfolio._risk_contrib,
  cross_asset._absorption_ratio, group_flow._mean_pairwise_corr, equity_factors._names_sectors.
Prices come from paper_account._fetch_price_series (vendored parquet: SPY, sector ETFs,
S&P single names) with a Polygon daily_closes fallback for off-parquet tickers.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import bot  # noqa: F401  -> vendor/macro bootstrap so engine.* / lib.* import

# --------------------------------------------------------------------------- #
# tunables (defaults; overridable via config/doctrine.yml `safety:` section)
# --------------------------------------------------------------------------- #
_DEFAULTS: dict[str, Any] = {
    "lookback_days": 1825,          # ~5y calendar window for the historical sim
    "min_obs_per_name": 60,         # a name needs >= this many closes to be "covered"
    "min_obs_total": 120,           # the book needs >= this many aligned days to score
    "covered_day_min_frac": 0.70,   # keep sim days where >=70% of invested weight has a return
    "bootstrap_B": 4000,            # block-bootstrap resamples (CI on Sharpe + maxDD)
    "bootstrap_block": 21,          # ~1 trading month blocks (preserve autocorrelation)
    # score-mapping anchors: metric value at which the sub-score hits 100 (good) / 0 (bad)
    "anchors": {
        "drawdown":   {"good": 0.0,  "bad": 0.45},   # |maxDD| fraction
        "volatility": {"good": 0.10, "bad": 0.45},   # annualized vol
        "beta":       {"good": 0.85, "bad": 2.00},   # whole-book beta vs SPY
        "cvar":       {"good": 0.012, "bad": 0.060}, # |1-day 95% CVaR| fraction
        "effective_n": {"good": 12.0, "bad": 1.5},   # 1/HHI
        "mean_corr":  {"good": 0.20, "bad": 0.80},
        "absorption": {"good": 0.35, "bad": 0.90},
        "largest_wt": {"good": 0.08, "bad": 0.30},   # biggest single-name weight
        "top5_wt":    {"good": 0.30, "bad": 0.80},
        "sector_hhi": {"good": 0.15, "bad": 0.60},
        "cash":       {"good": 0.20, "bad": 0.00},   # cash buffer (more = safer, up to 20%)
    },
    # composite weights — must sum to ~1.0
    "score_weights": {
        "drawdown": 0.22, "volatility": 0.18, "diversification": 0.18,
        "concentration": 0.14, "beta": 0.12, "tail": 0.10, "cash": 0.06,
    },
    # CONSUMED risk overlay — subtract-only de-gross of a fragile book (the safety read
    # actually changes the book: less gross, more cash). Risk control, never alpha; never levers.
    "overlay": {
        "enabled": True,
        "gross_floor": 0.50,    # never de-gross below 50% of the proposed book (rest -> cash)
        "score_full": 70.0,     # >= this safety score: no de-gross
        "score_min": 40.0,      # at/below this score: de-gross toward the floor
        "dd_soft_pct": 25.0,    # start de-grossing once |max drawdown| exceeds this
        "dd_hard_pct": 45.0,    # de-gross hits the floor by here
        "beta_target": 1.10,    # de-gross a book whose whole-book beta exceeds this
        "absorption_soft": 0.80,  # start de-grossing a one-factor-fragile book
        "absorption_hard": 0.95,
    },
}

_ROOT = Path(__file__).resolve().parent.parent

# ETFs are absent from the single-name GICS constituents map; classify the common ones so
# they don't all pool into a misleading "Unknown" concentration bucket. Sector SPDRs +
# thematic tech map to a GICS sector; broad/factor ETFs get their own diversifying bucket.
_ETF_SECTOR: dict[str, str] = {
    "XLE": "Energy", "XLK": "Information Technology", "XLF": "Financials",
    "XLV": "Health Care", "XLI": "Industrials", "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples", "XLU": "Utilities", "XLB": "Materials",
    "XLRE": "Real Estate", "XLC": "Communication Services",
    "SMH": "Information Technology", "SOXX": "Information Technology",
    "XSD": "Information Technology", "IGV": "Information Technology",
    "SKYY": "Information Technology", "VGT": "Information Technology",
    "XBI": "Health Care", "IBB": "Health Care", "KRE": "Financials",
    "XOP": "Energy", "GDX": "Materials", "ITB": "Consumer Discretionary",
    "XHB": "Consumer Discretionary", "XME": "Materials", "TAN": "Information Technology",
    # broad / factor / index ETFs — diversifying, not a single concentrated sector
    "SPY": "Broad / Factor ETF", "VOO": "Broad / Factor ETF", "IVV": "Broad / Factor ETF",
    "VTI": "Broad / Factor ETF", "QQQ": "Broad / Factor ETF", "MTUM": "Broad / Factor ETF",
    "IWM": "Broad / Factor ETF", "IWB": "Broad / Factor ETF", "IJH": "Broad / Factor ETF",
    "IJR": "Broad / Factor ETF", "RSP": "Broad / Factor ETF", "DIA": "Broad / Factor ETF",
    "VTV": "Broad / Factor ETF", "VUG": "Broad / Factor ETF", "USMV": "Broad / Factor ETF",
    "QUAL": "Broad / Factor ETF", "VEA": "Broad / Factor ETF", "EFA": "Broad / Factor ETF",
}


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _cfg() -> dict:
    """Merged safety config: hard defaults overlaid with config/doctrine.yml `safety:`."""
    try:
        from bot.doctrine_config import load_doctrine
        return _deep_merge(_DEFAULTS, (load_doctrine() or {}).get("safety", {}) or {})
    except Exception:
        return dict(_DEFAULTS)


def _doctrine_caps() -> dict:
    """The book's firebreaks (name cap, theme cap, cash floor) for breach checks."""
    out = {"name_cap": 0.08, "theme_cap_book": 0.25, "cash_floor": [0.05, 0.20],
           "rotation_floor": 0.05}
    try:
        from bot.doctrine_config import load_doctrine
        d = load_doctrine() or {}
        caps = d.get("caps") or {}
        out["name_cap"] = float(caps.get("name_cap", out["name_cap"]))
        out["theme_cap_book"] = float(caps.get("theme_cap_book", out["theme_cap_book"]))
        out["cash_floor"] = (d.get("sleeves") or {}).get("cash_floor", out["cash_floor"])
        out["rotation_floor"] = float((d.get("cash") or {}).get("rotation_floor",
                                                                 out["rotation_floor"]))
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- #
# book weights
# --------------------------------------------------------------------------- #
def _held_weights(portfolio_id: str | None) -> tuple[dict[str, float], float, float, str]:
    """Weights of the ACTUALLY-HELD book (market-value / NAV) + cash weight.

    Returns ({ticker: weight}, cash_weight, nav, source). `source` is "held" when the
    account carries live positions, else "" so the caller can fall back to the target book.
    """
    try:
        from portfolio import paper_account
    except Exception:
        return {}, 1.0, 0.0, ""
    try:
        state = paper_account._load_account(portfolio_id)
    except Exception:
        return {}, 1.0, 0.0, ""
    positions = (state or {}).get("positions") or {}
    cash = float((state or {}).get("cash") or 0.0)
    if not positions:
        return {}, 1.0, cash, ""
    mv: dict[str, float] = {}
    for t, pos in positions.items():
        shares = float((pos or {}).get("shares") or 0.0)
        if shares == 0.0:
            continue
        px = paper_account._current_price(t)
        if px is None or px <= 0:
            px = float((pos or {}).get("avg_cost") or 0.0)
        if px > 0:
            mv[t] = shares * px
    nav = cash + sum(mv.values())
    if nav <= 0:
        return {}, 1.0, cash, ""
    weights = {t: v / nav for t, v in mv.items() if v > 0}
    return weights, cash / nav, nav, "held"


def _target_weights(portfolio_id: str | None) -> tuple[dict[str, float], float, str]:
    """Weights from the latest built book (latest.json target weights) + cash weight.

    The fallback when nothing has been *filled* yet: a freshly-built book still gets a
    safety read against its intended allocation. Returns ({ticker: weight}, cash, source).
    """
    try:
        from portfolio import registry
        path = registry.data_dir(portfolio_id) / "latest.json"
    except Exception:
        path = _ROOT / "data" / "portfolio" / "latest.json"
    if not path.exists():
        return {}, 1.0, ""
    try:
        d = json.loads(path.read_text())
    except Exception:
        return {}, 1.0, ""
    weights: dict[str, float] = {}
    for p in d.get("positions") or []:
        t = p.get("ticker")
        w = p.get("weight")
        if t and isinstance(w, (int, float)) and w > 0:
            weights[str(t)] = weights.get(str(t), 0.0) + float(w)
    if not weights:
        return {}, 1.0, ""
    invested = sum(weights.values())
    cash = float(d.get("cash") if isinstance(d.get("cash"), (int, float)) else max(0.0, 1.0 - invested))
    return weights, cash, "target"


def _sleeve_map(portfolio_id: str | None) -> dict[str, str]:
    """{ticker: sleeve} from latest.json (best-effort) for a sleeve-level breakdown."""
    try:
        from portfolio import registry
        path = registry.data_dir(portfolio_id) / "latest.json"
        d = json.loads(path.read_text())
        return {str(p.get("ticker")): str(p.get("sleeve") or "—")
                for p in (d.get("positions") or []) if p.get("ticker")}
    except Exception:
        return {}


def book_weights(portfolio_id: str | None) -> dict[str, Any]:
    """Resolve the book to score: held positions first, target book as the fallback."""
    weights, cash_w, nav, source = _held_weights(portfolio_id)
    if not weights:
        weights, cash_w, source = _target_weights(portfolio_id)
        nav = nav or 0.0
    invested_w = float(sum(weights.values()))
    return {"weights": weights, "cash_weight": round(cash_w, 4),
            "invested_weight": round(invested_w, 4), "nav": nav,
            "source": source, "n_positions": len(weights)}


# --------------------------------------------------------------------------- #
# prices
# --------------------------------------------------------------------------- #
def _series(ticker: str, start: pd.Timestamp, end: pd.Timestamp, min_obs: int,
            network: bool = True) -> pd.Series | None:
    """Daily close series for one `ticker` in [start, end]. Vendored stores first
    (paper_account._fetch_price_series: yahoo store ETFs/SPY + breadth parquet single names);
    Polygon daily_closes fallback for off-parquet tickers only when `network` is allowed."""
    try:
        from portfolio import paper_account
        s = paper_account._fetch_price_series(ticker)
        if s is not None and len(s):
            s = s[(s.index >= start) & (s.index <= end)].dropna()
            if len(s) >= min_obs:
                return s.sort_index()
    except Exception:
        pass
    if not network:
        return None
    # Polygon fallback (cached on disk; window is fully elapsed so cache is safe)
    try:
        from data_layer import polygon
        d = polygon.daily_closes(ticker, start.date().isoformat(), end.date().isoformat())
        if d:
            s2 = pd.Series(d, dtype=float)
            s2.index = pd.to_datetime(s2.index)
            s2 = s2.sort_index().dropna()
            if len(s2) >= min_obs:
                return s2
    except Exception:
        pass
    return None


_BREADTH_CACHE: dict = {}


def _breadth_frame() -> "pd.DataFrame | None":
    """The vendored S&P single-name close panel, read ONCE and cached (so an N-name book is
    one parquet read, not N). Returns None if the vendored store isn't present."""
    if "df" in _BREADTH_CACHE:
        return _BREADTH_CACHE["df"]
    df = None
    try:
        from lib import config  # vendored macro lib
        p = config.data_dir() / "breadth" / "_closes_cache.parquet"
        if p.exists():
            df = pd.read_parquet(p)
            df.index = pd.to_datetime(df.index)
    except Exception:
        df = None
    _BREADTH_CACHE["df"] = df
    return df


def _aligned(tickers: list[str], start: pd.Timestamp, end: pd.Timestamp,
             min_obs: int, network: bool = True) -> tuple[pd.DataFrame, list[str]]:
    """Outer-aligned daily CLOSES frame (dates x covered tickers) + the covered list.

    Fast path: pull every S&P single name from the breadth panel in ONE read; only ETFs/SPY
    and off-panel names fall to the per-ticker loader (store + optional Polygon)."""
    cols: dict[str, pd.Series] = {}
    remaining = list(tickers)
    bf = _breadth_frame()
    if bf is not None and len(bf):
        in_panel = [t for t in remaining if t in bf.columns]
        if in_panel:
            sub = bf.loc[(bf.index >= start) & (bf.index <= end), in_panel]
            for t in in_panel:
                s = sub[t].astype(float).dropna()
                if len(s) >= min_obs:
                    cols[t] = s.sort_index()
        remaining = [t for t in remaining if t not in cols]
    for t in remaining:
        s = _series(t, start, end, min_obs, network=network)
        if s is not None and len(s) >= min_obs:
            cols[t] = s
    if not cols:
        return pd.DataFrame(), []
    closes = pd.DataFrame(cols).sort_index()
    return closes, list(cols.keys())


# --------------------------------------------------------------------------- #
# scoring helpers
# --------------------------------------------------------------------------- #
def _score(x: float | None, good: float, bad: float) -> float | None:
    """Linear map x -> [0,100], clamped: good -> 100, bad -> 0 (good may be < or > bad)."""
    if x is None or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    if good == bad:
        return 50.0
    s = (x - bad) / (good - bad) * 100.0
    return float(max(0.0, min(100.0, s)))


def _avg(vals: list[float | None]) -> float | None:
    keep = [v for v in vals if v is not None and math.isfinite(v)]
    return float(sum(keep) / len(keep)) if keep else None


def _grade(score: float | None) -> str:
    if score is None:
        return "—"
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "F"


def _verdict(score: float | None) -> str:
    if score is None:
        return "Not enough price history to assess safety."
    if score >= 85:
        return "Resilient — well-diversified with contained drawdown and market sensitivity."
    if score >= 70:
        return "Sound — reasonable risk profile; watch the flagged concentrations."
    if score >= 55:
        return "Moderate risk — diversification or drawdown is starting to bite."
    if score >= 40:
        return "Fragile — concentrated and/or drawdown-prone; trim the flagged risks."
    return "High risk — the book behaves like a single concentrated bet."


# --------------------------------------------------------------------------- #
# the report
# --------------------------------------------------------------------------- #
def compute_safety(portfolio_id: str | None = None, *, asof: str | None = None,
                   lookback_days: int | None = None, bootstrap: bool = True,
                   weights: dict[str, float] | None = None,
                   cash_weight: float | None = None, network: bool = True) -> dict:
    """Run the static-weight historical risk backtest of the current book and score it.

    Never raises — returns a payload carrying a `note` on any failure. `asof` lets a
    backtest reconstruct the window end (defaults to today); `bootstrap=False` skips the
    (heavier) block-bootstrap CI for a snappier live path.

    Pass an explicit `weights` map ({ticker: fraction-of-book}) to score a PROPOSED book
    in-flight (the bot's pre-build path) instead of reading the persisted account — this is
    how the safety read is CONSUMED by phase2's de-gross overlay before the book is written.
    """
    cfg = _cfg()
    caps = _doctrine_caps()
    anchors = cfg["anchors"]
    asof = asof or date.today().isoformat()
    end = pd.Timestamp(asof)
    lb = int(lookback_days or cfg["lookback_days"])
    start = end - pd.Timedelta(days=lb)

    out: dict[str, Any] = {
        "portfolio_id": portfolio_id or "flagship",
        "as_of": asof,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "backtest": {"method": "static-weight historical simulation of current holdings",
                     "rebalanced": False, "lookback_days": lb},
        "doctrine": caps,
    }

    if weights is not None:                      # score a proposed book passed in-flight
        invested = float(sum(weights.values()))
        cw = cash_weight if cash_weight is not None else round(max(0.0, 1.0 - invested), 4)
        bw = {"weights": dict(weights), "cash_weight": round(cw, 4),
              "invested_weight": round(invested, 4), "nav": 0.0,
              "source": "proposed", "n_positions": len(weights)}
    else:
        bw = book_weights(portfolio_id)
    out.update({k: bw[k] for k in ("cash_weight", "invested_weight", "nav",
                                   "source", "n_positions")})
    weights = bw["weights"]
    if not weights:
        out["note"] = "No holdings to assess (book is all cash / not built yet)."
        out["safety_score"], out["grade"] = None, "—"
        out["verdict"] = _verdict(None)
        return out

    held = sorted(weights, key=lambda t: weights[t], reverse=True)
    closes, covered = _aligned(held, start, end, int(cfg["min_obs_per_name"]), network=network)
    uncovered = [t for t in held if t not in covered]

    # ---- holdings-structure metrics (price-independent: use ALL held names) -------
    inv_total = sum(weights.values()) or 1.0
    w_inv = {t: weights[t] / inv_total for t in held}            # renormalized to invested
    hhi = float(sum(v * v for v in w_inv.values()))
    effective_n = float(1.0 / hhi) if hhi > 0 else float(len(held))
    largest_t = max(w_inv, key=w_inv.get)
    top5 = sorted(w_inv.values(), reverse=True)[:5]
    sectors = _sector_breakdown(held, w_inv)

    metrics: dict[str, Any] = {
        "effective_n": round(effective_n, 2),
        "hhi": round(hhi, 4),
        "n_names": len(held),
        "largest_weight": {"ticker": largest_t, "weight": round(weights[largest_t], 4)},
        "top5_weight": round(float(sum(top5)), 4),
        "sector_hhi": sectors["hhi"],
        "n_sectors": sectors["n_sectors"],
        "top_sector": sectors["top"],
        "sectors": sectors["rows"],
    }

    coverage = {"n_covered": len(covered), "n_total": len(held),
                "weight_covered": round(float(sum(weights[t] for t in covered)), 4),
                "uncovered": uncovered}
    out["coverage"] = coverage

    if len(covered) == 0 or closes.empty:
        out["metrics"] = metrics
        out["note"] = "Holdings have no usable price history — structure-only read."
        _finalize_score(out, metrics, anchors, cfg, caps, weights, w_inv, coverage, partial=True)
        return out

    # ---- price-driven metrics: the static-weight historical simulation -----------
    rets = closes.pct_change()
    wv = pd.Series({t: weights[t] for t in covered}).reindex(rets.columns).fillna(0.0)
    invested_cov = float(wv.sum())                              # invested weight w/ history

    valid = rets.notna()
    covered_w_by_day = valid.mul(wv, axis=1).sum(axis=1)        # invested weight live that day
    port = rets.fillna(0.0).mul(wv, axis=1).sum(axis=1)         # cash + gaps contribute 0
    thresh = float(cfg["covered_day_min_frac"]) * max(invested_cov, 1e-9)
    keep = (covered_w_by_day >= thresh) & (covered_w_by_day > 0)
    port = port[keep].dropna()

    spy = _series("SPY", start, end, int(cfg["min_obs_per_name"]), network=network)
    spy_ret = spy.pct_change().dropna() if spy is not None else None

    n_obs = int(len(port))
    out["lookback"] = {"days": lb,
                       "start": (port.index[0].date().isoformat() if n_obs else None),
                       "end": (port.index[-1].date().isoformat() if n_obs else None),
                       "n_obs": n_obs}

    if n_obs < int(cfg["min_obs_total"]):
        out["metrics"] = metrics
        out["note"] = f"Only {n_obs} aligned observations (<{cfg['min_obs_total']}) — structure-only read."
        _finalize_score(out, metrics, anchors, cfg, caps, weights, w_inv, coverage, partial=True)
        return out

    # whole-book drawdown / vol / tail / risk-adjusted (cash included via 0-contribution)
    try:
        from engine.validation import _maxdd, _sharpe
    except Exception:
        _maxdd = lambda r: float(np.min(np.cumprod(1 + np.asarray(r, float)) /  # noqa: E731
                                        np.maximum.accumulate(np.cumprod(1 + np.asarray(r, float))) - 1))
        _sharpe = lambda r, ann: (float(np.mean(r) / np.std(r) * math.sqrt(ann))  # noqa: E731
                                  if np.std(r) else float("nan"))

    arr = port.to_numpy()
    ann_vol = float(np.std(arr, ddof=1) * math.sqrt(252))
    maxdd = float(_maxdd(arr))
    sharpe = float(_sharpe(arr, 252))
    downside = arr[arr < 0]
    sortino = (float(np.mean(arr) / np.std(downside, ddof=1) * math.sqrt(252))
               if downside.size > 1 and np.std(downside, ddof=1) > 0 else None)
    dd_dev = float(np.std(downside, ddof=1) * math.sqrt(252)) if downside.size > 1 else None
    var95 = float(np.percentile(arr, 5))                       # 5th-pct daily return (negative)
    cvar95 = float(arr[arr <= var95].mean()) if (arr <= var95).any() else var95

    # beta / correlation vs SPY on the common dates
    beta_spy = corr_spy = None
    if spy_ret is not None and len(spy_ret):
        j = pd.concat([port.rename("p"), spy_ret.rename("m")], axis=1, join="inner").dropna()
        if len(j) >= 30:
            var_m = float(j["m"].var())
            if var_m > 0:
                beta_spy = round(float(j["p"].cov(j["m"]) / var_m), 3)
            corr_spy = round(float(j["p"].corr(j["m"])), 3)

    metrics.update({
        "max_drawdown_pct": round(maxdd * 100, 2),
        "ann_volatility_pct": round(ann_vol * 100, 2),
        "sharpe": round(sharpe, 2) if math.isfinite(sharpe) else None,
        "sortino": round(sortino, 2) if sortino is not None else None,
        "downside_dev_pct": round(dd_dev * 100, 2) if dd_dev is not None else None,
        "var95_1d_pct": round(var95 * 100, 2),
        "cvar95_1d_pct": round(cvar95 * 100, 2),
        "beta_spy": beta_spy,
        "corr_spy": corr_spy,
    })

    # name-structure: correlation, absorption, per-name risk contribution + beta
    _name_structure(metrics, rets, covered, weights, spy_ret)

    # block-bootstrap CI on the whole-book Sharpe + maxDD (uncertainty, not a point)
    if bootstrap:
        try:
            from engine.validation import block_bootstrap_ci
            ci = block_bootstrap_ci(port, block=int(cfg["bootstrap_block"]),
                                    B=int(cfg["bootstrap_B"]), ann=252)
            if ci:
                metrics["sharpe_ci"] = ci.get("sharpe_ci")
                metrics["max_drawdown_ci_pct"] = ci.get("maxdd_ci_pct")
                metrics["sharpe_gt0_prob"] = ci.get("sharpe_gt0_prob")
        except Exception:
            pass

    out["metrics"] = metrics
    _finalize_score(out, metrics, anchors, cfg, caps, weights, w_inv, coverage, partial=False)
    return out


def _sector_breakdown(held: list[str], w_inv: dict[str, float]) -> dict:
    """Sector weights + HHI from the vendored GICS constituents map (best-effort)."""
    smap: dict[str, tuple[str, str]] = {}
    try:
        from engine.equity_factors import _names_sectors
        smap = _names_sectors("broad") or {}
    except Exception:
        smap = {}
    by_sector: dict[str, float] = {}
    counts: dict[str, int] = {}
    for t in held:
        sec = _ETF_SECTOR.get(t) or (smap.get(t) or (t, "Unknown"))[1] or "Unknown"
        by_sector[sec] = by_sector.get(sec, 0.0) + w_inv.get(t, 0.0)
        counts[sec] = counts.get(sec, 0) + 1
    if not by_sector:
        return {"hhi": None, "n_sectors": 0, "top": None, "rows": []}
    tot = sum(by_sector.values()) or 1.0
    rows = sorted(({"sector": s, "weight": round(w / tot, 4), "n": counts[s]}
                   for s, w in by_sector.items()), key=lambda r: r["weight"], reverse=True)
    hhi = float(sum((w / tot) ** 2 for w in by_sector.values()))
    return {"hhi": round(hhi, 4), "n_sectors": len(by_sector), "top": rows[0], "rows": rows}


def _name_structure(metrics: dict, rets: pd.DataFrame, covered: list[str],
                    weights: dict[str, float], spy_ret: pd.Series | None) -> None:
    """Correlation matrix, absorption ratio, per-name beta + risk contribution."""
    R = rets[covered].dropna()
    if len(R) < 30 or R.shape[1] < 2:
        # too thin for a stable matrix — fall back to per-name vols only
        per = []
        for t in covered:
            s = rets[t].dropna()
            per.append({"ticker": t, "weight": round(weights[t], 4),
                        "vol_pct": round(float(s.std() * math.sqrt(252) * 100), 2) if len(s) > 5 else None})
        metrics["per_name"] = per
        return

    corr = R.corr()
    # mean pairwise correlation (cohesion) — reuse the engine's robust version
    try:
        from engine.group_flow import _mean_pairwise_corr
        mpc = _mean_pairwise_corr(R)
    except Exception:
        c = corr.to_numpy()
        n = c.shape[0]
        mpc = float((np.nansum(c) - np.trace(c)) / (n * (n - 1))) if n > 1 else None
    metrics["mean_pairwise_corr"] = round(mpc, 3) if mpc is not None else None

    try:
        from engine.cross_asset import _absorption_ratio
        cc = R.loc[:, R.std() > 0]
        ar = _absorption_ratio(cc.corr().to_numpy()) if cc.shape[1] >= 2 else None
    except Exception:
        ar = None
    metrics["absorption_ratio"] = round(ar, 3) if (ar is not None and math.isfinite(ar)) else None

    # top correlated pairs (the hidden doubled-up bets)
    pairs = []
    cols = list(corr.columns)
    for i in range(len(cols)):
        for jx in range(i + 1, len(cols)):
            v = corr.iloc[i, jx]
            if pd.notna(v):
                pairs.append((cols[i], cols[jx], float(v)))
    pairs.sort(key=lambda p: abs(p[2]), reverse=True)
    metrics["top_correlated_pairs"] = [{"a": a, "b": b, "corr": round(c, 2)}
                                       for a, b, c in pairs[:6]]

    # per-name risk contribution (share of book variance) + market beta
    cov = R.cov().to_numpy()
    w_cov = np.array([weights[t] for t in covered], float)
    w_cov = w_cov / w_cov.sum() if w_cov.sum() > 0 else w_cov
    rc = None
    try:
        from engine.portfolio import _risk_contrib
        rc = _risk_contrib(w_cov, cov)
    except Exception:
        pv = float(w_cov @ cov @ w_cov)
        rc = (w_cov * (cov @ w_cov)) / pv if pv > 0 else np.full(len(w_cov), np.nan)

    var_m = float(spy_ret.var()) if (spy_ret is not None and len(spy_ret)) else None
    per = []
    wname_beta = 0.0
    have_beta = False
    for k, t in enumerate(covered):
        s = R[t]
        beta = corr_spy = None
        if spy_ret is not None and var_m and var_m > 0:
            j = pd.concat([s.rename("a"), spy_ret.rename("m")], axis=1, join="inner").dropna()
            if len(j) >= 30:
                beta = round(float(j["a"].cov(j["m"]) / var_m), 2)
                corr_spy = round(float(j["a"].corr(j["m"])), 2)
                wname_beta += w_cov[k] * beta
                have_beta = True
        per.append({"ticker": t, "weight": round(weights[t], 4),
                    "vol_pct": round(float(s.std() * math.sqrt(252) * 100), 2),
                    "beta": beta, "corr_spy": corr_spy,
                    "risk_contribution": (round(float(rc[k]), 4)
                                          if rc is not None and np.isfinite(rc[k]) else None)})
    per.sort(key=lambda r: (r.get("risk_contribution") or 0), reverse=True)
    metrics["per_name"] = per
    metrics["weighted_name_beta"] = round(float(wname_beta), 3) if have_beta else None


def _finalize_score(out: dict, metrics: dict, anchors: dict, cfg: dict, caps: dict,
                    weights: dict[str, float], w_inv: dict[str, float],
                    coverage: dict, partial: bool) -> None:
    """Turn measured metrics into sub-scores, a composite safety score, breaches, caveats."""
    a = anchors

    def frac(key, take_abs=False):
        """A percent metric as a fraction (None if absent); abs() for signed losses."""
        v = metrics.get(key)
        if not isinstance(v, (int, float)):
            return None
        return abs(v) / 100.0 if take_abs else v / 100.0

    sc: dict[str, float | None] = {}
    sc["drawdown"] = _score(frac("max_drawdown_pct", True), a["drawdown"]["good"], a["drawdown"]["bad"])
    sc["volatility"] = _score(frac("ann_volatility_pct"), a["volatility"]["good"], a["volatility"]["bad"])
    sc["tail"] = _score(frac("cvar95_1d_pct", True), a["cvar"]["good"], a["cvar"]["bad"])
    beta_v = metrics.get("beta_spy")
    sc["beta"] = _score(abs(beta_v) if isinstance(beta_v, (int, float)) else None,
                        a["beta"]["good"], a["beta"]["bad"])

    div = _avg([
        _score(metrics.get("effective_n"), a["effective_n"]["good"], a["effective_n"]["bad"]),
        _score(metrics.get("mean_pairwise_corr"), a["mean_corr"]["good"], a["mean_corr"]["bad"]),
        _score(metrics.get("absorption_ratio"), a["absorption"]["good"], a["absorption"]["bad"]),
    ])
    sc["diversification"] = div

    largest = (metrics.get("largest_weight") or {}).get("weight")
    conc = _avg([
        _score(largest, a["largest_wt"]["good"], a["largest_wt"]["bad"]),
        _score(metrics.get("top5_weight"), a["top5_wt"]["good"], a["top5_wt"]["bad"]),
        _score(metrics.get("sector_hhi"), a["sector_hhi"]["good"], a["sector_hhi"]["bad"]),
    ])
    sc["concentration"] = conc

    sc["cash"] = _score(out.get("cash_weight"), a["cash"]["good"], a["cash"]["bad"])

    out["subscores"] = {k: (round(v, 1) if v is not None else None) for k, v in sc.items()}

    # composite — reweight over the sub-scores that are actually available
    sw = cfg["score_weights"]
    num = den = 0.0
    for k, wgt in sw.items():
        v = sc.get(k)
        if v is not None:
            num += wgt * v
            den += wgt
    score = round(num / den, 1) if den > 0 else None
    out["safety_score"] = score
    out["grade"] = _grade(score)
    out["verdict"] = _verdict(score)
    out["score_confidence"] = ("low" if (partial or coverage["weight_covered"] < 0.6
                               or den < 0.6) else "high")

    out["breaches"] = _breaches(metrics, caps, weights, out, coverage)
    out["caveats"] = _caveats(out, coverage, partial)


def _breaches(metrics: dict, caps: dict, weights: dict[str, float], out: dict,
              coverage: dict) -> list[dict]:
    """Doctrine firebreak + fragility breaches, detector-style (severity flag/warn)."""
    br: list[dict] = []
    name_cap = float(caps.get("name_cap", 0.08))
    # the single-name cap guards individual STOCKS (conviction sleeve); a sector/broad ETF
    # is internally diversified and is deliberately sized larger in the leadership sleeve,
    # so exclude ETFs from this breach to avoid a false positive on the 12.5% leadership legs.
    over = [(t, w) for t, w in weights.items()
            if w > name_cap + 1e-9 and t not in _ETF_SECTOR]
    for t, w in sorted(over, key=lambda x: -x[1]):
        br.append({"code": "name_cap", "severity": "warn",
                   "message": f"{t} is {w*100:.1f}% of the book — over the {name_cap*100:.0f}% single-name cap.",
                   "ticker": t, "weight": round(w, 4), "limit": name_cap})

    top5 = metrics.get("top5_weight")
    if isinstance(top5, (int, float)) and top5 >= 0.60:
        br.append({"code": "top_heavy", "severity": "flag",
                   "message": f"The top 5 positions are {top5*100:.0f}% of the invested book — concentrated at the top.",
                   "top5_weight": top5})

    cf = caps.get("cash_floor") or [0.05, 0.20]
    lo = float(cf[0]) if isinstance(cf, (list, tuple)) and cf else 0.05
    cashw = out.get("cash_weight")
    if isinstance(cashw, (int, float)) and cashw < float(caps.get("rotation_floor", lo)) - 1e-9:
        br.append({"code": "cash_floor", "severity": "warn",
                   "message": f"Cash is {cashw*100:.1f}% — below the {float(caps.get('rotation_floor', lo))*100:.0f}% rotation floor (no dry powder to rotate).",
                   "cash_weight": round(cashw, 4)})

    ar = metrics.get("absorption_ratio")
    if isinstance(ar, (int, float)) and ar >= 0.8:
        br.append({"code": "fragile_one_factor", "severity": "warn",
                   "message": f"Absorption ratio {ar:.2f} — most of the book's variance loads on one factor (fragile to a single shock).",
                   "absorption_ratio": ar})

    eff = metrics.get("effective_n")
    if isinstance(eff, (int, float)) and eff < 4:
        br.append({"code": "low_breadth", "severity": "warn",
                   "message": f"Effective breadth is only ~{eff:.1f} names — the book behaves like a handful of bets.",
                   "effective_n": eff})

    beta_v = metrics.get("beta_spy")
    if isinstance(beta_v, (int, float)) and beta_v >= 1.5:
        br.append({"code": "high_beta", "severity": "flag",
                   "message": f"Whole-book beta is {beta_v:.2f} — amplifies market moves ~{beta_v:.1f}x.",
                   "beta_spy": beta_v})

    mdd = metrics.get("max_drawdown_pct")
    if isinstance(mdd, (int, float)) and mdd <= -35:
        br.append({"code": "deep_drawdown", "severity": "flag",
                   "message": f"This basket would have drawn down {mdd:.0f}% peak-to-trough in the lookback.",
                   "max_drawdown_pct": mdd})

    if coverage["weight_covered"] < 0.6:
        br.append({"code": "low_coverage", "severity": "flag",
                   "message": f"Only {coverage['weight_covered']*100:.0f}% of the book has price history — read with caution.",
                   "weight_covered": coverage["weight_covered"]})
    return br


def _caveats(out: dict, coverage: dict, partial: bool) -> list[str]:
    c = [
        "Static-weight counterfactual: this holds today's exact weights flat over the "
        "lookback — it is not how the book actually traded and not a forward guarantee.",
        "Survivorship: the current names are survivors, so the historical drawdown of "
        "today's book understates the true risk of the selection process.",
    ]
    if out.get("source") == "target":
        c.append("Scored against the latest BUILT (target) weights — the book has not been filled yet.")
    if coverage.get("uncovered"):
        c.append(f"No price history for: {', '.join(coverage['uncovered'][:8])}"
                 + ("…" if len(coverage["uncovered"]) > 8 else "")
                 + " — these contribute nothing to the simulation.")
    if partial:
        c.append("Insufficient aligned history for the full price-driven read — structure metrics only.")
    return c


# --------------------------------------------------------------------------- #
# CONSUMED levers — the safety read CHANGES the book (not just the dashboard)
# --------------------------------------------------------------------------- #
def gross_overlay(report: dict, cfg: dict | None = None) -> dict:
    """SUBTRACT-ONLY gross/cash overlay derived from the safety read — the consumed lever.

    When the proposed/held book measures fragile, de-gross it: every position is scaled down
    by ``gross_mult`` (<= 1.0) and the freed weight becomes cash, so the bot actually CARRIES
    LESS RISK. It never levers up. This is risk control (drawdown / exposure management), NOT
    an alpha claim — the doctrine sizes cash and de-grosses a fragile book; this makes it
    mechanical. The most conservative trigger wins. ``gross_mult == 1.0`` is a no-op (disabled,
    no scorable read, or already-safe book).

    Returns {gross_mult, cash_added, reasons, triggers, enabled}.
    """
    cfg = cfg or _cfg()
    ov = cfg.get("overlay") or {}
    out = {"gross_mult": 1.0, "cash_added": 0.0, "reasons": [], "triggers": [],
           "enabled": bool(ov.get("enabled", True))}
    if not out["enabled"] or not report:
        return out
    floor = float(ov.get("gross_floor", 0.50))
    m = report.get("metrics") or {}
    mults: list[float] = []

    def _trip(reason, gm, **extra):
        gm = round(min(1.0, max(floor, float(gm))), 3)
        if gm < 1.0:
            mults.append(gm)
            out["triggers"].append({"reason": reason, "gross_mult": gm, **extra})

    score = report.get("safety_score")
    sf, sm = float(ov.get("score_full", 70.0)), float(ov.get("score_min", 40.0))
    if isinstance(score, (int, float)) and score < sf and sf > sm:
        _trip("low_safety_score", (score - sm) / (sf - sm), score=score)

    mdd = m.get("max_drawdown_pct")
    dds, ddh = float(ov.get("dd_soft_pct", 25.0)), float(ov.get("dd_hard_pct", 45.0))
    if isinstance(mdd, (int, float)) and abs(mdd) > dds and ddh > dds:
        _trip("deep_drawdown", 1.0 - (abs(mdd) - dds) / (ddh - dds) * (1.0 - floor),
              max_drawdown_pct=mdd)

    beta = m.get("beta_spy")
    bt = float(ov.get("beta_target", 1.10))
    if isinstance(beta, (int, float)) and beta > bt > 0:
        _trip("high_beta", bt / beta, beta_spy=beta)             # scaling book by gm scales beta by gm

    ar = m.get("absorption_ratio")
    afs, afh = float(ov.get("absorption_soft", 0.80)), float(ov.get("absorption_hard", 0.95))
    if isinstance(ar, (int, float)) and ar > afs and afh > afs:
        _trip("fragile_one_factor", 1.0 - (ar - afs) / (afh - afs) * (1.0 - floor),
              absorption_ratio=ar)

    if mults:
        gm = round(min(mults), 3)                                # most conservative; subtract-only
        out["gross_mult"] = gm
        invested = float(report.get("invested_weight") or 0.0)
        out["cash_added"] = round(invested * (1.0 - gm), 4)
        out["reasons"] = [t["reason"] for t in out["triggers"]]
    return out


def fragility_detectors(report: dict, mode: str = "self") -> list[dict]:
    """Whole-book fragility breaches as detector records — the SAME shape brain/detectors emits
    (``code/mode/subject/severity/payload``), so the safety read flows through the existing
    veto/advisory pipeline rather than living only on a panel. ``D7`` = book-level fragility
    (drawdown / one-factor concentration / breadth / beta). The single-name cap is left to D6
    (sleeves.enforce_book_caps) to avoid double-counting.
    """
    if not report:
        return []
    out: list[dict] = []
    for b in (report.get("breaches") or []):
        code = b.get("code")
        if code not in ("deep_drawdown", "fragile_one_factor", "low_breadth", "high_beta"):
            continue
        sev = ("veto" if mode == "self" else "flag") if b.get("severity") == "warn" else "flag"
        out.append({"code": "D7", "mode": mode, "subject": "book", "lot_id": None,
                    "severity": sev, "unverified": (mode == "operator"),
                    "payload": {"risk": code, "advisory": b.get("message")}})
    return out


# --------------------------------------------------------------------------- #
# persistence + batch (the automatic nightly hook reads/writes these)
# --------------------------------------------------------------------------- #
def _safety_path(portfolio_id: str | None) -> Path:
    try:
        from portfolio import registry
        return registry.data_dir(portfolio_id) / "safety.json"
    except Exception:
        return _ROOT / "data" / "portfolio" / "safety.json"


def persist(report: dict, portfolio_id: str | None = None) -> Path:
    p = _safety_path(portfolio_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(report, indent=2, default=str))
    return p


def load_safety(portfolio_id: str | None = None) -> dict | None:
    p = _safety_path(portfolio_id)
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except Exception:
        return None


def safety_for_all(asof: str | None = None, *, bootstrap: bool = True) -> dict:
    """Compute + persist a safety report for every registered portfolio (the nightly job)."""
    try:
        from portfolio import registry
        ids = registry.ids()
    except Exception:
        ids = ["flagship"]
    summary: dict[str, Any] = {}
    for pid in ids:
        try:
            rep = compute_safety(pid, asof=asof, bootstrap=bootstrap)
            persist(rep, pid)
            summary[pid] = {"safety_score": rep.get("safety_score"), "grade": rep.get("grade"),
                            "source": rep.get("source"), "n_positions": rep.get("n_positions")}
        except Exception as e:  # noqa: BLE001 — one bad book never breaks the loop
            summary[pid] = {"error": str(e)[:200]}
    return {"asof": asof or date.today().isoformat(), "portfolios": summary}


if __name__ == "__main__":
    import sys
    pid = sys.argv[1] if len(sys.argv) > 1 else None
    r = compute_safety(pid)
    print(json.dumps({k: r.get(k) for k in
                      ("portfolio_id", "source", "n_positions", "cash_weight",
                       "safety_score", "grade", "verdict", "subscores")}, indent=2, default=str))
    print("metrics:", json.dumps(r.get("metrics", {}), indent=2, default=str)[:1500])

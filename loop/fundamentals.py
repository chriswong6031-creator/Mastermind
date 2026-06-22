"""PIT fundamental factors (value + quality) — deepening the alpha research beyond price.

The price factor zoo (loop/factor_zoo) is closes-only. This adds the OTHER half of the cross-section
— valuation and quality — from SEC EDGAR, scored through the SAME frozen multiple-testing gauntlet.

The one thing that MUST be right is point-in-time correctness: using today's fundamentals on a past
date is catastrophic look-ahead. The EDGAR panel carries `asof_date` (the filing/availability date,
~a fiscal quarter after `period_end`), so the rule is strict: at rebalance date t, a name may only
use its latest fundamental row with `asof_date <= t`. Market cap uses the CURRENT price × the
last-REPORTED share count (both known at t), so value ratios re-rate daily with price while the
fundamental numerator only steps on a real filing.

Factors (all long-the-attractive-end, decile/quintile, monthly rebalance, PIT S&P-1500 membership,
survivorship-safe, $5 floor — inherited from the audited factor_experiment eligibility):
  VALUE   ep, bp, cfp, sp (sales/price), shareholder_yield  (high = cheap = long)
  QUALITY gross_prof, roe, roa  (high = good = long) · accruals, asset_growth  (low = good → negated)

Coverage: ~1,326 names overlapping the closes panel; EDGAR structured data starts ~2010, so the
fundamental backtest runs 2010→2026 (2022+ holdout intact). Volume/liquidity factors are NOT built —
there is no historical volume panel here (documented data gap, not faked).

Heavy + holdout-burning → on-demand (scripts/run_fundamentals.py); dashboard reads the persisted
data/backtest/fundamentals.json. Best-effort: honest 'unavailable' stub on missing data/infra.
"""
from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_EDGAR = _ROOT / "vendor" / "macro" / "data" / "edgar" / "fundamentals_panel.parquet"
_OUT = _ROOT / "data" / "backtest" / "fundamentals.json"

_FUND_START = "2010-01-01"     # EDGAR structured-data era (asof_date coverage)
_IC_HORIZONS = [21, 63, 126]

# metric → (how to read it, sign). sign +1 = long high; -1 = long low (negate before ranking).
# 'needs_price' metrics divide by market cap (price × shares); the rest are fundamental-only ratios.
_VALUE = ["ep", "bp", "cfp", "sp", "shareholder_yield"]
_QUALITY_HI = ["gross_prof", "roe", "roa"]
_QUALITY_LO = ["accruals", "asset_growth"]


# ─────────────────────────────────────────────────────────────────────────────
# PIT EDGAR loader
# ─────────────────────────────────────────────────────────────────────────────
_edgar = None
_edgar_tried = False


def load_edgar():
    """Cached {TICKER: (asof_ts_array, list_of_row_dicts)} sorted by asof_date for O(log n) PIT lookup."""
    global _edgar, _edgar_tried
    if _edgar_tried:
        return _edgar
    _edgar_tried = True
    try:
        import numpy as np
        import pandas as pd
        df = pd.read_parquet(_EDGAR)
        df["asof_date"] = pd.to_datetime(df["asof_date"])
        df = df.dropna(subset=["asof_date", "ticker"]).sort_values(["ticker", "asof_date"])
        idx = {}
        for tk, g in df.groupby("ticker", observed=True):
            idx[str(tk).upper()] = (g["asof_date"].values, g.to_dict("records"))
        _edgar = idx
    except Exception:  # noqa: BLE001
        _edgar = None
    return _edgar


def _pit_row(edgar_idx: dict, ticker: str, t):
    """The latest EDGAR row for `ticker` with asof_date <= t (point-in-time), or None."""
    import numpy as np
    e = edgar_idx.get((ticker or "").upper())
    if not e:
        return None
    asof_arr, rows = e
    pos = int(np.searchsorted(asof_arr, np.datetime64(t), side="right")) - 1
    return rows[pos] if pos >= 0 else None


def _zero_if_missing(x):
    """0.0 for None OR NaN, else float(x). NOTE: `x or 0.0` is WRONG here — np.nan is truthy, so
    `nan or 0.0` returns nan; `x != x` is the NaN-safe test (a missing dividend/buyback IS 0)."""
    try:
        return 0.0 if (x is None or x != x) else float(x)
    except (TypeError, ValueError):
        return 0.0


def _metric(row: dict, metric: str, price: float | None):
    """Compute one fundamental metric from a PIT row (+ current price for value ratios). None if N/A."""
    g = lambda k: row.get(k)  # noqa: E731
    try:
        shares = g("shares")
        mktcap = (price * shares) if (price and shares and shares > 0) else None
        if metric == "ep":
            return (g("ni") / mktcap) if (mktcap and g("ni") is not None) else None
        if metric == "bp":
            return (g("equity") / mktcap) if (mktcap and g("equity") is not None) else None
        if metric == "cfp":
            return (g("cfo") / mktcap) if (mktcap and g("cfo") is not None) else None
        if metric == "sp":
            return (g("revenue") / mktcap) if (mktcap and g("revenue") is not None) else None
        if metric == "shareholder_yield":
            # missing dividend/buyback = 0 contribution (NaN-safe — see _zero_if_missing)
            return ((_zero_if_missing(g("dividends")) + _zero_if_missing(g("repurchases"))) / mktcap) \
                if mktcap else None
        if metric == "gross_prof":
            a = g("assets")
            return (g("gross_profit") / a) if (a and a > 0 and g("gross_profit") is not None) else None
        if metric == "roe":
            eq = g("equity")
            return (g("ni") / eq) if (eq and eq > 0 and g("ni") is not None) else None
        if metric == "roa":
            a = g("assets")
            return (g("ni") / a) if (a and a > 0 and g("ni") is not None) else None
        if metric == "accruals":
            a = g("assets")
            return ((g("ni") - g("cfo")) / a) if (a and a > 0 and g("ni") is not None and g("cfo") is not None) else None
        if metric == "asset_growth":
            ap = g("assets_prior")
            return (g("assets") / ap - 1.0) if (ap and ap > 0 and g("assets") is not None) else None
    except Exception:  # noqa: BLE001
        return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# fundamental candidate (own materialize — needs PIT fundamentals + price[t])
# ─────────────────────────────────────────────────────────────────────────────
def _fund_class():
    import hashlib

    import numpy as np
    import pandas as pd
    from loop import factor_experiment as fe

    class FundamentalCandidate:
        """Cross-sectional PIT fundamental factor. Same eligibility + rebalance + weighting skeleton
        as the price factors, but the signal is a point-in-time fundamental ratio (asof_date <= t)."""

        def __init__(self, name, metric, sign, frac, edgar_idx, mem_df, side="long"):
            self.name = name
            self.spec = {"name": name, "metric": metric, "sign": int(sign), "frac": float(frac),
                         "side": side, "rebalance": "monthly", "universe": "sp1500_pit",
                         "source": "edgar_pit", "cost_bps": 3.0}
            self.spec_hash = hashlib.sha256(json.dumps(self.spec, sort_keys=True).encode()).hexdigest()
            self._edgar = edgar_idx
            self._mem = mem_df

        def materialize(self, closes: pd.DataFrame) -> pd.DataFrame:
            idx = closes.index
            names = [c for c in closes.columns if c != "SPY"]
            metric, sign, frac = self.spec["metric"], self.spec["sign"], self.spec["frac"]
            rebs = fe._rebalance_dates(idx)
            rebs = rebs[rebs >= pd.Timestamp(_FUND_START)]
            W = pd.DataFrame(0.0, index=idx, columns=closes.columns)
            rows = {}
            for t in rebs:
                loc = idx.get_loc(t)
                mem_set = fe.members_asof(self._mem, t)
                last = closes.iloc[loc]
                sig = {}
                for c in names:
                    if c not in mem_set:
                        continue
                    px = last.get(c)
                    if not (px and px >= fe.SELECT_FLOOR):    # valid price + $5 floor
                        continue
                    row = _pit_row(self._edgar, c, t)         # PIT: asof_date <= t ONLY
                    if row is None:
                        continue
                    v = _metric(row, metric, float(px))
                    if v is not None and np.isfinite(v):
                        sig[c] = sign * v
                if len(sig) < 20:                             # need a real cross-section to sort
                    continue
                s = pd.Series(sig).sort_values(ascending=False)
                k = max(1, int(round(frac * len(s))))
                longs = s.index[:k]
                row_w = pd.Series(0.0, index=closes.columns)
                if self.spec["side"] == "ls":     # market-neutral factor premium (isolates the tilt)
                    shorts = s.index[-k:]
                    row_w[longs] = 0.5 / len(longs)
                    row_w[shorts] = -0.5 / len(shorts)
                else:                              # long-only (gross 1.0; carries market beta)
                    row_w[longs] = 1.0 / len(longs)
                row_w["SPY"] = 0.0
                rows[t] = row_w
            if not rows:
                return W
            Wr = pd.DataFrame(rows).T.reindex(columns=closes.columns).sort_index()
            return Wr.reindex(idx).ffill().fillna(0.0)

        def raw_signal(self, closes: pd.DataFrame, t) -> pd.Series:
            """Raw (un-ranked) PIT signal at t over eligible names — for the IC term-structure."""
            names = [c for c in closes.columns if c != "SPY"]
            metric, sign = self.spec["metric"], self.spec["sign"]
            mem_set = fe.members_asof(self._mem, t)
            last = closes.loc[t]
            out = {}
            for c in names:
                if c not in mem_set:
                    continue
                px = last.get(c)
                if not (px and px >= fe.SELECT_FLOOR):
                    continue
                row = _pit_row(self._edgar, c, t)
                if row is None:
                    continue
                v = _metric(row, metric, float(px))
                if v is not None and np.isfinite(v):
                    out[c] = sign * v
            return pd.Series(out)

    return FundamentalCandidate


def build_fundamental_zoo(mem, edgar_idx):
    F = _fund_class()
    C = []
    for m in _VALUE:
        C.append(F(f"val_{m}", m, +1, 0.10, edgar_idx, mem))
    for m in _QUALITY_HI:
        C.append(F(f"qual_{m}", m, +1, 0.20, edgar_idx, mem))
    for m in _QUALITY_LO:
        C.append(F(f"qual_{m}_low", m, -1, 0.20, edgar_idx, mem))   # low accruals / low asset-growth
    # LONG-SHORT (market-neutral) variants of the canonical factors — these isolate the factor
    # PREMIUM from market beta (long-only deciles are beta-dominated → collapse to ~1 effective bet).
    for m, sign in [("ep", +1), ("bp", +1), ("roe", +1), ("gross_prof", +1), ("accruals", -1)]:
        fam = "val" if m in _VALUE else "qual"
        C.append(F(f"{fam}_{m}_ls", m, sign, 0.10, edgar_idx, mem, side="ls"))
    return C


# ─────────────────────────────────────────────────────────────────────────────
# regime-conditional IC (leakage-free price-derived regime proxy)
# ─────────────────────────────────────────────────────────────────────────────
def _regime_labels(closes):
    """risk_on / risk_off by SPY vs its trailing 200d MA — known at each date, no look-ahead."""
    import numpy as np
    import pandas as pd
    spy = closes["SPY"]
    ma = spy.rolling(200, min_periods=100).mean()
    return pd.Series(np.where(spy >= ma, "risk_on", "risk_off"), index=closes.index)


def edgar_coverage(closes, mem, edgar_idx):
    """Median fraction of price-eligible PIT S&P-1500 members that ACTUALLY have EDGAR data at a
    rebalance. The fundamental factors run only on this disclosed subset — delisted / non-filing
    names are dropped — so this number quantifies a real selection (partial-survivorship) caveat."""
    import numpy as np
    import pandas as pd
    from loop import factor_experiment as fe
    idx = closes.index
    rebs = fe._rebalance_dates(idx)
    rebs = rebs[rebs >= pd.Timestamp(_FUND_START)]
    names = [c for c in closes.columns if c != "SPY"]
    fracs = []
    for t in rebs[::6]:                                    # sample ~semiannually for speed
        mem_set = fe.members_asof(mem, t)
        last = closes.loc[t]
        elig = [c for c in names if c in mem_set and last.get(c) and last.get(c) >= fe.SELECT_FLOOR]
        if not elig:
            continue
        have = sum(1 for c in elig if _pit_row(edgar_idx, c, t) is not None)
        fracs.append(have / len(elig))
    return round(float(np.median(fracs)), 3) if fracs else None


def regime_conditional_ic(cand, closes, mem, edgar_idx):
    """Per-regime pooled rank-IC at the 63d horizon — does the factor's edge depend on the regime?"""
    import numpy as np
    import pandas as pd
    from engine.validation import rank_ic
    from loop import factor_experiment as fe
    names = [c for c in closes.columns if c != "SPY"]
    px = closes[names]
    idx = closes.index
    reg = _regime_labels(closes)
    rebs = fe._rebalance_dates(idx)
    rebs = rebs[(rebs >= pd.Timestamp(_FUND_START)) & (rebs <= idx[-127])]
    h = 63
    buckets = {"risk_on": [], "risk_off": []}
    for t in rebs:
        loc = idx.get_loc(t)
        sig = cand.raw_signal(closes, t)
        if len(sig) < 20:
            continue
        fwd = px.iloc[loc + h] / px.iloc[loc] - 1.0
        joint = pd.concat([sig, fwd], axis=1, keys=["s", "f"]).dropna()
        if len(joint) < 10:
            continue
        ic = rank_ic(joint["s"], joint["f"])
        if ic == ic:
            buckets[str(reg.loc[t])].append(ic)
    out = {}
    for r, ics in buckets.items():
        out[r] = {"mean_ic": round(float(np.mean(ics)), 4) if ics else None, "n_dates": len(ics)}
    return out


# ─────────────────────────────────────────────────────────────────────────────
# driver — gauntlet over the fundamental family (own EDGAR-valid window + holdout)
# ─────────────────────────────────────────────────────────────────────────────
def run(asof: str | None = None, write: bool = True) -> dict:
    try:
        import bot  # noqa: F401
        import pandas as pd
        from loop import factor_experiment as fe, harness, cluster, pbo
        from engine import validation as V
        edgar = load_edgar()
        if not edgar:
            return _stub(asof, "EDGAR fundamentals panel unavailable")

        closes, mem, idx, hyg = fe.load_panel()
        bill = fe.bill_yield()
        # restrict to the EDGAR-valid era; keep the 2022+ holdout locked
        closes = closes[closes.index >= pd.Timestamp(_FUND_START)]
        insample = closes[closes.index < fe.HOLDOUT_START]
        holdout = closes[closes.index >= fe.HOLDOUT_START]
        if len(insample) < 252 or len(holdout) < 60:
            return _stub(asof, "insufficient EDGAR-era panel after windowing")
        s6040 = harness.sixty_forty_sharpe(insample, bill)

        cands = build_fundamental_zoo(mem, edgar)
        pool, base = {}, {}
        for c in cands:
            m = harness.score(c, insample, bill, n_eff=1, sixty40_sharpe=s6040)
            if m is None:
                continue
            pool[c.spec_hash] = m["net"]
            base[c.spec_hash] = (c, m)
        if not base:
            return _stub(asof, "no fundamental candidate scored")

        n_eff = cluster.effective_n(pool, rho=fe.RHO)
        for h, (c, m) in base.items():
            mo = V.ret_moments(m["net"])
            if mo:
                d = V.deflated_sharpe(*mo, n_trials=max(1, n_eff), trading_year=252) or {"dsr": 0.0}
                m["dsr"], m["dsr_verdict"] = float(d["dsr"]), V.dsr_verdict(d["dsr"])
        pbo_res = pbo.cscv(pool, S=fe.PBO_S)
        pbo_val = pbo_res.get("pbo")
        pbo_ok = (pbo_val is None or pbo_val < 0.50)
        fdr = V.benjamini_hochberg({h: base[h][1]["p_value"] for h in base}, alpha=fe.BH_ALPHA)
        fdr_survivors = sum(1 for h in fdr if fdr[h]["reject"])

        rows, survivors, passers = [], [], 0
        for h, (c, m) in sorted(base.items(), key=lambda kv: -kv[1][1]["dsr"]):
            rej = bool(fdr.get(h, {}).get("reject"))
            passes = (m["dsr_verdict"].startswith("SURVIVES") and m["beats_spy"] and m["beats_6040"]
                      and m["crisis_pass"] and rej and m["fold_robust"] and pbo_ok)
            ho_sharpe, confirms = None, None
            if passes:
                passers += 1
                mh = harness.score(c, holdout, bill, n_eff=1, sixty40_sharpe=s6040)
                ho_sharpe = float(mh["sharpe"]) if mh else 0.0
                confirms = bool(ho_sharpe > 0 and ho_sharpe >= 0.5 * m["sharpe"])
                if confirms:
                    survivors.append(c.name)
            rows.append({
                "name": c.name, "metric": c.spec["metric"], "sign": c.spec["sign"],
                "family": "value" if c.spec["metric"] in _VALUE else "quality",
                "sharpe": round(float(m["sharpe"]), 3), "dsr": round(float(m["dsr"]), 3),
                "dsr_verdict": m["dsr_verdict"], "cagr": round(float(m["cagr"]), 2),
                "maxdd": round(float(m["maxdd"]), 2), "beats_spy": bool(m["beats_spy"]),
                "fdr_reject": rej, "in_sample_pass": bool(passes),
                "holdout_sharpe": round(ho_sharpe, 3) if ho_sharpe is not None else None,
                "holdout_confirms": confirms,
            })

        # regime-conditional IC for the survivors + top-DSR factor
        targets = {c.name: c for c, m in base.values() if c.name in survivors}
        if rows:
            top = rows[0]["name"]
            targets.setdefault(top, next(c for c, m in base.values() if c.name == top))
        regime_ic = {}
        for nm, c in list(targets.items())[:6]:
            try:
                regime_ic[nm] = regime_conditional_ic(c, closes, mem, edgar)
            except Exception:  # noqa: BLE001
                pass

        try:
            cov = edgar_coverage(closes, mem, edgar)
        except Exception:  # noqa: BLE001
            cov = None
        cov_gap = (f"fundamental factors run on the ~{round((cov or 0) * 100)}% of price-eligible "
                   f"S&P-1500 members that have EDGAR coverage at each rebalance — delisted / "
                   f"non-filing names are dropped (a disclosed/alive-biased subset; partial "
                   f"survivorship caveat)") if cov is not None else \
                  "fundamental factors use only EDGAR-covered members (a disclosed-subset caveat)"
        best = max(base.values(), key=lambda cm: cm[1]["dsr"])[1]
        verdict = {
            "as_of": str(asof or "")[:10] or None, "status": "ok",
            "methodology": {
                "source": "SEC EDGAR fundamentals (point-in-time via asof_date <= rebalance date)",
                "universe": "S&P 1500 PIT membership (survivorship-safe)",
                "window": f"{insample.index[0].date()}..{insample.index[-1].date()} IS | "
                          f"{holdout.index[0].date()}..{holdout.index[-1].date()} holdout",
                "gauntlet": "DSR re-deflated @ effective-N · PBO/CSCV · BH-FDR · one-shot holdout",
                "regime_proxy": "SPY vs trailing-200d MA (risk_on / risk_off), leakage-free",
            },
            "verdict": {
                "n_candidates": len(base), "effective_n": int(n_eff),
                "pbo": round(float(pbo_val), 3) if pbo_val is not None else None, "pbo_pass": pbo_ok,
                "best_sharpe": round(float(best["sharpe"]), 3), "best_dsr": round(float(best["dsr"]), 3),
                "best_dsr_verdict": best["dsr_verdict"], "fdr_survivors": fdr_survivors,
                "in_sample_passers": passers, "holdout_confirmed": survivors,
                "edgar_coverage_median": cov,
            },
            "factors": rows,
            "regime_conditional_ic": regime_ic,
            "data_gaps": ["volume/liquidity factors (Amihud, turnover) — NO historical volume panel "
                          "available; not built rather than faked",
                          "EDGAR structured data starts ~2010 → shorter window than the price factors",
                          cov_gap],
            "honest_read": ("Fundamentals are PIT (asof_date-gated) so there is no look-ahead, but the "
                            "window is shorter (2010+) and annual-frequency. The locked holdout is the "
                            "verdict; forward paper Brier remains the ultimate judge. No wiring into "
                            "the live engine — research only."),
        }
        if write:
            try:
                _OUT.parent.mkdir(parents=True, exist_ok=True)
                _OUT.write_text(json.dumps(verdict, indent=2, default=str))
            except Exception:  # noqa: BLE001
                pass
        return verdict
    except Exception as exc:  # noqa: BLE001
        return _stub(asof, str(exc)[:200])


def load() -> dict:
    try:
        return json.loads(_OUT.read_text())
    except Exception:  # noqa: BLE001
        return {"status": "unavailable",
                "note": "no fundamentals run recorded yet — run scripts/run_fundamentals.py"}


def _stub(asof, reason: str) -> dict:
    return {"as_of": str(asof or "")[:10] or None, "status": "unavailable", "error": reason}

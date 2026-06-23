"""Advanced historical alpha engine — a broad PRICE-factor library + IC term-structure + a
holdout-validated COMPOSITE, all under the frozen multiple-testing gauntlet.

This builds on loop/engine_backtest.py (which ran ~8 factors) to harness MORE signal and MORE
predictive power — honestly. The trap with "more predictive power through backtesting" is
overfitting: every extra factor you try is another lottery ticket. So the whole design is
multiple-testing-honest:

  * a wide library of ECONOMICALLY-DISTINCT price factors (momentum families, residual/idiosyncratic
    momentum, reversal, low-vol, downside-vol, MAX-lottery, idiosyncratic skew, 52w proximity, trend
    consistency, acceleration) — built by SUBCLASSING the audited FactorCandidate, so every name
    inherits its no-look-ahead / PIT-membership / $5-floor / NaN-free-lookback hygiene;
  * the ENTIRE pool (incl. the composite) is counted in cluster.effective_n → the DSR is re-deflated
    for the real number of independent bets, PBO/CSCV runs over the whole pool, and BH-FDR controls
    the family — a bigger zoo RAISES the bar, it doesn't lower it;
  * a COMPOSITE alpha is blended ONLY from de-correlated in-sample survivors and then judged on the
    LOCKED 2022+ holdout (one peek) — selection is in-sample, the verdict is out-of-sample;
  * an IC term-structure (date-clustered rank-IC across horizons) shows WHERE each signal's edge
    lives, not just a single Sharpe.

Heavy + holdout-burning → on-demand only (scripts/run_factor_zoo.py); the dashboard reads the
persisted data/backtest/factor_zoo.json. Never raises (honest stub on missing data/infra).
"""
from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_OUT = _ROOT / "data" / "backtest" / "factor_zoo.json"

_IC_HORIZONS = [21, 63, 126]   # business-day forward windows for the IC term-structure
_MAX_COMPOSITE = 5             # blend at most this many de-correlated survivors


# ─────────────────────────────────────────────────────────────────────────────
# extended factor library — subclasses the audited FactorCandidate (reuses materialize)
# ─────────────────────────────────────────────────────────────────────────────
def _zoo_classes():
    """Import the audited base lazily and return (ZooFactor, Composite, fe). Raises if unavailable."""
    import numpy as np
    import pandas as pd
    from loop import factor_experiment as fe

    class ZooFactor(fe.FactorCandidate):
        """Adds economically-distinct PRICE signals on top of the base kinds. Higher score = more
        attractive (we long the top). All windows clamp to available history (no IndexError)."""

        def _signal(self, hist, t):
            kind = self.spec["kind"]
            if kind in ("momentum", "reversal", "lowvol", "high52w"):
                return super()._signal(hist, t)
            lb, sk = self.spec["lookback"], self.spec["skip"]
            px = hist
            rets = px.pct_change()
            win = rets.iloc[-min(lb, len(rets) - 1):] if lb else rets.iloc[1:]
            if kind == "maxret":                      # LOTTERY/MAX: long LOW max-daily-return names
                return -win.max()
            if kind == "downvol":                     # long LOW downside semi-deviation
                # semideviation = sqrt(mean(min(r,0)^2)); 0 when there are NO down days (the BEST,
                # highest score) — using neg.std() would return NaN there and silently DROP the
                # calmest names from the ranking instead of ranking them top.
                dn = win.clip(upper=0.0)
                return -np.sqrt((dn * dn).mean())
            if kind == "skew":                        # long LOW idiosyncratic skew (anti-lottery)
                return -win.skew()
            if kind == "consistency":                 # long STEADY climbers (high % up-days)
                return (win > 0).mean()
            if kind == "accel":                       # momentum ACCELERATION (2nd derivative)
                h = max(2, lb // 2)
                if len(px) < lb + 1:
                    return pd.Series(np.nan, index=px.columns)
                recent = px.iloc[-1] / px.iloc[-1 - h] - 1.0
                older = px.iloc[-1 - h] / px.iloc[-1 - lb] - 1.0
                return recent - older
            if kind == "resid_mom":                   # IDIOSYNCRATIC momentum: cross-sectionally
                # demean daily returns (strip the common market factor), cumulate over [lb..sk]
                w = rets.iloc[-(lb + 1):]
                demeaned = w.sub(w.mean(axis=1), axis=0)
                if sk:
                    demeaned = demeaned.iloc[:-sk]
                return demeaned.sum()
            raise ValueError(kind)

    class Composite(fe.FactorCandidate):
        """A multi-factor blend: long the names with the best AVERAGE cross-sectional rank across its
        de-correlated components. Selection of components is in-sample; the verdict is the holdout."""

        def __init__(self, name, components, mem_df=None):
            lb = max(c.spec["lookback"] for c in components)
            sk = max(c.spec["skip"] for c in components)
            vw = max(c.spec["vol_win"] for c in components)
            super().__init__(name, "momentum", lookback=lb, skip=sk, side="long",
                             frac=0.10, vol_win=vw, mem_df=mem_df)
            self.spec["kind"] = "composite"
            self.spec["components"] = [c.name for c in components]
            import hashlib as _h
            self.spec_hash = _h.sha256(json.dumps(self.spec, sort_keys=True).encode()).hexdigest()
            self.components = components

        def _signal(self, hist, t):
            ranks = []
            for comp in self.components:
                try:
                    s = comp._signal(hist, t)
                    s = s[np.isfinite(s)]
                    if len(s) >= 10:
                        ranks.append(s.rank(pct=True))
                except Exception:  # noqa: BLE001
                    continue
            if not ranks:
                return pd.Series(dtype=float)
            return pd.concat(ranks, axis=1).mean(axis=1)

    return ZooFactor, Composite, fe


def build_zoo(mem):
    """~16 economically-distinct price factors across momentum / residual-momentum / reversal /
    low-vol / downside-vol / lottery / skew / 52w / consistency / acceleration families (with a few
    near-duplicate lookback variants so the effective-N clustering has something to collapse)."""
    Z, _, _ = _zoo_classes()
    f = lambda *a, **k: Z(*a, mem_df=mem, **k)  # noqa: E731
    C = [
        # momentum family
        f("mom_12_1_decile", "momentum", lookback=252, skip=21, side="long", frac=0.10),
        f("mom_12_1_ls",     "momentum", lookback=252, skip=21, side="ls",   frac=0.10),
        f("mom_6_1_decile",  "momentum", lookback=126, skip=21, side="long", frac=0.10),
        f("resid_mom_12_1",  "resid_mom", lookback=252, skip=21, side="long", frac=0.10),
        f("accel_12_6",      "accel",    lookback=252, skip=0,   side="long", frac=0.10),
        # reversal
        f("reversal_1m",     "reversal", lookback=21, side="long", frac=0.10),
        f("reversal_1w",     "reversal", lookback=5,  side="long", frac=0.10),
        # low / downside vol
        f("lowvol_60d",      "lowvol", lookback=0, side="long", frac=0.20, vol_win=60),
        f("lowvol_120d",     "lowvol", lookback=0, side="long", frac=0.20, vol_win=120),
        f("downvol_60d",     "downvol", lookback=60, side="long", frac=0.20),
        # lottery / skew (anti-lottery: long low-MAX, low-skew)
        f("maxret_low_1m",   "maxret", lookback=21, side="long", frac=0.20),
        f("skew_low_6m",     "skew",   lookback=126, side="long", frac=0.20),
        # 52w / 26w proximity
        f("high52w_decile",  "high52w", lookback=252, side="long", frac=0.10),
        f("high26w_decile",  "high52w", lookback=126, side="long", frac=0.10),
        # path quality
        f("consistency_6m",  "consistency", lookback=126, side="long", frac=0.20),
    ]
    return C


# ─────────────────────────────────────────────────────────────────────────────
# IC term-structure (date-clustered rank-IC across horizons)
# ─────────────────────────────────────────────────────────────────────────────
def _eligible_signals(factor, closes, mem):
    """{rebalance_date: raw signal Series over eligible names} — the same eligibility rules as
    materialize (PIT member, valid price ≥ floor, NaN-free lookback), but returns the RAW signal."""
    import numpy as np
    from loop import factor_experiment as fe
    idx = closes.index
    names = [c for c in closes.columns if c != "SPY"]
    px = closes[names]
    lb, sk, vw = factor.spec["lookback"], factor.spec["skip"], factor.spec["vol_win"]
    need = max(lb + sk + 1, vw + 1)
    rebs = fe._rebalance_dates(idx)
    rebs = rebs[rebs >= idx[need]]
    out = {}
    for t in rebs:
        loc = idx.get_loc(t)
        hist = px.iloc[: loc + 1]
        if len(hist) < need:
            continue
        mem_set = fe.members_asof(mem, t)
        window = hist.iloc[-need:]
        valid = window.notna().all(axis=0)
        last = hist.iloc[-1]
        ok = last.notna() & (last >= fe.SELECT_FLOOR)
        elig = [c for c in names if c in mem_set and bool(valid.get(c, False)) and bool(ok.get(c, False))]
        if len(elig) < 20:
            continue
        sig = factor._signal(hist[elig], t).dropna()
        sig = sig[np.isfinite(sig)]
        if len(sig) >= 20:
            out[t] = sig
    return out


def ic_term_structure(factor, closes, mem):
    """Per-horizon date-clustered rank-IC: at each rebalance, IC(signal, forward h-day return); pool
    across dates with a Newey-West HAC t (lags tied to the overlap). Shows where the edge lives."""
    import numpy as np
    import pandas as pd
    from engine.validation import rank_ic, newey_west_tstat
    names = [c for c in closes.columns if c != "SPY"]
    px = closes[names]
    idx = closes.index
    sigs = _eligible_signals(factor, closes, mem)
    prof = {}
    for h in _IC_HORIZONS:
        ics = []
        for t, sig in sigs.items():
            loc = idx.get_loc(t)
            if loc + h >= len(idx):
                continue
            fwd = px.iloc[loc + h] / px.iloc[loc] - 1.0     # forward h-day return (no look-ahead: t→t+h)
            joint = pd.concat([sig, fwd], axis=1, keys=["s", "f"]).dropna()
            if len(joint) >= 10:
                ic = rank_ic(joint["s"], joint["f"])
                if ic == ic:
                    ics.append(ic)
        if len(ics) >= 6:
            s = pd.Series(ics)
            nw = newey_west_tstat(s, lags=max(1, h // 21 + 1))
            prof[f"{h}d"] = {"mean_ic": round(float(s.mean()), 4),
                             "t_hac": round(nw["t"], 2) if nw.get("t") is not None else None,
                             "p_hac": round(nw["p"], 4) if nw.get("p") is not None else None,
                             "n_dates": len(ics)}
        else:
            prof[f"{h}d"] = {"n_dates": len(ics), "note": "building"}
    return prof


# ─────────────────────────────────────────────────────────────────────────────
# driver — score the zoo + composite through the frozen gauntlet
# ─────────────────────────────────────────────────────────────────────────────
def _metrics_row(name, m, fdr_h, passes_is, holdout_sharpe=None, confirms=None):
    return {
        "name": name, "sharpe": round(float(m["sharpe"]), 3), "dsr": round(float(m["dsr"]), 3),
        "dsr_verdict": m["dsr_verdict"], "cagr": round(float(m["cagr"]), 2),
        "maxdd": round(float(m["maxdd"]), 2), "beats_spy": bool(m["beats_spy"]),
        "beats_6040": bool(m["beats_6040"]), "crisis_pass": bool(m["crisis_pass"]),
        "fold_robust": bool(m["fold_robust"]), "p_value": round(float(m["p_value"]), 4),
        "fdr_q": round(float(fdr_h["q"]), 4) if fdr_h.get("q") is not None else None,
        "fdr_reject": bool(fdr_h.get("reject")), "in_sample_pass": bool(passes_is),
        "holdout_sharpe": round(holdout_sharpe, 3) if holdout_sharpe is not None else None,
        "holdout_confirms": confirms,
    }


def run(asof: str | None = None, write: bool = True) -> dict:
    """Score the full factor zoo + a holdout-validated composite through the frozen gauntlet.
    Returns (and persists) an enriched verdict. Heavy + holdout-burning. Never raises."""
    try:
        import bot  # noqa: F401
        from loop import factor_experiment as fe, harness, cluster, pbo
        from engine import validation as V
        _, Composite, _ = _zoo_classes()

        closes, mem, idx, hyg = fe.load_panel()
        bill = fe.bill_yield()
        n_names = len([c for c in closes.columns if c != "SPY"])
        insample = closes[closes.index < fe.HOLDOUT_START]
        holdout = closes[closes.index >= fe.HOLDOUT_START]
        s6040 = harness.sixty_forty_sharpe(insample, bill)

        # 1) score the zoo in-sample
        cands = build_zoo(mem)
        pool, base = {}, {}
        for c in cands:
            m = harness.score(c, insample, bill, n_eff=1, sixty40_sharpe=s6040)
            if m is None:
                continue
            pool[c.spec_hash] = m["net"]
            base[c.spec_hash] = (c, m)
        if not base:
            return _stub(asof, "no zoo candidate scored")

        # 2) provisional clustering → pick one best-DSR factor per cluster as composite components
        prelim_neff = cluster.effective_n(pool, rho=fe.RHO)
        for h, (c, m) in base.items():                 # provisional DSR for component selection
            mo = V.ret_moments(m["net"])
            if mo:
                d = V.deflated_sharpe(*mo, n_trials=max(1, prelim_neff), trading_year=252) or {"dsr": 0.0}
                m["dsr"], m["dsr_verdict"] = float(d["dsr"]), V.dsr_verdict(d["dsr"])
        labels = cluster.assign_label(pool, rho=fe.RHO)
        best_per_cluster = {}
        for h, (c, m) in base.items():
            lab = labels.get(h)
            if lab not in best_per_cluster or m["sharpe"] > best_per_cluster[lab][1]["sharpe"]:
                best_per_cluster[lab] = (c, m)
        components = [cm[0] for cm in sorted(best_per_cluster.values(),
                                             key=lambda cm: -cm[1]["sharpe"])[:_MAX_COMPOSITE]]

        # 3) build + score the composite IN-SAMPLE, add it to the pool (counted as a trial)
        composite = None
        if len(components) >= 2:
            composite = Composite("composite_alpha", components, mem_df=mem)
            cm = harness.score(composite, insample, bill, n_eff=1, sixty40_sharpe=s6040)
            if cm is not None:
                pool[composite.spec_hash] = cm["net"]
                base[composite.spec_hash] = (composite, cm)

        # 4) FINAL effective-N over the whole pool (incl composite) → re-deflate every DSR honestly
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

        # 5) per-candidate in-sample gate + one-shot holdout for passers
        comp_hash = composite.spec_hash if composite else None
        rows, survivors, passers, comp_row = [], [], 0, None
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
            row = _metrics_row(c.name, m, fdr.get(h, {}), passes, ho_sharpe, confirms)
            row["is_composite"] = (h == comp_hash)
            rows.append(row)
            if h == comp_hash:
                comp_row = row

        # 6) IC term-structure for the holdout-confirmed survivors + composite + top-DSR factor
        ic_targets = {c.name: c for c, m in base.values() if c.name in survivors}
        if composite is not None:
            ic_targets.setdefault(composite.name, composite)
        if rows:
            top = rows[0]["name"]
            ic_targets.setdefault(top, next(c for c, m in base.values() if c.name == top))
        ic_profiles = {}
        for nm, c in list(ic_targets.items())[:6]:        # bound cost
            try:
                # IN-SAMPLE only: the IC diagnostic must not touch the locked holdout (the holdout is
                # peeked ONCE, for the pass/fail confirmation — never re-used for a display metric).
                ic_profiles[nm] = ic_term_structure(c, insample, mem)
            except Exception:  # noqa: BLE001
                pass

        best = max(base.values(), key=lambda cm: cm[1]["dsr"])[1]
        # pool fingerprint: the holdout is peeked once PER POOL — recording the fingerprint makes a
        # later re-run with a CHANGED factor pool (a fresh holdout burn) auditable rather than silent.
        import hashlib
        pool_fp = hashlib.sha256(
            ("|".join(sorted(c.name for c, _ in base.values()))).encode()).hexdigest()[:16]
        verdict = {
            "as_of": str(asof or "")[:10] or None, "status": "ok",
            "pool_fingerprint": pool_fp, "holdout_burned": True,
            "methodology": {
                "harness": "loop.harness (frozen: cost_bps=3.0, embargo=63d, 5-fold purged CV, 3 crises)",
                "tests": ["DSR re-deflated @ effective-N (whole pool incl composite)", "PBO/CSCV",
                          "BH-FDR", "Newey-West HAC", "fold-robust", "one-shot 2022+ holdout",
                          "IC term-structure (in-sample, date-clustered)"],
                "universe": "S&P 1500 PIT membership (survivorship-safe; deep + delisted)",
                "in_sample": f"{insample.index[0].date()}..{insample.index[-1].date()}",
                "holdout": f"{holdout.index[0].date()}..{holdout.index[-1].date()}",
                "composite": "blended from de-correlated IN-SAMPLE survivors; verdict = holdout",
                "ic_scope": "in-sample only (the locked holdout is peeked once, for confirmation)",
                "holdout_discipline": ("one-shot per pool_fingerprint; re-running with a DIFFERENT "
                                       "fingerprint burns a fresh holdout (statistical discipline, "
                                       "not code-enforced — run deliberately)"),
            },
            "panel": {"n_names": n_names, "n_dates": len(idx), "span": f"{idx[0].date()}..{idx[-1].date()}"},
            "verdict": {
                "n_candidates": len(base), "effective_n": int(n_eff),
                "pbo": round(float(pbo_val), 3) if pbo_val is not None else None, "pbo_pass": pbo_ok,
                "best_sharpe": round(float(best["sharpe"]), 3),
                "best_dsr": round(float(best["dsr"]), 3), "best_dsr_verdict": best["dsr_verdict"],
                "fdr_survivors": fdr_survivors, "in_sample_passers": passers,
                "holdout_confirmed": survivors,
                "composite": comp_row,
            },
            "factors": rows,
            "ic_term_structure": ic_profiles,
            "honest_read": ("A wider zoo RAISES the bar (DSR re-deflated at effective-N over the whole "
                            "pool, PBO + BH-FDR across the family). The composite is selected from "
                            "in-sample survivors, so its IN-SAMPLE number is optimistic by "
                            "construction — its real verdict is the LOCKED-holdout confirmation. "
                            "Forward paper Brier remains the ultimate judge."),
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
                "note": "no factor-zoo run recorded yet — run scripts/run_factor_zoo.py"}


def _stub(asof, reason: str) -> dict:
    return {"as_of": str(asof or "")[:10] or None, "status": "unavailable", "error": reason}

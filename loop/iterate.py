"""One full self-improving iteration — proposer -> frozen judge -> forward jury.

Pipeline (all on REAL 2002-2026 ETF data):
  generate candidates -> backtest in-sample through the FROZEN harness -> effective-N
  cluster -> deflate Sharpe with n_eff -> PBO/CSCV over the pool -> BH-FDR -> MinBTL
  budget -> for in-sample passers, touch the LOCKED holdout ONCE -> promote gate
  (refuse-to-promote if budget exhausted) -> enroll survivors to forward paper.

Cardinal rule: in-sample DSR is a FILTER, not a verdict. Nothing reaches live sizing
here — the only path to live is the forward paper Brier (loop/paper.py), which stays
'building' until a forward window resolves. The honest expected outcome is that almost
nothing survives — exactly the discipline the whole platform is built on.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

import bot  # noqa: F401
from engine.equity_alloc import index_close, bill_yield
from engine import validation as V
from loop import candidates, harness, cluster, pbo, budget, holdout, promote, paper
from data_layer import store

UNIVERSE = ["SPY", "QQQ", "IEF", "TLT", "XLK", "XLF"]   # SPY = col 0 = benchmark
HOLDOUT_START = "2022-01-01"                              # LOCKED before the loop runs


def _load():
    closes = pd.DataFrame({t: index_close(t) for t in UNIVERSE}).ffill().dropna()
    return closes, bill_yield()


def iterate() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    con = store.connect()
    closes, bill = _load()
    insample = closes[closes.index < HOLDOUT_START]
    holdout_slice = closes[closes.index >= HOLDOUT_START]
    s6040 = harness.sixty_forty_sharpe(insample, bill)

    # ---- 1. backtest every candidate in-sample through the frozen harness ----
    pool, base = {}, {}
    for c in candidates.generate(UNIVERSE):
        m = harness.score(c, insample, bill, n_eff=1, sixty40_sharpe=s6040)
        if m:
            pool[c.spec_hash] = m["net"]
            base[c.spec_hash] = (c, m)

    # ---- 2. effective-N (near-duplicate sleeves are one bet) ----
    n_eff = cluster.effective_n(pool, rho=0.90)

    # ---- 3. re-deflate each Sharpe with n_eff (cheap, off stored net) ----
    for h, (c, m) in base.items():
        sr_d, skew, kurt, T = V.ret_moments(m["net"])
        dsr = V.deflated_sharpe(sr_d, skew, kurt, T, n_trials=max(1, n_eff), trading_year=252) or {"dsr": 0.0}
        m["dsr"], m["dsr_verdict"] = float(dsr["dsr"]), V.dsr_verdict(dsr["dsr"])

    # ---- 4. PBO/CSCV over the whole pool (is the SELECTION mining noise?) ----
    pbo_res = pbo.cscv(pool, S=12)
    pbo_val = pbo_res.get("pbo")

    # ---- 5. BH-FDR across every candidate tested this cycle ----
    fdr = V.benjamini_hochberg({h: base[h][1]["p_value"] for h in base}, alpha=0.10)

    # ---- 6. MinBTL trial budget ----
    best_sharpe = max(m["sharpe"] for _, m in base.values())
    years = (insample.index[-1] - insample.index[0]).days / 365.25
    exhausted = budget.exhausted(n_eff, best_sharpe, years)

    # ---- 7. persist the cumulative trial counter ----
    for h, (c, m) in base.items():
        store.record_trial(con, h, m, n_eff, now)
    cum = store.cumulative_trial_count(con)

    # ---- 8. holdout (touch ONCE, only for in-sample passers) + 9. promote ----
    touched = store.touched_set(con)
    results = []
    for h, (c, m) in base.items():
        passes_is = (m["dsr_verdict"].startswith("SURVIVES") and m["beats_spy"] and m["beats_6040"]
                     and m["crisis_pass"] and fdr[h]["reject"] and m["fold_robust"]
                     and (pbo_val is None or pbo_val < 0.50))
        hconf = False
        if passes_is and not exhausted and h not in touched:
            try:
                holdout.touch(h, touched)
                mh = harness.score(c, holdout_slice, bill, n_eff=1, sixty40_sharpe=s6040)
                ho_sharpe = mh["sharpe"] if mh else 0.0
                hconf = holdout.confirms(m["sharpe"], ho_sharpe)
                store.add_holdout_touch(con, h, HOLDOUT_START, ho_sharpe, now)
            except holdout.HoldoutBurned:
                hconf = False
        g = promote.gate(m, fdr_reject=fdr[h]["reject"], pbo=pbo_val,
                         holdout_confirms=hconf, budget_exhausted=exhausted)
        if g["stage"] == "paper":
            paper.enroll(h, c.spec["weights"], asof=str(closes.index[-1].date()))
            store.record_promotion(con, h, "paper", m["dsr"], n_eff, g["reason"], now)
        results.append((c, m, g))

    promoted = [r for r in results if r[2]["stage"] == "paper"]
    return {
        "n_candidates": len(base), "n_eff": n_eff, "pbo": pbo_val, "best_sharpe": round(best_sharpe, 3),
        "budget_exhausted": exhausted, "cumulative_trials": cum, "n_promoted": len(promoted),
        "fdr_survivors": sum(1 for h in fdr if fdr[h]["reject"]),
        "results": results, "in_sample_years": round(years, 1),
    }


if __name__ == "__main__":
    out = iterate()
    print("\n=== PHASE 3 — self-improving backtest iteration (real 2002-2026 data) ===")
    print(f"candidates={out['n_candidates']}  effective-N={out['n_eff']}  "
          f"in-sample={out['in_sample_years']}y  best Sharpe={out['best_sharpe']}")
    print(f"PBO={out['pbo']}  FDR survivors={out['fdr_survivors']}  "
          f"budget exhausted={out['budget_exhausted']}  cumulative trials={out['cumulative_trials']}")
    print(f"\nPROMOTED TO PAPER: {out['n_promoted']} / {out['n_candidates']}")
    top = sorted(out["results"], key=lambda r: -r[1]["dsr"])[:6]
    for c, m, g in top:
        print(f"  {c.spec_hash[:8]} dsr={m['dsr']:.3f} sharpe={m['sharpe']:.2f} "
              f"dd={m['maxdd']:.0f}% beats_spy={int(m['beats_spy'])} "
              f"crisis={int(m['crisis_pass'])} -> {g['stage']} ({g['reason'][:42]})")
    print("\nHonest read: in-sample DSR is a filter; the verdict is the forward paper Brier.")

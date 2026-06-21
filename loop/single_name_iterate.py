"""loop/single_name_iterate.py — one self-improving iteration on the SINGLE-NAME universe.

Mirrors loop/iterate.py exactly, but over the survivorship-safe S&P-1500 panel instead of
the ETF sleeves. It imports the FROZEN judge (loop.harness.score) and the FROZEN plumbing
(loop.cluster, loop.pbo, loop.budget, loop.holdout, loop.promote, engine.validation) and
RE-IMPLEMENTS NO STATISTICS. The only thing this module owns is the orchestration:
  generate candidates -> in-sample score through harness -> effective-N cluster ->
  re-deflate DSR with n_eff -> PBO/CSCV -> BH-FDR -> in-sample gate conjunction ->
  touch the LOCKED holdout once for passers -> report which WOULD enroll.

dry_run (default TRUE):
  When True, the function REPORTS which candidates would enroll to forward paper but makes
  NO writes to live loop state — it does NOT call paper.enroll, store.record_trial,
  store.record_promotion, store.add_holdout_touch, or persist the holdout-touch set. The
  holdout is still scored (read-only) for passers so the report is real, but the one-shot
  guard is enforced IN-MEMORY only, so a dry run never burns a live holdout look. Only a
  future explicit run (dry_run=False) flips the writes on; that path mirrors iterate.py.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

# frozen judge + plumbing — imported, NEVER re-implemented
from loop import harness, cluster, pbo, budget, holdout, promote, paper
from loop import single_name_candidates as snc
from loop import single_name_panel as snp
from engine import validation as V

HOLDOUT_START = "2022-01-01"   # LOCKED before any scoring (same boundary as iterate.py)
RHO = 0.90                     # effective-N clustering threshold (iterate.py)
PBO_S = 12                     # CSCV blocks (iterate.py)
BH_ALPHA = 0.10                # BH-FDR level (iterate.py)


def iterate(dry_run: bool = True) -> dict:
    """Run one single-name iteration. Returns a structured report.

    dry_run=True (default): pure read-only — no writes to the loop DB, no paper enroll,
    holdout one-shot guard enforced in-memory only.
    dry_run=False: mirrors loop/iterate.py side effects (DB trials/promotions, holdout
    touches, paper.enroll). Intended only for a future explicit, deliberate run.
    """
    now = datetime.now(timezone.utc).isoformat()

    panel = snp.load_panel()
    closes, mem, bill = panel["closes"], panel["mem"], panel["bill"]
    insample, holdout_slice = snp.split(closes, HOLDOUT_START)
    s6040 = harness.sixty_forty_sharpe(insample, bill)

    # ---- 1. in-sample score every candidate through the FROZEN harness ----
    pool, base = {}, {}
    for c in snc.generate(mem):
        m = harness.score(c, insample, bill, n_eff=1, sixty40_sharpe=s6040)
        if m:
            pool[c.spec_hash] = m["net"]
            base[c.spec_hash] = (c, m)

    if not base:
        return {"error": "no candidate produced a scorable backtest", "dry_run": dry_run}

    # ---- 2. effective-N (near-duplicate sleeves collapse to one bet) ----
    n_eff = cluster.effective_n(pool, rho=RHO)
    labels = cluster.assign_label(pool, rho=RHO)

    # ---- 3. re-deflate each Sharpe with n_eff (off stored net, like iterate.py) ----
    for h, (c, m) in base.items():
        mo = V.ret_moments(m["net"])
        if mo:
            sr_d, skew, kurt, T = mo
            dsr = V.deflated_sharpe(sr_d, skew, kurt, T, n_trials=max(1, n_eff),
                                    trading_year=252) or {"dsr": 0.0}
            m["dsr"], m["dsr_verdict"] = float(dsr["dsr"]), V.dsr_verdict(dsr["dsr"])

    # ---- 4. PBO / CSCV over the whole pool ----
    pbo_res = pbo.cscv(pool, S=PBO_S)
    pbo_val = pbo_res.get("pbo")

    # ---- 5. BH-FDR across the family ----
    fdr = V.benjamini_hochberg({h: base[h][1]["p_value"] for h in base}, alpha=BH_ALPHA)

    # ---- 6. MinBTL trial budget ----
    best_sharpe = max(m["sharpe"] for _, m in base.values())
    years = (insample.index[-1] - insample.index[0]).days / 365.25
    exhausted = budget.exhausted(n_eff, best_sharpe, years)

    # ---- 7. (writes only when NOT dry_run) persist cumulative trial counter ----
    con = None
    touched = set()
    if not dry_run:
        from data_layer import store as loop_store  # noqa: WPS433 (deferred to avoid DB on dry run)
        con = loop_store.connect()
        for h, (c, m) in base.items():
            loop_store.record_trial(con, h, m, n_eff, now)
        touched = loop_store.touched_set(con)

    # ---- 8. in-sample gate + holdout (touch ONCE for passers) + would-enroll ----
    results = []
    would_enroll = []
    for h, (c, m) in base.items():
        passes_is = (
            m["dsr_verdict"].startswith("SURVIVES") and m["beats_spy"] and m["beats_6040"]
            and m["crisis_pass"] and fdr[h]["reject"] and m["fold_robust"]
            and (pbo_val is None or pbo_val < 0.50)
        )
        first_fail = None
        if not passes_is:
            legs = {
                "dsr_survives": m["dsr_verdict"].startswith("SURVIVES"),
                "beats_spy": m["beats_spy"], "beats_6040": m["beats_6040"],
                "crisis_pass": m["crisis_pass"], "fdr_reject": fdr[h]["reject"],
                "fold_robust": m["fold_robust"],
                "pbo_ok": (pbo_val is None or pbo_val < 0.50),
            }
            first_fail = next((k for k, v in legs.items() if not v), "?")

        hconf, ho_sharpe = False, None
        if passes_is and not exhausted and h not in touched:
            try:
                holdout.touch(h, touched)         # in-memory in dry run; persisted below if not
                mh = harness.score(c, holdout_slice, bill, n_eff=1, sixty40_sharpe=s6040)
                ho_sharpe = mh["sharpe"] if mh else 0.0
                hconf = holdout.confirms(m["sharpe"], ho_sharpe)
                if not dry_run:
                    loop_store.add_holdout_touch(con, h, HOLDOUT_START, ho_sharpe, now)
            except holdout.HoldoutBurned:
                hconf = False

        g = promote.gate(m, fdr_reject=fdr[h]["reject"], pbo=pbo_val,
                         holdout_confirms=hconf, budget_exhausted=exhausted)

        if g["stage"] == "paper":
            would_enroll.append(h)
            if not dry_run:
                # FactorCandidate.spec has no static 'weights' key (unlike the ETF
                # candidate); its live target weights come from materialize. Pass the
                # last-row {ticker: weight} mapping (over the FULL closes index, so the
                # archived asof matches closes.index[-1]) — NOT the spec dict.
                W = c.materialize(closes)
                live_weights = {k: float(v) for k, v in W.iloc[-1].to_dict().items()
                                if v != 0.0}
                paper.enroll(h, live_weights, asof=str(closes.index[-1].date()))
                loop_store.record_promotion(con, h, "paper", m["dsr"], n_eff, g["reason"], now)

        results.append({
            "name": c.name, "spec_hash": h, "cluster": labels.get(h),
            "sharpe": round(m["sharpe"], 3), "dsr": round(m["dsr"], 4),
            "dsr_verdict": m["dsr_verdict"], "cagr": round(m["cagr"], 2),
            "maxdd": round(m["maxdd"], 1),
            "beats_spy": bool(m["beats_spy"]), "beats_6040": bool(m["beats_6040"]),
            "crisis_pass": bool(m["crisis_pass"]), "fold_robust": bool(m["fold_robust"]),
            "p_value": round(m["p_value"], 4),
            "fdr_q": fdr[h].get("q"), "fdr_reject": bool(fdr[h]["reject"]),
            "passes_is": bool(passes_is), "first_fail": first_fail,
            "holdout_sharpe": (round(ho_sharpe, 3) if ho_sharpe is not None else None),
            "holdout_confirms": bool(hconf), "stage": g["stage"], "gate_reason": g["reason"],
        })

    results.sort(key=lambda r: -r["dsr"])
    return {
        "dry_run": dry_run,
        "n_candidates": len(base), "n_eff": n_eff, "pbo": pbo_val,
        "best_sharpe": round(best_sharpe, 3), "in_sample_years": round(years, 1),
        "budget_exhausted": exhausted, "s6040": round(s6040, 3),
        "fdr_survivors": sum(1 for h in fdr if fdr[h]["reject"]),
        "would_enroll": would_enroll, "n_would_enroll": len(would_enroll),
        "panel": {
            "names": int(closes.shape[1] - 2), "dates": int(closes.shape[0]),
            "span": [str(closes.index[0].date()), str(closes.index[-1].date())],
            "hygiene": panel["hygiene"],
        },
        "results": results,
    }


if __name__ == "__main__":
    import json as _json
    out = iterate(dry_run=True)
    print("=== SINGLE-NAME factor iteration (DRY RUN — no writes, no enroll) ===")
    p = out.get("panel", {})
    print(f"panel: {p.get('names')} names  {p.get('dates')} days  span {p.get('span')}")
    print(f"candidates={out['n_candidates']}  effective-N={out['n_eff']}  "
          f"in-sample={out['in_sample_years']}y  best Sharpe={out['best_sharpe']}  "
          f"6040 hurdle={out['s6040']}")
    print(f"PBO={out['pbo']}  FDR survivors={out['fdr_survivors']}  "
          f"budget exhausted={out['budget_exhausted']}")
    print(f"\nWOULD ENROLL TO PAPER (dry run): {out['n_would_enroll']} / {out['n_candidates']}")
    print(f"{'strategy':32s} {'clu':>3s} {'Shrp':>5s} {'DSR':>6s} {'spy':>3s} {'604':>3s} "
          f"{'cris':>4s} {'fold':>4s} {'rej':>3s} {'HOshrp':>7s} {'conf':>4s} {'stage':>9s}")
    for r in out["results"]:
        ho = f"{r['holdout_sharpe']:.2f}" if r["holdout_sharpe"] is not None else "  -"
        print(f"{r['name']:32s} {str(r['cluster']):>3s} {r['sharpe']:5.2f} {r['dsr']:6.3f} "
              f"{int(r['beats_spy']):>3d} {int(r['beats_6040']):>3d} {int(r['crisis_pass']):>4d} "
              f"{int(r['fold_robust']):>4d} {int(r['fdr_reject']):>3d} {ho:>7s} "
              f"{int(r['holdout_confirms']):>4d} {r['stage']:>9s}")
    print("\nHonest read: in-sample gate is a FILTER; the verdict is the forward paper Brier.")
    print(_json.dumps({"would_enroll": out["would_enroll"]}))
    print("DONE_SINGLE_NAME_ITERATE")

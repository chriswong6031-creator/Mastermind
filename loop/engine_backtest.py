"""Historical engine-backtest verdict — the HIGH-statistical-power, leakage-free answer.

The forward shadow books + prediction log measure the LLM/calibration layers, but those are
small-n and slow. The deterministic engine is different: it has NO parametric leakage, so it can be
judged historically over 20+ years × ~1,500 names — n in the thousands, today. This module surfaces
that verdict.

It reuses the FROZEN, committed gauntlet (loop.harness / loop.cluster / loop.pbo / engine.validation)
and the audited, survivorship-safe factor construction (loop.factor_experiment building blocks) to
run a cross-sectional single-name factor panel through: in-sample score → effective-N clustering →
DSR re-deflation → PBO/CSCV → BH-FDR → one-shot holdout confirmation.

Discipline: this is HEAVY and it BURNS the one-shot holdout per spec, so it is NOT run in the daily
build — it is generated on demand (`scripts/run_engine_backtest.py`) and the dashboard reads only the
persisted `data/backtest/engine_verdict.json`. `load()` is the cheap read path; `build()` is the run.
"""
from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_VERDICT = _ROOT / "data" / "backtest" / "engine_verdict.json"


def load() -> dict:
    """Cheap read of the persisted verdict (for the API). Honest empty-but-valid if none yet."""
    try:
        return json.loads(_VERDICT.read_text())
    except Exception:  # noqa: BLE001
        return {"status": "unavailable",
                "note": "no engine backtest recorded yet — run scripts/run_engine_backtest.py"}


def build(asof: str | None = None, write: bool = True) -> dict:
    """Run the frozen historical gauntlet and return (and optionally persist) the verdict.

    Heavy + holdout-burning — call from the on-demand script, NOT the daily build. Best-effort:
    returns a status='unavailable' stub (never raises) if data/infra is missing in this environment.
    """
    try:
        import bot  # noqa: F401 — vendor/macro on sys.path
        from loop import factor_experiment as fe, harness, cluster, pbo
        from engine import validation as V

        closes, mem, idx, hyg = fe.load_panel()
        bill = fe.bill_yield()
        n_names = len([c for c in closes.columns if c != "SPY"])
        insample = closes[closes.index < fe.HOLDOUT_START]
        holdout = closes[closes.index >= fe.HOLDOUT_START]
        s6040 = harness.sixty_forty_sharpe(insample, bill)

        cands = fe.build_pool(mem)
        pool, base = {}, {}
        for c in cands:
            m = harness.score(c, insample, bill, n_eff=1, sixty40_sharpe=s6040)
            if m is None:
                continue
            pool[c.spec_hash] = m["net"]
            base[c.spec_hash] = (c, m)
        if not base:
            return _stub(asof, "no candidate scored (degenerate panel)")

        n_eff = cluster.effective_n(pool, rho=fe.RHO)
        for h, (c, m) in base.items():                 # re-deflate DSR with the effective trial count
            mo = V.ret_moments(m["net"])
            if mo:
                sr_d, skew, kurt, T = mo
                dsr = V.deflated_sharpe(sr_d, skew, kurt, T, n_trials=max(1, n_eff),
                                        trading_year=252) or {"dsr": 0.0}
                m["dsr"], m["dsr_verdict"] = float(dsr["dsr"]), V.dsr_verdict(dsr["dsr"])

        pbo_res = pbo.cscv(pool, S=fe.PBO_S)
        pbo_val = pbo_res.get("pbo")
        pbo_ok = (pbo_val is None or pbo_val < 0.50)
        fdr = V.benjamini_hochberg({h: base[h][1]["p_value"] for h in base}, alpha=fe.BH_ALPHA)
        fdr_survivors = sum(1 for h in fdr if fdr[h]["reject"])

        rows_sorted = sorted(base.items(), key=lambda kv: -kv[1][1]["dsr"])
        top, passers = [], 0
        for h, (c, m) in rows_sorted:
            rej = bool(fdr.get(h, {}).get("reject"))
            passes_is = (m["dsr_verdict"].startswith("SURVIVES") and m["beats_spy"] and m["beats_6040"]
                         and m["crisis_pass"] and rej and m["fold_robust"] and pbo_ok)
            ho_sharpe, confirms = None, None
            if passes_is:
                passers += 1
                mh = harness.score(c, holdout, bill, n_eff=1, sixty40_sharpe=s6040)
                ho_sharpe = float(mh["sharpe"]) if mh else 0.0
                confirms = bool(ho_sharpe > 0 and ho_sharpe >= 0.5 * m["sharpe"])
            top.append({
                "name": c.name, "sharpe": round(float(m["sharpe"]), 3),
                "dsr": round(float(m["dsr"]), 3), "dsr_verdict": m["dsr_verdict"],
                "cagr": round(float(m["cagr"]), 2), "maxdd": round(float(m["maxdd"]), 2),
                "beats_spy": bool(m["beats_spy"]), "beats_6040": bool(m["beats_6040"]),
                "crisis_pass": bool(m["crisis_pass"]), "fold_robust": bool(m["fold_robust"]),
                "p_value": round(float(m["p_value"]), 4), "fdr_q": round(float(fdr[h]["q"]), 4) if fdr.get(h, {}).get("q") is not None else None,
                "fdr_reject": rej, "in_sample_pass": bool(passes_is),
                "holdout_sharpe": round(ho_sharpe, 3) if ho_sharpe is not None else None,
                "holdout_confirms": confirms,
            })

        best = rows_sorted[0][1][1]
        verdict = {
            "as_of": str(asof or "")[:10] or None,
            "status": "ok",
            "methodology": {
                "harness": "loop.harness (frozen: cost_bps=3.0, embargo=63d, 5-fold purged CV, 3 crisis windows)",
                "tests": ["DSR (deflated Sharpe)", "PBO/CSCV", "BH-FDR", "Newey-West HAC", "fold-robust", "one-shot holdout"],
                "universe": "S&P 1500 PIT membership (survivorship-safe; deep + delisted)",
                "in_sample": f"{insample.index[0].date()}..{insample.index[-1].date()}",
                "holdout": f"{holdout.index[0].date()}..{holdout.index[-1].date()}",
            },
            "panel": {"n_names": n_names, "n_dates": len(idx),
                      "span": f"{idx[0].date()}..{idx[-1].date()}",
                      "hygiene_clamped": int(hyg.get("n_clamped", 0))},
            "verdict": {
                "n_candidates": len(base), "effective_n": int(n_eff),
                "pbo": round(float(pbo_val), 3) if pbo_val is not None else None, "pbo_pass": pbo_ok,
                "best_sharpe": round(float(best["sharpe"]), 3),
                "best_dsr": round(float(best["dsr"]), 3), "best_dsr_verdict": best["dsr_verdict"],
                "fdr_survivors": fdr_survivors, "in_sample_passers": passers,
                "holdout_confirms": sum(1 for t in top if t.get("holdout_confirms")),
            },
            "top_candidates": top[:12],
            "honest_read": ("In-sample DSR/PBO/FDR are a FILTER, not proof. The real verdict is the "
                            "forward paper Brier + the holdout confirmation. n is large here "
                            "(thousands of name-days) because the deterministic engine has no "
                            "parametric leakage — unlike the LLM layers, which need forward shadow books."),
        }
        if write:
            try:
                _VERDICT.parent.mkdir(parents=True, exist_ok=True)
                _VERDICT.write_text(json.dumps(verdict, indent=2, default=str))
            except Exception:  # noqa: BLE001
                pass
        return verdict
    except Exception as exc:  # noqa: BLE001
        return _stub(asof, str(exc)[:200])


def _stub(asof, reason: str) -> dict:
    return {"as_of": str(asof or "")[:10] or None, "status": "unavailable", "error": reason,
            "note": "engine backtest could not run in this environment (data/network)"}

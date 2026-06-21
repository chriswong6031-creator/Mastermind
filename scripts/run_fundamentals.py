"""On-demand PIT fundamental-factor backtest (value + quality) through the frozen gauntlet.

Writes data/backtest/fundamentals.json (surfaced read-only on the dashboard). Heavy and it BURNS
the one-shot holdout per pool — run deliberately.

    python scripts/run_fundamentals.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: F401,E402
from loop import fundamentals  # noqa: E402


def main() -> None:
    v = fundamentals.run(asof=date.today().isoformat(), write=True)
    if v.get("status") != "ok":
        print("fundamentals UNAVAILABLE:", v.get("error") or v.get("note"))
        return
    ver = v["verdict"]
    print("fundamentals verdict written ->", fundamentals._OUT)
    print(f"  candidates={ver['n_candidates']} effective_n={ver['effective_n']} "
          f"PBO={ver['pbo']} (pass={ver['pbo_pass']})")
    print(f"  best Sharpe={ver['best_sharpe']} DSR={ver['best_dsr']} ({ver['best_dsr_verdict']})")
    print(f"  FDR survivors={ver['fdr_survivors']} in-sample passers={ver['in_sample_passers']}")
    print(f"  holdout-confirmed: {ver['holdout_confirmed']}")
    print("  factors:")
    for r in v["factors"]:
        print(f"    {r['name']:24s} [{r['family']:7s}] dsr={r['dsr']:.3f} sharpe={r['sharpe']:.2f} "
              f"fdr={int(r['fdr_reject'])} pass={int(r['in_sample_pass'])}")
    if v.get("regime_conditional_ic"):
        print("  regime-conditional IC (63d):")
        for nm, o in v["regime_conditional_ic"].items():
            print(f"    {nm:24s} risk_on={o.get('risk_on',{}).get('mean_ic')} "
                  f"risk_off={o.get('risk_off',{}).get('mean_ic')}")


if __name__ == "__main__":
    main()

"""On-demand advanced factor-zoo backtest.

Scores a broad price-factor library + a holdout-validated composite through the frozen
multiple-testing gauntlet and writes data/backtest/factor_zoo.json (surfaced read-only on the
dashboard). Heavy (~1-2 min) and it BURNS the one-shot holdout per spec — run deliberately.

    python scripts/run_factor_zoo.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: F401,E402
from loop import factor_zoo  # noqa: E402


def main() -> None:
    v = factor_zoo.run(asof=date.today().isoformat(), write=True)
    if v.get("status") != "ok":
        print("factor zoo UNAVAILABLE:", v.get("error") or v.get("note"))
        return
    ver = v["verdict"]
    print("factor zoo verdict written ->", factor_zoo._OUT)
    print(f"  pool_fingerprint={v.get('pool_fingerprint')}  (holdout burned once for THIS pool; "
          f"a different fingerprint = a fresh holdout)")
    print(f"  candidates={ver['n_candidates']} effective_n={ver['effective_n']} "
          f"PBO={ver['pbo']} (pass={ver['pbo_pass']})")
    print(f"  best Sharpe={ver['best_sharpe']} DSR={ver['best_dsr']} ({ver['best_dsr_verdict']})")
    print(f"  FDR survivors={ver['fdr_survivors']} in-sample passers={ver['in_sample_passers']}")
    print(f"  holdout-confirmed: {ver['holdout_confirmed']}")
    comp = ver.get("composite")
    if comp:
        print(f"  COMPOSITE: dsr={comp['dsr']} sharpe={comp['sharpe']} "
              f"is_pass={comp['in_sample_pass']} holdout_sharpe={comp['holdout_sharpe']} "
              f"confirms={comp['holdout_confirms']}")
    print("  top factors:")
    for r in v["factors"][:6]:
        print(f"    {r['name']:22s} dsr={r['dsr']:.3f} sharpe={r['sharpe']:.2f} "
              f"fdr_rej={int(r['fdr_reject'])} pass={int(r['in_sample_pass'])}")


if __name__ == "__main__":
    main()

"""On-demand historical engine-backtest verdict generator.

Runs the FROZEN cross-sectional factor gauntlet (DSR / PBO / BH-FDR / one-shot holdout) over the
survivorship-safe S&P-1500 panel and writes data/backtest/engine_verdict.json, which the dashboard
surfaces read-only. Heavy (~10-60s) and it BURNS the one-shot holdout per spec — run deliberately,
not in the daily loop.

    python scripts/run_engine_backtest.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root → import bot/loop

import bot  # noqa: F401,E402 — vendor/macro on sys.path before importing loop/engine deps
from loop import engine_backtest  # noqa: E402


def main() -> None:
    v = engine_backtest.build(asof=date.today().isoformat(), write=True)
    if v.get("status") != "ok":
        print("engine backtest UNAVAILABLE:", v.get("error") or v.get("note"))
        return
    ver = v["verdict"]
    print("engine backtest verdict written ->", engine_backtest._VERDICT)
    print(f"  candidates={ver['n_candidates']} effective_n={ver['effective_n']} "
          f"PBO={ver['pbo']} (pass={ver['pbo_pass']})")
    print(f"  best Sharpe={ver['best_sharpe']} DSR={ver['best_dsr']} ({ver['best_dsr_verdict']})")
    print(f"  FDR survivors={ver['fdr_survivors']} in-sample passers={ver['in_sample_passers']} "
          f"holdout confirms={ver['holdout_confirms']}")


if __name__ == "__main__":
    main()

"""On-demand Arm-A desk-lever backtest (AB_EXPERIMENT §4.1).

Runs the FROZEN multiple-testing gauntlet (DSR / PBO / BH-FDR / one-shot 2022+ holdout) over the
survivorship-safe S&P-1500 panel for the desk levers — applied across FIVE economically-distinct
baseline families (12-1 momentum, low-vol, trend-consistency/quality, 1-month reversal, multi-factor
composite), each crossed with {Baseline, Concentration (L2), EntryTiming (L3), Combo (L2+L3)} = 20
candidates — and writes data/backtest/desk_levers.json, which the dashboard surfaces read-only.

A wide, DE-CORRELATED pool makes the multiple-testing deflation honest (effective_n > 1) and lets us
check whether L3's / L2's edge is CONSISTENT across families or a momentum-only artifact.

These are PRICE-FACTOR PROXIES for the desk levers (the historical panel has no Conviction Index /
entry technicals — see loop/desk_levers.py docstring; the composite family is the closest honest
stand-in for the confluence pick). Heavy and it BURNS the one-shot holdout per spec — run
deliberately, not in the daily loop.

    python scripts/run_desk_levers.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root → import bot/loop

import bot  # noqa: F401,E402 — vendor/macro on sys.path before importing loop/engine deps
from loop import desk_levers  # noqa: E402


def _b(x) -> str:
    return "Y" if x else "-"


def main() -> None:
    v = desk_levers.build(asof=date.today().isoformat(), write=True)
    if v.get("status") != "ok":
        print("desk-lever Arm-A backtest UNAVAILABLE:", v.get("error") or v.get("note"))
        return
    ver = v["verdict"]
    print("desk-lever verdict written ->", desk_levers._OUT)
    print(f"  pool_fingerprint={v.get('pool_fingerprint')}  (holdout burned once for THIS pool; "
          f"a different fingerprint = a fresh holdout)")
    print(f"  candidates={ver['n_candidates']} effective_n={ver['effective_n']} "
          f"({ver.get('n_families')} families × {ver.get('n_levers_per_family')} levers)  "
          f"PBO={ver['pbo']} (pass={ver['pbo_pass']})")
    print(f"  best Sharpe={ver['best_sharpe']} DSR={ver['best_dsr']} ({ver['best_dsr_verdict']})")
    print(f"  FDR survivors={ver['fdr_survivors']}  Arm-A passers={ver['arm_a_passers']}")
    print()
    print("  PRICE-FACTOR PROXY for the desk levers — Arm A clears/kills the factorable L2/L3;")
    print("  it cannot justify the LLM seat build (that needs Arm B's forward H4/H5). See §5/§7.")
    print("  (No historical engine-confluence panel exists — the composite family is the stand-in.)")
    print()

    # readable comparison table (delta is vs each lever's OWN family baseline)
    hdr = (f"  {'lever':26s} {'Shrp':>5s} {'DSR':>6s} {'DSR>0':>5s} {'PBO':>4s} {'FDR':>4s} "
           f"{'hold':>5s} {'PASS':>4s}  {'dSharpe':>8s} {'dDSR':>7s} {'dHoldShrp':>9s}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in v["levers"]:
        d = r.get("delta_vs_baseline") or {}
        ds = d.get("sharpe")
        dd = d.get("dsr")
        dh = d.get("holdout_sharpe")
        ho = r.get("holdout_sharpe")
        print(f"  {r['name']:26s} {r['sharpe']:5.2f} {r['dsr']:6.3f} {_b(r['dsr_positive']):>5s} "
              f"{_b(r['pbo_pass']):>4s} {_b(r['fdr_survive']):>4s} "
              f"{(f'{ho:.2f}' if ho is not None else '   --'):>5s} "
              f"{_b(r['arm_a_pass']):>4s}  "
              f"{(f'{ds:+.3f}' if ds is not None else '   base'):>8s} "
              f"{(f'{dd:+.3f}' if dd is not None else '  base'):>7s} "
              f"{(f'{dh:+.3f}' if dh is not None else '     base'):>9s}")
    print()
    print("  Per-lever verdict (§4.1 bars):")
    for r in v["levers"]:
        print(f"    {r['name']:26s} {r['verdict_line']}")
    print()

    # the headline cross-family read: is L3 / L2 a real lever or a momentum-only artifact?
    cfr = ver.get("cross_family_read") or {}
    print("  CROSS-FAMILY read (is each lever a general edge or a momentum-only artifact?):")
    for key in ("l3_entry_timing", "l2_concentration", "combo_l2_l3"):
        t = cfr.get(key)
        if t:
            print(f"    {key:18s} {t['verdict']}")
    print()
    passers = ver["arm_a_passers"]
    if passers:
        print(f"  §4.1 VERDICT: lever(s) PASS Arm A vs baseline -> {passers}")
        print("  (Arm A can CLEAR the factorable L2/L3; the seat-build decision still needs Arm B.)")
    else:
        print("  §4.1 VERDICT: NO lever cleared the Arm-A bar (DSR>0, PBO<0.5, BH-FDR q=0.10, "
              "2022+ holdout) -> the factorable levers are not historically confirmed.")


if __name__ == "__main__":
    main()

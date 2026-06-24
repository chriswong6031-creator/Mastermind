"""DRY-RUN the Flagship judgment desk (P1+P2) — invoke the seats for REAL (live Opus + the macro
dashboard) and PRINT their outputs WITHOUT applying anything to the live book.

This is a read-only proof of the seats' REASONING QUALITY: no rebalance, no position close, no
publish. The PM submits to the isolated ``flagship_judgment`` path (never the live ``flagship``
book). Run from the credentialed main-checkout shell with the full Macro Dashboard on PYTHONPATH:

    DASH="/Users/chriswong/Documents/Cluade/Macro Dashboard"
    ln -sfn "$DASH/data" vendor/macro/data
    PYTHONPATH="$DASH:$PYTHONPATH" python scripts/dryrun_desk.py
"""
import os
import sys
import json
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# seats that gate on these flags must see them ON for this process only (we never persist/trade)
os.environ["MASTERMIND_FLAGSHIP_JUDGMENT"] = "1"
os.environ["MASTERMIND_GATE_OFFICER"] = "1"
os.environ["MASTERMIND_RISK_OFFICER"] = "1"
os.environ.setdefault("FLAGSHIP_PM_MAX_TURNS", "12")  # bound the armed PM session cost/time

import bot  # noqa: E402,F401 — bootstraps vendor/macro onto sys.path

ASOF = date.today().isoformat()


def _regime() -> dict:
    for p in ("vendor/macro/data/regime/latest.json",
              "vendor/macro_src/data/regime/latest.json"):
        try:
            return json.load(open(p))
        except Exception:
            continue
    return {}


def main() -> None:
    regime = _regime()
    print("=" * 70)
    print(f"DRY-RUN — Flagship judgment desk @ {ASOF}  (nothing is applied)")
    print("=" * 70)
    print("regime:", {k: regime.get(k) for k in ("quad", "quad_name", "liquidity_overlay", "cycle_tag")})
    try:
        from brain import client
        print("client.available:", client.available())
    except Exception as e:  # noqa: BLE001
        print("client import error:", repr(e))

    # 1. MACRO STRATEGIST -------------------------------------------------------
    strat = None
    try:
        from brain import strategist
        strat = strategist.run(ASOF, regime)
        print("\n--- (1) MACRO STRATEGIST ---")
        if strat:
            print("backdrop:", strat.get("backdrop_stance"), "| supportive:", strat.get("supportive"),
                  "| crowding:", strat.get("crowding_flags"))
            for th in (strat.get("confirmed_themes") or [])[:10]:
                print(f"  • {th.get('theme')} [{th.get('stage')}] lead={th.get('leadership')}: "
                      f"{', '.join((th.get('names') or [])[:8])}")
            print("rationale:", (strat.get("rationale") or "")[:700])
        else:
            print("(no strategist output — seat returned None)")
    except Exception as e:  # noqa: BLE001
        print("STRATEGIST error:", repr(e))

    # 2. PM-CONVICTION (the thematic target book) -------------------------------
    book = None
    try:
        from brain import pm_conviction
        print("\n--- (2) PM-CONVICTION — building the target book (armed Opus, may take minutes) ---")
        book = pm_conviction.build_book([], [], regime=regime, asof=ASOF, strategist=strat, gate_info={})
        if book and book.get("ran"):
            for h in (book.get("holdings") or []):
                print(f"  {str(h.get('ticker')):6s} {float(h.get('weight') or 0)*100:5.1f}%  "
                      f"[{h.get('conviction')}]  {(h.get('thesis') or '')[:150]}")
            print("cash:", book.get("cash"), "| summary:", (book.get("summary") or "")[:600])
            if book.get("sold_note"):
                print("sold:", str(book["sold_note"])[:400])
        else:
            print("(PM did not produce a book; ran =", (book or {}).get("ran"), ")")
    except Exception as e:  # noqa: BLE001
        print("PM error:", repr(e))

    # 3. GATE OFFICER (veto/withhold/trim — decisions only, NOT applied) --------
    try:
        if book and book.get("holdings"):
            from brain import gate_officer
            rows = [{"ticker": h.get("ticker"), "weight": h.get("weight"),
                     "bull": h.get("thesis", ""), "bear": "", "divergences": []}
                    for h in book["holdings"]]
            gate = gate_officer.gate_assess(rows, ASOF, regime=regime, portfolio_ctx={})
            print("\n--- (3) GATE OFFICER — veto/withhold/trim (NOT applied) ---")
            for d in (gate.get("decisions") or []):
                print(f"  {str(d.get('ticker')):6s} {str(d.get('action')):9s} "
                      f"scale={d.get('scale')}  {str(d.get('reason') or '')[:130]}")
            print("rationale:", (gate.get("rationale") or "")[:400])
        else:
            print("\n--- (3) GATE OFFICER — skipped (no PM book to review) ---")
    except Exception as e:  # noqa: BLE001
        print("GATE error:", repr(e))

    # 4. RISK OFFICER (held-position exits/trims — decisions only, NOT applied) -
    try:
        from portfolio import position_log
        from brain import risk_officer
        try:
            held = position_log.open_positions()
        except Exception:  # noqa: BLE001
            held = []
        risk = risk_officer.risk_assess([], ASOF, regime=regime, held_positions=held)
        print("\n--- (4) RISK OFFICER — held-position exits/trims (NOT applied) ---")
        print("held positions reviewed:", len(held) if hasattr(held, "__len__") else "?")
        for d in (risk.get("decisions") or []):
            print(f"  {str(d.get('ticker')):6s} {str(d.get('action')):6s} "
                  f"scale={d.get('scale')}  {str(d.get('reason') or '')[:130]}")
        print("rationale:", (risk.get("rationale") or "")[:400])
    except Exception as e:  # noqa: BLE001
        print("RISK error:", repr(e))

    print("\n" + "=" * 70)
    print("DRY RUN COMPLETE — nothing applied (no rebalance / close / publish).")
    print("PM submission (if any) went to the isolated flagship_judgment path, not the live book.")
    print("=" * 70)


if __name__ == "__main__":
    main()

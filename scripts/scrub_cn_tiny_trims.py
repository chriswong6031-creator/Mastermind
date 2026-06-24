"""One-off: scrub the two de-minimis CN Brain trims from 2026-06-22.

The 2026-06-23 01:36 UTC rebalance snapped 688411.SS and 688585.SS back to their exact
target weights against a drifted NAV, manufacturing two fractional SELLS the Brain never
intended (its own sold_note: "No trims elsewhere; ... held flat"). The new no-trade band
prevents recurrence; this reverses the two that already landed so the book reads as the
Brain meant it to (hold, no change). 601126.SS (full exit) and 603156.SS (deliberate 7%->6%
trim) are LEFT ALONE — they were real decisions.

Reverses, atomically and with a backup:
  - account.json     : restore the sold shares, debit the cash the sells credited
  - fills.jsonl      : drop the two sell fills
  - decisions.jsonl  : drop the two sells from the day's `executed` list
  - latest.json      : recompute cash / gross / NAV + the two positions' marks from the
                       fixed account (positions are re-marked live by the API, but keep the
                       published snapshot self-consistent)

Idempotent: refuses to run twice (the sell fills are gone after the first pass).
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

BOOK = Path(__file__).resolve().parent.parent / "data" / "portfolios" / "china"

# The two trims to reverse: (ticker, sold_shares, price) exactly as recorded in fills.jsonl.
TRIMS = [
    ("688411.SS", 19.826594, 286.98),
    ("688585.SS", 51.207122, 162.59),
]
ASOF = "2026-06-22"


def _read_jsonl(p: Path) -> list[dict]:
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def main() -> None:
    account_p = BOOK / "account.json"
    fills_p = BOOK / "fills.jsonl"
    decisions_p = BOOK / "decisions.jsonl"
    latest_p = BOOK / "latest.json"

    fills = _read_jsonl(fills_p)
    to_drop = {(ASOF, tk, "sell", round(sh, 6)) for tk, sh, _ in TRIMS}
    matched = [f for f in fills if (f.get("date"), f.get("ticker"), f.get("side"),
                                    round(float(f.get("shares") or 0), 6)) in to_drop]
    if len(matched) != len(TRIMS):
        raise SystemExit(f"Expected {len(TRIMS)} sell fills to scrub, found {len(matched)} "
                         f"— already scrubbed or data changed; aborting (no writes).")

    # ---- backup everything we touch ----
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = BOOK / f"_backup_scrub_tiny_trims_{ts}"
    backup.mkdir(parents=True, exist_ok=True)
    for p in (account_p, fills_p, decisions_p, latest_p):
        if p.exists():
            shutil.copy2(p, backup / p.name)
    print(f"backup -> {backup}")

    # ---- account.json: restore shares, debit the cash the sells credited ----
    account = json.loads(account_p.read_text())
    print(f"cash before: {account['cash']:.4f}")
    for tk, sh, px in TRIMS:
        pos = account["positions"][tk]
        before_sh = pos["shares"]
        pos["shares"] = before_sh + sh
        account["cash"] -= sh * px
        print(f"  {tk}: shares {before_sh:.6f} -> {pos['shares']:.6f}  (cash -{sh * px:.2f})")
    print(f"cash after:  {account['cash']:.4f}")
    account_p.write_text(json.dumps(account, indent=2))

    # ---- fills.jsonl: drop the two sells ----
    kept = [f for f in fills if f not in matched]
    fills_p.write_text("".join(json.dumps(f) + "\n" for f in kept))
    print(f"fills: {len(fills)} -> {len(kept)}")

    # ---- decisions.jsonl: drop the two sells from `executed` ----
    drop_tickers = {tk for tk, _, _ in TRIMS}
    decisions = _read_jsonl(decisions_p)
    n_removed = 0
    for d in decisions:
        ex = d.get("executed")
        if not ex:
            continue
        new_ex = [e for e in ex if not (e.get("side") == "sell" and e.get("ticker") in drop_tickers)]
        n_removed += len(ex) - len(new_ex)
        d["executed"] = new_ex
    decisions_p.write_text("".join(json.dumps(d, ensure_ascii=False, default=str) + "\n" for d in decisions))
    print(f"decisions: removed {n_removed} executed sell record(s)")

    # ---- latest.json: recompute marks/cash/gross/NAV from the fixed account ----
    latest = json.loads(latest_p.read_text())
    cash = account["cash"]
    invested = 0.0
    for pos in latest.get("positions", []):
        tk = pos.get("ticker")
        acc = account["positions"].get(tk)
        if not acc:
            continue
        shares = acc["shares"]
        cost = acc["avg_cost"]
        cur = pos.get("current_price") or cost
        mv = shares * cur
        invested += mv
        pos["market_value"] = round(mv, 2)
        pos["unrealized_pnl"] = round((cur - cost) * shares, 2)
        pos["unrealized_pct"] = round((cur / cost - 1) * 100, 2) if cost else None
    nav = cash + invested
    latest["cash_usd"] = round(cash, 2)
    latest["nav"] = round(nav, 2)
    latest["cash"] = round(cash / nav, 4) if nav else None
    latest["gross"] = round(invested / nav, 4) if nav else None
    latest_p.write_text(json.dumps(latest, indent=2, ensure_ascii=False))
    print(f"latest.json: nav={nav:.2f}  cash={latest['cash']}  gross={latest['gross']}")
    print("done.")


if __name__ == "__main__":
    main()

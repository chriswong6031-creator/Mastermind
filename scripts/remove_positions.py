"""Erase named positions from a paper book AS IF THEY WERE NEVER BOUGHT.

For each target ticker held: returns its full purchase COST (shares × avg_cost) to cash — so the
position's unrealized P&L is handed back to the book's value, not realized as a loss — deletes the
position, and removes every trace of it from the trade history and holdings surfaces:

  account.json          positions{} entry deleted, cash += cost basis
  fills.jsonl           every fill line for the ticker dropped (the trade-history blotter)
  latest.json           positions[] / executed_today[] entries dropped; cash_usd / nav / cash /
                        gross recomputed from the survivors  (Brain books only)
  positions_ledger.json the ticker's lifecycle entry dropped  (Brain books — open list + activity)
  decisions.jsonl       holdings[] / executed[] entries dropped from each record  (Brain books)

The Brain's free-text prose (summary / brain_text / decision thesis) is left untouched — excising
two names from generated prose can't be done cleanly. PAPER ONLY; reversible from the backup.

Usage:  python3 scripts/remove_positions.py <pid> <T1,T2,...>
        python3 scripts/remove_positions.py self_directed AVGO,NVDA
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: F401,E402 — bootstraps vendor/macro onto sys.path

from portfolio import registry  # noqa: E402


def _read_json(p: Path):
    return json.loads(p.read_text()) if p.exists() else None


def _restore_account(acct_path: Path, targets: set[str]) -> dict:
    """Delete target positions; credit each one's full cost basis (shares×avg_cost) back to cash."""
    acct = _read_json(acct_path)
    restored = {}
    for t in list(acct.get("positions", {})):
        if t.upper() in targets:
            pos = acct["positions"].pop(t)
            cost = float(pos["shares"]) * float(pos["avg_cost"])
            acct["cash"] = float(acct.get("cash", 0.0)) + cost
            restored[t.upper()] = round(cost, 2)
    acct_path.write_text(json.dumps(acct, indent=2, default=str))
    return {"restored": restored, "cash_after": round(acct.get("cash", 0.0), 2)}


def _scrub_fills(fills_path: Path, targets: set[str]) -> int:
    if not fills_path.exists():
        return 0
    kept, dropped = [], 0
    for line in fills_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            kept.append(line)
            continue
        if (rec.get("ticker") or "").upper() in targets:
            dropped += 1
        else:
            kept.append(line)
    fills_path.write_text(("\n".join(kept) + "\n") if kept else "")
    return dropped


def _scrub_latest(latest_path: Path, targets: set[str], restored: dict) -> None:
    """Brain-book latest.json: drop the positions / executed_today rows and re-derive the money fields."""
    d = _read_json(latest_path)
    if not d:
        return
    d["positions"] = [p for p in d.get("positions", []) if (p.get("ticker") or "").upper() not in targets]
    d["executed_today"] = [e for e in d.get("executed_today", []) if (e.get("ticker") or "").upper() not in targets]
    cash_usd = float(d.get("cash_usd", 0.0)) + sum(restored.values())
    invested = sum(float(p.get("market_value") or 0.0) for p in d["positions"])
    nav = cash_usd + invested
    d["cash_usd"] = round(cash_usd, 2)
    d["nav"] = round(nav, 2)
    d["cash"] = round(cash_usd / nav, 4) if nav else 1.0
    d["gross"] = round(invested / nav, 4) if nav else 0.0
    latest_path.write_text(json.dumps(d, indent=2, default=str, ensure_ascii=False))


def _scrub_ledger(ledger_path: Path, targets: set[str]) -> int:
    d = _read_json(ledger_path)
    if not isinstance(d, dict):
        return 0
    drop = [k for k, v in d.items() if (v.get("ticker") or k.split(":")[-1]).upper() in targets]
    for k in drop:
        d.pop(k)
    ledger_path.write_text(json.dumps(d, indent=2, default=str))
    return len(drop)


def _scrub_decisions(dec_path: Path, targets: set[str]) -> int:
    if not dec_path.exists():
        return 0
    out, touched = [], 0
    for line in dec_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            out.append(line)
            continue
        for arr in ("holdings", "executed"):
            if isinstance(rec.get(arr), list):
                before = len(rec[arr])
                rec[arr] = [h for h in rec[arr] if (h.get("ticker") or "").upper() not in targets]
                touched += before - len(rec[arr])
        out.append(json.dumps(rec, default=str))
    dec_path.write_text(("\n".join(out) + "\n") if out else "")
    return touched


def main(pid: str, tickers: list[str]) -> int:
    targets = {t.upper().strip() for t in tickers if t.strip()}
    is_sd = pid == "self_directed"
    base = (registry._ROOT / "data" / "portfolio" / "self_directed") if is_sd else registry.data_dir(pid)
    print(f"[{pid}] removing {sorted(targets)} from {base}")

    res = _restore_account(base / "account.json", targets)
    print(f"   account: restored cost basis {res['restored']} → cash {res['cash_after']:,.2f}")
    n_fills = _scrub_fills(base / "fills.jsonl", targets)
    print(f"   fills.jsonl: dropped {n_fills} line(s)")

    if not is_sd:  # Brain-book surfaces (self_directed computes its book live from account+fills)
        _scrub_latest(base / "latest.json", targets, res["restored"])
        n_led = _scrub_ledger(base / "positions_ledger.json", targets)
        n_dec = _scrub_decisions(base / "decisions.jsonl", targets)
        print(f"   latest.json: positions/executed_today/money re-derived")
        print(f"   positions_ledger.json: dropped {n_led} entry(s)")
        print(f"   decisions.jsonl: dropped {n_dec} holdings/executed entry(s)")
    print(f"[{pid}] done.")

    # MW2 emitter (f): operator-script governance event
    try:
        from control_plane import governance as _gov
        _gov.append({
            "event_type": "operator_action",
            "target": pid,
            "actor": "operator-script",
            "reason": (
                f"remove_positions: erased {sorted(targets)} from book '{pid}' "
                f"(cost basis {res.get('restored')} returned to cash; fills.jsonl scrubbed)"
            ),
            "before": f"positions held: {sorted(targets)}",
            "after": f"positions removed; cash restored to {res.get('cash_after')}",
            "rollback": (
                f"restore data/portfolios/{pid}/ (account.json, fills.jsonl, "
                "latest.json, positions_ledger.json, decisions.jsonl) from git backup"
            ),
            "source_artifact": "scripts/remove_positions.py",
        })
    except Exception:  # noqa: BLE001
        pass

    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2].split(",")))

"""One-off: remove AVGO & NVDA from the Heavyweight, Autonomous, and Self-Directed
paper books *as if they never existed*.

For each book we:
  - refund the cost basis of the AVGO/NVDA lots back to cash (restores the unrealized
    loss to the portfolio value — the book reconciles back to a flat $1,000,000 NAV),
  - delete the AVGO/NVDA positions,
  - erase the AVGO/NVDA buy fills from the trade-history blotter (fills.jsonl),
  - for the two paper_account brain books, also fix the inception nav_history row,
    the positions_ledger entries, and the latest.json / portfolio.json display
    snapshots (cash fraction, cash_usd, gross, positions list).

The brain's prose journal (decisions.jsonl / latest.json `summary`) is left intact —
it is a historical reasoning record and self-heals on the next daily build.

A full backup of each touched book dir is written under data/_backup_remove_nvda_avgo_<ts>/.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REMOVE = {"AVGO", "NVDA"}
EPS = 0.01

TS = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
BACKUP_ROOT = ROOT / "data" / f"_backup_remove_nvda_avgo_{TS}"

# (id, kind, dir) — kind drives which derived files we repair.
BOOKS = [
    ("heavyweight",   "paper_account", ROOT / "data" / "portfolios" / "heavyweight"),
    ("autonomous",    "paper_account", ROOT / "data" / "portfolios" / "autonomous"),
    ("self_directed", "self_directed", ROOT / "data" / "portfolio" / "self_directed"),
]


def _read_json(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def _write_json(p: Path, obj) -> None:
    p.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")


def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _write_jsonl(p: Path, rows: list[dict]) -> None:
    body = "".join(json.dumps(r, default=str) + "\n" for r in rows)
    p.write_text(body, encoding="utf-8")


def backup(book_dir: Path) -> None:
    dest = BACKUP_ROOT / book_dir.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(book_dir, dest)
    print(f"  backed up {book_dir} -> {dest}")


def fix_account(book_dir: Path) -> tuple[float, float, list[str]]:
    """Refund removed lots' cost basis to cash, drop the positions.
    Returns (refund_total, new_cash, removed_tickers)."""
    acct = _read_json(book_dir / "account.json")
    positions: dict = acct.get("positions") or {}
    refund = 0.0
    removed = []
    for tk in list(positions):
        if tk.upper() in REMOVE:
            pos = positions[tk]
            refund += float(pos["shares"]) * float(pos["avg_cost"])
            del positions[tk]
            removed.append(tk.upper())
    acct["cash"] = round(float(acct.get("cash", 0.0)) + refund, 2)
    _write_json(book_dir / "account.json", acct)
    return refund, acct["cash"], removed


def fix_fills(book_dir: Path) -> int:
    """Erase AVGO/NVDA fills from the blotter. Returns count removed."""
    p = book_dir / "fills.jsonl"
    rows = _read_jsonl(p)
    kept = [r for r in rows if (r.get("ticker") or "").upper() not in REMOVE]
    _write_jsonl(p, kept)
    return len(rows) - len(kept)


def fix_nav_history(book_dir: Path, new_cash: float) -> None:
    """Rewrite cash/invested on each nav_history row to match the refund (nav unchanged)."""
    p = book_dir / "nav_history.jsonl"
    if not p.exists():
        return
    rows = _read_jsonl(p)
    for r in rows:
        if "cash" in r:
            r["cash"] = round(new_cash, 2)
        if "nav" in r and "invested" in r:
            r["invested"] = round(float(r["nav"]) - new_cash, 2)
    _write_jsonl(p, rows)


def fix_ledger(book_dir: Path) -> int:
    """Delete positions_ledger entries for the removed tickers. Returns count removed."""
    p = book_dir / "positions_ledger.json"
    if not p.exists():
        return 0
    led = _read_json(p)
    drop = [k for k, v in led.items()
            if (isinstance(v, dict) and (v.get("ticker") or "").upper() in REMOVE)
            or k.split(":")[-1].upper() in REMOVE]
    for k in drop:
        del led[k]
    _write_json(p, led)
    return len(drop)


def fix_snapshot(book_dir: Path, fname: str, new_cash: float) -> None:
    """Repair latest.json / portfolio.json display snapshot: drop the removed positions,
    refresh cash fraction / cash_usd / gross. Prose (summary/decisions) left untouched."""
    p = book_dir / fname
    if not p.exists():
        return
    d = _read_json(p)
    nav = float(d.get("nav") or 1_000_000.0)
    d["positions"] = [pos for pos in (d.get("positions") or [])
                      if (pos.get("ticker") or "").upper() not in REMOVE]
    d["cash_usd"] = round(new_cash, 2)
    d["cash"] = round(new_cash / nav, 6) if nav else d.get("cash")
    d["gross"] = round(sum(float(pos.get("weight") or 0.0) for pos in d["positions"]), 6)
    _write_json(p, d)


def main() -> None:
    print(f"Removing {sorted(REMOVE)} from books as if they never existed.")
    print(f"Backup root: {BACKUP_ROOT}\n")
    for pid, kind, book_dir in BOOKS:
        print(f"[{pid}]  ({kind})  {book_dir}")
        backup(book_dir)
        refund, new_cash, removed = fix_account(book_dir)
        n_fills = fix_fills(book_dir)
        print(f"  removed positions: {removed}  refund=${refund:,.2f}  -> cash=${new_cash:,.2f}")
        print(f"  erased {n_fills} fill line(s) from trade history")
        if kind == "paper_account":
            fix_nav_history(book_dir, new_cash)
            n_led = fix_ledger(book_dir)
            fix_snapshot(book_dir, "latest.json", new_cash)
            fix_snapshot(book_dir, "portfolio.json", new_cash)
            print(f"  dropped {n_led} positions_ledger entr(ies); fixed nav_history + latest.json + portfolio.json")
        print()
    print(f"DONE. Backup at {BACKUP_ROOT}")


if __name__ == "__main__":
    main()

"""Empirical blast-radius sweep of the `altman_distress` engine veto.

Runs the deterministic decision matrix (portfolio.lenses.full) across a broad,
multi-sector universe and tallies how often `altman_distress` fires and how often
the name ends up engine-blocked. The point is to see whether the veto is
sector-specific (capital-intensive names where Altman Z is known to misfire) or
broad — and whether it blanket-blocks names that are obviously NOT distressed
(net-cash mega-caps), which would mean the veto is misleading Mastermind.

Read-only, paper-only. Nothing is gated or executed.

Run:  python -m scripts.altman_blast_radius
"""
from __future__ import annotations

import json
import sys
import traceback
from collections import defaultdict
from datetime import date
from pathlib import Path

import bot  # noqa: F401  (bootstraps vendor/macro onto sys.path)

from portfolio import lenses

# (ticker, sector, prior) — prior="safe" = should NOT be distressed (net-cash / IG mega-cap),
# "lev" = structurally levered (Altman Z known to misfire), "" = neutral/unknown.
UNIVERSE = [
    # Mega-cap tech / net-cash — should be the cleanest "not distressed" controls
    ("AAPL", "Tech", "safe"), ("MSFT", "Tech", "safe"), ("GOOGL", "Tech", "safe"),
    ("NVDA", "Tech", "safe"), ("META", "Tech", "safe"), ("AVGO", "Tech", "safe"),
    ("ADBE", "Tech", "safe"), ("CRM", "Tech", "safe"), ("ORCL", "Tech", ""),
    ("NOW", "Tech", "safe"), ("INTU", "Tech", "safe"),
    # Consumer staples
    ("PG", "Staples", "safe"), ("KO", "Staples", ""), ("PEP", "Staples", ""),
    ("COST", "Staples", "safe"), ("WMT", "Staples", ""), ("MO", "Staples", "lev"),
    ("PM", "Staples", "lev"),
    # Consumer discretionary
    ("AMZN", "Discretionary", "safe"), ("HD", "Discretionary", ""), ("MCD", "Discretionary", "lev"),
    ("NKE", "Discretionary", "safe"), ("SBUX", "Discretionary", "lev"), ("TSLA", "Discretionary", "safe"),
    # Healthcare / pharma
    ("JNJ", "Healthcare", "safe"), ("UNH", "Healthcare", ""), ("LLY", "Healthcare", ""),
    ("PFE", "Healthcare", ""), ("MRK", "Healthcare", ""), ("ABBV", "Healthcare", "lev"),
    ("TMO", "Healthcare", ""),
    # Financials / banks (leverage by design — Z-score not meant for them)
    ("JPM", "Financials", "lev"), ("BAC", "Financials", "lev"), ("WFC", "Financials", "lev"),
    ("GS", "Financials", "lev"), ("MS", "Financials", "lev"), ("C", "Financials", "lev"),
    # Energy
    ("XOM", "Energy", ""), ("CVX", "Energy", ""), ("COP", "Energy", ""),
    ("SLB", "Energy", ""), ("OXY", "Energy", "lev"),
    # Industrials (capital-intensive)
    ("BA", "Industrials", "lev"), ("CAT", "Industrials", "lev"), ("GE", "Industrials", "lev"),
    ("HON", "Industrials", ""), ("UNP", "Industrials", "lev"), ("UPS", "Industrials", "lev"),
    ("LMT", "Industrials", "lev"), ("RTX", "Industrials", "lev"),
    # Materials
    ("LIN", "Materials", ""), ("FCX", "Materials", "lev"), ("NEM", "Materials", ""),
    ("DOW", "Materials", "lev"), ("NUE", "Materials", ""),
    # REITs (high leverage by design)
    ("AMT", "REIT", "lev"), ("PLD", "REIT", "lev"), ("O", "REIT", "lev"),
    ("SPG", "REIT", "lev"), ("EQIX", "REIT", "lev"),
    # Communications / telecom
    ("T", "Comm", "lev"), ("VZ", "Comm", "lev"), ("CMCSA", "Comm", "lev"),
    ("TMUS", "Comm", "lev"), ("DIS", "Comm", ""), ("NFLX", "Comm", ""),
    # Utilities (control — known from the XLU run)
    ("NEE", "Utilities", "lev"), ("DUK", "Utilities", "lev"), ("SO", "Utilities", "lev"),
    ("AEP", "Utilities", "lev"),
    # Airlines / autos / cruise — genuinely distress-prone, the veto SHOULD discriminate here
    ("DAL", "Distress-prone", "lev"), ("UAL", "Distress-prone", "lev"), ("AAL", "Distress-prone", "lev"),
    ("CCL", "Distress-prone", "lev"), ("F", "Distress-prone", "lev"), ("GM", "Distress-prone", "lev"),
]

_OUT = Path(__file__).resolve().parent.parent / "data" / "cache" / "altman_blast_radius.json"


def _review(ticker: str) -> dict:
    m = lenses.full(ticker, "name")
    s = m.get("synthesis", {}) or {}
    vetoes = s.get("vetoes") or []
    return {
        "ticker": ticker,
        "size_authority": s.get("size_authority"),
        "confluence": s.get("confluence"),
        "n_scored": s.get("n_scored"),
        "vetoes": vetoes,
        "altman": "altman_distress" in vetoes,
        "blocked": bool(vetoes) or s.get("size_authority") == "blocked",
    }


def main() -> int:
    asof = date.today().isoformat()
    results = []
    print(f"=== ALTMAN BLAST-RADIUS SWEEP — {asof} — {len(UNIVERSE)} names ===")
    for i, (ticker, sector, prior) in enumerate(UNIVERSE, 1):
        try:
            r = _review(ticker)
        except Exception as e:  # one bad name must not kill the batch
            r = {"ticker": ticker, "error": repr(e), "altman": None, "blocked": None,
                 "size_authority": None, "confluence": None, "vetoes": None}
            traceback.print_exc()
        r["sector"] = sector
        r["prior"] = prior
        results.append(r)
        if i % 10 == 0:
            print(f"  ...progress {i}/{len(UNIVERSE)}")

    # --- tallies ---
    by_sector = defaultdict(lambda: {"n": 0, "altman": 0, "blocked": 0})
    safe_misfires, lev_fires, distress_prone_fires = [], [], []
    n_err = 0
    for r in results:
        if r.get("error"):
            n_err += 1
            continue
        b = by_sector[r["sector"]]
        b["n"] += 1
        b["altman"] += 1 if r["altman"] else 0
        b["blocked"] += 1 if r["blocked"] else 0
        if r["altman"]:
            if r["prior"] == "safe":
                safe_misfires.append(r["ticker"])    # net-cash names flagged distressed = clear bug
            elif r["prior"] == "lev":
                lev_fires.append(r["ticker"])
            if r["sector"] == "Distress-prone":
                distress_prone_fires.append(r["ticker"])

    print("\n=== ALTMAN_DISTRESS FIRE RATE BY SECTOR ===")
    print(f"{'sector':16s} {'n':>3} {'altman':>7} {'blocked':>8}  altman%  blocked%")
    tot_n = tot_alt = tot_blk = 0
    for sector in sorted(by_sector):
        b = by_sector[sector]
        tot_n += b["n"]; tot_alt += b["altman"]; tot_blk += b["blocked"]
        ap = 100 * b["altman"] / b["n"] if b["n"] else 0
        bp = 100 * b["blocked"] / b["n"] if b["n"] else 0
        print(f"{sector:16s} {b['n']:>3} {b['altman']:>7} {b['blocked']:>8}  {ap:6.0f}%  {bp:7.0f}%")
    ap = 100 * tot_alt / tot_n if tot_n else 0
    bp = 100 * tot_blk / tot_n if tot_n else 0
    print(f"{'TOTAL':16s} {tot_n:>3} {tot_alt:>7} {tot_blk:>8}  {ap:6.0f}%  {bp:7.0f}%")

    print(f"\nSAFE_MISFIRES (net-cash / IG names flagged altman_distress — clear false positives):")
    print(f"  {safe_misfires}")
    print(f"DISTRESS-PRONE names that fired (the veto SHOULD catch these): {distress_prone_fires}")
    print(f"errors: {n_err}")

    summary = {
        "asof": asof, "n_universe": len(UNIVERSE), "n_errors": n_err,
        "totals": {"n": tot_n, "altman": tot_alt, "blocked": tot_blk,
                   "altman_pct": round(ap, 1), "blocked_pct": round(bp, 1)},
        "by_sector": {k: dict(v) for k, v in by_sector.items()},
        "safe_misfires": safe_misfires,
        "distress_prone_fires": distress_prone_fires,
        "results": results,
    }
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(json.dumps(summary, indent=2, default=str))
    print(f"\nwrote {_OUT}")
    print("=== DONE ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

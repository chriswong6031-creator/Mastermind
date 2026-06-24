"""Engine-only review of the top-10 XLU holdings (no LLM, no armed session).

Runs the deterministic decision matrix (portfolio.lenses.full) for each name and
prints the synthesis read: size authority, confluence, vetoes, divergences, plus a
compact per-lens summary. Paper-only, read-only — nothing is gated or executed.

Run:  python -m scripts.review_xlu_engine
"""
from __future__ import annotations

import json
import traceback
from datetime import date
from pathlib import Path

import bot  # noqa: F401  (bootstraps vendor/macro onto sys.path)

from portfolio import lenses

# Top-10 XLU holdings by weight (SSGA, as of 2026-06-22)
XLU_TOP10 = [
    ("NEE", 12.85), ("SO", 7.54), ("DUK", 6.90), ("CEG", 6.31), ("AEP", 5.08),
    ("SRE", 4.29), ("D", 4.29), ("VST", 3.80), ("ETR", 3.68), ("XEL", 3.52),
]

_OUT = Path(__file__).resolve().parent.parent / "data" / "cache" / "xlu_engine_review.json"


def _review(ticker: str) -> dict:
    m = lenses.full(ticker, "name")
    s = m.get("synthesis", {}) or {}
    blocked = bool(s.get("vetoes")) or s.get("size_authority") == "blocked"
    # compact per-lens read
    lens_rows = {}
    for k, v in (m.get("lenses", {}) or {}).items():
        if isinstance(v, dict):
            lens_rows[k] = {
                "status": v.get("status"),
                "score": v.get("score"),
                "direction": v.get("direction") or v.get("lean"),
            }
    return {
        "ticker": ticker,
        "size_authority": s.get("size_authority"),
        "confluence": s.get("confluence"),
        "n_scored": s.get("n_scored"),
        "score": s.get("score") or s.get("composite") or s.get("combined"),
        "lean": s.get("lean") or s.get("recommend"),
        "vetoes": s.get("vetoes"),
        "divergences": [d.get("kind") if isinstance(d, dict) else d
                        for d in (s.get("divergences") or [])],
        "blocked": blocked,
        "lenses": lens_rows,
    }


def main() -> int:
    asof = date.today().isoformat()
    results = []
    print(f"=== XLU TOP-10 ENGINE REVIEW — {asof} ===")
    for ticker, weight in XLU_TOP10:
        try:
            r = _review(ticker)
            r["xlu_weight"] = weight
            results.append(r)
            print(f"{ticker:5s} w={weight:5.2f}%  auth={str(r['size_authority']):8s} "
                  f"confluence={str(r['confluence']):>5} score={str(r['score']):>6} "
                  f"lean={str(r['lean']):>10} vetoes={r['vetoes']} div={r['divergences']}")
        except Exception as e:  # one bad name must not kill the batch
            results.append({"ticker": ticker, "xlu_weight": weight, "error": repr(e)})
            print(f"{ticker:5s} w={weight:5.2f}%  ERROR: {e!r}")
            traceback.print_exc()
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text(json.dumps({"asof": asof, "results": results}, indent=2, default=str))
    print(f"\nwrote {_OUT}")
    print("=== DONE ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

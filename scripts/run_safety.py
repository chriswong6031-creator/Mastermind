"""Compute + persist the portfolio safety scorecard for every (or one) registered book.

The same routine the daily loop calls — exposed standalone for on-demand recompute.

  python -m scripts.run_safety                 # all portfolios, persist data/.../safety.json
  python -m scripts.run_safety flagship        # just the flagship book, printed verbose
  python -m scripts.run_safety --no-bootstrap  # skip the (heavier) bootstrap CI
"""
from __future__ import annotations

import argparse
import json

import bot  # noqa: F401  -> vendor/macro bootstrap


def main() -> None:
    ap = argparse.ArgumentParser(description="Portfolio safety scorecard")
    ap.add_argument("portfolio", nargs="?", default=None,
                    help="portfolio id (default: all registered)")
    ap.add_argument("--asof", default=None, help="window end date (default: today)")
    ap.add_argument("--no-bootstrap", action="store_true", help="skip the bootstrap CI")
    args = ap.parse_args()

    from portfolio import safety
    bootstrap = not args.no_bootstrap

    if args.portfolio:
        rep = safety.compute_safety(args.portfolio, asof=args.asof, bootstrap=bootstrap)
        safety.persist(rep, args.portfolio)
        print(json.dumps({k: rep.get(k) for k in
                          ("portfolio_id", "source", "n_positions", "cash_weight",
                           "safety_score", "grade", "verdict", "subscores")},
                         indent=2, default=str))
        for b in rep.get("breaches", []):
            print(f"  [{b['severity']}] {b['message']}")
    else:
        out = safety.safety_for_all(asof=args.asof, bootstrap=bootstrap)
        print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()

"""The write-back bridge (bot side): emit the portfolio.v1 contract.

Writes data/portfolio/latest.json (machine hub) + site/portfolio.json (page-facing).
In production a macro-repo build_portfolio.py (cloned from build_transmission.py)
renders site/portfolio.html from this same JSON; here we own the contract.
"""
from __future__ import annotations

import json
from pathlib import Path

import bot  # noqa: F401

_ROOT = Path(__file__).resolve().parent.parent


def write(payload: dict, portfolio_id: str | None = None) -> dict:
    """Write both contracts; always succeed (return paths).

    portfolio_id None/'flagship' → the legacy data/portfolio/ dir; any other id →
    data/portfolios/<id>/ (so every book has its own published contract). The id is
    stamped into the payload so the dashboard knows which book it is reading.
    """
    from portfolio import registry
    pid = portfolio_id or registry.DEFAULT_ID
    payload = {"schema": "portfolio.v1", "is_paper": True, "portfolio_id": pid, **payload,
               "disclaimer": "Paper-only / display-only. Accountability, not alpha. Not investment advice.",
               "zh": {"disclaimer": "仅纸面/仅展示；从不自动执行。"}}
    base = registry.data_dir(portfolio_id)
    hub = base / "latest.json"
    site = base / "portfolio.json"   # site/ in prod; local mirror here
    base.mkdir(parents=True, exist_ok=True)
    for p in (hub, site):
        p.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
    return {"hub": str(hub), "site": str(site)}

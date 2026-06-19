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


def write(payload: dict) -> dict:
    """Write both contracts; always succeed (return paths)."""
    payload = {"schema": "portfolio.v1", "is_paper": True, **payload,
               "disclaimer": "Paper-only / display-only. Accountability, not alpha. Not investment advice.",
               "zh": {"disclaimer": "仅纸面/仅展示；从不自动执行。"}}
    hub = _ROOT / "data" / "portfolio" / "latest.json"
    site = _ROOT / "data" / "portfolio" / "portfolio.json"   # site/ in prod; local mirror here
    hub.parent.mkdir(parents=True, exist_ok=True)
    for p in (hub, site):
        p.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
    return {"hub": str(hub), "site": str(site)}

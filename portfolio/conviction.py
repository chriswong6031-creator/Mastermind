"""Conviction sleeve — a name takes paper size only when ALL sides confirm.

Closes the loop: candidate names (Claude's open proposals + the leadership universe) are
each run through the multi-sided decision matrix; a name is sized ONLY if its synthesis
says size_authority == 'up' AND it trips no hard veto (parabolic / Altman distress /
cycle-blocked). Size is confluence-weighted, subtract-only, capped per name. Everything
else is shown but held at 0 — discipline over enthusiasm.
"""
from __future__ import annotations

import bot  # noqa: F401

from portfolio import lenses

# liquid leadership/AI-complex names that carry a full stockdata lens read
_SHORTLIST = ["AVGO", "NVDA", "AMD", "MU", "GEV", "PLTR", "DELL", "TSM", "AMAT", "MRVL",
              "ORCL", "VST", "BWXT", "ANET", "LRCX", "KLAC", "MSFT", "GOOGL", "META", "AAPL"]


def candidates() -> list[str]:
    """Conviction candidate pool: open ledger theses (Claude's proposals) + the leadership shortlist."""
    try:
        from brain import ledger
        proposed = {t["subject"].upper() for t in ledger.all_theses() if t.get("status") == "open"}
    except Exception:
        proposed = set()
    return sorted(set(_SHORTLIST) | proposed)


def build(budget: float, name_cap: float = 0.08) -> list[dict]:
    """Return sized conviction positions for names where the matrix confirms all sides."""
    passed = []
    for t in candidates():
        try:
            syn = lenses.full(t, "name")["synthesis"]
        except Exception:
            continue
        if syn.get("size_authority") == "up" and not syn.get("vetoes"):
            passed.append({"ticker": t, "confluence": max(0.0, syn["confluence"]),
                           "bull": syn["bull"], "bear": syn["bear"],
                           "divergences": [d["pattern"] for d in syn.get("divergences", [])]})
    # confluence-weighted, normalized to the sleeve budget, capped per name (subtract-only)
    tot = sum(p["confluence"] for p in passed) or 1.0
    for p in passed:
        p["weight"] = round(min(p["confluence"] / tot * budget, name_cap), 4)
        p["sleeve"] = "conviction"
        p["verdict"] = "add"
    return [p for p in passed if p["weight"] > 0]

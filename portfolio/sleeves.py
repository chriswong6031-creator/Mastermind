"""Three-sleeve assembler + cross-sleeve firebreaks (doctrine §2).

Leadership = engine.narrative_rotation.allocate() AS-IS (equal-weight, mechanical,
monthly cadence). Conviction = top_picks + stock_score half-Kelly (capped, falsifiable).
Cash = sized, never residual. The NEW glue is the cross-sleeve BOOK theme cap (0.25)
and the rotation-capacity floor — neither engine enforces these.
"""
from __future__ import annotations

from collections import defaultdict

from bot.doctrine_config import load_doctrine


def enforce_book_caps(positions: list[dict]) -> dict:
    """Apply the firebreaks across BOTH sleeves (subtract-only).

    positions: [{ticker, theme_id, sleeve, weight}]  (weights = fraction of book)
    Returns the capped book + any breaches (which feed detector D6).
    """
    cfg = load_doctrine()
    theme_cap = cfg["caps"]["theme_cap_book"]
    name_cap = cfg["caps"]["name_cap"]

    breaches = []
    # single-name cap (ours, tighter) — subtract-only clamp.
    # Leadership-sleeve holdings are diversified ETFs/baskets, not single-name concentration,
    # so the name cap applies only to discretionary single names (conviction sleeve).
    for p in positions:
        if p.get("sleeve") == "leadership":
            continue
        if p["weight"] > name_cap:
            breaches.append({"code": "D6", "kind": "name", "subject": p["ticker"],
                             "weight": round(p["weight"], 4), "cap": name_cap})
            p["weight"] = name_cap

    # cross-sleeve theme cap (the firebreak A2.1)
    by_theme: dict[str, float] = defaultdict(float)
    for p in positions:
        by_theme[p["theme_id"]] += p["weight"]
    for theme, w in by_theme.items():
        if w > theme_cap:
            breaches.append({"code": "D6", "kind": "theme", "subject": theme,
                             "weight": round(w, 4), "cap": theme_cap})
            scale = theme_cap / w
            for p in positions:
                if p["theme_id"] == theme:
                    p["weight"] *= scale

    return {"positions": positions, "breaches": breaches}


def no_rotation_capacity(cash: float, top_theme_concentration: float) -> bool:
    """Detector D3 — the operator's structural pre-condition for the failure."""
    nrc = load_doctrine()["cash"]["no_rotation_capacity"]
    return cash <= nrc["cash_max"] and top_theme_concentration >= nrc["top_theme_concentration_min"]


def binding_cash(macro_implied: float) -> float:
    """Cash is sized, orthogonal to market risk: max(macro-implied, rotation floor)."""
    floor = load_doctrine()["cash"]["rotation_floor"]
    return max(macro_implied, floor)


def assemble(leadership: dict, conviction: dict, macro_implied_cash: float) -> dict:
    """Combine the sleeves into one book under the budgets + firebreaks. TODO(engine wiring)."""
    raise NotImplementedError(
        "wire leadership=narrative_rotation.allocate(); conviction=top_picks/stock_score; "
        "then enforce_book_caps + binding_cash"
    )

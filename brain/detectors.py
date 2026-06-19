"""Failure-mode detectors D1-D6 (doctrine §7) — two modes off one engine.

SELF   = hard sizing vetoes in the bot's own paper path (compose with the half-Kelly
         subtract-only sizer; mirror stock_desk._reconcile — de-escalate only).
OPERATOR = the same checks read-only over the user's real holdings (WATCHLIST.md);
         emit blunt advisories, never an order. Operator entry is unknown -> the
         time clock degrades to "lagged the leader N weeks", tagged (unverified) (A7).

D3/D5/D6 are implemented (pure logic); D1/D2/D4 need the brain/price context and are
typed seams.
"""
from __future__ import annotations

from datetime import date

from bot.doctrine_config import load_doctrine
from portfolio.sleeves import no_rotation_capacity


def _det(code, mode, subject, payload, severity="flag", unverified=False, lot_id=None):
    return {"code": code, "mode": mode, "subject": subject, "lot_id": lot_id,
            "severity": severity, "unverified": unverified, "payload": payload}


def d3_no_rotation_capacity(cash: float, top_theme_concentration: float, mode: str) -> list[dict]:
    if no_rotation_capacity(cash, top_theme_concentration):
        sev = "veto" if mode == "self" else "flag"
        return [_det("D3", mode, "book",
                     {"cash": round(cash, 3), "top_theme": round(top_theme_concentration, 3),
                      "advisory": "no dry powder to rotate when the next theme confirms"}, sev)]
    return []


def d5_dead_capital(lots: list[dict], asof: date, mode: str) -> list[dict]:
    """Time-stop surface: held past window AND unresolved AND RS lagging the leader."""
    cfg = load_doctrine()["time_stop"]
    out = []
    for lot in lots:
        elapsed_ok = lot.get("time_stop_by") and asof >= lot["time_stop_by"]
        unresolved = lot.get("rel_return_since_entry", 0.0) <= cfg["unresolved_rel_entry_max"]
        rs_gap = lot.get("rs_leader_gap", 0.0) >= cfg["rs_leader_gap_min"]
        if elapsed_ok and unresolved and rs_gap:
            out.append(_det("D5", mode, lot["ticker"],
                            {"time_stop_by": str(lot["time_stop_by"]), "rs_leader_gap": lot["rs_leader_gap"],
                             "advisory": "right-but-early: capital is dead now, redeploy"},
                            severity="veto" if mode == "self" else "flag",
                            unverified=(mode == "operator"), lot_id=lot.get("id")))
    return out


def d6_cap_breach(breaches: list[dict], mode: str) -> list[dict]:
    """Single-theme/name cap breach (>0.25 book / >0.08 name). Fed by sleeves.enforce_book_caps."""
    sev = "veto" if mode == "self" else "flag"
    return [_det("D6", mode, b["subject"],
                 {"kind": b["kind"], "weight": b["weight"], "cap": b["cap"],
                  "advisory": "architecture firebreak breached"}, sev) for b in breaches]


# --- typed seams (need brain narrative / price context) ---
def d1_thesis_defense(*args, **kwargs) -> list[dict]:  # disposition effect
    raise NotImplementedError("brain-message tone shift: 'where is money going' -> defending narrative")


def d2_late_stage_reach(*args, **kwargs) -> list[dict]:  # buying order_layer=4 adjacency
    raise NotImplementedError("flag evaluating a 4th-derivative name as a Stage 3-4 trim tell")


def d4_avg_down_into_divergence(*args, **kwargs) -> list[dict]:  # RULE 6.1
    raise NotImplementedError("adding to a lot diverging from a rotating leader")

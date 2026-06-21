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


# --- D1/D2/D4 — behavioural failure-mode tells (deterministic proxies; ADVISORY like D3/D6) ---
# These mirror the doctrine's behavioural detectors. The pure tells they target (message-tone shift,
# order-layer adjacency, intent-to-average-down) aren't directly observable, so each uses a concrete,
# defensible price/position proxy. Advisory: they flag for the operator, they do not size.

def d1_disposition(lots: list[dict], mode: str) -> list[dict]:
    """D1 disposition effect (proxy): a HELD loser carried well past patience — still underwater vs
    entry AND held beyond HALF its time-stop window — is the tell that the thesis is being DEFENDED
    rather than re-underwritten. (D5 is the harder exit at the full window + RS lag; D1 is the earlier
    behavioural warning.)"""
    out = []
    for lot in lots:
        rel = lot.get("rel_return_since_entry")
        held = lot.get("held_days") or 0
        win = lot.get("time_stop_td") or 63
        if rel is not None and rel < 0 and held >= win / 2.0:
            out.append(_det("D1", mode, lot["ticker"],
                            {"held_days": held, "rel_return": round(rel, 4),
                             "advisory": "held loser past patience — disposition-effect risk; re-underwrite or cut"},
                            severity="flag", unverified=(mode == "operator"), lot_id=lot.get("id")))
    return out


def d2_late_stage_reach(new_buys: list[dict], mode: str) -> list[dict]:
    """D2 late-stage reach (proxy): a NEW buy that is already EXTENDED (extension lens bear /
    parabolic / far above its 200dma) is reaching late in the move — a Stage 3-4 trim tell, not an
    entry. (The hard parabolic veto already blocks the worst; this flags the merely-extended new adds
    that still cleared the gate.)"""
    out = []
    for b in new_buys:
        if b.get("extension_bear") or b.get("parabolic"):
            out.append(_det("D2", mode, b["ticker"],
                            {"parabolic": bool(b.get("parabolic")),
                             "advisory": "new buy into an already-extended name — late-stage reach"},
                            severity="flag", unverified=(mode == "operator"), lot_id=b.get("id")))
    return out


def d4_avg_down_into_divergence(lots: list[dict], mode: str) -> list[dict]:
    """D4 avg-down into divergence (proxy, RULE 6.1): ADDING to a lot (weight up vs last run) that is
    BOTH underwater since entry AND lagging the rotating leader (RS gap) — throwing good money after a
    name the leadership has left behind."""
    out = []
    for lot in lots:
        rel = lot.get("rel_return_since_entry")
        if (lot.get("is_add") and rel is not None and rel < 0
                and (lot.get("rs_leader_gap") or 0) >= 0.10):
            out.append(_det("D4", mode, lot["ticker"],
                            {"rel_return": round(rel, 4), "rs_leader_gap": lot.get("rs_leader_gap"),
                             "advisory": "averaging down into a name diverging from the rotating leader"},
                            severity="flag", unverified=(mode == "operator"), lot_id=lot.get("id")))
    return out

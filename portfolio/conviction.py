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


def build(budget: float, name_cap: float = 0.08) -> tuple[list[dict], list[dict]]:
    """Return (sized_positions, rejected) where rejected contains every evaluated name
    that did NOT make the size gate, with the veto/bear detail that kept it out.

    Sizing behaviour is unchanged — this only adds visibility into the gate's decisions.
    """
    passed = []
    rejected: list[dict] = []

    for t in candidates():
        try:
            full = lenses.full(t, "name")
            syn = full["synthesis"]
            rows: list[dict] = full.get("rows", [])
        except Exception:
            continue

        vetoes: list[str] = syn.get("vetoes", [])
        confluence: float = syn.get("confluence", 0.0)

        if syn.get("size_authority") == "up" and not vetoes:
            passed.append({"ticker": t, "confluence": max(0.0, confluence),
                           "bull": syn["bull"], "bear": syn["bear"],
                           "divergences": [d["pattern"] for d in syn.get("divergences", [])]})
        else:
            # Determine a short human-readable rejection reason.
            if vetoes:
                reason = "Vetoed: " + ", ".join(vetoes)
            elif syn.get("size_authority") == "blocked":
                reason = "Blocked (size_authority=blocked)"
            elif confluence <= -0.3:
                reason = f"Negative confluence ({confluence:+.2f})"
            else:
                reason = f"Insufficient confluence ({confluence:+.2f}, need >0.30)"

            # Extract bear bullets from the matrix rows (cap at 4).
            bear_pts: list[str] = []
            for r in rows:
                if r.get("direction") == "bear" and len(bear_pts) < 4:
                    lens_name = r.get("lens", "")
                    note = r.get("note") or ""
                    val = r.get("value") or {}
                    if lens_name == "extension":
                        pv2 = val.get("pct_vs_200dma")
                        para = val.get("parabolic")
                        bear_pts.append(
                            f"Extension: grade={val.get('grade')}"
                            + (", parabolic=True" if para else "")
                            + (f", +{pv2:.1f}% vs 200dma" if pv2 is not None else "")
                        )
                    elif lens_name == "valuation":
                        vz = val.get("value_z")
                        bear_pts.append(
                            f"Valuation stretched"
                            + (f" (value_z={vz:.2f})" if vz is not None else "")
                        )
                    elif lens_name == "flows_13f":
                        ns = val.get("n_selling")
                        nb = val.get("n_buying")
                        bear_pts.append(
                            f"13F distribution: {ns} funds selling vs {nb} buying"
                            if ns is not None else "13F smart-money net negative"
                        )
                    elif lens_name == "quality":
                        acct = val.get("accounting")
                        bear_pts.append(
                            f"Quality / accounting flag: {acct}"
                            if acct else "Quality lens bearish"
                        )
                    elif lens_name == "solvency":
                        az = val.get("altman_zone")
                        bear_pts.append(
                            f"Solvency: Altman zone={az}" if az else "Solvency concern"
                        )
                    elif lens_name == "macro_risk":
                        score = val.get("score")
                        bear_pts.append(
                            f"Macro risk elevated (score={score:.2f})" if score is not None
                            else "Macro risk elevated"
                        )
                    elif lens_name == "conviction":
                        band = val.get("band")
                        bear_pts.append(
                            f"Engine conviction band={band}" if band else "Engine conviction bearish"
                        )
                    elif note:
                        bear_pts.append(f"{lens_name.replace('_', ' ').title()}: {note}")
                    else:
                        bear_pts.append(f"{lens_name.replace('_', ' ').title()} lens bearish")

            rejected.append({
                "ticker": t,
                "reason": reason,
                "vetoes": vetoes,
                "bear": bear_pts,
                "confluence": round(confluence, 3),
            })

    # confidence-weighted sizing (unchanged)
    tot = sum(p["confluence"] for p in passed) or 1.0
    for p in passed:
        p["weight"] = round(min(p["confluence"] / tot * budget, name_cap), 4)
        p["sleeve"] = "conviction"
        p["verdict"] = "add"

    sized = [p for p in passed if p["weight"] > 0]
    # sort rejected worst-confluence first so the most-bearish names surface at top
    rejected.sort(key=lambda x: x["confluence"])
    return sized, rejected

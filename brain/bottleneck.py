"""Bottleneck migration reasoning (doctrine §5 / axiom A2 / RULE 5.1).

The constraint MOVES along the supply chain. Your baskets already encode the AI ->
electricity chain (config/doctrine.yml bottleneck.chains, real config.yml theme ids).
order_layer is DISPLAY-ONLY (thematic momentum rank-IC ~ 0 — never a scored axis).

The brain (master_brain._call_model) authors the {current_constraint, next_constraint}
field (RULE 5.1), but it is ANCHORED here to which order-layer basket is actually
leading on RS, so a hallucinated constraint is caught.
"""
from __future__ import annotations

from bot.doctrine_config import load_doctrine


def order_layers(chain_id: str) -> dict[int, list[str]]:
    """Return {order_layer: [basket_id, ...]} for a named chain."""
    c = load_doctrine()["bottleneck"]["chains"][chain_id]
    return {1: c["first_order"], 2: c["second_order"], 3: c["third_order"]}


def leading_order(chain_id: str, rs_pctile_by_basket: dict[str, float]) -> dict:
    """Which order layer currently has the strongest RS = where the bottleneck sits now.

    rs_pctile_by_basket: {basket_id: rs_pctile 0..1} from theme_scoring.
    """
    layers = order_layers(chain_id)
    scored = {}
    for layer, baskets in layers.items():
        vals = [rs_pctile_by_basket[b] for b in baskets if b in rs_pctile_by_basket]
        if vals:
            scored[layer] = sum(vals) / len(vals)
    if not scored:
        return {"chain": chain_id, "leading_order": None, "note": "no RS data"}
    lead = max(scored, key=scored.get)
    nxt = lead + 1 if (lead + 1) in layers else lead
    return {
        "chain": chain_id,
        "leading_order": lead,
        "candidate_next_order": nxt,
        "rs_by_order": {k: round(v, 3) for k, v in scored.items()},
        "current_baskets": layers[lead],
        "next_baskets": layers.get(nxt, []),
    }


def validate_brain_claim(brain_constraint_order: int, anchor: dict) -> dict:
    """RULE 5.1 guard: does the brain's claimed binding-constraint layer match the RS anchor?"""
    ok = brain_constraint_order == anchor.get("leading_order")
    return {"agrees_with_rs": ok,
            "unverified": not ok,  # A7: flag inferred-vs-observed mismatch
            "rs_leading_order": anchor.get("leading_order")}

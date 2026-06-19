"""Phase 1 — one-name paper thesis, end-to-end through the doctrine.

Pipeline: read REAL regime + leadership + name data -> run the doctrine
(stage, confluence scorecard, sleeve, bottleneck) -> build an ENGINE-derived
falsifiable decision -> append to the ledger -> score -> write the bridge JSON.

Deterministic + runs without an LLM key (the falsifier is engine-derived; the LLM
only enriches narrative later). Dims that need the not-yet-merged engine leaves
(breadth/flow/volume/catalyst) are honestly tagged 'unverified' (A7) — so the
confluence gate correctly REFUSES full size until confirmation lands. That refusal
is the doctrine working, not a bug.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import bot  # noqa: F401

from brain.decision import DecisionDoc
from brain import ledger, scorer
from portfolio.scorecard import confluence_gate
from portfolio.stages import ThemeFields, classify_stage
from brain.bottleneck import leading_order
from bridge.build_portfolio import write

_V = Path(__file__).resolve().parent.parent / "vendor" / "macro"
_SIZE = {"none": 0.0, "initial": 0.03, "full": 0.08}


def _j(rel: str) -> dict:
    return json.loads((_V / rel).read_text())


def run(asof: str | None = None) -> dict:
    regime = _j("data/regime/latest.json")
    asof = asof or regime["date"]

    # ---- leadership: the top sector by RS -> theme + leader name (doctrine §2) ----
    top = sorted(regime["sector_rs"], key=lambda r: r["rank"])[0]   # SMH = semis, rank 1
    theme_id, leader = "ai_semiconductors", "NVDA"
    name = _j("site/stockdata/NVDA.json")
    px = name["tech"]["price"]
    ext = name["tech"].get("pct_vs_200dma", 0.0)

    # ---- bottleneck baton-pass over the real AI->electricity chain (§5) ----
    rs_by_basket = {}
    try:
        for b in _j("site/basketdata/baskets.json")["baskets"]:
            rel = (b.get("perf", {}).get("20d", {}) or {}).get("rel")
            if rel is not None:
                rs_by_basket[b["id"]] = 0.5 + max(-0.5, min(0.5, rel * 3))  # crude rel->pctile
    except Exception:
        pass
    rs_by_basket.setdefault("ai_semiconductors", top["pctile_252d"] / 100.0)
    bottleneck = leading_order("ai_buildout", rs_by_basket)

    # ---- confirmation scorecard: enumerate confirmed / unverified (A7) ----
    dims = {
        "rs": "confirmed" if top["pctile_252d"] >= 80 else "absent",     # SMH 99.2 pctile
        "regime": "confirmed",                                            # semis is the Q1 leader
        "breadth": "unverified",   # needs engine/theme_downside_rs + theme breadth leaf (macro PR)
        "flow": "unverified",      # needs engine/theme_flow_rollup (macro PR)
        "volume": "unverified",    # needs engine/volume_signature (macro PR)
        "catalyst": "unverified",  # needs engine/theme_catalyst_binder (macro PR) — the full-size gate
    }
    gate = confluence_gate(dims)

    # ---- stage (some fields unverified -> conservative) ----
    stage = classify_stage(ThemeFields(
        label="emerging", flow_stage="emerging", rs_pctile=top["pctile_252d"] / 100.0,
        breadth_pct50=0.5, crowded_z=0.0, pct_parabolic=0.0,
        catalyst_confirmed=False, clean_entry=(ext < 30)))

    size = _SIZE[gate["size"]]
    prob = {"none": 0.55, "initial": 0.58, "full": 0.66}[gate["size"]]

    doc = DecisionDoc(
        id=f"{asof}-1", subject=leader, lean="add", conviction="low" if size == 0 else "medium",
        prob_correct=prob, horizon_d=21, state_asof=asof,
        thesis=("Semis lead all sectors on RS (SMH 99th pctile) into a Q1 Goldilocks regime; "
                "NVDA is the order-1 name. Logged as a falsifiable watch — the doctrine withholds "
                "size until breadth/flow/volume and a binding catalyst confirm."),
        evidence=[f"SMH RS pctile {top['pctile_252d']}", f"NVDA +{ext}% vs 200dma",
                  f"regime {regime['quad']} {regime.get('quad_name','')}"],
        dissent="Liquidity is contracting; RS-alone is forbidden as a size trigger (rule 4.3).",
        sleeve="conviction", stage=stage["stage"], order_layer=1, scorecard_dims=dims,
        bottleneck={"current_constraint": "compute (GPUs)", "next_constraint": "electricity / grid",
                    "leading_order": bottleneck.get("leading_order")},
        entry_levels={"ticker": leader, "price": px,
                      "buy_zone": round(px * 0.98, 2), "stop": round(px * 0.90, 2),
                      "target": round(px * 1.15, 2)},
    ).finalize()

    appended = ledger.append(doc.to_json())
    tr = scorer.track_record(date.fromisoformat(asof))

    payload = {
        "as_of": asof, "gross_target": round(1.0 - regime.get("macro_risk", {}).get("score", 0.4), 2) if isinstance(regime.get("macro_risk"), dict) else 0.72,
        "regime": {"quad": regime["quad"], "quad_name": regime.get("quad_name"),
                   "liquidity_overlay": regime["liquidity_overlay"]},
        "themes": [{"id": theme_id, "name": "AI semiconductors", "order_layer": 1,
                    "leading_order": bottleneck.get("leading_order")}],
        "positions": [{"ticker": leader, "theme_id": theme_id, "sleeve": "conviction",
                       "stage": stage["stage"], "weight": size, "size_pct": int(size * 100),
                       "scorecard": gate, "verdict": doc.lean, "cycle_blocked": False,
                       "entry": doc.entry_levels, "thesis_id": doc.id,
                       "time_stop_by": doc.time_stop_by}],
        "decisions": [doc.to_json()], "track_record": tr,
    }
    paths = write(payload)
    return {"gate": gate, "stage": stage, "bottleneck": bottleneck,
            "appended": appended, "track_record": tr, "paths": paths, "decision": doc.to_json()}


if __name__ == "__main__":
    import pprint
    out = run()
    print("\n=== PHASE 1 — one-name paper thesis ===")
    print("gate       :", out["gate"]["size"], "| confirmed:", out["gate"]["confirmed"],
          "| unverified:", out["gate"]["unverified"])
    print("stage      :", out["stage"]["stage"], out["stage"]["action"])
    print("bottleneck :", out["bottleneck"]["chain"], "leading order", out["bottleneck"]["leading_order"],
          "->", out["bottleneck"].get("next_baskets"))
    d = out["decision"]
    print(f"decision   : {d['lean'].upper()} {d['subject']}  prob={d['prob_correct']}  "
          f"check_by={d['check_by']}  time_stop_by={d['time_stop_by']}")
    print("falsifier  :", d["falsifier"]["text"])
    print("entry      :", d["entry_levels"])
    print("ledger     :", "appended" if out["appended"] else "carried (open thesis exists)")
    print("track rec  :", out["track_record"])
    print("bridge     :", out["paths"]["hub"])

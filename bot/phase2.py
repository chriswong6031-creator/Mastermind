"""Phase 2 — the gated daily loop + a multi-name 3-sleeve paper book.

gate (material-change) -> assemble state -> LLM-optional adjudication -> build the
Leadership sleeve (mechanical, top-RS sectors, trend-gated) + the Conviction sleeve
(single names through the confluence scorecard) -> cross-sleeve firebreaks -> detectors
-> ledger + Brier scorer -> persist (SQLite/Postgres) -> bridge JSON.

The doctrine's whole point shows up here: the book is PRESENT in the leader mechanically
(SMH via the leadership sleeve) without CHASING the single name (NVDA stays a gated watch
in the conviction sleeve until breadth/flow/volume/catalyst confirm).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import bot  # noqa: F401

from brain import gate, panel, ledger, scorer
from brain.decision import DecisionDoc
from portfolio.scorecard import confluence_gate
from portfolio.sleeves import enforce_book_caps, no_rotation_capacity, binding_cash
from brain import detectors
from bridge.build_portfolio import write
from data_layer import store
from bot.doctrine_config import load_doctrine

_V = Path(__file__).resolve().parent.parent / "vendor" / "macro"
_LEADER_NAME = {"SMH": ("NVDA", "ai_semiconductors")}   # sector ETF -> its order-1 single name


def _j(rel):
    return json.loads((_V / rel).read_text())


def run(asof: str | None = None, force: bool = False, research: bool = False) -> dict:
    cfg = load_doctrine()
    regime = _j("data/regime/latest.json")
    asof = asof or regime["date"]
    secrs = sorted(regime["sector_rs"], key=lambda r: r["rank"])
    top_sector = secrs[0]["ticker"]

    con = store.connect()
    sig = gate.state_signature(regime, top_sector)
    decision = gate.should_run(sig, store.last_run(con),
                               interval_days=cfg["scorecard"].get("rs_down_day_lookback_d", 1) and 1,
                               force=force)
    if not decision["run"]:
        store.record_run(con, asof, False, decision["triggers"], sig, datetime.now(timezone.utc).isoformat())
        return {"ran": False, "reason": "carried forward (no material change)", "positions": store.positions(con, asof)}

    # ---- ARMED Claude research (optional): reason over the web/news, propose theses,
    #      gate them into the falsifiable ledger before sizing ----
    research_out = None
    if research:
        from brain import research_desk
        research_out = research_desk.daily_research_and_ingest(asof)

    # ---- LEADERSHIP sleeve: top-RS sectors, trend-gated, equal-weight (mechanical) ----
    lead_budget = sum(cfg["sleeves"]["leadership_target"]) / 2     # midpoint 0.50
    leaders = [s for s in secrs[:6] if s.get("above_200d_trend")][:4]   # the 200d trend gate
    lw = round(lead_budget / max(1, len(leaders)), 4)
    book = [{"ticker": s["ticker"], "theme_id": s["ticker"], "sleeve": "leadership",
             "stage": 2, "weight": lw, "verdict": "hold", "rs_pctile": s["pctile_252d"]}
            for s in leaders]

    # ---- CONVICTION sleeve: single names through the confluence scorecard ----
    conv_candidates = []
    for etf, (name, theme) in _LEADER_NAME.items():
        s = next((x for x in secrs if x["ticker"] == etf), None)
        if not s:
            continue
        dims = {"rs": "confirmed" if s["pctile_252d"] >= 80 else "absent", "regime": "confirmed",
                "breadth": "unverified", "flow": "unverified", "volume": "unverified", "catalyst": "unverified"}
        conv_candidates.append({"ticker": name, "theme_id": theme, "gate": confluence_gate(dims),
                                "dims": dims, "stage": 1})
    adjud = {a["ticker"]: a for a in panel.adjudicate({"quad": regime["quad"]}, conv_candidates)}
    _SIZE = {"none": 0.0, "initial": 0.03, "full": 0.08}

    decisions = []
    for c in conv_candidates:
        a = adjud[c["ticker"]]
        size = _SIZE[c["gate"]["size"]]
        nm = _j(f"site/stockdata/{c['ticker']}.json") if (_V / f"site/stockdata/{c['ticker']}.json").exists() else None
        px = (nm or {}).get("tech", {}).get("price")
        doc = DecisionDoc(
            id=f"{asof}-{c['ticker']}", subject=c["ticker"], lean=("add" if a["lean"] == "add" else "watch"),
            conviction=a["conviction"], prob_correct={"none": 0.55, "initial": 0.58, "full": 0.66}[c["gate"]["size"]],
            horizon_d=21, state_asof=asof, sleeve="conviction", stage=c["stage"], order_layer=1,
            scorecard_dims=c["dims"], thesis=f"{c['ticker']} is the order-1 name in {c['theme_id']}; "
            f"adjudicated '{a['lean']}' — {a['rationale']}.",
            evidence=[f"sector RS pctile {next(x['pctile_252d'] for x in secrs if x['ticker'] in _LEADER_NAME)}"],
            dissent="RS-alone is forbidden as a full-size trigger (rule 4.3).",
            entry_levels={"ticker": c["ticker"], "price": px} if px else {"ticker": c["ticker"]},
        ).finalize()
        decisions.append(doc.to_json())
        ledger.append(doc.to_json())
        store.insert_thesis(con, doc.to_json())
        book.append({"ticker": c["ticker"], "theme_id": c["theme_id"], "sleeve": "conviction",
                     "stage": c["stage"], "weight": size, "verdict": doc.lean,
                     "thesis_id": doc.id, "time_stop_by": doc.time_stop_by,
                     "scorecard": c["gate"]["size"]})

    # ---- cross-sleeve firebreaks + cash ----
    capped = enforce_book_caps(book)
    book = capped["positions"]
    gross = round(sum(p["weight"] for p in book), 4)
    macro_implied_cash = round(max(0.0, 1.0 - gross), 4)
    cash = round(binding_cash(macro_implied_cash), 4)
    top_theme_conc = max((p["weight"] for p in book), default=0.0)

    # ---- detectors (self mode) ----
    fired = detectors.d3_no_rotation_capacity(cash, top_theme_conc, "self") + \
        detectors.d6_cap_breach(capped["breaches"], "self")

    # ---- persist + score + bridge ----
    for p in book:
        store.upsert_position(con, asof, {**p, "size_pct": int(round(p["weight"] * 100)),
                                          "cycle_blocked": 0, "reason": {"sleeve": p["sleeve"]}})
    tr = scorer.track_record(date.fromisoformat(asof))
    store.save_track_record(con, asof, tr)
    store.record_run(con, asof, True, decision["triggers"], sig, datetime.now(timezone.utc).isoformat())

    payload = {
        "as_of": asof, "gross": gross, "cash": cash,
        "regime": {"quad": regime["quad"], "quad_name": regime.get("quad_name"),
                   "liquidity_overlay": regime["liquidity_overlay"]},
        "sleeves": {"leadership": round(sum(p["weight"] for p in book if p["sleeve"] == "leadership"), 4),
                    "conviction": round(sum(p["weight"] for p in book if p["sleeve"] == "conviction"), 4),
                    "cash": cash},
        "positions": book, "decisions": decisions, "detectors": fired, "track_record": tr,
        "llm_used": any(a.get("llm_used") for a in adjud.values()),
    }
    paths = write(payload)
    return {"ran": True, "triggers": decision["triggers"], "book": book, "sleeves": payload["sleeves"],
            "detectors": fired, "track_record": tr, "paths": paths, "llm_used": payload["llm_used"],
            "research": research_out}


if __name__ == "__main__":
    out = run()
    if not out["ran"]:
        print("Gate: carried forward —", out["reason"])
    else:
        print("\n=== PHASE 2 — gated multi-name 3-sleeve book ===")
        print("triggers   :", out["triggers"], "| llm:", out["llm_used"])
        print("sleeves    :", out["sleeves"])
        print("book:")
        for p in sorted(out["book"], key=lambda x: -x["weight"]):
            print(f"  {p['ticker']:6} {p['sleeve']:11} w={p['weight']:.3f}  {p['verdict']}"
                  + (f"  gate={p.get('scorecard')}" if p["sleeve"] == "conviction" else ""))
        print("detectors  :", [f"{d['code']}/{d['mode']}/{d['severity']}" for d in out["detectors"]] or "none")
        print("track rec  :", out["track_record"])
        print("bridge     :", out["paths"]["hub"])

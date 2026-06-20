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
from portfolio import position_log, thesis as thesis_mod
from portfolio import lenses as lenses_mod

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
    book = []
    for s in leaders:
        ldr_px = ((_j(f"site/stockdata/{s['ticker']}.json") or {}).get("tech", {}) or {}).get("price") \
            if (_V / f"site/stockdata/{s['ticker']}.json").exists() else None
        book.append({"ticker": s["ticker"], "theme_id": s["ticker"], "sleeve": "leadership",
                     "stage": 2, "weight": lw, "verdict": "hold", "rs_pctile": s["pctile_252d"],
                     "entry_price": ldr_px})

    # ---- CONVICTION sleeve: only names the multi-sided decision matrix CONFIRMS ----
    # A name takes size only if every side agrees (size_authority='up') and it trips no hard
    # veto (parabolic / Altman distress / cycle-blocked). Candidates = Claude's open proposals
    # + the leadership universe. Each sized holding gets an accountable, falsifiable thesis.
    from portfolio import conviction
    conv_budget = sum(cfg["sleeves"]["conviction_target"]) / 2     # midpoint 0.30
    decisions = []
    # maps thesis_id -> (synth, matrix_rows) for thesis_full composition
    _synth_map: dict[str, tuple[dict, list]] = {}
    for c in conviction.build(conv_budget, name_cap=cfg["caps"]["name_cap"]):
        t = c["ticker"]
        px = ((_j(f"site/stockdata/{t}.json") or {}).get("tech", {}) or {}).get("price") \
            if (_V / f"site/stockdata/{t}.json").exists() else None
        # re-run the full matrix to capture rows for thesis_full prose
        try:
            _matrix = lenses_mod.decision_matrix(t, "name")
            _synth = lenses_mod.synthesize(_matrix)
            _matrix_rows = _matrix["rows"]
        except Exception:
            _synth = {}
            _matrix_rows = []
        doc = DecisionDoc(
            id=f"{asof}-{t}-conv", subject=t, lean="add", conviction="medium",
            prob_correct=round(0.55 + min(0.15, c["confluence"] * 0.4), 2),
            horizon_d=21, state_asof=asof, sleeve="conviction", order_layer=1,
            thesis=f"Multi-sided confluence {c['confluence']:+.2f} (bull {c['bull']}/bear {c['bear']}); "
                   f"all sides confirm and no hard veto — sized {round(c['weight']*100)}%.",
            evidence=[f"size_authority=up", f"confluence={c['confluence']:+.2f}"]
                     + ([f"divergence:{d}" for d in c["divergences"]] if c["divergences"] else []),
            dissent="Held at 0 if any side vetoes (parabolic/distress/cycle-blocked).",
            entry_levels={"ticker": t, "price": px} if px else {"ticker": t},
        ).finalize()
        _synth_map[doc.id] = (_synth, _matrix_rows)
        decisions.append(doc.to_json())
        ledger.append(doc.to_json())
        store.insert_thesis(con, doc.to_json())
        book.append({"ticker": t, "theme_id": "conviction", "sleeve": "conviction", "stage": 2,
                     "weight": c["weight"], "verdict": "add", "thesis_id": doc.id,
                     "time_stop_by": doc.time_stop_by, "confluence": c["confluence"],
                     "entry_price": px})

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

    # ---- update positions ledger (timestamps, history) ----
    position_log.update(book, asof)

    # ---- enrich each position with opened_at / held_days / entry_price / thesis_full ----
    decisions_by_id = {d["id"]: d for d in decisions}
    for p in book:
        ledger_info = position_log.get_entry_info(p.get("sleeve", ""), p["ticker"])
        p["opened_at"] = ledger_info.get("opened_at")
        p["held_days"] = ledger_info.get("held_days")
        if p.get("entry_price") is None:
            p["entry_price"] = ledger_info.get("entry_price")

        # compose thesis_full
        sleeve = p.get("sleeve", "leadership")
        if sleeve == "conviction":
            thesis_id = p.get("thesis_id", "")
            dec_doc = decisions_by_id.get(thesis_id)
            _synth, _matrix_rows = _synth_map.get(thesis_id, ({}, []))
            facts_for_thesis = {"_matrix_rows": _matrix_rows}
            p["thesis_full"] = thesis_mod.compose(p, dec_doc, _synth, facts_for_thesis)
        else:
            p["thesis_full"] = thesis_mod.compose(p, None, None, None)

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
        "llm_used": False,   # conviction sizing is deterministic (decision matrix); the armed LLM layer runs in the daily loop
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
                  + (f"  confluence={p.get('confluence'):+.2f} (all sides confirm)" if p["sleeve"] == "conviction" else ""))
        print("detectors  :", [f"{d['code']}/{d['mode']}/{d['severity']}" for d in out["detectors"]] or "none")
        print("track rec  :", out["track_record"])
        print("bridge     :", out["paths"]["hub"])

"""In-process MCP server — the bot's data + action surface for Claude.

This is how the web app ARMS Claude and lets Claude COMMUNICATE BACK. The tools run
in THIS Python process (via the Agent SDK's in-process MCP), so Claude never touches
the filesystem directly — it calls typed tools we control.

READ tools  → give Claude the live dashboard + bot state to reason over.
ACTION tools → let Claude write conclusions back to the app's RESEARCH/PROPOSAL surface
               (research notes, proposed theses, emerging-theme flags, recommendations).

Accountability spine: action tools write to a REVIEW QUEUE (jsonl/markdown the web app
renders), never to the execution path. The engine still derives sizing + the falsifier;
nothing here auto-executes a trade. Paper-only.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

import bot  # noqa: F401
from claude_agent_sdk import tool, create_sdk_mcp_server

_ROOT = Path(__file__).resolve().parent.parent
_V = _ROOT / "vendor" / "macro"
_RESEARCH = _ROOT / "data" / "research"
_PROPOSALS = _ROOT / "data" / "brain" / "proposals.jsonl"
_READ_ROOTS = [_V / "site", _V / "data", _ROOT / "data"]


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _json(obj) -> dict:
    return _ok(json.dumps(obj, default=str, ensure_ascii=False)[:8000])


def _read_json(p: Path):
    return json.loads(p.read_text()) if p.exists() else None


def _pick(d, keys):
    """Shallow projection — {k: d[k]} for the keys present (drops noise/bulk)."""
    d = d or {}
    return {k: d.get(k) for k in keys if k in d}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(p: Path, row: dict):
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps({**row, "logged_at": _now()}, default=str) + "\n")


# Marker the streaming layer scans tool results for, to surface a research paper as a
# clickable button in the chat (followed by a JSON blob with the paper meta).
PAPER_MARKER = "__PAPER__"


def _today() -> str:
    d = _read_json(_V / "data" / "regime" / "latest.json") or {}
    return d.get("date") or datetime.now(timezone.utc).date().isoformat()


def _stock_price(t: str):
    d = _read_json(_V / "site" / "stockdata" / f"{t.upper()}.json") or {}
    return (d.get("tech") or {}).get("price")


# ---------------- READ tools ----------------
@tool("get_regime", "Current US macro regime read (quad, growth/inflation scores, liquidity).", {})
async def get_regime(args):
    d = _read_json(_V / "data" / "regime" / "latest.json") or {}
    keys = ["date", "quad", "quad_name", "growth_score", "inflation_score", "liquidity_overlay", "cycle_tag"]
    return _json({k: d.get(k) for k in keys} | {"sector_rs_top": (d.get("sector_rs") or [])[:6]})


@tool("get_themes", "Narrative baskets with recent relative performance (the theme universe).",
      {"type": "object", "properties": {"region": {"type": "string"}}})
async def get_themes(args):
    d = _read_json(_V / "site" / "basketdata" / "baskets.json") or {}
    out = [{"id": b["id"], "name": b.get("name"), "category": b.get("category"),
            "perf_20d_rel": ((b.get("perf") or {}).get("20d") or {}).get("rel"),
            "n_members": b.get("n_members")} for b in (d.get("baskets") or [])]
    return _json({"as_of": d.get("as_of"), "themes": out})


@tool("get_standouts", "The ranked single-name buy board (top picks with suggested size).", {})
async def get_standouts(args):
    d = _read_json(_V / "site" / "factordata" / "us_standouts.json")
    if not d:
        return _ok("us_standouts.json not built locally (ships in the Pages artifact).")
    buys = (d.get("buy") or d.get("standouts") or [])[:20]
    return _json({"gate_go": d.get("gate_go"), "rank_by": d.get("rank_by"), "buy": buys})


@tool("get_portfolio", "The bot's current paper book + track record.", {})
async def get_portfolio(args):
    return _json(_read_json(_ROOT / "data" / "portfolio" / "latest.json") or {"status": "no book yet"})


@tool("get_quiver_strategy", "A competitor Quiver AI strategy's latest scraped snapshot — holdings, recent trades/rebalances, metrics, and the AI's per-pick justification. For learning their process.",
      {"type": "object", "properties": {"name": {"type": "string", "enum": ["chatgpt_enhanced", "claude_enhanced", "chatgpt_standard", "claude_standard"]}}, "required": ["name"]})
async def get_quiver_strategy(args):
    from data_layer import quiver
    snap = quiver.latest_snapshot(args["name"])
    if not snap:
        return _ok("no snapshot yet — run scripts/quiver_pull.py")
    return _json({k: snap[k] for k in ["strategy", "model", "scraped", "metrics", "holdings",
                                       "recently_opened", "recently_closed", "recently_rebalanced", "top_picks"]})


@tool("get_quiver_compare", "Compare all four competitor Quiver AI strategies (ChatGPT/Claude x Enhanced/Standard) — model, metrics, holdings, turnover — to infer their process and where we can beat them.", {})
async def get_quiver_compare(args):
    from data_layer import quiver
    out = {}
    for k in quiver.STRATEGIES:
        s = quiver.latest_snapshot(k)
        if s:
            out[k] = {"model": s["model"], "metrics": s["metrics"],
                      "holdings": [(h["ticker"], h["pct_nav"]) for h in s["holdings"]],
                      "turnover": {"opened": len(s["recently_opened"]), "closed": len(s["recently_closed"]),
                                   "rebalanced": len(s["recently_rebalanced"])}}
    return _json(out)


@tool("get_decision_matrix", "The MULTI-SIDED decision matrix for a name or theme — every lens (valuation, quality, growth, narrative, leadership, asymmetry, risk, policy/admin tilt, Fed, institutional flows, options, rate sensitivity, cross-asset, conviction) with its read + honest status, plus the confluence/divergence synthesis. ALWAYS call this before any verdict.",
      {"type": "object", "properties": {"subject": {"type": "string"}, "kind": {"type": "string", "enum": ["name", "theme"]}}, "required": ["subject"]})
async def get_decision_matrix(args):
    from portfolio import lenses
    return _json(lenses.full(args["subject"], args.get("kind", "name")))


@tool("get_divergences", "Just the divergence patterns for a subject — where the lenses DISAGREE (the edge or the trap): distribution, early_edge, high_confluence_buy, crowded_top, policy_early.",
      {"type": "object", "properties": {"subject": {"type": "string"}, "kind": {"type": "string", "enum": ["name", "theme"]}}, "required": ["subject"]})
async def get_divergences(args):
    from portfolio import lenses
    s = lenses.synthesize(lenses.decision_matrix(args["subject"], args.get("kind", "name")))
    return _json({"divergences": s["divergences"], "confluence": s["confluence"], "vetoes": s["vetoes"]})


@tool("get_altdata", "Alternative-data flow + Trump-family/administration linkage for a NAME — cross-signal convergence across congress/insider/government-contract/lobbying/Trump-trade channels, plus whether the name sits in the latent-stake entity graph (e.g. American Bitcoin -> Hut 8, branded crypto but the value accrues to an AI-power-infra parent). A QUALITATIVE research signal: politically-linked smart-money flow. Public-record + CONTEXT-ONLY (never a scored axis) — informs narrative/conviction, never sizes alone.",
      {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]})
async def get_altdata(args):
    t = (args.get("ticker") or "").upper()
    bt = (_read_json(_V / "site" / "altdata" / "by_ticker.json")
          or _read_json(_V / "data" / "altdata" / "by_ticker.json") or {})
    rec = (bt.get("tickers") or {}).get(t)
    latent = _read_json(_V / "site" / "altdata" / "latent.json") or {}
    graph = None
    for w in (latent.get("watch") or []):
        if (w.get("ticker") or "").upper() == t:
            graph = {"in_graph": True, "themes": [th.get("en") for th in (w.get("themes") or [])],
                     "trump_people": w.get("trump_people"), "top_holder": (w.get("top_holder") or {}).get("owner"),
                     "alt_corroborated": w.get("alt_corroborated"), "note": w.get("note")}
            break
    mismatch = next((m for m in (latent.get("mismatches") or [])
                     if t in {(m.get("repointed_ticker") or "").upper(), (m.get("entity_ticker") or "").upper()}), None)
    if rec is None and graph is None and mismatch is None:
        return _ok(f"no alt-data signal for {t} — not flagged by any political/insider/contract channel.")
    return _json({"ticker": t, "flow": rec, "latent_graph": graph, "label_mismatch": mismatch,
                  "note": "Public-record alt-data (Quiver + SEC EDGAR). Context-only — informs narrative, never sizes alone."})


@tool("get_news",
      "Recent financial-news flow for a NAME — count of recent reputable headlines, a context-only "
      "sentiment lean aggregated from per-article tags, the baskets/sectors it touches, and the top "
      "headlines (title/source/url/summary). A QUALITATIVE research signal: what the tape is SAYING "
      "about this name. CONTEXT-ONLY (never a scored axis) — informs narrative/conviction, never sizes alone.",
      {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]})
async def get_news(args):
    t = (args.get("ticker") or "").upper()
    bt = (_read_json(_V / "site" / "news" / "by_ticker.json")
          or _read_json(_V / "data" / "news" / "by_ticker.json") or {})
    rec = (bt.get("tickers") or {}).get(t)
    if rec is None:
        return _ok(f"no news-flow signal for {t} — not covered by the macro news surface.")
    return _json({"ticker": t, "news": rec,
                  "note": "Public-record financial news flow. Context-only — informs narrative, never sizes alone."})


@tool("get_intelligence",
      "The UNIFIED News & Intelligence read for a NAME — both sides of the tape in ONE call: "
      "(1) news_flow (demand-side: what the market is SAYING — recent headline count, sentiment lean, "
      "top headlines) and (2) the alt-data signal (supply-side: what political/insider/contract/"
      "affiliation money is DOING — signal_score 0-100, action, conviction, channels, affiliations). "
      "Shown SIDE BY SIDE, never blended — the divergence between them is the edge (early = flow into a "
      "QUIET tape; crowded/late = flow into a LOUD tape). CONTEXT-ONLY — informs conviction, never sizes alone.",
      {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]})
async def get_intelligence(args):
    t = (args.get("ticker") or "").upper()
    uni = (_read_json(_V / "site" / "intelligence" / "by_ticker.json")
           or _read_json(_V / "data" / "intelligence" / "by_ticker.json") or {})
    rec = (uni.get("tickers") or {}).get(t)
    if rec is None:
        # the unified bundle isn't published yet — compose from the standalone feeds
        news = ((_read_json(_V / "site" / "news" / "by_ticker.json") or {}).get("tickers") or {}).get(t)
        mm = (_read_json(_V / "site" / "altdata" / "mastermind.json") or {}).get("signals") or []
        alt = next((s for s in mm if (s.get("ticker") or "").upper() == t), None)
        if news is None and alt is None:
            return _ok(f"no news or alt-data intelligence for {t}.")
        rec = {"ticker": t, "news": news, "alt": alt, "has_news": bool(news), "has_alt": bool(alt)}
    return _json({"ticker": t, **rec,
                  "note": "News flow (what the tape says) + alt-data signal (what smart money does), side "
                          "by side. Context-only — the divergence between them is the read; never sizes alone."})


@tool("get_quote",
      "Live (15-min delayed) market price(s) via Polygon for one or more tickers — the Brain's "
      "real-time price read: mark held positions, sanity-check an entry level, or confirm a move "
      "is real before forming a thesis. Returns {TICKER: price}. Prices are delayed and for "
      "reference/marks only — never a trade trigger on their own.",
      {"type": "object", "properties": {"tickers": {"type": "array", "items": {"type": "string"}}},
       "required": ["tickers"]})
async def get_quote(args):
    from data_layer import polygon
    tks = [str(t) for t in (args.get("tickers") or []) if t]
    px = polygon.quotes(tks)
    if not any(v is not None for v in px.values()):
        return _ok("no live quotes available — Polygon layer offline or unkeyed (set POLYGON_API_KEY).")
    return _json({"quotes": px,
                  "note": "Live 15-min delayed prices (Polygon). For marks/entry checks — not a signal."})


@tool("get_daily_briefing",
      "START HERE each session. The dashboard's RANKED daily briefing for the brain — the triaged "
      "worklist so you don't scan every name cold. Returns: macro_context (regime/quad, cycle, "
      "liquidity, Fed stance, plain-English posture, next catalysts) to frame the day; priority_queue "
      "(top names by transmission priority = facet-agreement × signal-strength, each with the "
      "FACTS-derived situation, lean, evidence, source mix, and falsifier); and divergences (the subset "
      "where the tape and smart-money DISAGREE — spend your depth budget here). Context-only — a triage, "
      "never a position size.",
      {"type": "object", "properties": {"top": {"type": "integer"}}})
async def get_daily_briefing(args):
    top = int(args.get("top") or 20)
    b = (_read_json(_V / "site" / "intelligence" / "briefing.json")
         or _read_json(_V / "data" / "intelligence" / "briefing.json"))
    if b:
        return _json({"as_of": b.get("as_of"), "macro_context": b.get("macro_context"),
                      "n_actionable": b.get("n_actionable"), "n_divergences": b.get("n_divergences"),
                      "priority_queue": (b.get("priority_queue") or [])[:top],
                      "divergences": b.get("divergences"), "how_to_use": b.get("how_to_use")})
    # not published yet — compose a live briefing from the per-engine intake funnel
    from brain import intake
    q = intake.build(limit=top)
    return _json({"as_of": q.get("as_of"), "macro_context": q.get("macro_context"),
                  "priority_queue": q.get("candidates"),
                  "note": "Composed live from the dashboard signal engines (briefing.json not built yet)."})


@tool("get_intel_hub",
      "The INTELLIGENCE HUB central command — the dashboard's deepest read, fusing ALL FIVE desks "
      "(news flow · alt-data smart-money · divergence radar · factor buy-board · POLICY intent) into "
      "one reasoned dossier per name. Each carries a composite CONVICTION (0-100, rewards independent "
      "cross-desk agreement, docked by an unanswered falsifier), the 5-desk direction matrix, and the "
      "2nd/3rd-order FLAGS that name the setup: stealth_accumulation / early_edge (smart money before "
      "the crowd) / crowded_top (distribution risk) / confirmed_trend / fading / policy_aligned / "
      "policy_conflict / THEME_WIDE (the whole basket is moving — durable) / ISOLATED (name-specific). "
      "Pass a ticker for that name's full dossier; omit it for the ranked command + divergence alerts + "
      "sector heat. The richest single pull; context-only, never sizes.",
      {"type": "object", "properties": {"ticker": {"type": "string"}, "top": {"type": "integer"}}})
async def get_intel_hub(args):
    h = (_read_json(_V / "site" / "intel_hub" / "hub.json")
         or _read_json(_V / "data" / "intel_hub" / "hub.json"))
    if not h:
        return _ok("intel hub not built yet (site/intel_hub/hub.json absent — ships in the daily build).")
    t = (args.get("ticker") or "").upper()
    if t:
        d = next((x for x in (h.get("command") or []) if (x.get("ticker") or "").upper() == t), None)
        if not d:
            return _ok(f"{t} not in the intel-hub command (no cross-desk signal today).")
        return _json({"ticker": t, **d, "macro_context": h.get("macro_context"),
                      "note": "Full 5-desk dossier. The flags name the setup; track the falsifier."})
    top = int(args.get("top") or 15)

    def _slim(d):
        return {k: d.get(k) for k in ("ticker", "name", "composite_conviction", "lean", "n_confirm",
                "n_dissent", "flags", "read", "peers", "sectors", "falsifier") if k in d}
    return _json({"as_of": h.get("as_of"), "macro_context": h.get("macro_context"),
                  "desks": h.get("desks"), "counts": h.get("counts"),
                  "n_actionable": h.get("n_actionable"),
                  "command": [_slim(d) for d in (h.get("command") or [])[:top]],
                  "divergence_alerts": h.get("divergence_alerts"),
                  "sector_heat": (h.get("sector_heat") or [])[:8],
                  "how_to_use": h.get("how_to_use")})


@tool("get_intake_candidates",
      "The unified CANDIDATE QUEUE — every name the dashboard's signal engines flagged today, deduped "
      "and ranked with full PROVENANCE: for each ticker, which engines fired (briefing / divergence / "
      "buy-board / radar / alt-data / news-surge / open-thesis), why, the net lean, confidence, and "
      "falsifier. Corroboration across INDEPENDENT engines lifts a name. This is the intake funnel that "
      "replaces the old static shortlist — use it to choose what to research. Optional salience tiers "
      "split it into ACT (high-score, corroborated) / WATCH / DIVERGENT. Context-only — never sizes.",
      {"type": "object", "properties": {"limit": {"type": "integer"}, "tiers": {"type": "boolean"}}})
async def get_intake_candidates(args):
    from brain import intake
    limit = int(args.get("limit") or 30)
    out = intake.build(limit=limit)
    if args.get("tiers"):
        out["salience"] = intake.salience_tiers(limit)
    return _json(out)


@tool("get_ticker_package",
      "ONE-CALL DEEP DIVE for a name — bundles every per-ticker read so you don't chain five tools: the "
      "unified intelligence (news flow + alt-data signal + radar divergence + factor buy-board, side by "
      "side, with the brain summary), the decision-matrix divergences/confluence/vetoes (where the lenses "
      "agree or disagree), and the intake provenance (which engines flagged it). Call this once you've "
      "picked a name off the briefing/intake queue and want the full picture before a verdict. "
      "Context-only — informs conviction, never sizes alone.",
      {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]})
async def get_ticker_package(args):
    t = (args.get("ticker") or "").upper()
    uni = (_read_json(_V / "site" / "intelligence" / "by_ticker.json")
           or _read_json(_V / "data" / "intelligence" / "by_ticker.json") or {})
    intel = (uni.get("tickers") or {}).get(t)
    pkg = {"ticker": t, "intelligence": intel}
    try:
        from portfolio import lenses
        s = lenses.synthesize(lenses.decision_matrix(t, "name"))
        pkg["lenses"] = {"divergences": s.get("divergences"), "confluence": s.get("confluence"),
                         "vetoes": s.get("vetoes")}
    except Exception as e:  # noqa: BLE001
        pkg["lenses"] = {"error": f"decision matrix unavailable ({e})"}
    try:
        from brain import intake
        prov = next((c for c in intake.queue(60) if c["ticker"] == t), None)
        pkg["intake"] = prov
    except Exception:  # noqa: BLE001
        pkg["intake"] = None
    if intel is None and pkg.get("intake") is None:
        return _ok(f"no per-ticker intelligence for {t} — not flagged by any dashboard engine.")
    pkg["note"] = ("Full per-name picture: intelligence facets + lens divergences + intake provenance. "
                   "The divergence between demand-tape and supply-smart-money is the read; never sizes alone.")
    return _json(pkg)


@tool("get_fundamentals",
      "Valuation + financials + earnings + accounting-quality + the engine's conviction read for a NAME "
      "(from the per-ticker stock file). Use this for a fundamental picture before an add/cut verdict — "
      "P/E + forward tier, margins/growth/ROE/accruals, next earnings + SUE, analyst rating/target, the "
      "Piotroski/accounting verdict, and the composite conviction score/band/size with its cautions.",
      {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]})
async def get_fundamentals(args):
    t = (args.get("ticker") or "").upper()
    d = _read_json(_V / "site" / "stockdata" / f"{t}.json")
    if not d:
        return _ok(f"no stock file for {t} (covered: S&P 1500 + crypto; ships in the Pages artifact).")
    return _json({
        "ticker": t, "name": d.get("name"), "sector": d.get("sector"), "asof": d.get("asof"),
        "valuation": _pick(d.get("valuation"), ["trailing_pe", "forward_pe", "forward_tier", "price_to_book",
                           "price_to_sales", "earnings_yield", "shareholder_yield", "value_z"]),
        "financials": _pick(d.get("financials"), ["gross_margin", "net_margin", "fcf_margin", "rev_growth",
                            "ni_growth", "roe", "roa", "debt_to_assets", "accruals"]),
        "earnings": _pick(d.get("earnings"), ["next_date", "eps_forecast", "sue_z", "summary"]),
        "analyst": _pick(d.get("analyst"), ["rating", "target", "forward_pe", "div_yield"]),
        "accounting_quality": _pick(d.get("accounting_quality"), ["verdict", "headline", "piotroski", "n_caution"]),
        "factors": _pick(d.get("factors"), ["composite", "fundamental_score"]),
        "tech": _pick(d.get("tech"), ["price", "pct_vs_50dma", "pct_vs_200dma", "rsi14", "off_52w_high_pct"]),
        "conviction": _pick(d.get("conviction"), ["score", "band", "verdict", "size", "risk",
                            "cycle_blocked", "cautions"]),
    })


@tool("get_options",
      "Dealer-positioning / options read for a liquid NAME (gamma exposure): the GEX regime (long/short) + "
      "gamma-flip level + distance, magnets, IV30, put/call OI, max pain, call/put walls, the expected daily/"
      "weekly move, and the volatility-hole state (compression -> where price can vacuum to). Use for entry "
      "timing + risk bands, not stock selection.",
      {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]})
async def get_options(args):
    t = (args.get("ticker") or "").upper()
    d = _read_json(_V / "site" / "gex" / f"{t}.json")
    if not d:
        return _ok(f"no options/GEX file for {t} (only the ~liquid options universe is covered).")
    return _json({
        "ticker": t, "asof": (d.get("meta") or {}).get("asof"),
        "summary": _pick(d.get("summary"), ["spot", "regime", "tier", "net_gex_bn", "gamma_flip",
                         "dist_to_flip_pct", "magnet_up", "magnet_down", "iv30", "put_call_oi_ratio",
                         "max_pain", "call_wall", "put_wall"]),
        "expected_move": _pick(d.get("expected_move"), ["daily_pct", "weekly_pct", "front"]),
        "vol_hole": _pick(d.get("vol_hole"), ["state", "bias", "upper", "lower", "to_upper_pct",
                          "to_lower_pct", "compression"]),
    })


@tool("get_anticipation",
      "The forward-looking anticipation index for a watchlist NAME — a directional conviction read with "
      "multi-leg confluence: the index (-1..+1) + band, how many drivers align, how much to TRUST the "
      "direction, the realized-vol cone, and per-horizon (short/medium/long) expected moves + the drivers/"
      "guards/caveats. Context for a timing lean; honest about trust.",
      {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]})
async def get_anticipation(args):
    t = (args.get("ticker") or "").upper()
    d = _read_json(_V / "site" / "anticipationdata" / f"{t}.json")
    if not d:
        return _ok(f"no anticipation file for {t} (only the curated forward-signal watchlist is covered).")
    return _json({
        "ticker": t, "name": d.get("name"), "group": d.get("group"), "as_of": d.get("as_of"),
        **_pick(d, ["anticipation_index", "index_band", "confluence_value", "n_go_legs",
                    "direction_trust", "trust", "vol_cone_ann", "horizons", "drivers", "guards", "caveats"]),
    })


@tool("read_signal", "Read a published signal/data JSON by path (allowlisted to the dashboard + bot data roots).",
      {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
async def read_signal(args):
    p = (Path(args["path"]) if Path(args["path"]).is_absolute() else _ROOT / args["path"]).resolve()
    if not any(str(p).startswith(str(r.resolve())) for r in _READ_ROOTS):
        return _ok("DENIED: path outside the allowed data roots.")
    d = _read_json(p)
    return _json(d) if d is not None else _ok(f"not found: {p}")


# ---------------- ACTION tools (write back to the app's review queue) ----------------
@tool("save_research_note", "Persist a research note for the web app to render (Claude's conclusions/analysis).",
      {"type": "object", "properties": {"title": {"type": "string"}, "body": {"type": "string"},
       "tickers": {"type": "array", "items": {"type": "string"}}}, "required": ["title", "body"]})
async def save_research_note(args):
    slug = re.sub(r"[^a-z0-9]+", "-", args["title"].lower())[:48].strip("-")
    p = _RESEARCH / "notes" / f"{int(time.time())}_{slug}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"# {args['title']}\n\n*tickers: {', '.join(args.get('tickers') or [])} · {_now()}*\n\n{args['body']}\n")
    return _ok(f"saved research note → {p.relative_to(_ROOT)}")


@tool("propose_thesis", "Propose a FALSIFIABLE investment thesis to the review queue (engine derives the falsifier + size; never auto-executed).",
      {"type": "object", "properties": {
          "subject": {"type": "string"}, "lean": {"type": "string", "enum": ["add", "overweight", "avoid", "underweight", "watch"]},
          "conviction": {"type": "string", "enum": ["low", "medium", "high"]}, "horizon_d": {"type": "integer"},
          "thesis": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}},
          "prob_correct": {"type": "number"}},
       "required": ["subject", "lean", "thesis"]})
async def propose_thesis(args):
    _append(_PROPOSALS, {"source": "claude_cli", "status": "proposed", **args})
    return _ok(f"thesis proposed for {args['subject']} ({args['lean']}) → review queue. NOT executed.")


@tool("flag_emerging_theme", "Flag a newly-detected emerging narrative/theme for the web app.",
      {"type": "object", "properties": {"name": {"type": "string"}, "stage": {"type": "string"},
       "tickers": {"type": "array", "items": {"type": "string"}}, "rationale": {"type": "string"}},
       "required": ["name", "rationale"]})
async def flag_emerging_theme(args):
    _append(_RESEARCH / "emerging.jsonl", {"source": "claude_cli", **args})
    return _ok(f"flagged emerging theme '{args['name']}' → web app.")


@tool("recommend_action", "Log a paper recommendation (buy/trim/exit/watch) with rationale for review.",
      {"type": "object", "properties": {"ticker": {"type": "string"},
       "action": {"type": "string", "enum": ["buy", "add", "trim", "exit", "watch"]},
       "rationale": {"type": "string"}}, "required": ["ticker", "action", "rationale"]})
async def recommend_action(args):
    _append(_RESEARCH / "recommendations.jsonl", {"source": "claude_cli", "status": "paper", **args})
    return _ok(f"recommendation logged: {args['action']} {args['ticker']} (paper, for review).")


# ---------------- evaluate -> research-paper -> trade (the user-pushed-name flow) ----------------
@tool("evaluate_gate",
      "PRELIMINARY GATE for a name the user wants you to consider adding. Runs the multi-sided "
      "decision matrix and returns whether it passes the engine's size gate (size_authority 'up' "
      "AND no hard veto), the confluence, the hard vetoes, and the bearish/bullish lenses. Call "
      "this FIRST when the user pushes a ticker: if it does NOT pass, explain why and stop; if it "
      "passes, tell the user it cleared preliminary inspection and that you're now writing the "
      "research paper before deciding.",
      {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]})
async def evaluate_gate(args):
    from portfolio import lenses
    t = (args.get("ticker") or "").upper()
    m = lenses.full(t, "name")
    syn = m.get("synthesis") or {}
    rows = m.get("rows") or []
    vetoes = syn.get("vetoes") or []
    conf = float(syn.get("confluence", 0.0) or 0.0)
    authority = syn.get("size_authority")
    passed = authority == "up" and not vetoes
    if vetoes:
        reason = "hard veto — " + ", ".join(vetoes)
    elif authority == "blocked":
        reason = "blocked (size_authority=blocked)"
    elif conf <= -0.3:
        reason = f"negative confluence ({conf:+.2f})"
    elif authority != "up":
        reason = f"insufficient confluence ({conf:+.2f}; need > 0.30 with leadership + trend confirmation)"
    else:
        reason = f"cleared — confluence {conf:+.2f}, no hard veto"
    bears = [(r.get("lens", "").replace("_", " ") + (f": {r['note']}" if r.get("note") else ""))
             for r in rows if r.get("direction") == "bear"][:5]
    bulls = [r.get("lens", "").replace("_", " ") for r in rows if r.get("direction") == "bull"][:6]
    return _json({"ticker": t, "passed": passed, "confluence": round(conf, 3),
                  "size_authority": authority, "vetoes": vetoes, "reason": reason,
                  "bear_lenses": bears, "bull_lenses": bulls,
                  "divergences": syn.get("divergences")})


@tool("file_research_paper",
      "File the holistic research paper YOU wrote for a name and get the combined Conviction-Index "
      "gate result. Call this AFTER evaluate_gate passes and you've done the deep research (tools + "
      "web) and written the full report in markdown. The tool combines the engine buy-score with "
      "your research score (combined = round(0.5*engine + 0.5*research); CONFIRMED if combined >= 60 "
      "and viability != avoid). It STORES the paper in the Research dashboard and returns paper_id + "
      "confirmed so the chat shows the user a button to open it. Use EXACTLY these report headings: "
      "## Thesis, ## Pros, ## Cons, ## Valuation, ## Fundamentals, ## Revenue streams, ## Competitive "
      "landscape & moat, ## Confirmed catalysts, ## Pending catalysts, ## Potential catalysts, "
      "## Forward earnings (recalculated), ## Bull / base / bear scenarios, ## Second- and third-order "
      "effects, ## Variant perception — what the market is missing, ## What to watch, ## Other factors.",
      {"type": "object", "properties": {
          "ticker": {"type": "string"},
          "report_md": {"type": "string"},
          "research_score": {"type": "integer"},
          "viability": {"type": "string", "enum": ["compelling", "fair", "rich", "avoid"]},
          "recommend": {"type": "boolean"},
          "summary": {"type": "string"},
          "key_risks": {"type": "array", "items": {"type": "string"}},
          "fair_value": {"type": "number"},
          "price_assessment": {"type": "string"},
          "confidence": {"type": "string", "enum": ["low", "medium", "high"]}},
       "required": ["ticker", "report_md", "research_score", "viability", "recommend", "summary"]})
async def file_research_paper(args):
    from portfolio import lenses
    from brain import research_paper as rp
    t = (args.get("ticker") or "").upper()
    syn = (lenses.full(t, "name") or {}).get("synthesis") or {}
    confluence = float(syn.get("confluence", 0.0) or 0.0)
    asof = _today()
    price = None
    try:
        from data_layer import polygon
        price = (polygon.quotes([t]) or {}).get(t)
    except Exception:
        price = None
    price = price if price else _stock_price(t)

    report_md = rp._strip_leading_narration(args["report_md"])
    paper = {
        "schema": rp.SCHEMA, "id": f"{asof}-{t}", "ticker": t, "asof": asof,
        "generated_at": rp._now(), "mode": "advisor", "model": "advisor-chat",
        "price_at_review": price, "research_score": int(args["research_score"]),
        "viability": args["viability"], "recommend": bool(args["recommend"]),
        "confidence": args.get("confidence") or "medium", "fair_value": args.get("fair_value"),
        "price_assessment": args.get("price_assessment") or "", "summary": args["summary"],
        "sections": rp._split_sections(report_md), "key_risks": args.get("key_risks") or [],
        "report_md": report_md,
    }
    rp._attach_gate(paper, confluence)          # -> engine_score, combined, confirmed, gate_reason
    rp.save_paper(paper)
    try:
        rp.write_feed_note(paper)
    except Exception:
        pass
    meta = {"paper_id": paper["id"], "ticker": t, "title": f"{t} — Research Report",
            "combined": paper["combined"], "confirmed": paper["confirmed"],
            "research_score": paper["research_score"], "engine_score": paper["engine_score"],
            "viability": paper["viability"]}
    verdict = ("CONFIRMED (combined >= 60) — you may add it."
               if paper["confirmed"] else f"NOT confirmed ({paper['gate_reason']}) — do not add it.")
    return _ok(f"{PAPER_MARKER} {json.dumps(meta)}\n"
               f"Filed research paper {paper['id']} → Research dashboard. Conviction Index "
               f"{paper['combined']}/100 (engine {paper['engine_score']} + research "
               f"{paper['research_score']}). {verdict}")


@tool("execute_trade",
      "Conduct an ad-hoc PAPER trade in the book — add / trim / exit ONE name. For an ADD you MUST "
      "have already filed a CONFIRMED research paper for the ticker (combined >= 60); the tool "
      "refuses an add otherwise. Trims and exits are always allowed (risk-down). Funds from cash, "
      "never disturbs other positions, never executes a real trade — shows in Trades + live P&L. "
      "After an add, tell the user it's been added and offer the research paper.",
      {"type": "object", "properties": {
          "ticker": {"type": "string"},
          "action": {"type": "string", "enum": ["add", "trim", "exit"]},
          "weight": {"type": "number"},
          "thesis": {"type": "string"}},
       "required": ["ticker", "action"]})
async def execute_trade(args):
    from portfolio import advisor_trade
    from brain import research_paper as rp
    t = (args.get("ticker") or "").upper()
    action = (args.get("action") or "").lower()
    if action in ("add", "buy", "increase"):
        paper = rp.latest_for(t)
        if not paper or not paper.get("confirmed"):
            return _ok(f"REFUSED: no CONFIRMED research paper on file for {t}. Run the flow first — "
                       f"evaluate_gate -> write + file_research_paper -> if confirmed, then add.")
    res = advisor_trade.execute(t, action, weight=args.get("weight"), thesis=args.get("thesis"))
    if not res.get("ok"):
        return _ok(f"trade NOT executed for {t}: {res.get('error', 'unknown')}")
    try:
        _append(_RESEARCH / "recommendations.jsonl",
                {"source": "advisor_chat", "status": "executed_paper", "ticker": t,
                 "action": action, "rationale": args.get("thesis") or "",
                 **{k: res.get(k) for k in ("shares", "price", "value", "weight")}})
    except Exception:
        pass
    return _ok(res["note"] + " (paper trade — shows in Trades + live P&L.)")


_READ = [get_regime, get_themes, get_standouts, get_portfolio, get_decision_matrix, get_divergences,
         get_altdata, get_news, get_intelligence, get_intel_hub, get_daily_briefing, get_intake_candidates,
         get_ticker_package, get_fundamentals, get_options, get_anticipation, get_quote,
         get_quiver_strategy, get_quiver_compare, evaluate_gate, read_signal]
_ACTION = [save_research_note, propose_thesis, flag_emerging_theme, recommend_action,
           file_research_paper, execute_trade]
_ALL = _READ + _ACTION
SERVER_NAME = "bot"

# tool names as Claude sees them: mcp__<server>__<tool>
TOOL_NAMES = [f"mcp__{SERVER_NAME}__{t.name}" for t in _ALL]
WEB_TOOLS = ["WebSearch", "WebFetch"]


def build_server():
    """Return the in-process MCP server config to hand the SDK via mcp_servers={SERVER_NAME: cfg}."""
    return create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=_ALL)


def armed_allowed_tools() -> list[str]:
    """The tool allow-list for an armed research session: bot tools + web + read-only files."""
    return TOOL_NAMES + WEB_TOOLS + ["Read", "Grep", "Glob"]

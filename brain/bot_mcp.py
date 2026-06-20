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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(p: Path, row: dict):
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps({**row, "logged_at": _now()}, default=str) + "\n")


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


_READ = [get_regime, get_themes, get_standouts, get_portfolio, get_decision_matrix, get_divergences,
         get_quiver_strategy, get_quiver_compare, read_signal]
_ACTION = [save_research_note, propose_thesis, flag_emerging_theme, recommend_action]
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

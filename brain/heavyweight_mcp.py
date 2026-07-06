"""The Heavyweight desk's MCP surface — the CONCENTRATED book that presses the firm's best ideas.

Heavyweight is the sibling of the autonomous desk (brain/autonomous_mcp.py), but with two
defining differences:

  1. Its tradable UNIVERSE is the UNION of every published book's latest.json (flagship,
     autonomous, etf) — NOT a Flagship-only mirror. The deterministic builder (``bot/heavyweight.py``)
     DROPS anything the Brain submits that no published book holds. self_directed is EXCLUDED from
     the sourcing union (ruling R1: its book mirrors the defensive yardstick and must not seed HW).
     The rule is enforced in trusted Python, never on the LLM's good behaviour. One-name-per-
     fragility-chain cluster is also enforced (the Brain picks the BEST expression of each theme).
  2. It is given EXPLICIT, read-only visibility into Flagship — ``get_flagship_book`` /
     ``get_flagship_trades`` / ``get_flagship_research`` / ``get_flagship_thinking`` — so the
     Brain can see exactly what Flagship is doing (holdings, blotter, per-name research papers,
     the reasoning trace) and concentrate the firm's highest-conviction, most asymmetric names.

Sizing rails (enforced in Python): each name 5%–50% of NAV, sub-5% nibbles DROPPED, top ~8 names
kept. A name held by ANY published book (flagship, autonomous, or etf) is eligible even if Flagship
does not currently hold it.

Like the autonomous desk, ``submit_book`` only RECORDS the decided book to a per-portfolio file
(``_pending_decision.json``); the trusted layer reads it after the session, enforces the
universe + one-per-cluster + 5–50% sizing rails, and rebalances. The Brain never trades.
Everything is scoped to ``portfolio_id="heavyweight"`` so peer books are only ever READ.

NOTE on visibility vs the autonomous fix: the autonomous Brain had raw Read/Grep/Glob stripped
and read_signal firewalled off the portfolio dirs (it must NOT see other books). Heavyweight is
the deliberate exception — it sees Flagship, but ONLY through these four typed, read-only tools
(it also has no raw Read/Grep/Glob, and read_signal still denies book dirs), so its cross-book
view is intentional and bounded to Flagship.
"""
from __future__ import annotations

import json
from pathlib import Path

import bot  # noqa: F401
from claude_agent_sdk import tool, create_sdk_mcp_server

from brain import bot_mcp

SERVER_NAME = "heavydesk"
PORTFOLIO_ID = "heavyweight"
FLAGSHIP_ID = "flagship"

BOOK_MARKER = "__BOOK__"


def _json_big(obj, cap: int = 24000) -> dict:
    return bot_mcp._ok(json.dumps(obj, default=str, ensure_ascii=False)[:cap])


def submission_path(portfolio_id: str = PORTFOLIO_ID) -> Path:
    from portfolio import registry
    return registry.data_dir(portfolio_id) / "_pending_decision.json"


def clear_submission(portfolio_id: str = PORTFOLIO_ID) -> None:
    try:
        submission_path(portfolio_id).unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass


def read_submission(portfolio_id: str = PORTFOLIO_ID) -> dict | None:
    p = submission_path(portfolio_id)
    try:
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# desk tools (own book + submit)
# ---------------------------------------------------------------------------

@tool("get_my_book",
      "Your CURRENT Heavyweight portfolio: cash, NAV, and every holding with its shares, weight, "
      "average cost, live price, unrealized P&L, and the rationale you last gave it. Call this FIRST "
      "to see what you already hold (and where you are pressing winners) before deciding today.",
      {})
async def get_my_book(args):
    from portfolio import paper_account, registry
    state = paper_account._load_account(PORTFOLIO_ID)
    tickers = list((state.get("positions") or {}).keys())
    prices: dict[str, float] = {}
    for t in tickers + ["SPY"]:
        px = paper_account._current_price(t)
        if px:
            prices[t] = px
    pnl = paper_account.positions_pnl(prices, PORTFOLIO_ID)
    nav = paper_account.nav(prices, PORTFOLIO_ID)
    rationales: dict[str, str] = {}
    latest = registry.data_dir(PORTFOLIO_ID) / "latest.json"
    try:
        if latest.exists():
            for p in (json.loads(latest.read_text()).get("positions") or []):
                rationales[p.get("ticker")] = p.get("rationale") or (p.get("thesis_full") or {}).get("summary")
    except Exception:
        pass
    holdings = []
    for t, rec in pnl.items():
        mv = rec.get("market_value")
        holdings.append({
            "ticker": t,
            "shares": rec.get("shares"),
            "avg_cost": rec.get("avg_cost"),
            "current_price": rec.get("current_price"),
            "market_value": mv,
            "weight": round(mv / nav, 4) if (mv and nav) else None,
            "unrealized_pnl": rec.get("unrealized_pnl"),
            "unrealized_pct": rec.get("unrealized_pct"),
            "rationale": rationales.get(t),
        })
    return _json_big({
        "cash": round(float(state.get("cash") or 0.0), 2),
        "nav": round(nav, 2),
        "starting_nav": state.get("starting_nav"),
        "inception_date": state.get("inception_date"),
        "n_holdings": len(holdings),
        "holdings": holdings,
    })


@tool("submit_book",
      "Submit your FINAL concentrated book for today as a COMPLETE target book — this is how you "
      "trade. The desk rebalances to exactly these weights (a name you OMIT but currently hold is "
      "SOLD). RULES enforced by the desk after you submit: (1) you may ONLY hold names that appear "
      "in the firm's published books (flagship, autonomous, or etf) — anything else is dropped; "
      "(2) AT MOST ONE name per correlated cluster — submit the highest-conviction expression per "
      "theme, others are dropped; (3) each weight is clamped to 5%–50% of NAV and names you size "
      "below 5% are DROPPED (no nibbles); (4) at most ~8 names are kept (the top by weight). "
      "So submit a SHORT, high-conviction list. Weights are fractions of NAV (0–1); the remainder "
      "stays in cash (hold cash when conviction is thin). Provide a one-paragraph conviction "
      "rationale for EVERY holding (required) + an overall summary of how you are concentrating "
      "the firm's best ideas. Call this ONCE, at the end, after your research. "
      "OPTIONAL governance fields (provide when you can — they improve the shadow decision ledger): "
      "falsifiers (list of strings — what would cause you to reverse this book within 5 days), "
      "evidence_planes (list of strings — data sources you relied on), "
      "expected_failure_mode (string — the most likely way this book loses money).",
      {"type": "object", "properties": {
          "holdings": {"type": "array", "items": {"type": "object", "properties": {
              "ticker": {"type": "string"},
              "weight": {"type": "number", "description": "fraction of NAV, 0.05–0.50"},
              "rationale": {"type": "string", "description": "one paragraph: why this is a top, asymmetric bet, now"},
              "conviction": {"type": "string", "enum": ["high", "medium", "low"]}},
              "required": ["ticker", "weight", "rationale"]}},
          "summary": {"type": "string", "description": "how the book presses Flagship's best ideas today"},
          "sold_note": {"type": "string", "description": "optional: what you exited or trimmed and why"},
          "falsifiers": {"type": "array", "items": {"type": "string"},
                         "description": "what would cause you to reverse this book within 5 days"},
          "evidence_planes": {"type": "array", "items": {"type": "string"},
                              "description": "data sources / signal planes you relied on for this decision"},
          "expected_failure_mode": {"type": "string",
                                    "description": "the most likely way this book loses money"}},
       "required": ["holdings", "summary"]})
async def submit_book(args):
    # Record the Brain's RAW decided book (dedup + basic validity only). The authoritative
    # universe + 5–50% sizing rails live in bot/heavyweight._enforce so there is ONE normalizer
    # and the logged weights are the executed ones (no submit-time scaling here).
    holdings = args.get("holdings") or []
    cleaned: list[dict] = []
    gross = 0.0
    seen: set[str] = set()
    for h in holdings:
        t = (h.get("ticker") or "").upper().strip()
        try:
            w = float(h.get("weight") or 0.0)
        except (TypeError, ValueError):
            w = 0.0
        r = (h.get("rationale") or "").strip()
        if not t or t in seen or w <= 0 or not r:
            continue
        seen.add(t)
        cleaned.append({"ticker": t, "weight": w, "rationale": r,
                        "conviction": (h.get("conviction") or "high")})
        gross += w
    payload = {
        "holdings": cleaned,
        "summary": (args.get("summary") or "").strip(),
        "sold_note": (args.get("sold_note") or "").strip(),
        "gross": round(gross, 4),
    }
    p = submission_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
    note = (f"Book submitted: {len(cleaned)} names. The desk will enforce the Flagship-only "
            "universe + the 5–50% sizing rails (sub-5% names dropped, top ~8 kept) and rebalance "
            "the paper account to the surviving weights at the next mark.")
    return bot_mcp._ok(f"{BOOK_MARKER} {json.dumps({'n': len(cleaned), 'gross': round(gross, 4)})}\n{note}")


# ---------------------------------------------------------------------------
# Flagship visibility (READ-ONLY) — the "see exactly what Flagship is doing" substrate
# ---------------------------------------------------------------------------

@tool("get_flagship_book",
      "Flagship's CURRENT book — the universe you choose from. Returns every Flagship holding "
      "(ticker, sleeve, weight, verdict, held_days, a short thesis) plus its pending orders, sleeve "
      "budgets, the macro regime, and Flagship's own track record. These tickers are the ONLY names "
      "you may hold. Use get_flagship_research(ticker) for a name's full report.",
      {})
async def get_flagship_book(args):
    from portfolio import registry
    p = registry.data_dir(FLAGSHIP_ID) / "latest.json"
    if not p.exists():
        return bot_mcp._ok("Flagship has not published a book yet — no universe to choose from.")
    try:
        d = json.loads(p.read_text())
    except Exception as e:
        return bot_mcp._ok(f"could not read Flagship book: {e}")
    positions = []
    for row in (d.get("positions") or []):
        tf = row.get("thesis_full") or {}
        positions.append({
            "ticker": row.get("ticker"), "sleeve": row.get("sleeve"),
            "weight": row.get("weight"), "verdict": row.get("verdict"),
            "held_days": row.get("held_days"), "rs_pctile": row.get("rs_pctile"),
            "stage": row.get("stage"),
            "thesis": (tf.get("summary") or tf.get("why_now") or "")[:400],
        })
    pend = [{"ticker": o.get("ticker"), "weight": o.get("weight"), "side": o.get("side"),
             "status": o.get("status")} for o in (d.get("pending_orders") or [])]
    return _json_big({
        "as_of": d.get("as_of"),
        "universe": sorted({p["ticker"] for p in positions if p.get("ticker")}
                           | {o["ticker"] for o in pend if o.get("ticker")}),
        "n_positions": len(positions),
        "positions": positions,
        "pending_orders": pend,
        "sleeves": d.get("sleeves"),
        "gross": d.get("gross"), "cash": d.get("cash"),
        "regime": d.get("regime"),
        "track_record": d.get("track_record"),
    })


@tool("get_flagship_trades",
      "Flagship's trade history + position timeline: the FIFO blotter (every buy/sell with realized "
      "P&L), the currently-open lots (with held_days / add-trim history), and the pending order queue. "
      "Use this to see how Flagship has been managing each name — what it is adding to vs trimming.",
      {})
async def get_flagship_trades(args):
    from portfolio import trade_history, position_log, paper_account
    try:
        blotter = trade_history.history(None, FLAGSHIP_ID)[:60]
    except Exception:
        blotter = []
    try:
        open_lots = position_log.open_positions(FLAGSHIP_ID)
    except Exception:
        open_lots = []
    try:
        closed = position_log.closed_positions(FLAGSHIP_ID)[:40]
    except Exception:
        closed = []
    try:
        pend = paper_account.load_pending(FLAGSHIP_ID)
    except Exception:
        pend = []
    return _json_big({
        "blotter": blotter, "open_positions": open_lots,
        "closed_positions": closed, "pending_orders": pend,
    })


@tool("get_flagship_research",
      "Flagship's per-name RESEARCH PAPERS — the holistic reports the engine digests before sizing a "
      "name (thesis, viability, research score, fair value, key risks, the report body). Pass a "
      "ticker for that name's full latest paper; omit it for a compact list across all names.",
      {"type": "object", "properties": {"ticker": {"type": "string"}}})
async def get_flagship_research(args):
    from brain import research_paper
    t = (args.get("ticker") or "").upper().strip()
    if t:
        paper = research_paper.latest_for(t)
        if not paper:
            return bot_mcp._ok(f"no research paper on file for {t}.")
        return _json_big({k: paper.get(k) for k in (
            "id", "ticker", "mode", "viability", "research_score", "recommend",
            "fair_value", "price_at_review", "summary", "key_risks", "sections", "report_md")})
    papers = research_paper.load_papers()
    compact = [{"ticker": p.get("ticker"), "viability": p.get("viability"),
                "research_score": p.get("research_score"), "recommend": p.get("recommend"),
                "summary": (p.get("summary") or "")[:240]} for p in papers[:60]]
    return _json_big({"n": len(compact), "papers": compact})


@tool("get_flagship_thinking",
      "Flagship's reasoning TRACE — the step-by-step record of a book build (every reasoning step + "
      "the decisions: what it sized, vetoed, and why). Omit run_id for the list of recent Flagship "
      "book builds (newest first); pass a run_id for that build's steps.",
      {"type": "object", "properties": {"run_id": {"type": "string"}}})
async def get_flagship_thinking(args):
    from brain import runlog
    rid = (args.get("run_id") or "").strip()
    if not rid:
        runs = [r for r in runlog.list_runs() if r.get("kind") == "book"][:20]
        return _json_big({"n": len(runs), "runs": [
            {k: r.get(k) for k in ("run_id", "ts", "title", "summary")} for r in runs]})
    run = runlog.read_run(rid)
    steps = run.get("steps") or []
    # keep the decision/trade steps + the most recent reasoning so the trace fits the cap
    decisions = [s for s in steps if s.get("type") in ("decision", "trade")]
    return _json_big({
        "run_id": run.get("run_id"), "ts": run.get("ts"), "kind": run.get("kind"),
        "n_steps": len(steps), "decisions": decisions[:120], "steps_tail": steps[-40:],
    })


_DESK_TOOLS = [get_my_book, submit_book,
               get_flagship_book, get_flagship_trades, get_flagship_research, get_flagship_thinking]

# Reuse the macro-dashboard READ tools, minus get_portfolio (Heavyweight reads Flagship through the
# dedicated get_flagship_* tools, and its OWN book via get_my_book).
_READ_TOOLS = [t for t in bot_mcp._READ if t.name != "get_portfolio"]


def build_servers() -> dict:
    """The mcp_servers map for the SDK: the macro READ tools (bot) + this heavyweight desk."""
    return {
        bot_mcp.SERVER_NAME: create_sdk_mcp_server(name=bot_mcp.SERVER_NAME, version="0.1.0", tools=_READ_TOOLS),
        SERVER_NAME: create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=_DESK_TOOLS),
    }


def allowed_tools() -> list[str]:
    """Macro READ tools + the heavydesk tools (own book + submit + the 4 Flagship-visibility
    readers) + web. NO raw Read/Grep/Glob: Heavyweight's cross-book view is intentional but bounded
    to Flagship via the typed get_flagship_* tools (read_signal still denies portfolio book dirs),
    so it can never wander into the autonomous / self-directed books."""
    read = [f"mcp__{bot_mcp.SERVER_NAME}__{t.name}" for t in _READ_TOOLS]
    desk = [f"mcp__{SERVER_NAME}__{t.name}" for t in _DESK_TOOLS]
    return read + desk + bot_mcp.WEB_TOOLS

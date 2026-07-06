"""The ETF desk's MCP surface — the FREE-FORM, ETF-only book the Opus Brain manages itself.

Sibling of ``brain/autonomous_mcp.py``, but the universe is US-listed ETFs ONLY (index / sector /
factor / momentum / duration / cash — ``portfolio.etf_universe``), and the Brain gets a purpose-built
rotation board (``get_etf_board``) that pre-digests the macro dashboard's regime / sector-RS / risk
signals so it doesn't scan 199 engine modules cold. The Brain researches via the macro READ tools +
the board + the web, then calls ONE tool — ``submit_book`` — with its complete target portfolio,
each holding carrying a one-paragraph rationale.

The tool RECORDS the decided book to the etf portfolio's ``_pending_decision.json``; the
deterministic builder (``bot/etf.py``) reads it after the session, RE-ENFORCES the ETF-only
allowlist + the risk guardrails, and rebalances the paper account. All trading logic stays in the
trusted Python layer, bound to the etf book only.

Safety (mirrors the autonomous desk): only the typed ``mcp__bot__*`` / ``mcp__desk__*`` read tools +
web — NO raw Read/Grep/Glob, NO flagship ``get_portfolio``. The Brain reads its OWN book via
``get_my_book`` and cannot reach another book's state. ``submit_book`` drops any non-ETF / off-list
ticker so a single name can never enter this book.
"""
from __future__ import annotations

import json

import bot  # noqa: F401  -> vendor/macro onto sys.path
from claude_agent_sdk import tool, create_sdk_mcp_server

from brain import autonomous_mcp, bot_mcp

SERVER_NAME = "desk"          # same desk name as the autonomous book → persona uses mcp__desk__*
PORTFOLIO_ID = "etf"
BENCHMARK = "SPY"

# Marker the builder / streaming layer scans a tool result for (shared with the autonomous desk).
BOOK_MARKER = autonomous_mcp.BOOK_MARKER


# ---------------------------------------------------------------------------
# submission helpers — reuse the autonomous desk's per-portfolio file machinery
# ---------------------------------------------------------------------------

def submission_path():
    return autonomous_mcp.submission_path(PORTFOLIO_ID)


def clear_submission():
    autonomous_mcp.clear_submission(PORTFOLIO_ID)


def read_submission():
    return autonomous_mcp.read_submission(PORTFOLIO_ID)


# ---------------------------------------------------------------------------
# book tools
# ---------------------------------------------------------------------------

@tool("get_my_book",
      "Your CURRENT ETF portfolio: cash, NAV (USD), and every holding with its shares, weight, "
      "average cost, live price, unrealized P&L, and the rationale you last gave it. The book holds "
      "US-listed ETFs ONLY. Call this FIRST to see exactly what you already hold before deciding "
      "today's trades.",
      {})
async def get_my_book(args):
    from portfolio import etf_universe, paper_account, registry
    state = paper_account._load_account(PORTFOLIO_ID)
    tickers = list((state.get("positions") or {}).keys())
    etf_universe.warm(set(tickers) | {BENCHMARK})        # one batched Yahoo request
    prices: dict[str, float] = {}
    for t in tickers + [BENCHMARK]:
        px = etf_universe.price(t)
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
            "ticker": t, "group": etf_universe.group_of(t),
            "shares": rec.get("shares"), "avg_cost": rec.get("avg_cost"),
            "current_price": rec.get("current_price"), "market_value": mv,
            "weight": round(mv / nav, 4) if (mv and nav) else None,
            "unrealized_pnl": rec.get("unrealized_pnl"),
            "unrealized_pct": rec.get("unrealized_pct"),
            "rationale": rationales.get(t),
        })
    pending = paper_account.load_pending(PORTFOLIO_ID)
    return bot_mcp._json({
        "cash": round(float(state.get("cash") or 0.0), 2),
        "nav": round(nav, 2), "currency": "USD", "benchmark": BENCHMARK,
        "starting_nav": state.get("starting_nav"),
        "inception_date": state.get("inception_date"),
        "n_holdings": len(holdings), "holdings": holdings,
        "pending_orders": pending,
    })


@tool("get_etf_board",
      "START HERE. The ETF ROTATION BOARD — the macro dashboard's signals pre-digested for you in "
      "one call: the regime frame (quad, cycle, liquidity), the ranked sector/factor RELATIVE-"
      "STRENGTH table (sector_rs: the tape's leaders/laggards with 20d/60d momentum + 200d trend), "
      "a distilled risk_state (calm / elevated / stressed — drives your duration & cash), the "
      "rate→equity transmission for sizing duration, cross-asset fragility, and a per-ETF TREND "
      "state (above/below 50d & 200d) across your whole universe. Use it to anchor every allocation "
      "before you reach for the web.",
      {})
async def get_etf_board(args):
    from brain import etf_board
    return bot_mcp._json(etf_board.build_board())


@tool("get_quote",
      "Confirm an ETF is PRICEABLE and see its USD mark before you rely on it. Returns the universe "
      "group (core_index / sectors / factors / duration / cash / diversifier), the USD price the "
      "book will transact at, and priceable=false for anything we cannot mark (it will be SKIPPED) "
      "or any ticker OUTSIDE the ETF allowlist (single names and off-list ETFs are rejected by this "
      "book — in_universe=false).",
      {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]})
async def get_quote(args):
    from portfolio import etf_universe
    t = (args.get("ticker") or "").upper().strip()
    if not t:
        return bot_mcp._json({"error": "no ticker"})
    in_uni = etf_universe.is_etf(t)
    px = etf_universe.price(t) if in_uni else None
    return bot_mcp._json({
        "ticker": t, "in_universe": in_uni, "group": etf_universe.group_of(t),
        "price": round(px, 4) if px else None,
        "priceable": bool(in_uni and px and px > 0),
        "note": None if in_uni else "Off-list: this book trades US-listed ETFs only; submission rejected.",
    })


@tool("submit_book",
      "Submit your FINAL decided ETF portfolio for today as a COMPLETE target book — this is how you "
      "trade. The desk rebalances the paper account to exactly these weights: a name you include is "
      "bought/held to its weight, a name you OMIT (that you currently hold) is SOLD in full, a changed "
      "weight is trimmed/added. So include EVERY ETF you want to keep. Weights are fractions of NAV "
      "(0-1) and must sum to <= 1.0 (the remainder stays in cash). Provide a one-paragraph rationale "
      "for EVERY holding (required) plus an overall summary, and optionally note what you sold and why. "
      "Trade US-listed ETFs ONLY (index / sector / factor / momentum / duration / cash) — any single "
      "name or off-list ticker is REJECTED. Confirm a name with get_quote first. Note the desk's "
      "guardrails: any single ETF over 35% is clamped, micro-changes under ~1.5% of NAV are not traded, "
      "and in a stressed risk_state offensive (growth/cyclical) gross is capped. Call this ONCE, at the end. "
      "OPTIONAL governance fields (provide when you can — they improve the shadow decision ledger): "
      "falsifiers (list of strings — what would cause you to reverse this book within 5 days), "
      "evidence_planes (list of strings — data sources you relied on), "
      "expected_failure_mode (string — the most likely way this book loses money).",
      {"type": "object", "properties": {
          "holdings": {"type": "array", "items": {"type": "object", "properties": {
              "ticker": {"type": "string", "description": "a US-listed ETF in the book's universe"},
              "weight": {"type": "number", "description": "fraction of NAV, 0-1"},
              "rationale": {"type": "string", "description": "one paragraph: why you own this, now"},
              "conviction": {"type": "string", "enum": ["high", "medium", "low"]}},
              "required": ["ticker", "weight", "rationale"]}},
          "summary": {"type": "string", "description": "overall thesis / how the book is positioned today"},
          "sold_note": {"type": "string", "description": "optional: what you exited or trimmed and why"},
          "falsifiers": {"type": "array", "items": {"type": "string"},
                         "description": "what would cause you to reverse this book within 5 days"},
          "evidence_planes": {"type": "array", "items": {"type": "string"},
                              "description": "data sources / signal planes you relied on for this decision"},
          "expected_failure_mode": {"type": "string",
                                    "description": "the most likely way this book loses money"}},
       "required": ["holdings", "summary"]})
async def submit_book(args):
    from portfolio import etf_universe
    holdings = args.get("holdings") or []
    cleaned: list[dict] = []
    rejected_offlist: list[str] = []
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
        if not etf_universe.is_etf(t):
            rejected_offlist.append(t)        # single name or off-list ETF — not tradeable here
            continue
        seen.add(t)
        cleaned.append({"ticker": t, "weight": w, "rationale": r,
                        "group": etf_universe.group_of(t),
                        "conviction": (h.get("conviction") or "medium")})
        gross += w
    # no leverage — if the Brain over-allocates, scale down proportionally (mirrors rebalance())
    scaled = False
    if gross > 1.0 and cleaned:
        scale = 1.0 / gross
        for h in cleaned:
            h["weight"] = round(h["weight"] * scale, 6)
        gross, scaled = 1.0, True
    payload = {
        "holdings": cleaned,
        "summary": (args.get("summary") or "").strip(),
        "sold_note": (args.get("sold_note") or "").strip(),
        "gross": round(gross, 4),
        "scaled_to_no_leverage": scaled,
    }
    p = submission_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
    cash_pct = max(0.0, 1.0 - gross) * 100
    note = (f"ETF book submitted: {len(cleaned)} holdings, {gross * 100:.0f}% invested, "
            f"{cash_pct:.0f}% cash" + (" (scaled to remove leverage)" if scaled else "")
            + (f". REJECTED {len(rejected_offlist)} off-list ticker(s) (this book is US ETFs only): "
               + ", ".join(rejected_offlist) if rejected_offlist else "")
            + ". The desk will re-enforce the universe + risk guardrails and rebalance at the next mark.")
    return bot_mcp._ok(f"{BOOK_MARKER} {json.dumps({'n': len(cleaned), 'gross': round(gross, 4)})}\n{note}")


_DESK_TOOLS = [get_my_book, get_etf_board, get_quote, submit_book]

# Reuse the macro-dashboard READ tools, but drop get_portfolio (the FLAGSHIP book) and bot_mcp's
# Polygon get_quote (the ETF book quotes via the desk's universe-aware get_quote above, so a name
# the dashboard hasn't cached still marks correctly).
_READ_TOOLS = [t for t in bot_mcp._READ if t.name not in ("get_portfolio", "get_quote")]


def build_servers() -> dict:
    """The mcp_servers map: the macro READ tools (bot) + the ETF desk."""
    return {
        bot_mcp.SERVER_NAME: create_sdk_mcp_server(name=bot_mcp.SERVER_NAME, version="0.1.0", tools=_READ_TOOLS),
        SERVER_NAME: create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=_DESK_TOOLS),
    }


def allowed_tools() -> list[str]:
    """Macro READ tools + the ETF desk's own tools + web. NO raw Read/Grep/Glob, NO flagship
    get_portfolio, NO gated execute_trade — a free-form book that can only see its OWN state, the
    macro desks, and the ETF rotation board."""
    read = [f"mcp__{bot_mcp.SERVER_NAME}__{t.name}" for t in _READ_TOOLS]
    desk = [f"mcp__{SERVER_NAME}__{t.name}" for t in _DESK_TOOLS]
    return read + desk + bot_mcp.WEB_TOOLS

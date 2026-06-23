"""The autonomous desk's MCP surface — the FREE-FORM book the Opus Brain manages itself.

Unlike the gated flagship (brain/bot_mcp.execute_trade, which refuses an add without a
CONFIRMED research paper), this desk lets the Brain trade WHATEVER it is convinced of. The
Brain researches (reusing the macro-dashboard READ tools from bot_mcp + WebSearch/WebFetch),
then calls ONE tool — ``submit_book`` — with its complete target portfolio for the day, each
holding carrying a one-paragraph rationale. No gate, no research paper, no sleeves.

The tool does NOT execute the trade itself; it RECORDS the decided book to a per-portfolio
file (``_pending_decision.json``). The deterministic builder (``bot/autonomous.py``) reads it
after the session and rebalances the paper account to those targets under the usual
market-hours discipline — so all the trading logic stays in the trusted Python layer, bound
to the autonomous book only.
"""
from __future__ import annotations

import json
from pathlib import Path

import bot  # noqa: F401
from claude_agent_sdk import tool, create_sdk_mcp_server

from brain import bot_mcp

SERVER_NAME = "desk"
PORTFOLIO_ID = "autonomous"

# Marker the builder / any streaming layer can scan a tool result for.
BOOK_MARKER = "__BOOK__"


def submission_path(portfolio_id: str = PORTFOLIO_ID) -> Path:
    from portfolio import registry
    return registry.data_dir(portfolio_id) / "_pending_decision.json"


def clear_submission(portfolio_id: str = PORTFOLIO_ID) -> None:
    """Remove a stale decision file before a fresh run so a no-op turn can't replay yesterday."""
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
# tools
# ---------------------------------------------------------------------------

@tool("get_my_book",
      "Your CURRENT autonomous portfolio: cash, NAV, and every holding with its shares, weight, "
      "average cost, live price, unrealized P&L, and the rationale you last gave it. Call this FIRST "
      "to see exactly what you already hold before deciding today's trades.",
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
    # prior rationales from the last published book
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
    pending = paper_account.load_pending(PORTFOLIO_ID)
    return bot_mcp._json({
        "cash": round(float(state.get("cash") or 0.0), 2),
        "nav": round(nav, 2),
        "starting_nav": state.get("starting_nav"),
        "inception_date": state.get("inception_date"),
        "n_holdings": len(holdings),
        "holdings": holdings,
        "pending_orders": pending,
    })


@tool("submit_book",
      "Submit your FINAL decided portfolio for today as a COMPLETE target book — this is how you "
      "trade. The desk rebalances the paper account to exactly these weights: a name you include "
      "is bought/held to its weight, a name you OMIT (that you currently hold) is SOLD in full, a "
      "changed weight is trimmed/added. So include EVERY name you want to keep. Weights are "
      "fractions of NAV (0-1) and must sum to <= 1.0 (the remainder stays in cash). Provide a "
      "one-paragraph rationale for EVERY holding (required) plus an overall summary, and optionally "
      "note what you sold and why. There is NO gate and NO research-paper requirement — buy whatever "
      "you are convinced of. Call this ONCE, at the end, after your research. Trade liquid US-listed "
      "equities/ETFs (use get_quote to confirm a name is priceable before relying on it).",
      {"type": "object", "properties": {
          "holdings": {"type": "array", "items": {"type": "object", "properties": {
              "ticker": {"type": "string"},
              "weight": {"type": "number", "description": "fraction of NAV, 0-1"},
              "rationale": {"type": "string", "description": "one paragraph: why you own this, now"},
              "conviction": {"type": "string", "enum": ["high", "medium", "low"]}},
              "required": ["ticker", "weight", "rationale"]}},
          "summary": {"type": "string", "description": "overall thesis / how the book is positioned today"},
          "sold_note": {"type": "string", "description": "optional: what you exited or trimmed and why"}},
       "required": ["holdings", "summary"]})
async def submit_book(args):
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
    note = (f"Book submitted: {len(cleaned)} holdings, {gross * 100:.0f}% invested, {cash_pct:.0f}% cash"
            + (" (scaled to remove leverage)" if scaled else "")
            + ". The desk will rebalance the paper account to these targets at the next mark.")
    return bot_mcp._ok(f"{BOOK_MARKER} {json.dumps({'n': len(cleaned), 'gross': round(gross, 4)})}\n{note}")


_DESK_TOOLS = [get_my_book, submit_book]

# Reuse the macro-dashboard READ tools, but drop get_portfolio (that's the FLAGSHIP book — the
# autonomous Brain reads its OWN book via get_my_book to avoid confusion).
_READ_TOOLS = [t for t in bot_mcp._READ if t.name != "get_portfolio"]


def build_servers() -> dict:
    """The mcp_servers map to hand the SDK: the macro READ tools (bot) + this desk."""
    return {
        bot_mcp.SERVER_NAME: create_sdk_mcp_server(name=bot_mcp.SERVER_NAME, version="0.1.0", tools=_READ_TOOLS),
        SERVER_NAME: create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=_DESK_TOOLS),
    }


def allowed_tools() -> list[str]:
    """Macro READ tools + the desk's own tools + web. NO gated execute_trade / research-paper
    tools — this book is free-form.

    Deliberately NO raw Read/Grep/Glob: with the session cwd at the repo root those reached ANY
    file — including the OTHER books' state (data/portfolio/latest.json) — with no allowlist,
    which was a latent path for the autonomous Brain to peek at Flagship. The Brain researches
    only through the typed mcp__bot__* read tools + the web; it reads its OWN book via the desk.
    The path-controlled mcp__bot__read_signal is still available but now denies portfolio-book
    dirs (see bot_mcp._DENY_ROOTS)."""
    read = [f"mcp__{bot_mcp.SERVER_NAME}__{t.name}" for t in _READ_TOOLS]
    desk = [f"mcp__{SERVER_NAME}__{t.name}" for t in _DESK_TOOLS]
    return read + desk + bot_mcp.WEB_TOOLS

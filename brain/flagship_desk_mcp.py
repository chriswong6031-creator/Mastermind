"""The FLAGSHIP judgment desk's MCP surface — a flagship-scoped twin of brain/autonomous_mcp.

The PM-CONVICTION seat (brain/pm_conviction.py) reuses the autonomous Brain's proven research
tools + submit_book FLOW, but it manages the FLAGSHIP book, not the autonomous one. Reusing
brain/autonomous_mcp.build_servers() directly was a bug (caught by the live dry-run): that desk is
hardwired to PORTFOLIO_ID="autonomous", so get_my_book showed the PM the WRONG book and submit_book
wrote to the autonomous book's _pending_decision.json — while pm_conviction read back from
"flagship_judgment", so it always found nothing (ran=False) and would have silently no-op'd the
whole judgment layer in production (and dirtied the autonomous book's pending file).

This module fixes that with two scopes:
  * READ_PORTFOLIO  = "flagship"          — get_my_book shows the PM the REAL Flagship book.
  * SUBMIT_PORTFOLIO = "flagship_judgment" — submit_book writes to an ISOLATED pending path that
    pm_conviction.build_book reads back; phase2's judgment layer then applies it to the Flagship
    paper account. Never collides with the live autonomous book.

The research READ tools are identical to the autonomous desk (the same macro-dashboard surface).
"""
from __future__ import annotations

import json

import bot  # noqa: F401 — bootstraps vendor/macro onto sys.path
from claude_agent_sdk import tool, create_sdk_mcp_server

from brain import bot_mcp
from brain import autonomous_mcp as _auto

SERVER_NAME = "desk"
READ_PORTFOLIO = "flagship"
SUBMIT_PORTFOLIO = "flagship_judgment"
BOOK_MARKER = _auto.BOOK_MARKER

# submission_path / clear_submission / read_submission are generic (they take a portfolio_id) —
# reuse them, scoped to SUBMIT_PORTFOLIO, so pm_conviction reads exactly what submit_book writes.
submission_path = _auto.submission_path
clear_submission = _auto.clear_submission
read_submission = _auto.read_submission


@tool("get_my_book",
      "Your CURRENT Flagship portfolio: cash, NAV, and every holding with its shares, weight, "
      "average cost, live price, unrealized P&L, and the rationale it last carried. Call this FIRST "
      "to see what Flagship already holds before deciding today's complete target book.",
      {})
async def get_my_book(args):
    from portfolio import paper_account, registry
    state = paper_account._load_account(READ_PORTFOLIO)
    tickers = list((state.get("positions") or {}).keys())
    prices: dict[str, float] = {}
    for t in tickers + ["SPY"]:
        px = paper_account._current_price(t)
        if px:
            prices[t] = px
    pnl = paper_account.positions_pnl(prices, READ_PORTFOLIO)
    nav = paper_account.nav(prices, READ_PORTFOLIO)
    rationales: dict[str, str] = {}
    latest = registry.data_dir(READ_PORTFOLIO) / "latest.json"
    try:
        if latest.exists():
            for p in (json.loads(latest.read_text()).get("positions") or []):
                rationales[p.get("ticker")] = p.get("rationale") or (p.get("thesis_full") or {}).get("summary")
    except Exception:  # noqa: BLE001
        pass
    holdings = []
    for t, rec in pnl.items():
        mv = rec.get("market_value")
        holdings.append({
            "ticker": t, "shares": rec.get("shares"), "avg_cost": rec.get("avg_cost"),
            "current_price": rec.get("current_price"), "market_value": mv,
            "weight": round(mv / nav, 4) if (mv and nav) else None,
            "unrealized_pnl": rec.get("unrealized_pnl"), "unrealized_pct": rec.get("unrealized_pct"),
            "rationale": rationales.get(t),
        })
    return bot_mcp._json({
        "book": "flagship", "cash": round(float(state.get("cash") or 0.0), 2),
        "nav": round(nav, 2), "starting_nav": state.get("starting_nav"),
        "inception_date": state.get("inception_date"), "n_holdings": len(holdings),
        "holdings": holdings,
    })


@tool("submit_book",
      "Submit your FINAL decided Flagship portfolio for today as a COMPLETE target book — this is how "
      "you trade. A name you include is bought/held to its weight; a name you OMIT that Flagship "
      "currently holds is SOLD; a changed weight is trimmed/added. Include EVERY name you want to keep. "
      "Weights are fractions of NAV (0-1) summing to <= 1.0 (remainder = cash). Give a one-paragraph "
      "rationale for EVERY holding (required) + an overall summary. Your champions are then checked by "
      "a blind adversary + hard risk caps before they enter the book, so size with that in mind. Call "
      "this ONCE, at the end, after your research. Confirm pricability with get_quote before relying on a name.",
      {"type": "object", "properties": {
          "holdings": {"type": "array", "items": {"type": "object", "properties": {
              "ticker": {"type": "string"},
              "weight": {"type": "number", "description": "fraction of NAV, 0-1"},
              "rationale": {"type": "string", "description": "one paragraph: why you own this, now"},
              "conviction": {"type": "string", "enum": ["high", "medium", "low"]}},
              "required": ["ticker", "weight", "rationale"]}},
          "summary": {"type": "string", "description": "overall thesis / how the book is positioned today"},
          "sold_note": {"type": "string", "description": "optional: what you exited or trimmed and why"},
          # W4 B1 — the three-questions duty. Additive (schema growth only): a submission that omits
          # these is still accepted; the judgment book logs 'three_questions_incomplete'. Each
          # not_holding_should row is graded 21 trading days forward as a rotation CALL even absent a trade.
          "own_more": {"type": "array", "description": "names you would size UP if you could",
                       "items": {"type": "object", "properties": {
                           "ticker": {"type": "string"}, "why_now": {"type": "string"},
                           "probability": {"type": "number"}, "check_by": {"type": "string"}}}},
          "own_less": {"type": "array", "description": "names you are trimming/exiting and why",
                       "items": {"type": "object", "properties": {
                           "ticker": {"type": "string"}, "why_now": {"type": "string"},
                           "probability": {"type": "number"}, "check_by": {"type": "string"}}}},
          "not_holding_should": {"type": "array",
                                 "description": "names you do NOT hold but the regime says you SHOULD "
                                 "be rotating into — each with ticker, why_now, probability (0-1), check_by",
                                 "items": {"type": "object", "properties": {
                                     "ticker": {"type": "string"}, "why_now": {"type": "string"},
                                     "probability": {"type": "number"}, "check_by": {"type": "string"}}}},
          # W-L / L2 — the JOURNAL DUTY (charter P6). Additive (schema growth only): a submission that
          # omits these is still accepted; the judgment book logs 'journal_incomplete'. For EACH of your
          # past calls that graded badly (listed in your prompt's JOURNAL DUTY block) you record a lesson;
          # successes may be narrated too. Each lesson references its draft_id from the duty block.
          "journal_lessons": {"type": "array",
                              "description": "one lesson per badly-graded past call (from your JOURNAL DUTY "
                              "block). MISTAKE: {draft_id, what_i_believed, what_actually_happened, why_wrong "
                              "(one of bad-signal|bad-timing|bad-sizing|ignored-plane|crowd-follow|label-trust|"
                              "thesis-drift|luck-bad), rule_i_adopt (ONE falsifiable sentence), confidence_in_rule "
                              "(0-1)}. SUCCESS: {draft_id, what_worked, skill_or_luck, rule_i_keep, confidence_in_rule}",
                              "items": {"type": "object", "properties": {
                                  "draft_id": {"type": "string"},
                                  "what_i_believed": {"type": "string"},
                                  "what_actually_happened": {"type": "string"},
                                  "why_wrong": {"type": "string"},
                                  "rule_i_adopt": {"type": "string"},
                                  "what_worked": {"type": "string"},
                                  "skill_or_luck": {"type": "string"},
                                  "rule_i_keep": {"type": "string"},
                                  "confidence_in_rule": {"type": "number"}}}}},
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
    scaled = False
    if gross > 1.0 and cleaned:
        scale = 1.0 / gross
        for h in cleaned:
            h["weight"] = round(h["weight"] * scale, 6)
        gross, scaled = 1.0, True
    payload = {"holdings": cleaned, "summary": (args.get("summary") or "").strip(),
               "sold_note": (args.get("sold_note") or "").strip(),
               "gross": round(gross, 4), "scaled_to_no_leverage": scaled,
               # three-questions fields passed through verbatim (pm_conviction normalises them).
               "own_more": args.get("own_more") or [],
               "own_less": args.get("own_less") or [],
               "not_holding_should": args.get("not_holding_should") or [],
               # W-L / L2 journal duty completions, passed through verbatim (judgment_book records them).
               "journal_lessons": args.get("journal_lessons") or []}
    p = submission_path(SUBMIT_PORTFOLIO)          # <- the FIX: write to the flagship_judgment path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
    cash_pct = max(0.0, 1.0 - gross) * 100
    note = (f"Flagship book submitted: {len(cleaned)} holdings, {gross * 100:.0f}% invested, "
            f"{cash_pct:.0f}% cash" + (" (scaled to remove leverage)" if scaled else "")
            + ". It will be adversary-checked + risk-capped, then applied to the Flagship paper account.")
    return bot_mcp._ok(f"{BOOK_MARKER} {json.dumps({'n': len(cleaned), 'gross': round(gross, 4)})}\n{note}")


_DESK_TOOLS = [get_my_book, submit_book]
_READ_TOOLS = [t for t in bot_mcp._READ if t.name != "get_portfolio"]


def build_servers() -> dict:
    return {
        bot_mcp.SERVER_NAME: create_sdk_mcp_server(name=bot_mcp.SERVER_NAME, version="0.1.0", tools=_READ_TOOLS),
        SERVER_NAME: create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=_DESK_TOOLS),
    }


def allowed_tools() -> list[str]:
    read = [f"mcp__{bot_mcp.SERVER_NAME}__{t.name}" for t in _READ_TOOLS]
    desk = [f"mcp__{SERVER_NAME}__{t.name}" for t in _DESK_TOOLS]
    return read + desk + bot_mcp.WEB_TOOLS

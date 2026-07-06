"""The China desk's MCP surface — the FREE-FORM all-China book the Opus Brain manages itself.

Sibling of ``brain/autonomous_mcp.py`` but pointed at the macro China desks and the multi-venue
(A-share / HK / ADR) universe. The Brain researches via the China read tools below + the web,
then calls ONE tool — ``submit_book`` — with its complete target portfolio for the day, each
holding carrying a one-paragraph rationale. No gate, no research paper, no sleeves.

The tool RECORDS the decided book to the china portfolio's ``_pending_decision.json``; the
deterministic builder (``bot/china.py``) reads it after the session and rebalances the paper
account (USD-marked) to those targets. All trading logic stays in the trusted Python layer,
bound to the china book only.

Safety (mirrors the autonomous desk): only typed ``mcp__china__*`` read tools + web — NO raw
Read/Grep/Glob, NO flagship get_portfolio, NO gated execute_trade. The Brain reads its OWN book
via get_my_book and cannot reach another book's state.
"""
from __future__ import annotations

import json

import bot  # noqa: F401  -> vendor/macro onto sys.path
from claude_agent_sdk import tool, create_sdk_mcp_server

from brain import autonomous_mcp, bot_mcp, china_intake

SERVER_NAME = "china"
PORTFOLIO_ID = "china"
BENCHMARK = "FXI"

# Registry-driven so the HK desk (brain/hk_mcp.py) reuses this contract: the China book is CNY /
# A-shares-only; the HK book is HKD / HK-only. ALLOWED_VENUES empty = unrestricted.
from portfolio import registry as _registry
CURRENCY = _registry.currency(PORTFOLIO_ID)            # "CNY"
ALLOWED_VENUES = set(_registry.venues(PORTFOLIO_ID))   # {"A-share"}

# Marker the builder / streaming layer can scan a tool result for (shared with the autonomous desk).
BOOK_MARKER = autonomous_mcp.BOOK_MARKER

# bot_mcp._json hard-SLICES the serialized payload at 8000 chars, which can cut mid-record and
# yield INVALID JSON. For the list-bearing China tools we instead drop trailing rows until the
# whole payload fits, so the tool ALWAYS returns parseable JSON (no mid-string truncation).
_JSON_CAP = 7600


def _capped_json(payload: dict, shrink_keys: list[str], cap: int = _JSON_CAP):
    while True:
        s = json.dumps(payload, default=str, ensure_ascii=False)
        if len(s) <= cap:
            return bot_mcp._ok(s)
        longest = max((k for k in shrink_keys if isinstance(payload.get(k), list) and payload[k]),
                      key=lambda k: len(payload[k]), default=None)
        if longest is None:
            return bot_mcp._ok(s[:cap])     # nothing left to shrink (defensive; unreachable in practice)
        payload[longest] = payload[longest][:-1]
        payload["truncated"] = True


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
      "Your CURRENT China portfolio: cash, NAV (CNY), and every holding with shares, weight, "
      "average cost, live CNY price, unrealized P&L, and the rationale you last gave it. The book "
      "holds mainland A-shares (*.SS / *.SZ) ONLY, marked in CNY. Call this FIRST to see exactly "
      "what you already hold before deciding today.",
      {})
async def get_my_book(args):
    from portfolio import fx, paper_account, registry
    state = paper_account._load_account(PORTFOLIO_ID)
    tickers = list((state.get("positions") or {}).keys())
    prices: dict[str, float] = {}
    for t in tickers + [BENCHMARK]:
        base = fx.usd_to(paper_account._current_price(t), CURRENCY)   # shared store is USD → book ccy
        if base:
            prices[t] = base
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
            "ticker": t, "name": china_intake.display_name(t), "venue": china_intake._venue(t),
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
        "nav": round(nav, 2), "currency": CURRENCY,
        "benchmark": BENCHMARK,
        "starting_nav": state.get("starting_nav"),
        "inception_date": state.get("inception_date"),
        "n_holdings": len(holdings), "holdings": holdings,
        "pending_orders": pending,
    })


@tool("submit_book",
      "Submit your FINAL decided China portfolio for today as a COMPLETE target book — this is how "
      "you trade. The desk rebalances the paper account to exactly these weights: a name you include "
      "is bought/held to its weight, a name you OMIT (that you currently hold) is SOLD in full. So "
      "include EVERY name you want to keep. Weights are fractions of NAV (0-1) and must sum to <= 1.0 "
      "(the remainder stays in cash). Provide a one-paragraph rationale for EVERY holding (required) "
      "plus an overall summary, and optionally note what you sold and why. There is NO gate. Trade "
      "liquid mainland A-shares ONLY (ticker like 600519.SS / 300750.SZ); Hong Kong (*.HK) and "
      "US-listed ADRs are REJECTED by this book. Confirm a name is priceable with get_quote "
      "before relying on it (names we cannot price are skipped). Call this ONCE, at the end. "
      "OPTIONAL governance fields (provide when you can — they improve the shadow decision ledger): "
      "falsifiers (list of strings — what would cause you to reverse this book within 5 days), "
      "evidence_planes (list of strings — data sources you relied on), "
      "expected_failure_mode (string — the most likely way this book loses money).",
      {"type": "object", "properties": {
          "holdings": {"type": "array", "items": {"type": "object", "properties": {
              "ticker": {"type": "string", "description": "venue-suffixed: *.SS/*.SZ A-share, *.HK Hong Kong, bare = US ADR"},
              "weight": {"type": "number", "description": "fraction of NAV, 0-1"},
              "rationale": {"type": "string", "description": "one paragraph: why you own this, now — name the company alongside its ticker, e.g. 贵州茅台 (600519.SS), never a bare code"},
              "conviction": {"type": "string", "enum": ["high", "medium", "low"]}},
              "required": ["ticker", "weight", "rationale"]}},
          "summary": {"type": "string", "description": "overall thesis / how the book is positioned today — refer to each name by company name + ticker, e.g. 贵州茅台 (600519.SS)"},
          "sold_note": {"type": "string", "description": "optional: what you exited or trimmed and why"},
          "falsifiers": {"type": "array", "items": {"type": "string"},
                         "description": "what would cause you to reverse this book within 5 days"},
          "evidence_planes": {"type": "array", "items": {"type": "string"},
                              "description": "data sources / signal planes you relied on for this decision"},
          "expected_failure_mode": {"type": "string",
                                    "description": "the most likely way this book loses money"}},
       "required": ["holdings", "summary"]})
async def submit_book(args):
    holdings = args.get("holdings") or []
    cleaned: list[dict] = []
    rejected_offvenue: list[str] = []
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
        v = china_intake._venue(t)
        if ALLOWED_VENUES and v not in ALLOWED_VENUES:
            rejected_offvenue.append(t)      # wrong venue for this book (e.g. an HK name in the A-share book)
            continue
        seen.add(t)
        cleaned.append({"ticker": t, "weight": w, "rationale": r,
                        "venue": v,
                        "conviction": (h.get("conviction") or "medium")})
        gross += w
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
    note = (f"China book submitted: {len(cleaned)} holdings, {gross * 100:.0f}% invested, "
            f"{cash_pct:.0f}% cash" + (" (scaled to remove leverage)" if scaled else "")
            + (f". REJECTED {len(rejected_offvenue)} off-venue name(s) (this book is A-shares only): "
               + ", ".join(rejected_offvenue) if rejected_offvenue else "")
            + ". The desk will rebalance the paper account to these targets at the next mark.")
    return bot_mcp._ok(f"{BOOK_MARKER} {json.dumps({'n': len(cleaned), 'gross': round(gross, 4)})}\n{note}")


# ---------------------------------------------------------------------------
# China research read tools
# ---------------------------------------------------------------------------

@tool("get_china_regime",
      "The China macro-regime read: growth/inflation quadrant, scores + confidence, PBoC liquidity "
      "overlay, cycle tag, property read, sector RS, and current alerts. Your top-down frame.", {})
async def get_china_regime(args):
    raw = china_intake._read("china_regime/latest.json") or {}
    keep = ("date", "quad", "quad_name", "growth_score", "inflation_score", "confidence",
            "liquidity_overlay", "cycle_tag", "pending_quad", "sector_rs", "property",
            "fear_euphoria", "conditions", "alerts")
    return bot_mcp._json({k: raw.get(k) for k in keep if k in raw}
                         or {"note": "China regime not built yet."})


def _slim_standout(s: dict) -> dict:
    """Project a buy-board row to the decision-relevant fields — dropping the heavy spark_svg /
    axes / provenance blobs that would blow past the tool's serialization cap and corrupt the JSON."""
    if not isinstance(s, dict):
        return {}
    c = s.get("conviction") if isinstance(s.get("conviction"), dict) else {}
    size = c.get("size") if isinstance(c.get("size"), dict) else {}
    return {
        "ticker": s.get("ticker"), "name": s.get("name"), "sector": s.get("sector"),
        "label": s.get("label"), "urgency": s.get("urgency"), "dir": s.get("dir"),
        "alpha": s.get("alpha"), "alpha_entry": s.get("alpha_entry"),
        "conviction_score": c.get("score"), "band": c.get("band"),
        "entry_bucket": size.get("bucket"), "entry_note": size.get("note"),
        "verdict": c.get("verdict"), "cycle_blocked": c.get("cycle_blocked"),
    }


@tool("get_china_standouts",
      "The ranked China single-name buy boards: the A-share standouts (conviction score + band, "
      "label, urgency, residual alpha, and the ENTRY gate — entry_bucket='avoid' / cycle_blocked "
      "means good company but don't chase the entry) and the Hong-Kong buy board. The desks' best "
      "ideas right now.",
      {"type": "object", "properties": {"limit": {"type": "integer", "description": "max names per board (default 20, max 40)"}}})
async def get_china_standouts(args):
    limit = max(1, min(int(args.get("limit") or 20), 40))
    a = china_intake._read("factordata/china_standouts.json") or {}
    hk = china_intake._read("factordata/hk_standouts.json") or {}
    # Only surface the board(s) for this book's venue — the A-share book never sees the HK board.
    show_a = (not ALLOWED_VENUES) or ("A-share" in ALLOWED_VENUES)
    show_hk = (not ALLOWED_VENUES) or ("HK" in ALLOWED_VENUES)
    out: dict = {"as_of": a.get("as_of") or hk.get("as_of"),
                 "note": "Buy board(s) for this book's venue (slimmed). entry_bucket='avoid'/cycle_blocked "
                         "= wait for a pullback. Call get_china_intake for the corroborated ranking."}
    shrink: list[str] = []
    if show_a:
        out["a_share_buy"] = [_slim_standout(s) for s in (a.get("buy") or [])[:limit]]; shrink.append("a_share_buy")
    if show_hk:
        out["hk_buy"] = [_slim_standout(s) for s in (hk.get("buy") or [])[:limit]]; shrink.append("hk_buy")
    return _capped_json(out, shrink)


@tool("get_china_intake",
      "The UNIFIED China candidate funnel — every per-ticker China desk (A-share buy board, alpha "
      "leaders, reversal watch, HK board) deduped and ranked with provenance: which desks flagged "
      "each name, the directional lean, and the corroboration count. Your shortlist of what to study.",
      {"type": "object", "properties": {"limit": {"type": "integer", "description": "max candidates (default 25, max 60)"}}})
async def get_china_intake(args):
    limit = max(1, min(int(args.get("limit") or 25), 60))
    built = china_intake.build(limit)
    # Project a compact candidate row so the funnel — the Brain's primary shortlist — never exceeds
    # the tool serialization cap and returns truncated/invalid JSON. Restrict to the book's venue.
    slim = [{"ticker": c.get("ticker"), "name": china_intake.display_name(c.get("ticker")),
             "venue": c.get("venue"), "score": c.get("score"),
             "n_sources": c.get("n_sources"), "lean": c.get("lean"),
             "sources": c.get("sources"), "reasons": (c.get("reasons") or [])[:2],
             "falsifier": c.get("falsifier")}
            for c in (built.get("candidates") or [])
            if not ALLOWED_VENUES or c.get("venue") in ALLOWED_VENUES]
    return _capped_json({"as_of": built.get("as_of"), "macro_context": built.get("macro_context"),
                         "n_universe": built.get("n_universe"), "candidates": slim,
                         "note": built.get("note")}, ["candidates"])


@tool("get_china_brief",
      "The macro China desk's narrative brief — a context-only LLM read of the regime, the working "
      "rotation thesis, transmission channels to watch, and watch-items. Qualitative framing, not a "
      "scored signal.", {})
async def get_china_brief(args):
    b = china_intake._read("china_brief.json") or {}
    keep = ("summary", "regime_read", "rotation_check", "transmission", "watch_items",
            "conflicts", "confidence", "state_asof", "disclaimer")
    out = {k: b.get(k) for k in keep if k in b}
    return bot_mcp._json(out or {"note": "China brief not built yet."})


@tool("get_quote",
      "Confirm a Greater-China name is PRICEABLE and see its CNY mark before you rely on it. Returns "
      "the venue (A-share/HK/ADR), quote currency, the local-currency price, and the CNY price the "
      "book will actually transact at (A-shares are native CNY; HK HKD and US-ADR USD are FX-converted "
      "to CNY). priceable=false means the desk will SKIP this name — pick another.",
      {"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]})
async def get_quote(args):
    from portfolio import fx, paper_account
    t = (args.get("ticker") or "").upper().strip()
    if not t:
        return bot_mcp._json({"error": "no ticker"})
    usd = paper_account._current_price(t)        # shared store returns USD
    base = fx.usd_to(usd, CURRENCY)              # the book's base currency (CNY for china, HKD for hk)
    cur = fx.currency_of(t)                       # native quote currency
    local = round(usd * fx.rate_per_usd(cur), 4) if usd else None   # native-currency price
    return bot_mcp._json({
        "ticker": t, "name": china_intake.display_name(t), "venue": china_intake._venue(t),
        "currency": cur, "price_local": local, "base_currency": CURRENCY,
        "price_base": round(base, 4) if base else None,
        "priceable": bool(base and base > 0),
    })


_DESK_TOOLS = [get_my_book, submit_book]
_READ_TOOLS = [get_china_regime, get_china_standouts, get_china_intake, get_china_brief, get_quote]
_ALL_TOOLS = _DESK_TOOLS + _READ_TOOLS


def build_servers() -> dict:
    """The mcp_servers map: a single 'china' server with the desk + China read tools."""
    return {SERVER_NAME: create_sdk_mcp_server(name=SERVER_NAME, version="0.1.0", tools=_ALL_TOOLS)}


def allowed_tools() -> list[str]:
    """The china desk's own tools + web. NO raw Read/Grep/Glob, NO flagship get_portfolio, NO gated
    execute_trade — a free-form book that can only see its OWN state and the China desks."""
    return [f"mcp__{SERVER_NAME}__{t.name}" for t in _ALL_TOOLS] + bot_mcp.WEB_TOOLS

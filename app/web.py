"""Dashboard web routes — GET / serves the static dashboard; /api/* expose
the data contracts the page JS fetches at runtime.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

# Lazy import so the module loads even if brain/ isn't fully initialised yet
def _cached_zh(text: str):
    """Safe wrapper: returns None if brain.translate isn't available."""
    try:
        from brain.translate import cached_zh
        return cached_zh(text)
    except Exception:
        return None

router = APIRouter()

_STATIC = Path(__file__).parent / "static"

# The Mastermind project root is two levels up from app/web.py
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _data() -> Path:
    """Return the Mastermind-local data/ directory (not the macro vendor data/)."""
    return _PROJECT_ROOT / "data"


def _macro_data() -> Path:
    """Return the vendored macro engine's data/ directory (read-only)."""
    return _PROJECT_ROOT / "vendor" / "macro" / "data"


def _portfolio_dir(portfolio_id: str | None = None) -> Path:
    """The per-portfolio state directory (flagship → legacy data/portfolio/)."""
    try:
        from portfolio import registry
        return registry.data_dir(portfolio_id)
    except Exception:
        return _data() / "portfolio"


# The free-form "Brain" books — each backed by a bot module exposing load_decisions(). Maps a
# portfolio id to that module (None for the gated flagship / self-directed, which have no Brain
# decision log). Shared by /api/decisions and /api/portfolio's banner-summary fallback.
_BRAIN_BOOK_MODULES = {"autonomous": "bot.autonomous", "heavyweight": "bot.heavyweight",
                       "china": "bot.china", "hk": "bot.hk", "etf": "bot.etf"}


def _brain_book_module(portfolio: str):
    """Import the bot module backing a Brain book's decision log, or None for non-Brain books."""
    import importlib
    name = _BRAIN_BOOK_MODULES.get(portfolio)
    return importlib.import_module(name) if name else None


def _account_tickers(portfolio_id: str | None = None) -> list[str]:
    """Tickers currently held in a paper account (for live marks)."""
    try:
        acct = json.loads((_portfolio_dir(portfolio_id) / "account.json").read_text())
        return list((acct.get("positions") or {}).keys())
    except Exception:
        return []


def _live_prices(tickers: list[str]) -> dict[str, float]:
    """Live (intraday, TTL-cached) US quotes for `tickers` via Yahoo (yfinance), in USD; {} when
    unavailable.

    Yahoo reflects TODAY's tape. This REPLACED the Polygon path, whose key is an EOD/delayed tier
    that returned a stale prevDay close — so on a fast day (SMH -7%) US books mis-marked to yesterday.
    Only bare US symbols (no venue suffix) route here; venue-suffixed names (``*.HK`` / ``*.SS`` /
    ``*.SZ``) are marked off their own live/snapshot path (and a Yahoo HK/CN fetch would need the
    suffix anyway). One batched ``warm`` keeps the whole book to a single request."""
    us = [t for t in (tickers or []) if t and "." not in t]
    if not us:
        return {}
    try:
        from data_layer import yahoo_feed
        yahoo_feed.warm(us)                          # ONE batched request; TTL-cached for liveness
        out: dict[str, float] = {}
        for t in us:
            v = yahoo_feed.price_local(t)
            if v and v > 0:
                out[t] = float(v)
        return out
    except Exception:
        return {}


def _book_marks(portfolio_id: str | None = None) -> dict[str, float]:
    """Current marks for a book's held names, in the book's BASE currency — the live valuation
    input for NAV / per-position P&L.

      * USD books (flagship / autonomous / heavyweight)  → batched live Polygon delayed quotes.
      * HKD / CNY books (hk / china)                      → the vendored snapshot price converted
        to the base currency via ``portfolio.fx`` (Polygon carries no HK / A-share listings — and
        a venue-suffixed request can HANG — so we never route them through ``_live_prices``).

    Returns {} when nothing is priceable, so callers degrade to the avg-cost mark (no movement)
    rather than mis-marking. Network-light: USD is one batched call; the non-US path is file reads.
    """
    tickers = _account_tickers(portfolio_id)
    if not tickers:
        return {}
    try:
        from portfolio import registry
        ccy = registry.currency(portfolio_id)
    except Exception:
        ccy = "USD"
    if ccy == "USD":
        return _live_prices(tickers)
    # single-currency non-US book: mark each name in its base currency off the shared snapshot.
    try:
        from portfolio import fx, paper_account
    except Exception:
        return {}
    # Pre-warm the per-venue live caches in ONE batched request so the per-name loop below hits a
    # warm cache instead of firing a separate fetch per holding. Hong Kong needs this most: without
    # it, yahoo_feed fires one yf.download PER NAME (8 names ≈ 8 sequential downloads, ~5-12s); a
    # single warm() pulls the whole book in one call. (A-shares already bulk-cache on first touch via
    # tushare's _load_day, so they need no explicit warm.)
    hk = [t for t in tickers if (t or "").upper().endswith(".HK")]
    if hk:
        try:
            from data_layer import yahoo_feed
            yahoo_feed.warm(hk)
        except Exception:
            pass
    out: dict[str, float] = {}
    for t in tickers:
        try:
            base = fx.usd_to(paper_account._current_price(t), ccy)
        except Exception:
            base = None
        if base and base > 0:
            out[t] = float(base)
    return out


def _latest_quiver(strategy_dir: Path) -> dict[str, Any] | None:
    """Return the parsed JSON from the newest date-named file in a strategy dir."""
    try:
        files = sorted(strategy_dir.glob("*.json"), reverse=True)
        if not files:
            return None
        return json.loads(files[0].read_text())
    except Exception:
        return None


def _parse_note(path: Path) -> dict[str, Any] | None:
    """Parse a research note markdown file into {title, tickers, date, body_md}."""
    try:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        if not lines:
            return None

        # title from first # heading
        title = lines[0].lstrip("# ").strip() if lines[0].startswith("#") else path.stem

        # find the *tickers: ... · ISO-date* line
        ticker_line_idx = None
        tickers: list[str] = []
        date: str = ""
        for i, ln in enumerate(lines[1:], 1):
            m = re.match(r"\*tickers:\s*(.*?)\s*·\s*([\d\-T:.+Z]+)\*", ln)
            if m:
                raw_tickers = m.group(1)
                tickers = [t.strip() for t in raw_tickers.split(",") if t.strip()]
                date = m.group(2)
                ticker_line_idx = i
                break

        # body = everything after title + ticker line
        body_start = (ticker_line_idx + 1) if ticker_line_idx is not None else 1
        body_lines = lines[body_start:]
        body_md = "\n".join(body_lines).strip()

        # fall back to mtime for sort key if no date parsed
        sort_key = date or path.stat().st_mtime_ns.__str__()

        return {
            "title": title,
            "tickers": tickers,
            "date": date,
            "body_md": body_md,
            "_sort_key": sort_key,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

# no-cache so a UI update (new chat widget, research-paper changes, etc.) is never masked by
# the browser's heuristic cache — the page revalidates (etag/last-modified) on every load
# instead of silently serving a stale index that's missing freshly-added features.
_NOCACHE = {"Cache-Control": "no-cache"}


@router.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(_STATIC / "index.html", media_type="text/html", headers=_NOCACHE)


@router.get("/research", include_in_schema=False)
def research_page() -> FileResponse:
    """The Research page — same SPA; the client opens the Research view from this path."""
    return FileResponse(_STATIC / "index.html", media_type="text/html", headers=_NOCACHE)


@router.get("/self", include_in_schema=False)
def self_directed_page() -> FileResponse:
    """The Self-Directed book — same SPA; the client opens that view from this path."""
    return FileResponse(_STATIC / "index.html", media_type="text/html", headers=_NOCACHE)


@router.get("/desk", include_in_schema=False)
def desk_page() -> FileResponse:
    """The Desk observability page — same SPA; the client opens the Desk view from this path."""
    return FileResponse(_STATIC / "index.html", media_type="text/html", headers=_NOCACHE)


@router.get("/theme.css", include_in_schema=False)
def theme_css() -> FileResponse:
    """Serve the macro design-system stylesheet the dashboard links."""
    return FileResponse(_STATIC / "theme.css", media_type="text/css", headers=_NOCACHE)


@router.get("/theme.js", include_in_schema=False)
def theme_js() -> FileResponse:
    """Serve the macro theme toggle script (optional; dark renders without it)."""
    return FileResponse(_STATIC / "theme.js", media_type="application/javascript", headers=_NOCACHE)


@router.get("/chat.js", include_in_schema=False)
def chat_js() -> FileResponse:
    """Serve the live advisor chat widget (the floating Brain popup).

    no-cache so a widget update is never masked by the browser's heuristic cache.
    """
    return FileResponse(_STATIC / "chat.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


def _company_meta(ticker: str) -> dict:
    """Company name / sector / current price / fundamentals for the research PDF, merged from
    the vendored macro stockdata (fundamentals) + Polygon (live price, optional description /
    market cap). Every field is best-effort; missing ones are simply omitted."""
    t = (ticker or "").upper().strip()
    meta: dict[str, Any] = {}
    sd: dict = {}
    try:
        p = _PROJECT_ROOT / "vendor" / "macro" / "site" / "stockdata" / f"{t}.json"
        if p.exists():
            sd = json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        sd = {}
    fin = sd.get("financials") or {}
    an = sd.get("analyst") or {}
    ear = sd.get("earnings") or {}
    tech = sd.get("tech") or {}
    fac = sd.get("factors") or {}
    # macro stockdata stores margins/growth/yields in PERCENT units (e.g. 85.2 = +85.2%);
    # the PDF formatter expects fractions, so normalise here (÷100, None-safe).
    def _frac(v):
        try:
            return float(v) / 100.0
        except (TypeError, ValueError):
            return None

    meta["name"] = sd.get("name")
    meta["sector"] = fac.get("sector")
    meta["rev_growth"] = _frac(fin.get("rev_growth"))
    meta["gross_margin"] = _frac(fin.get("gross_margin"))
    meta["net_margin"] = _frac(fin.get("net_margin"))
    meta["roe"] = _frac(fin.get("roe") if fin.get("roe") is not None else an.get("roe"))
    meta["fwd_pe"] = an.get("forward_pe") if an.get("forward_pe") is not None else an.get("pe_yf")
    meta["div_yield"] = _frac(an.get("div_yield"))
    meta["analyst_target"] = an.get("target")
    meta["rating"] = an.get("rating")
    meta["next_earnings"] = ear.get("next_date")
    # live (delayed) price; fall back to the stockdata snapshot price
    price = None
    try:
        from data_layer import polygon
        price = polygon.quote(t)
    except Exception:  # noqa: BLE001
        price = None
    meta["price"] = price if price else tech.get("price")
    # optional richer reference: company description + market cap (Polygon, best-effort)
    try:
        from data_layer import polygon
        det = polygon.ticker_details(t)
        if det:
            meta["name"] = meta.get("name") or det.get("name")
            meta["sector"] = meta.get("sector") or det.get("sector")
            meta["description"] = det.get("description")
            meta["market_cap"] = det.get("market_cap")
    except Exception:  # noqa: BLE001
        pass
    return {k: v for k, v in meta.items() if v is not None}


@router.get("/research_paper.pdf", include_in_schema=False)
def research_paper_pdf(id: str = "", ticker: str = "") -> Response:
    """Generate a beautifully formatted research-paper PDF on the spot (deterministic, no AI).
    `id` selects the exact saved paper; `ticker` falls back to that name's latest paper."""
    try:
        from brain import research_paper
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"research store unavailable: {exc}"}, status_code=503)

    paper = None
    try:
        papers = research_paper.load_papers()
        if id:
            paper = next((p for p in papers if p.get("id") == id), None)
        if paper is None and ticker:
            paper = research_paper.latest_for(ticker)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"could not load paper: {exc}"}, status_code=500)
    if paper is None:
        return JSONResponse({"error": "research paper not found"}, status_code=404)

    try:
        from app import research_pdf
    except Exception as exc:  # noqa: BLE001 — reportlab missing, etc.
        return JSONResponse({"error": f"PDF engine unavailable (is reportlab installed?): {exc}"},
                            status_code=503)

    meta = {}
    try:
        meta = _company_meta(paper.get("ticker") or "")
    except Exception:  # noqa: BLE001 — metadata is enrichment; render without it on failure
        meta = {}
    try:
        pdf = research_pdf.build(paper, meta)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"PDF generation failed: {exc}"}, status_code=500)

    tkr = (paper.get("ticker") or "research").upper()
    datestr = str(paper.get("asof") or paper.get("generated_at") or "")[:10]
    # allowlist-sanitise: ticker/asof come from the paper JSON; a stray CR/LF/quote would
    # otherwise produce an illegal Content-Disposition header (and 500 the response)
    fname = re.sub(r"[^A-Za-z0-9._-]", "_",
                   f"Mastermind_{tkr}{('_' + datestr) if datestr else ''}.pdf")
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"',
                             "Cache-Control": "no-cache"})


@router.get("/api/performance")
def api_performance(portfolio: str = "flagship") -> JSONResponse:
    """Equity curve and performance summary for a $1M paper account (default: flagship)."""
    try:
        from portfolio import paper_account
        payload = paper_account.performance(portfolio_id=portfolio,
                                            prices=_book_marks(portfolio))
        return JSONResponse(payload)
    except Exception as exc:
        # never 500 — return a safe minimal payload
        return JSONResponse({
            "inception_date": None,
            "starting_nav": 1_000_000,
            "current_nav": 1_000_000,
            "cash": 1_000_000,
            "invested": 0.0,
            "total_return_pct": 0.0,
            "vs_spy_pct": 0.0,
            "day_change_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "realized_since": None,
            "series": [],
            "note": f"Performance unavailable: {exc}",
        })


@router.get("/api/risk")
def api_risk(portfolio: str = "flagship", recompute: bool = False) -> JSONResponse:
    """Portfolio safety scorecard: a static-weight historical risk backtest of the live book
    (max drawdown · diversification · ticker correlations · beta · vol/VaR/CVaR + a 0-100
    safety score). Serves the nightly-persisted snapshot; pass ?recompute=true to rebuild now
    (skips the heavier bootstrap CI for a snappy response). Never 500 — read-only, never an order.
    """
    try:
        from portfolio import safety
        rep = None if recompute else safety.load_safety(portfolio)
        if rep is None:
            rep = safety.compute_safety(portfolio, bootstrap=not recompute)
            # a fresh (not bot-built) read isn't consumed, so show the INDICATED de-gross
            if "overlay" not in rep:
                rep["overlay"] = {**safety.gross_overlay(rep), "applied": False}
            try:
                safety.persist(rep, portfolio)
            except Exception:
                pass
        return JSONResponse(rep)
    except Exception as exc:
        return JSONResponse({
            "portfolio_id": portfolio, "safety_score": None, "grade": "—",
            "verdict": "Safety report unavailable.", "metrics": {}, "subscores": {},
            "breaches": [], "caveats": [], "note": f"Safety unavailable: {exc}",
        })


@router.get("/api/portfolio")
def api_portfolio(portfolio: str = "flagship") -> JSONResponse:
    path = _portfolio_dir(portfolio) / "latest.json"
    if not path.exists():
        return JSONResponse({"error": "no book yet", "portfolio_id": portfolio}, status_code=404)
    try:
        payload = json.loads(path.read_text())

        # ------------------------------------------------------------------
        # Live marks: attach current price + unrealized P&L to each position
        # (Polygon delayed quotes via the account's avg-cost lots). Degrades to
        # nulls offline so the client always renders an honest dash.
        # ------------------------------------------------------------------
        try:
            from portfolio import paper_account
            prices = _book_marks(portfolio)
            pnl = paper_account.positions_pnl(prices, portfolio_id=portfolio) if prices else {}
            for pos in payload.get("positions", []):
                rec = pnl.get(pos.get("ticker"))
                if rec:
                    pos["cost_basis"] = rec.get("avg_cost")   # true avg-cost basis
                    pos["current_price"] = rec.get("current_price")
                    pos["market_value"] = rec.get("market_value")
                    pos["unrealized_pnl"] = rec.get("unrealized_pnl")
                    pos["unrealized_pct"] = rec.get("unrealized_pct")
        except Exception:
            pass

        # ------------------------------------------------------------------
        # Inject zh fields from the cache (read-only — no LLM in this path)
        # ------------------------------------------------------------------

        # disclaimer_zh
        disclaimer = payload.get("disclaimer")
        if disclaimer:
            zh_d = _cached_zh(disclaimer)
            if zh_d:
                payload["disclaimer_zh"] = zh_d

        # positions[].research.summary_zh (the Research Desk gate block)
        for pos in payload.get("positions", []):
            rb = pos.get("research")
            if rb and rb.get("summary"):
                zh_s = _cached_zh(rb["summary"])
                if zh_s:
                    rb["summary_zh"] = zh_s

        # positions[].thesis_full._zh
        for pos in payload.get("positions", []):
            tf = pos.get("thesis_full")
            if not tf:
                continue
            zh_tf: dict[str, Any] = {}
            for field in ("summary", "why_now", "sizing_rationale", "what_would_prove_wrong"):
                v = tf.get(field)
                if v:
                    zh = _cached_zh(v)
                    if zh:
                        zh_tf[field] = zh
            bull_zh = [_cached_zh(b) for b in (tf.get("bull") or [])]
            if any(zh for zh in bull_zh):
                zh_tf["bull"] = [zh if zh else b for zh, b in zip(bull_zh, tf.get("bull", []))]
            bear_zh = [_cached_zh(b) for b in (tf.get("bear") or [])]
            if any(zh for zh in bear_zh):
                zh_tf["bear"] = [zh if zh else b for zh, b in zip(bear_zh, tf.get("bear", []))]
            if zh_tf:
                tf["_zh"] = zh_tf

        # rejected[]._zh
        for rej in payload.get("rejected", []):
            zh_rej: dict[str, Any] = {}
            reason = rej.get("reason")
            if reason:
                zh_r = _cached_zh(reason)
                if zh_r:
                    zh_rej["reason"] = zh_r
            bear_zh = [_cached_zh(b) for b in (rej.get("bear") or [])]
            if any(zh for zh in bear_zh):
                zh_rej["bear"] = [zh if zh else b for zh, b in zip(bear_zh, rej.get("bear", []))]
            if zh_rej:
                rej["_zh"] = zh_rej

        # Banner write-up (top-of-page one-paragraph summary). It's published from the live
        # submission, which is CLEARED at the start of every run — so a run that carries the book
        # unchanged (feed gate, off-hours, skipped Brain) nulls it even though the last decision's
        # rationale still holds. For the Brain books, fall back to the most recent decision-log
        # summary so the banner never goes blank when the book hasn't changed.
        if not payload.get("summary"):
            src = _brain_book_module(portfolio)
            if src is not None:
                try:
                    # walk newest→oldest to the last decision that actually CARRIES a summary —
                    # a skipped/feed-gated run appends a summary-less entry, which shouldn't blank
                    # the banner when an earlier rationale (the still-current book) holds.
                    for rec in src.load_decisions(60):
                        if rec.get("summary"):
                            payload["summary"] = rec["summary"]
                            if not payload.get("sold_note"):
                                payload["sold_note"] = rec.get("sold_note")
                            break
                except Exception:  # noqa: BLE001
                    pass
        # zh for the banner summary (cache lookup; client falls back to English when cold)
        _sum = payload.get("summary")
        if _sum and not payload.get("summary_zh"):
            zh_sum = _cached_zh(_sum)
            if zh_sum:
                payload["summary_zh"] = zh_sum

        return JSONResponse(payload)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# The tab-switcher status payload is just NAV/return chips — re-pricing every book on every poll or
# tab click is wasteful, so cache the assembled payload for a short window. A live trade or reprice
# shows up within _PORTFOLIOS_TTL; the per-book live marks underneath have their own (shorter) caches.
_PORTFOLIOS_TTL = 45.0  # seconds
_portfolios_cache: dict[str, Any] = {}  # {"payload": dict, "ts": float}


def _portfolio_status(meta: dict) -> dict:
    """Assemble one book's tab-switcher row (metadata + quick status). I/O-bound (live marks +
    benchmark history), so callers run these concurrently across books."""
    from portfolio import paper_account, registry
    pid = meta["id"]
    status: dict[str, Any] = {"nav": None, "total_return_pct": None, "vs_spy_pct": None,
                              "day_change_pct": None, "holdings": 0, "cash_pct": None, "as_of": None}
    # the self-directed book has its own engine (not paper_account) — read its NAV/return directly
    if pid == "self_directed":
        try:
            from portfolio import self_directed
            bk = self_directed.book()
            alloc = bk.get("allocation") or {}
            status.update({
                "nav": bk.get("nav"),
                "total_return_pct": alloc.get("total_return_pct"),
                "cash_pct": round((alloc.get("cash_pct") or 0) * 100, 1),
                "holdings": alloc.get("n_positions") or 0,
                "as_of": bk.get("inception_date"),
            })
        except Exception:
            pass
        return {**{k: meta.get(k) for k in ("id", "name", "tagline", "kind", "manager", "benchmark", "currency")},
                "status": status}
    try:
        perf = paper_account.performance(portfolio_id=pid, prices=_book_marks(pid))
        nav = perf.get("current_nav") or 0
        status.update({
            "nav": perf.get("current_nav"),
            "total_return_pct": perf.get("total_return_pct"),
            "vs_spy_pct": perf.get("vs_spy_pct"),
            "day_change_pct": perf.get("day_change_pct"),
            "cash_pct": round((perf.get("cash") or 0) / nav * 100, 1) if nav else None,
            "as_of": perf.get("realized_since"),
        })
    except Exception:
        pass
    try:
        latest = registry.data_dir(pid) / "latest.json"
        if latest.exists():
            d = json.loads(latest.read_text())
            status["holdings"] = len(d.get("positions") or [])
            status["as_of"] = d.get("as_of") or status["as_of"]
    except Exception:
        pass
    return {**{k: meta.get(k) for k in ("id", "name", "tagline", "kind", "manager", "benchmark", "currency")},
            "status": status}


@router.get("/api/portfolios")
def api_portfolios() -> JSONResponse:
    """The set of portfolios the dashboard switches between, each with a quick status
    (NAV, return, vs-SPY, holdings) for the tab labels. The flagship is the gated engine
    book; the autonomous book is managed free-form by the Opus Brain.

    Each book's status is I/O-bound (live marks + benchmark history); we price them concurrently
    and cache the assembled payload for ``_PORTFOLIOS_TTL`` so a tab click / poll doesn't re-price."""
    from portfolio import registry
    now = time.time()
    cached = _portfolios_cache.get("payload")
    if cached is not None and (now - _portfolios_cache.get("ts", 0.0)) < _PORTFOLIOS_TTL:
        return JSONResponse(cached)

    metas = registry.all_portfolios()
    # Price the books concurrently — each row is independent and network-bound, so wall-clock
    # collapses to roughly the slowest single book instead of the sum across all of them.
    with ThreadPoolExecutor(max_workers=max(1, len(metas))) as ex:
        out = list(ex.map(_portfolio_status, metas))

    payload = {"portfolios": out, "default": registry.DEFAULT_ID}
    _portfolios_cache["payload"] = payload
    _portfolios_cache["ts"] = now
    return JSONResponse(payload)


@router.get("/api/decisions")
def api_decisions(portfolio: str = "autonomous", limit: int = 60) -> JSONResponse:
    """The autonomous Brain's daily decision log — what it bought / sold / held each day, and
    the rationale for every holding. Empty for the gated flagship (it journals via research papers)."""
    try:
        _src = _brain_book_module(portfolio)
        if _src is None:
            return JSONResponse({"decisions": [], "note": "decision log is Brain-book-only (autonomous/heavyweight/china/hk/etf)"})
        decisions = _src.load_decisions(limit)
        today_iso = date.today().isoformat()
        # Region books (China A-shares, HK) trade opaque numeric / HK codes — resolve the human
        # display name (Chinese for A-shares, English for HK/ADR) for every holding AND executed
        # trade so the Daily Decision Log's buy/sell chips and holding rows read like the Positions
        # panel. Resolved server-side on every read (not just baked in at log time) so historical
        # entries logged before names were captured backfill too; mirrors api_trades. US books are
        # self-describing and stay code-only.
        _name = None
        try:
            from portfolio import registry
            if registry.venues(portfolio):
                from brain import china_intake
                _name = china_intake.display_name
        except Exception:  # noqa: BLE001
            _name = None
        # A dollar figure alone doesn't say how much of a position a SELL trimmed or whether it
        # made money. Enrich each executed sell with the fraction of the position sold (pct_sold;
        # 1.0 = full exit) and the realized P&L + %, sourced from the SAME FIFO blotter the Trade
        # History panel uses so the two agree. Derived from fills on every read, so historical
        # decision-log entries (which stored only ticker/side/value) backfill too.
        try:
            from portfolio import trade_history
            _sell = trade_history.sell_realized(portfolio)
        except Exception:  # noqa: BLE001
            _sell = {}
        # Attach cached Chinese for the AI write-ups so the Daily Decision Log renders in
        # Chinese when zh is toggled. cached_zh() is a pure cache lookup (None -> client
        # falls back to English) — warmed by brain.translate.translate_decisions() on the
        # daily run; English never regresses if the cache is cold.
        for d in decisions:
            # flag a decision logged today so the UI can highlight + tag it "new"
            d["today"] = str(d.get("asof") or "")[:10] == today_iso
            for rec in (d.get("executed") or []):
                if _name and rec.get("ticker") and not rec.get("name"):
                    nm = _name(rec["ticker"])
                    if nm and nm.upper() != rec["ticker"].upper():
                        rec["name"] = nm
                if rec.get("side") == "sell":
                    det = _sell.get((d.get("asof"), (rec.get("ticker") or "").upper()))
                    if det:
                        for k in ("pct_of_position", "realized_pnl", "realized_pct"):
                            if det.get(k) is not None and rec.get(k) is None:
                                rec[k] = det[k]
            if _name:
                for h in (d.get("holdings") or []):
                    tk = (h.get("ticker") or "")
                    if tk and not h.get("name"):
                        nm = _name(tk)
                        if nm and nm.upper() != tk.upper():
                            h["name"] = nm
            for fld in ("summary", "sold_note", "brain_text"):
                v = d.get(fld)
                if v:
                    zh = _cached_zh(v)
                    if zh:
                        d[fld + "_zh"] = zh
            for h in (d.get("holdings") or []):
                r = h.get("rationale")
                if r:
                    zh = _cached_zh(r)
                    if zh:
                        h["rationale_zh"] = zh
        return JSONResponse({"decisions": decisions})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"decisions": [], "error": str(exc)})


@router.get("/api/posture")
def api_posture(book: str = "flagship") -> JSONResponse:
    """The book's STRATEGY-LABEL signal — a glance-able posture on top of the free-form rationale:
    {book, posture_label, posture_label_zh, posture_tone, sub_strategy, favored[], avoided[],
    driver, detail, cash_pct, invested_pct, available}.

    Derived deterministically from the book's structured state (cash / gross / net trade flow /
    macro regime) and enriched with a keyword scan over the Brain's own write-up. Read-only,
    offline (no LLM), and graceful: an absent / malformed book degrades to ``available: False``
    rather than raising. The `book` id is any registry portfolio (flagship / autonomous /
    heavyweight / china / hk / etf)."""
    try:
        from brain import posture as _posture
        return JSONResponse(_posture.posture(book))
    except Exception as exc:  # noqa: BLE001 — never raise; degrade to an honest stub
        return JSONResponse({"book": book, "available": False, "posture_label": "—",
                             "posture_label_zh": "—", "posture_tone": "muted", "sub_strategy": None,
                             "favored": [], "avoided": [], "driver": None, "detail": None,
                             "cash_pct": None, "invested_pct": None, "error": str(exc)})


@router.get("/api/etf/outcomes")
def api_etf_outcomes() -> JSONResponse:
    """The ETF book's accountability scorecard — every past pick forward-graded vs SPY (21d
    rel-return), with hit-rate, per-conviction calibration, weight-IC, and the book-edge (Newey-West
    t over independent windows). 'building' until enough resolves. Backs the ETF track-record panel."""
    try:
        from portfolio import etf_outcomes
        return JSONResponse(etf_outcomes.summary())
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"scorecard": {"status": "building"}, "error": str(exc)})


@router.get("/api/overnight-tape")
def api_overnight_tape() -> JSONResponse:
    """The LIVE overnight cross-asset tape — US index futures, international indices, FX/rates, vol,
    commodities and crypto, each with its overnight % change, plus a distilled risk read
    (calm/elevated/stressed). What's moving while the cash market is shut — the thing the EOD macro
    dashboard can't see. Polled by the dashboard's Overnight Tape panel; cached ~5 min server-side."""
    try:
        from data_layer import overnight
        return JSONResponse(overnight.tape())
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"groups": {}, "risk": {"state": "calm", "reasons": ["unavailable"]},
                             "live": False, "error": str(exc)})


@router.get("/api/research")
def api_research() -> JSONResponse:
    notes_dir = _data() / "research" / "notes"
    if not notes_dir.exists():
        return JSONResponse([])
    try:
        parsed: list[dict[str, Any]] = []
        for p in notes_dir.glob("*.md"):
            note = _parse_note(p)
            if note:
                parsed.append(note)
        # sort newest first
        parsed.sort(key=lambda n: n["_sort_key"], reverse=True)
        # collapse identical notes (same title + body) — keep the newest of each;
        # earlier test runs wrote duplicate note files, which would otherwise spam the feed
        seen: set[tuple[str, str]] = set()
        deduped: list[dict[str, Any]] = []
        for n in parsed:
            key = (n["title"], n["body_md"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(n)
        # strip internal sort key, cap at 30; inject zh fields from cache
        out = []
        for n in deduped[:30]:
            note = {k: v for k, v in n.items() if k != "_sort_key"}
            # keys are always present (null when uncached) so the client can rely on them
            note["title_zh"] = _cached_zh(note.get("title") or "")
            note["body_md_zh"] = _cached_zh(note.get("body_md") or "")
            out.append(note)
        return JSONResponse(out)
    except Exception:
        return JSONResponse([])


@router.get("/api/research_papers")
def api_research_papers() -> JSONResponse:
    """The Research page contract — one row per gated buy decision, newest first.

    Each row is a holistic research paper joined with the live book's gate result: the
    engine buy-score, the research score, the combined Conviction Index, the viability, the
    action (buy_confirmed / research_held / evaluated), the decision time, and the full
    report (markdown + sections) for the 'view thesis' panel.
    """
    try:
        from brain import research_paper
        papers = research_paper.load_papers()
    except Exception as exc:
        return JSONResponse({"papers": [], "error": str(exc)})

    # join with the latest book: ticker -> (research_block, action, trade_time, weight)
    gate: dict[str, dict] = {}
    try:
        book = json.loads((_data() / "portfolio" / "latest.json").read_text())
        for pos in book.get("positions", []):
            rb = pos.get("research")
            if rb and pos.get("sleeve") == "conviction":
                gate[(pos.get("ticker") or "").upper()] = {
                    "block": rb, "action": "buy_confirmed",
                    "trade_time": pos.get("opened_at"), "weight": pos.get("weight"),
                }
        for held in book.get("research_held", []):
            t = (held.get("ticker") or "").upper()
            gate.setdefault(t, {"block": held, "action": "research_held",
                                "trade_time": None, "weight": 0.0})
    except Exception:
        pass

    out: list[dict[str, Any]] = []
    for p in papers:
        t = (p.get("ticker") or "").upper()
        g = gate.get(t, {})
        rb = g.get("block") or {}
        summary = p.get("summary") or ""
        out.append({
            "id": p.get("id"),
            "ticker": t,
            "asof": p.get("asof"),
            "reviewed_at": p.get("generated_at"),
            "trade_time": g.get("trade_time"),
            "action": g.get("action", "evaluated"),
            "mode": p.get("mode"),
            # prefer the live book's gate result; fall back to the gate the paper stamped on
            # itself at review time (so a reviewed-but-not-traded name still shows its scores)
            "engine_score": rb.get("engine_score", p.get("engine_score")),
            "research_score": p.get("research_score"),
            "combined": rb.get("combined", p.get("combined")),
            "confirmed": rb.get("confirmed", p.get("confirmed")),
            "viability": p.get("viability"),
            "recommend": p.get("recommend"),
            "confidence": p.get("confidence"),
            "fair_value": p.get("fair_value"),
            "price_at_review": p.get("price_at_review"),
            "price_assessment": p.get("price_assessment"),
            "weight": g.get("weight"),
            "summary": summary,
            "summary_zh": _cached_zh(summary) if summary else None,
            "report_md": p.get("report_md"),
            "report_md_zh": _cached_zh(p.get("report_md") or "") if p.get("report_md") else None,
            "key_risks": p.get("key_risks") or [],
        })
    return JSONResponse({"papers": out})


@router.get("/api/outcome_ledger")
def api_outcome_ledger() -> JSONResponse:
    """The Calibration page contract — the engine's accountability scorecard (brain.outcome_ledger).

    On every RESOLVED thesis it joins what the engine PREDICTED (prob_correct) with what HAPPENED
    (realized rel-return + hit/miss) and what it SAW (the point-in-time lens snapshot). Returns:
      - summary           : n / status / Brier / hit-rate / calibration_error
      - reliability_curve : per predicted-probability bucket, mean predicted vs realized hit-rate
                            (is the engine's stated 60% actually 60%?)
      - lens_edge         : per (lens, direction) realized hit-rate (which lenses actually predicted)
      - lens_weights      : the reliability multiplier the self-calibrating gate now applies per lens
      - records           : the resolved theses log (newest first, capped)
    status='building' (n=0) until the first cohort matures (~2026-07-17). Evergreen — the page polls it.
    """
    try:
        from brain import outcome_ledger
        summary = outcome_ledger.summary()
        curve = outcome_ledger.reliability_curve()
        edge = outcome_ledger.lens_edge(min_n=1)
        weights = outcome_ledger.lens_weights()
        records = outcome_ledger.load()
    except Exception as exc:
        return JSONResponse({"summary": {"n": 0, "status": "building", "brier": None,
                                         "hit_rate": None, "calibration_error": None},
                             "reliability_curve": [], "lens_edge": [], "lens_weights": {},
                             "records": [], "error": str(exc)})
    records = sorted(records, key=lambda r: r.get("asof_resolved") or "", reverse=True)[:200]
    rec_out = [{
        "thesis_id": r.get("thesis_id"), "subject": r.get("subject"), "sleeve": r.get("sleeve"),
        "asof_decided": r.get("asof_decided"), "asof_resolved": r.get("asof_resolved"),
        "prob_correct": r.get("prob_correct"), "realized_rel": r.get("realized_rel"),
        "outcome": r.get("outcome"), "quad_at_entry": r.get("quad_at_entry"),
        "confluence_at_entry": r.get("confluence_at_entry"), "lens_dirs": r.get("lens_dirs") or {},
    } for r in records]
    return JSONResponse({"summary": summary, "reliability_curve": curve, "lens_edge": edge,
                         "lens_weights": weights, "records": rec_out})


@router.get("/api/trades")
def api_trades(portfolio: str = "flagship") -> JSONResponse:
    """Open/closed position summaries PLUS a complete per-fill blotter (`history`):
    every individual buy/sell, with realized P&L on sells and live unrealized P&L
    on still-open buy remainders. Scoped to a portfolio (default: flagship)."""
    try:
        from portfolio import market_calendar, paper_account, position_log, registry, trade_history
        prices = _live_prices(_account_tickers(portfolio))
        history = trade_history.history(prices, portfolio_id=portfolio)
        pending = paper_account.load_pending(portfolio)
        # Region books (China A-shares, HK) trade opaque numeric / HK codes — attach the
        # human display name (Chinese for A-shares, English for HK/ADR) to each blotter and
        # pending row so Trade History reads like the Positions panel. Scoped to venue-
        # restricted books; US tickers are self-describing and stay code-only.
        if registry.venues(portfolio):
            from brain import china_intake
            for row in (*history, *pending):
                tk = (row.get("ticker") or "")
                nm = china_intake.display_name(tk)
                if nm and nm.upper() != tk.upper():
                    row["name"] = nm
        return JSONResponse({
            "open": position_log.open_positions(portfolio_id=portfolio),
            "closed": position_log.closed_positions(portfolio_id=portfolio),
            "history": history,
            # PENDING orders queued while the market is closed — fill at next open
            "pending": pending,
            "market": market_calendar.status(),
        })
    except Exception as exc:
        return JSONResponse({"open": [], "closed": [], "history": [],
                             "pending": [], "market": {}, "error": str(exc)})


# ---------------------------------------------------------------------------
# Self-Directed book (the third portfolio) — user-driven manual paper trading.
# Read-only marks come from the same delayed Polygon feed as the rest of the dashboard;
# orders/theses are written by the user. PAPER ONLY — long-only, no leverage.
# ---------------------------------------------------------------------------

class _OrderReq(BaseModel):
    ticker: str
    side: str                          # "buy" | "sell"
    shares: float | None = None        # share-sized order
    notional: float | None = None      # OR dollar-sized order ($ → shares at the fill price)


class _ThesisReq(BaseModel):
    ticker: str
    note: str = ""


@router.get("/api/self_directed")
def api_self_directed() -> JSONResponse:
    """The Self-Directed book: positions (live marks + weights), allocation scorecard,
    market state, pending orders. Settles any due pending orders on read."""
    try:
        from portfolio import self_directed
        # one batched live-price fetch for all held + pending names, then build the book
        held = list((self_directed._load_account().get("positions") or {}).keys())
        pend = [o.get("ticker") for o in self_directed._load_pending()]
        prices = _live_prices(sorted({*held, *[t for t in pend if t]}))
        return JSONResponse(self_directed.book(prices=prices))
    except Exception as exc:  # noqa: BLE001 — never 500 the dashboard
        return JSONResponse({"nav": 1_000_000.0, "cash": 1_000_000.0, "invested": 0.0,
                             "positions": [], "pending": [],
                             "allocation": {"cash_pct": 1.0, "gross": 0.0, "n_positions": 0,
                                            "largest_weight": 0.0, "total_unrealized_pnl": 0.0,
                                            "total_return_pct": 0.0},
                             "market": {"is_open": False, "session": "closed"},
                             "error": str(exc)})


@router.get("/api/self_directed/history")
def api_self_directed_history() -> JSONResponse:
    """Trade History blotter for the Self-Directed book (every fill + the pending queue)."""
    try:
        from portfolio import self_directed
        held = list((self_directed._load_account().get("positions") or {}).keys())
        prices = _live_prices(held)
        return JSONResponse(self_directed.history(prices=prices))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"history": [], "pending": [], "realized_total": 0.0,
                             "n_closed": 0, "n_buys": 0, "win_rate": None, "error": str(exc)})


@router.get("/api/self_directed/search")
def api_self_directed_search(q: str = "") -> JSONResponse:
    """Live US-stock search (ticker or company name) for the order ticket."""
    try:
        from data_layer import polygon
        return JSONResponse({"results": polygon.search_tickers(q)})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"results": [], "error": str(exc)})


@router.get("/api/self_directed/quote")
def api_self_directed_quote(ticker: str = "") -> JSONResponse:
    """Live price + company name + market state for one ticker (order-ticket display)."""
    try:
        from portfolio import self_directed
        return JSONResponse(self_directed.quote_info(ticker))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ticker": (ticker or "").upper(), "price": None, "error": str(exc)})


@router.post("/api/self_directed/order")
def api_self_directed_order(req: _OrderReq) -> JSONResponse:
    """Place a buy/sell. Fills now at market if open; otherwise queues to the next open."""
    try:
        from portfolio import self_directed
        return JSONResponse(self_directed.place_order(
            req.ticker, req.side, req.shares, notional=req.notional))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/api/self_directed/thesis")
def api_self_directed_thesis(req: _ThesisReq) -> JSONResponse:
    """Save (or clear) the user's conviction thesis note for a position."""
    try:
        from portfolio import self_directed
        saved = self_directed.set_thesis(req.ticker, req.note)
        return JSONResponse({"ok": True, "ticker": (req.ticker or "").upper(), "thesis": saved})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.post("/api/self_directed/cancel")
def api_self_directed_cancel(order_id: str = "") -> JSONResponse:
    """Cancel a still-pending (unfilled) order."""
    try:
        from portfolio import self_directed
        return JSONResponse({"ok": self_directed.cancel_order(order_id)})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.get("/api/outcomes")
def api_outcomes() -> JSONResponse:
    """Realized thesis outcomes — triple-barrier + rel-return vs SPY (the learning signal that
    feeds the Brier track record). `labels` per thesis, `summary` stats, and the graded
    `track_record`. Best-effort: returns empty-but-valid on any failure."""
    try:
        import bot  # noqa: F401 — bootstraps vendor/macro for the price accessor
        from datetime import date as _date
        from brain import calibration, outcomes, scorer
        asof = _date.today()
        realized = outcomes.realized_returns(asof)
        try:
            cal = calibration.load() or calibration.compute(asof)
        except Exception:  # noqa: BLE001
            cal = {}
        return JSONResponse({
            "labels": outcomes.all_labels(asof),
            "summary": outcomes.summary(asof),
            "track_record": scorer.track_record(asof, realized=realized),
            "calibration": cal,
        })
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"labels": [], "summary": {}, "track_record": {},
                             "calibration": {}, "error": str(exc)})


@router.get("/api/shadow_books")
def api_shadow_books() -> JSONResponse:
    """Parallel forward shadow books — the leakage-free A/B of decision policies (prod vs
    no-committee vs no-calibration vs engine-only). Returns the persisted leaderboard
    (per-policy forward NAV return, hit rate, Brier, holdings divergence vs prod). Best-effort:
    falls back to a live recompute if the file is absent, then to empty-but-valid."""
    try:
        import bot  # noqa: F401 — bootstraps vendor/macro for the price accessor
        from portfolio import shadow_books
        data = shadow_books.load_leaderboard()
        if not data:
            from datetime import date as _date
            data = shadow_books.run(_date.today().isoformat())
        return JSONResponse({
            "as_of": data.get("as_of"),
            "leaderboard": data.get("leaderboard", []),
            "books": data.get("books", {}),
            "policies": [{"id": p["id"], "label": p["label"], "label_zh": p.get("label_zh"),
                          "desc": p.get("desc"), "desc_zh": p.get("desc_zh")}
                         for p in shadow_books.POLICIES],
        })
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"leaderboard": [], "books": {}, "policies": [], "error": str(exc)})


@router.get("/api/predictions")
def api_predictions() -> JSONResponse:
    """Universe-wide forward prediction log — a falsifiable rel-return thesis on EVERY name the
    engine has a directional opinion on (~1,600), forward-labeled. Returns coverage + a
    date-CLUSTERED cross-sectional scorecard (rank-IC of ladder.score vs forward rel-return,
    directional hit-rate + Brier, 'up' edge), each with a CI + effective-n so small samples never
    overclaim. This is the statistical-power unlock — n grows with breadth, not portfolio turnover."""
    try:
        import bot  # noqa: F401 — bootstraps vendor/macro for the price panel
        from datetime import date as _date
        from portfolio import predictions
        return JSONResponse(predictions.summary(_date.today().isoformat()))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"coverage": {}, "scorecard": {}, "error": str(exc)})


@router.get("/api/rejections")
def api_rejections() -> JSONResponse:
    """Off-policy REJECTION log — every name the gate rejected (conviction veto / research hold /
    committee drop / timing withhold), forward-graded vs SPY. Returns coverage + the veto-regret
    scorecard ('did the gate veto winners?', split by reject stage). Read-only; best-effort."""
    try:
        import bot  # noqa: F401 — bootstraps vendor/macro for the price labeler
        from datetime import date as _date
        from portfolio import rejections
        return JSONResponse(rejections.summary(_date.today().isoformat()))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"coverage": {}, "scorecard": {}, "error": str(exc)})


@router.get("/api/shadow_bandit")
def api_shadow_bandit() -> JSONResponse:
    """Discounted Thompson Sampling over the shadow policies — each arm's discounted forward hit-rate
    posterior + P(arm is best). A faster, regime-adaptive read of which policy is winning than the raw
    cumulative-return leaderboard. Read-only (never switches the live policy); best-effort."""
    try:
        import bot  # noqa: F401 — bootstraps vendor/macro for the shadow ledgers
        from portfolio import bandit
        return JSONResponse(bandit.rank_shadow_books())
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"status": "building", "arms": [], "error": str(exc)})


@router.get("/api/engine_backtest")
def api_engine_backtest() -> JSONResponse:
    """Historical engine-backtest verdict — the HIGH-statistical-power, leakage-free read on the
    deterministic engine (DSR / PBO / BH-FDR / one-shot holdout over the survivorship-safe S&P-1500
    panel). Read-only: surfaces the persisted artifact (generated on demand by
    scripts/run_engine_backtest.py); honest 'unavailable' until a run is recorded."""
    try:
        import bot  # noqa: F401
        from loop import engine_backtest
        return JSONResponse(engine_backtest.load())
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"status": "unavailable", "error": str(exc)})


@router.get("/api/factor_zoo")
def api_factor_zoo() -> JSONResponse:
    """Advanced factor-alpha lab — a broad price-factor library + IC term-structure + a
    holdout-validated COMPOSITE, scored under the frozen multiple-testing gauntlet (DSR re-deflated
    at effective-N over the whole pool, PBO/CSCV, BH-FDR, one-shot 2022+ holdout). Read-only:
    surfaces the persisted artifact (generated on demand by scripts/run_factor_zoo.py)."""
    try:
        import bot  # noqa: F401
        from loop import factor_zoo
        return JSONResponse(factor_zoo.load())
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"status": "unavailable", "error": str(exc)})


@router.get("/api/fundamentals")
def api_fundamentals() -> JSONResponse:
    """PIT fundamental factors (value + quality) from SEC EDGAR — point-in-time (asof_date-gated, no
    look-ahead), scored through the frozen gauntlet, with regime-conditional IC. Read-only: surfaces
    the persisted artifact (generated on demand by scripts/run_fundamentals.py)."""
    try:
        import bot  # noqa: F401
        from loop import fundamentals
        return JSONResponse(fundamentals.load())
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"status": "unavailable", "error": str(exc)})


@router.get("/api/readiness")
def api_readiness() -> JSONResponse:
    """Forward-proof readiness — which thresholds have crossed (calibration left cold-start,
    cross-sectional IC now statistically honest, shadow books first resolved) + the persistent
    alerts the daily build records. Drives the dashboard's 'go look now' banner."""
    try:
        import bot  # noqa: F401
        from portfolio import readiness
        return JSONResponse({"status": readiness.status(), "alerts": readiness.alerts()})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"status": {}, "alerts": [], "error": str(exc)})


@router.get("/api/macro")
def api_macro() -> JSONResponse:
    """Key macro reads for the current regime: yield-curve regime, real 10y yield,
    DXY, VIX. Curve/real-yield come from the macro engine's rate-transmission state
    (calibrated, EN/ZH labelled); DXY/VIX levels + 1d change from the engine price
    store. Levels are EOD-grade (indices aren't on the delayed equity-snapshot key)."""
    out: dict[str, Any] = {"as_of": None, "yield_curve": None, "real_yield": None,
                           "dxy": None, "vix": None}
    # --- engine rate-transmission state (curve + real yields) ---
    try:
        regime = json.loads((_macro_data() / "regime" / "latest.json").read_text())
        out["as_of"] = regime.get("date")
        rates = (((regime.get("rate_inflation_transmission") or {}).get("state") or {})
                 .get("rates") or {})
        if rates:
            out["yield_curve"] = {
                "regime": rates.get("regime"),
                "direction": rates.get("direction"),
                "curve_2s10s": rates.get("curve_2s10s"),
                "nominal_10y": rates.get("nominal_10y"),
                "policy_gap": rates.get("policy_gap"),
                "label_en": (rates.get("label") or {}).get("en"),
                "label_zh": (rates.get("label") or {}).get("zh"),
            }
            out["real_yield"] = {
                "real_10y": rates.get("real_10y"),
                "pctile": rates.get("real_10y_pctile"),
                "chg_63d_bp": rates.get("real_10y_chg_63d_bp"),
                "regime": rates.get("regime"),
                "direction": rates.get("direction"),
            }
    except Exception:
        pass

    # --- DXY + VIX levels + 1d change from the engine yahoo store ---
    def _level_chg(ticker: str) -> dict | None:
        try:
            import bot  # noqa: F401  -> vendor/macro on sys.path
            from lib import store
            df = store.read("yahoo", ticker)
            if df is None or "close" not in getattr(df, "columns", []):
                return None
            s = df["close"].astype(float).dropna()
            if len(s) == 0:
                return None
            last = float(s.iloc[-1])
            prev = float(s.iloc[-2]) if len(s) > 1 else last
            return {"value": round(last, 2),
                    "chg": round(last - prev, 2),
                    "chg_pct": round((last / prev - 1) * 100, 2) if prev else 0.0}
        except Exception:
            return None

    dxy = _level_chg("DX-Y.NYB")
    vix = _level_chg("^VIX")
    if dxy:
        out["dxy"] = dxy
    if vix:
        # plain-English vol regime band
        v = vix["value"]
        band = ("calm" if v < 15 else "normal" if v < 20
                else "elevated" if v < 30 else "stress")
        vix["band"] = band
        out["vix"] = vix
    return JSONResponse(out)


# ---------------------------------------------------------------------------
# Brain Log (activity) localisation
# ---------------------------------------------------------------------------
# The activity feed assembles its English strings by interpolating tickers,
# weights and free-text theses, so a pure translation cache can't cover them.
# Instead each event carries a parallel zh title/detail: the STRUCTURED parts
# (verbs, sleeves, quadrant names, scaffolding) are mapped deterministically here
# while tickers/numbers stay verbatim; the FREE-TEXT parts (decision thesis,
# research note title/body) fall back to cached_zh(). The client picks the _zh
# variant only when zh is toggled, so English never regresses.
_TRADE_VERB_ZH = {
    "buy": "买入", "sell": "卖出", "add": "加仓", "trim": "减仓",
    "open": "开仓", "close": "平仓", "reduce": "减仓", "increase": "加仓",
    "exit": "退出", "hold": "持有", "rebalance": "再平衡", "cover": "回补",
}
_SLEEVE_ZH = {
    "conviction": "信念", "mechanical": "机械", "tactical": "战术",
    "hedge": "对冲", "core": "核心", "satellite": "卫星", "liquidity": "流动性",
}
_DECISION_LEAN_ZH = {
    "buy": "买入", "sell": "卖出", "add": "加仓", "trim": "减仓",
    "watch": "观察", "hold": "持有", "avoid": "回避", "exit": "退出",
    "reduce": "减仓", "increase": "加仓",
}
_QUAD_ZH = {
    "Goldilocks": "黄金区间", "Reflation": "再通胀", "Stagflation": "滞胀",
    "Growth-Scare": "增长恐慌", "Growth Scare": "增长恐慌", "Deflation": "通缩",
}


@router.get("/api/activity")
def api_activity() -> JSONResponse:
    """Reverse-chronological activity timeline (cap 60).

    Assembles events from:
      - positions_ledger history entries  (kind "trade")
      - decisions[] in latest.json        (kind "decision")
      - research note files               (kind "research")
      - runs table via latest.json as_of  (kind "run")

    Each event also carries title_zh / detail_zh so the Brain Log renders in
    Chinese when zh is toggled (see the localisation maps above).
    """
    events: list[dict] = []

    # --- trades from ledger history ---
    try:
        ledger_path = _data() / "portfolio" / "positions_ledger.json"
        if ledger_path.exists():
            ledger = json.loads(ledger_path.read_text())
            for key, entry in ledger.items():
                for h in entry.get("history", []):
                    ts = h.get("ts") or ""
                    ev = h.get("event", "")
                    ticker = entry.get("ticker", key.split(":")[-1])
                    weight = h.get("weight")
                    w_str = f" ({round((weight or 0)*100, 1)}%)" if weight is not None else ""
                    sleeve = entry.get("sleeve", "")
                    still_open = entry.get("still_open")
                    verb_zh = _TRADE_VERB_ZH.get(ev.lower(), ev.upper())
                    sleeve_zh = _SLEEVE_ZH.get(sleeve.lower(), sleeve)
                    events.append({
                        "ts": ts,
                        "kind": "trade",
                        "title": f"{ev.upper()} {ticker}{w_str}",
                        "title_zh": f"{verb_zh} {ticker}{w_str}",
                        "detail": (
                            f"{sleeve} sleeve | "
                            f"{'open' if still_open else 'closed'}"
                        ),
                        "detail_zh": (
                            f"{sleeve_zh}组合 | "
                            f"{'持有中' if still_open else '已平仓'}"
                        ),
                    })
    except Exception:
        pass

    # --- decisions from latest.json ---
    try:
        portfolio_path = _data() / "portfolio" / "latest.json"
        if portfolio_path.exists():
            portfolio = json.loads(portfolio_path.read_text())
            asof = portfolio.get("as_of", "")
            for d in portfolio.get("decisions", []):
                lean = d.get("lean", "watch")
                subject = d.get("subject", "?")
                thesis = d.get("thesis") or ""
                lean_zh = _DECISION_LEAN_ZH.get(lean.lower(), lean.upper())
                thesis_zh = _cached_zh(thesis) or thesis
                events.append({
                    "ts": d.get("logged_at") or asof or "",
                    "kind": "decision",
                    "title": f"Decision: {lean.upper()} {subject}",
                    "title_zh": f"决策：{lean_zh} {subject}",
                    "detail": thesis[:200],
                    "detail_zh": thesis_zh[:200],
                })
            # top-level run event
            if asof:
                regime = (portfolio.get("regime") or {})
                quad = regime.get("quad_name") or regime.get("quad")
                quad_zh = _QUAD_ZH.get(quad, quad)
                gross_pct = portfolio.get("gross", 0) * 100
                cash_pct = portfolio.get("cash", 0) * 100
                events.append({
                    "ts": asof,
                    "kind": "run",
                    "title": f"Book rebuilt — {asof}",
                    "title_zh": f"组合重建 — {asof}",
                    "detail": (
                        f"Quad: {quad} | "
                        f"gross={gross_pct:.1f}% cash={cash_pct:.1f}%"
                    ),
                    "detail_zh": (
                        f"象限：{quad_zh} | "
                        f"总敞口={gross_pct:.1f}% 现金={cash_pct:.1f}%"
                    ),
                })
    except Exception:
        pass

    # --- research notes (deduped on title+body, newest kept) ---
    try:
        notes_dir = _data() / "research" / "notes"
        if notes_dir.exists():
            parsed = [n for n in (_parse_note(p) for p in notes_dir.glob("*.md"))
                      if n and n.get("date")]
            parsed.sort(key=lambda n: n["_sort_key"], reverse=True)
            seen: set[tuple[str, str]] = set()
            for note in parsed:
                key = (note["title"], note["body_md"])
                if key in seen:
                    continue
                seen.add(key)
                tickers_str = (", ".join(note["tickers"]) if note.get("tickers") else "")
                title = note["title"]
                body_md = note.get("body_md") or ""
                title_zh = _cached_zh(title) or title
                body_zh = _cached_zh(body_md) or body_md
                events.append({
                    "ts": note["date"],
                    "kind": "research",
                    "title": title,
                    "title_zh": title_zh,
                    "detail": (
                        (f"Tickers: {tickers_str} | " if tickers_str else "")
                        + body_md[:160]
                    ),
                    "detail_zh": (
                        (f"标的：{tickers_str} | " if tickers_str else "")
                        + body_zh[:160]
                    ),
                })
    except Exception:
        pass

    # flag actions logged today so the Brain Log can highlight + tag them "new".
    # Wall-clock date is the trading-day source of truth here (matches the data's as_of).
    today_iso = date.today().isoformat()
    for e in events:
        e["today"] = (e.get("ts") or "")[:10] == today_iso

    # sort newest first, cap at 60
    events.sort(key=lambda e: e.get("ts") or "", reverse=True)
    return JSONResponse(events[:60])




@router.get("/api/runs")
def api_runs() -> JSONResponse:
    """List all run-log entries, newest first.
    Each entry: {run_id, ts, kind, title, n_steps, cost_usd, summary}.

    title_zh / summary_zh carry the cached Chinese translation of the run's AI write-up so
    the Brain Activity ("Full Trace") log renders in Chinese when zh is toggled. cached_zh()
    is a pure cache lookup (returns None -> client falls back to English) until
    brain.translate.translate_runs() warms the cache on the daily run; English never regresses.
    """
    try:
        from brain import runlog
        runs = runlog.list_runs()
        for r in runs:
            tz = _cached_zh(r.get("title") or "")
            if tz:
                r["title_zh"] = tz
            sz = _cached_zh(r.get("summary") or "")
            if sz:
                r["summary_zh"] = sz
        return JSONResponse(runs)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/runlog")
def api_runlog(run_id: str | None = None) -> JSONResponse:
    """Return the complete granular step-trace for a run.
    Pass ?run_id=ID or omit for the most recent run.
    Returns {run_id, ts, kind, title, steps: [{ts, type, title, detail, ...}]}."""
    try:
        from brain import runlog
        return JSONResponse(runlog.read_run(run_id or None))
    except Exception as exc:
        return JSONResponse({"run_id": run_id, "steps": [], "error": str(exc)}, status_code=500)


@router.get("/api/competitors")
def api_competitors() -> JSONResponse:
    base = _data() / "quiver"
    strategies: list[dict[str, Any]] = []
    strategy_names = ["chatgpt_enhanced", "claude_enhanced", "chatgpt_standard", "claude_standard"]
    for name in strategy_names:
        d = _latest_quiver(base / name)
        if d is None:
            continue
        holdings = d.get("holdings", [])
        top_holdings = [h["ticker"] for h in holdings[:8]]
        top_picks = d.get("top_picks", [])
        strategies.append({
            "strategy": d.get("strategy", name),
            "slug": d.get("slug", name),
            "model": d.get("model", ""),
            "scraped": d.get("scraped", ""),
            "metrics": d.get("metrics", {}),
            "n_holdings": len(holdings),
            "top_holdings": top_holdings,
            "n_top_picks": len(top_picks),
        })

    note = (
        "Quiver benchmark strategies: claude_* runs claude-haiku-4-5 (mini-tier), "
        "chatgpt_* runs gpt-5.4 — both momentum/single-factor rotation. "
        "Mastermind gates size by multi-side confluence, hard vetoes (parabolic/distress/cycle-blocked), "
        "and a falsifiable scorecard ledger — never auto-executes."
    )
    return JSONResponse({"strategies": strategies, "note": note})


# ---------------------------------------------------------------------------
# DESK observability — surface the multi-seat Flagship "desk" artifacts the
# committee/gate/risk machinery already writes to disk. STRICTLY READ-ONLY and
# ADDITIVE: every endpoint is wrapped try/except → JSONResponse, never raises,
# and degrades to {"status": "building"} / [] when an artifact is absent.
#
# Artifact tree (relative to the project data/ dir):
#   committee/<asof>/_FLAGSHIP/strategist.json   — the macro strategist verdict
#   committee/<asof>/<TICKER>/{forge,sentinel,nexus}.json — per-name committee
#   gate_officer/<asof>/decisions.json           — portfolio gate decisions
#   risk_officer/<asof>/decisions.json           — portfolio risk decisions
#   portfolios/flagship/watchlist.jsonl          — parked (withheld) names
#   brain/{calibration,reputation}.json,
#   brain/attribution/_rollup.json, brain/cio/<week>.{json,md} — per-seat scorecard
# ---------------------------------------------------------------------------

def _committee_dir() -> Path:
    return _data() / "committee"


def _gate_officer_dir() -> Path:
    return _data() / "gate_officer"


def _risk_officer_dir() -> Path:
    return _data() / "risk_officer"


def _macro_risk_dir() -> Path:
    return _data() / "macro_risk"


def _is_date_dir(name: str) -> bool:
    """A committee/<asof> dir name is an ISO date (YYYY-MM-DD)."""
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", name or ""))


def _latest_asof(*roots: Path) -> str | None:
    """The most-recent ISO-date subdir name across one or more artifact roots ({} → None)."""
    dates: set[str] = set()
    for root in roots:
        try:
            for p in root.iterdir():
                if p.is_dir() and _is_date_dir(p.name):
                    dates.add(p.name)
        except Exception:  # noqa: BLE001
            continue
    return max(dates) if dates else None


def _read_json(path: Path) -> dict[str, Any]:
    """Parse a JSON file → dict; {} on any failure (missing / malformed)."""
    try:
        if path.exists():
            d = json.loads(path.read_text())
            return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}
    return {}


@router.get("/api/desk/strategist")
def api_desk_strategist(asof: str = "") -> JSONResponse:
    """The macro STRATEGIST verdict for `asof` (default: most-recent available) — the confirmed
    themes (with member names + why), the backdrop stance, the crowding flags, the emerging
    watch-list and the rationale. 'building' until the desk has written a strategist artifact."""
    try:
        asof = (asof or "")[:10]
        if not (asof and _is_date_dir(asof)):
            asof = _latest_asof(_committee_dir())
        if not asof:
            return JSONResponse({"status": "building", "asof": None})
        j = _read_json(_committee_dir() / asof / "_FLAGSHIP" / "strategist.json")
        verdict = j.get("verdict") or {}
        if not verdict:
            return JSONResponse({"status": "building", "asof": asof})
        inp = j.get("input") or {}
        # bound payloads: cap the per-theme member list + the emerging/crowding arrays
        themes = []
        for t in (verdict.get("confirmed_themes") or [])[:20]:
            themes.append({
                "theme": t.get("theme"),
                "stage": t.get("stage"),
                "leadership": t.get("leadership"),
                "names": (t.get("names") or [])[:12],
                "why": (t.get("why") or "")[:600],
            })
        return JSONResponse({
            "status": "scoring",
            "asof": asof,
            "regime": {k: (inp.get("regime") or {}).get(k) for k in
                       ("quad", "quad_name", "liquidity_overlay", "cycle_tag")},
            "confirmed_themes": themes,
            "backdrop_stance": verdict.get("backdrop_stance"),
            "supportive": verdict.get("supportive"),
            "watch_emerging": (verdict.get("watch_emerging") or [])[:12],
            "crowding_flags": (verdict.get("crowding_flags") or [])[:12],
            "rationale": (verdict.get("rationale") or "")[:1600],
            "calibration_multiplier": verdict.get("calibration_multiplier"),
        })
    except Exception as exc:  # noqa: BLE001 — never raise; degrade to building
        return JSONResponse({"status": "building", "asof": None, "error": str(exc)})


@router.get("/api/desk/decisions")
def api_desk_decisions(asof: str = "") -> JSONResponse:
    """The per-name desk decision log for `asof` (default: most-recent) — one readable row per name:
    what FORGE confirmed, SENTINEL's stance, NEXUS's action, the Gate Officer's action, and the
    realized risk action, each joined from the committee + gate + risk artifacts. 'building' when no
    committee dir exists for the date."""
    try:
        asof = (asof or "")[:10]
        if not (asof and _is_date_dir(asof)):
            asof = _latest_asof(_committee_dir(), _gate_officer_dir(), _risk_officer_dir())
        if not asof:
            return JSONResponse({"status": "building", "asof": None, "decisions": []})

        # Gate + Risk per-name decisions (keyed by ticker) for the date
        def _by_ticker(root: Path) -> dict[str, dict]:
            j = _read_json(root / asof / "decisions.json")
            out: dict[str, dict] = {}
            for dec in ((j.get("result") or {}).get("decisions") or []):
                tk = str(dec.get("ticker") or "").upper().strip()
                if tk:
                    out[tk] = {"action": str(dec.get("action") or "").lower(),
                               "scale": dec.get("scale"),
                               "reason": (dec.get("reason") or "")[:400]}
            return out

        gate = _by_ticker(_gate_officer_dir())
        risk = _by_ticker(_risk_officer_dir())

        cdir = _committee_dir() / asof
        rows: list[dict[str, Any]] = []
        tickers: list[str] = []
        try:
            if cdir.exists():
                tickers = sorted(p.name for p in cdir.iterdir()
                                 if p.is_dir() and p.name != "_FLAGSHIP")
        except Exception:  # noqa: BLE001
            tickers = []
        # also surface gate/risk-only names (e.g. a withheld name with no committee folder)
        for tk in sorted(set(gate) | set(risk)):
            if tk not in tickers:
                tickers.append(tk)

        for tk in tickers[:120]:
            forge = _read_json(cdir / tk / "forge.json")
            sentinel = _read_json(cdir / tk / "sentinel.json")
            nexus = _read_json(cdir / tk / "nexus.json")
            g = gate.get(tk) or {}
            r = risk.get(tk) or {}
            rows.append({
                "ticker": tk,
                "forge": ({"confirmed": forge.get("confirmed"),
                           "combined": forge.get("combined"),
                           "viability": forge.get("viability"),
                           "size_mult": forge.get("size_mult")} if forge else None),
                "sentinel": ({"stance": sentinel.get("stance"),
                              "confidence": sentinel.get("confidence"),
                              "strongest_bear": (sentinel.get("strongest_bear") or "")[:400]}
                             if sentinel else None),
                "nexus": ({"action": nexus.get("action"), "scale": nexus.get("scale"),
                           "lean": nexus.get("lean"),
                           "rationale": (nexus.get("rationale") or "")[:400]} if nexus else None),
                "gate": (g or None),
                "risk": (r or None),
            })
        return JSONResponse({"status": "scoring" if rows else "building",
                             "asof": asof, "decisions": rows})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"status": "building", "asof": None, "decisions": [], "error": str(exc)})


@router.get("/api/desk/watchlist")
def api_desk_watchlist(book: str = "flagship") -> JSONResponse:
    """The parked (withheld) names for a book — the daily re-review queue. Dedups to the latest
    record per ticker, each with its reason + asof. [] when the watchlist is empty/absent. The
    module is flagship-scoped today; `book` is accepted for forward-compatibility."""
    try:
        from portfolio import watchlist
        # the re-review state machine (state + days-in-state) is the source of truth when present;
        # fall back to the append-log latest() for any parked name not yet in the snapshot so the
        # surface is never thinner than before (back-compatible).
        state_by_ticker = {}
        try:
            for s in (watchlist.state_rows() or []):
                t = (s.get("ticker") or "").upper()
                if t:
                    state_by_ticker[t] = s
        except Exception:  # noqa: BLE001
            state_by_ticker = {}
        rows = []
        for r in (watchlist.latest() or []):
            t = (r.get("ticker") or "").upper()
            s = state_by_ticker.get(t) or {}
            rows.append({"ticker": t,
                         "asof": str(r.get("asof") or "")[:10],
                         "reason": (s.get("reason") or r.get("reason") or "")[:400],
                         "combined": r.get("combined"),
                         "state": s.get("state") or "watch",
                         "days_in_state": s.get("days_in_state"),
                         "last_review": str(s.get("last_review") or "")[:10] or None})
        # surface EXPIRED names from the snapshot too (they've left the append-log's active view).
        for t, s in state_by_ticker.items():
            if s.get("state") == "expired" and not any(x["ticker"] == t for x in rows):
                rows.append({"ticker": t,
                             "asof": str(s.get("asof") or "")[:10],
                             "reason": (s.get("expire_reason") or s.get("reason") or "")[:400],
                             "combined": s.get("combined"),
                             "state": "expired",
                             "days_in_state": s.get("days_in_state"),
                             "last_review": str(s.get("last_review") or "")[:10] or None})
        return JSONResponse({"book": book, "watchlist": rows[:120]})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"book": book, "watchlist": [], "error": str(exc)})


@router.get("/api/desk/scorecard")
def api_desk_scorecard() -> JSONResponse:
    """The per-seat accountability scorecard: each seat's calibration (multiplier / reliability /
    n / status), its cumulative attributed bps (the Brinson credit rollup), and its reputation
    label — plus the latest CIO weekly note. Every input degrades independently to empty/None, so
    the payload is always valid even before any seat has cleared cold-start."""
    out: dict[str, Any] = {"seats": [], "cio": None, "status": "building"}
    # --- per-agent calibration (the spine of the table) ---
    agents: dict[str, Any] = {}
    try:
        from brain import calibration
        agents = (calibration.load() or {}).get("agents") or {}
    except Exception:  # noqa: BLE001
        agents = {}
    # --- cumulative attributed bps per seat (Brinson rollup) ---
    attr_seats: dict[str, Any] = {}
    try:
        from brain import attribution
        attr_seats = (attribution.rollup() or {}).get("seats") or {}
    except Exception:  # noqa: BLE001
        attr_seats = {}
    # --- CIO weekly review: structured per-seat reputation + the note (md + week) ---
    # Prefer the PERSISTED weekly artifact (data/brain/cio/<week>.json, written by cio.write() on the
    # daily/weekly build) — it's a single bounded file read, so the live endpoint stays snappy. Fall
    # back to a fresh, LLM-free cio.review() only when nothing has been persisted yet (which does the
    # heavier KPI scan). Either way the per-seat reputation labels + the note (md + week) are surfaced.
    rep_by_seat: dict[str, str] = {}
    cio_block: dict[str, Any] | None = None
    try:
        rep = None
        cio_dir = _data() / "brain" / "cio"
        try:
            files = sorted(cio_dir.glob("*.json"), reverse=True) if cio_dir.exists() else []
            if files:
                rep = json.loads(files[0].read_text())
        except Exception:  # noqa: BLE001
            rep = None
        if not rep:
            from brain import cio
            rep = cio.review()  # LLM-free; deterministic fallback note
        for s in (rep.get("per_seat") or []):
            if s.get("seat"):
                rep_by_seat[s["seat"]] = s.get("reputation")
        cio_block = {"week": rep.get("iso_week"),
                     "note_md": (rep.get("note_md") or "")[:8000],
                     "whats_working": rep.get("whats_working") or [],
                     "whats_broken": rep.get("whats_broken") or [],
                     "tuning_recommendations": (rep.get("tuning_recommendations") or [])[:12]}
    except Exception:  # noqa: BLE001
        cio_block = None

    # the seats we surface, in desk reading order (calibration keys); only emit those that exist
    # in at least one source so the table stays honest, but always include the core six.
    _order = ["strategist", "forge", "sentinel", "nexus", "gate", "risk",
              "pm", "timing", "autonomous", "heavyweight", "china", "hk"]
    seats: list[dict[str, Any]] = []
    try:
        keys = [k for k in _order if k in agents or k in attr_seats or k in rep_by_seat]
        # append any unexpected calibration agents not in our order list (forward-compatible)
        for k in agents:
            if k not in keys:
                keys.append(k)
        for k in keys:
            cal = agents.get(k) or {}
            ab = attr_seats.get(k) or {}
            seats.append({
                "seat": k,
                "status": cal.get("status"),
                "multiplier": cal.get("multiplier"),
                "reliability": cal.get("reliability"),
                "n": cal.get("n"),
                "attributed_bps": ab.get("attributed_bps"),
                "attributed_n": ab.get("n"),
                "reputation": rep_by_seat.get(k),
            })
    except Exception:  # noqa: BLE001
        seats = []
    out["seats"] = seats
    out["cio"] = cio_block
    out["status"] = "scoring" if seats else "building"
    # --- nightly per-book Opus-cost spend vs the tripwire cap (OFF/unlimited by default) ---
    try:
        from brain import cost_guard
        out["cost"] = cost_guard.summary()
    except Exception:  # noqa: BLE001 — additive; never break the scorecard
        out["cost"] = None
    return JSONResponse(out)


@router.get("/api/desk/macro-risk")
def api_desk_macro_risk(asof: str = "") -> JSONResponse:
    """The MACRO RISK OFFICER read for `asof` (default: most-recent, else computed LIVE). The top-down
    DEFENSE state the desk lacked on 2026-06-23: the deterministic RISK STATE (risk_on/caution/risk_off)
    + per-axis fragility (vol / credit-USD / liquidity / crowding / dealer-gamma), the leading-edge
    fragile DRIVER chains, the hard gross cap + add-block (the teeth), the driver-aware defensive tilt,
    and the falsifier. Reads the persisted artifact; falls back to a fresh deterministic compute (no
    LLM) so the card is never blank. Never 500s."""
    def _shape(st: dict) -> dict:
        st = st or {}
        return {
            "asof": st.get("asof"),
            "state": st.get("state"),
            "fragility": st.get("fragility"),
            "gross_cap": st.get("gross_cap"),
            "allow_adds": st.get("allow_adds"),
            "axes": st.get("axes") or {},
            "signals": (st.get("signals") or [])[:10],
            "drivers": (st.get("drivers") or [])[:8],
            "hot_tickers": (st.get("hot_tickers") or [])[:12],
            "defensive_tilt": st.get("defensive_tilt") or {},
            "falsifier": (st.get("falsifier") or "")[:600],
            "check_by": st.get("check_by"),
            "rationale": (st.get("rationale") or "")[:1600],
            "lead_driver": (st.get("lead_driver") or "")[:300],
        }
    try:
        asof = (asof or "")[:10]
        if not (asof and _is_date_dir(asof)):
            asof = _latest_asof(_macro_risk_dir())
        if asof:
            j = _read_json(_macro_risk_dir() / asof / "state.json")
            st = j.get("state") or {}
            if st:
                return JSONResponse({"status": "scoring", **_shape(st)})
        # nothing persisted yet — compute the deterministic state LIVE (no LLM) so the card renders
        from brain import macro_risk
        reg = _read_json(Path(_data().parent / "vendor" / "macro" / "data" / "regime" / "latest.json")) \
            if (_data().parent / "vendor" / "macro" / "data" / "regime" / "latest.json").exists() else {}
        st = macro_risk.risk_state(reg.get("date") or "", reg or None)
        return JSONResponse({"status": "scoring" if st.get("state") else "building", **_shape(st)})
    except Exception as exc:  # noqa: BLE001 — never raise; degrade to building
        return JSONResponse({"status": "building", "state": None, "error": str(exc)})


@router.get("/api/desk/firm-exposure")
def api_desk_firm_exposure() -> JSONResponse:
    """READ-ONLY firm-level cross-book exposure monitor — where the independent books (flagship,
    heavyweight, US/CN/HK/ETF Brains) have piled into the SAME names / sectors. Surfaces the flagged
    concentrations + the top firm-wide exposures + a sector rollup. A MONITOR only: it never changes
    any allocation or trades. Degrades to an honest empty payload; never 500s."""
    try:
        from portfolio import firm_exposure
        return JSONResponse(firm_exposure.summary())
    except Exception as exc:  # noqa: BLE001 — never raise; degrade to an honest stub
        return JSONResponse({"as_of": None, "books": [], "n_books": 0, "top_exposures": [],
                             "flags": [], "by_sector": {}, "by_chain": {}, "thresholds": {},
                             "currency_clean": False, "note": f"Firm exposure unavailable: {exc}"})

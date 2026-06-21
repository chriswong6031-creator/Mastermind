"""Dashboard web routes — GET / serves the static dashboard; /api/* expose
the data contracts the page JS fetches at runtime.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, Response

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


def _account_tickers(portfolio_id: str | None = None) -> list[str]:
    """Tickers currently held in a paper account (for live marks)."""
    try:
        acct = json.loads((_portfolio_dir(portfolio_id) / "account.json").read_text())
        return list((acct.get("positions") or {}).keys())
    except Exception:
        return []


def _live_prices(tickers: list[str]) -> dict[str, float]:
    """Live (delayed) quotes for `tickers`; {} if the Polygon layer is unavailable."""
    try:
        from data_layer import polygon
        return {k: v for k, v in polygon.quotes(tickers).items() if v is not None}
    except Exception:
        return {}


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
        payload = paper_account.performance(portfolio_id=portfolio)
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
            prices = _live_prices(_account_tickers(portfolio))
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

        return JSONResponse(payload)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/portfolios")
def api_portfolios() -> JSONResponse:
    """The set of portfolios the dashboard switches between, each with a quick status
    (NAV, return, vs-SPY, holdings) for the tab labels. The flagship is the gated engine
    book; the autonomous book is managed free-form by the Opus Brain."""
    from portfolio import paper_account, registry
    out = []
    for meta in registry.all_portfolios():
        pid = meta["id"]
        status: dict[str, Any] = {"nav": None, "total_return_pct": None, "vs_spy_pct": None,
                                  "day_change_pct": None, "holdings": 0, "cash_pct": None, "as_of": None}
        try:
            perf = paper_account.performance(portfolio_id=pid)
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
        out.append({**{k: meta.get(k) for k in ("id", "name", "tagline", "kind", "manager")},
                    "status": status})
    return JSONResponse({"portfolios": out, "default": registry.DEFAULT_ID})


@router.get("/api/decisions")
def api_decisions(portfolio: str = "autonomous", limit: int = 60) -> JSONResponse:
    """The autonomous Brain's daily decision log — what it bought / sold / held each day, and
    the rationale for every holding. Empty for the gated flagship (it journals via research papers)."""
    try:
        if portfolio != "autonomous":
            return JSONResponse({"decisions": [], "note": "decision log is autonomous-only"})
        from bot import autonomous
        return JSONResponse({"decisions": autonomous.load_decisions(limit)})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"decisions": [], "error": str(exc)})


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
        from portfolio import market_calendar, paper_account, position_log, trade_history
        prices = _live_prices(_account_tickers(portfolio))
        return JSONResponse({
            "open": position_log.open_positions(portfolio_id=portfolio),
            "closed": position_log.closed_positions(portfolio_id=portfolio),
            "history": trade_history.history(prices, portfolio_id=portfolio),
            # PENDING orders queued while the market is closed — fill at next open
            "pending": paper_account.load_pending(portfolio),
            "market": market_calendar.status(),
        })
    except Exception as exc:
        return JSONResponse({"open": [], "closed": [], "history": [],
                             "pending": [], "market": {}, "error": str(exc)})


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

    # sort newest first, cap at 60
    events.sort(key=lambda e: e.get("ts") or "", reverse=True)
    return JSONResponse(events[:60])




@router.get("/api/runs")
def api_runs() -> JSONResponse:
    """List all run-log entries, newest first.
    Each entry: {run_id, ts, kind, title, n_steps, cost_usd, summary}."""
    try:
        from brain import runlog
        return JSONResponse(runlog.list_runs())
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

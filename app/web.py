"""Dashboard web routes — GET / serves the static dashboard; /api/* expose
the data contracts the page JS fetches at runtime.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

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


def _account_tickers() -> list[str]:
    """Tickers currently held in the paper account (for live marks)."""
    try:
        acct = json.loads((_data() / "portfolio" / "account.json").read_text())
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

@router.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(_STATIC / "index.html", media_type="text/html")


@router.get("/research", include_in_schema=False)
def research_page() -> FileResponse:
    """The Research page — same SPA; the client opens the Research view from this path."""
    return FileResponse(_STATIC / "index.html", media_type="text/html")


@router.get("/theme.css", include_in_schema=False)
def theme_css() -> FileResponse:
    """Serve the macro design-system stylesheet the dashboard links."""
    return FileResponse(_STATIC / "theme.css", media_type="text/css")


@router.get("/theme.js", include_in_schema=False)
def theme_js() -> FileResponse:
    """Serve the macro theme toggle script (optional; dark renders without it)."""
    return FileResponse(_STATIC / "theme.js", media_type="application/javascript")


@router.get("/api/performance")
def api_performance() -> JSONResponse:
    """Equity curve and performance summary for the $1M paper account."""
    try:
        from portfolio import paper_account
        payload = paper_account.performance()
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
def api_portfolio() -> JSONResponse:
    path = _data() / "portfolio" / "latest.json"
    if not path.exists():
        return JSONResponse({"error": "no book yet"}, status_code=404)
    try:
        payload = json.loads(path.read_text())

        # ------------------------------------------------------------------
        # Live marks: attach current price + unrealized P&L to each position
        # (Polygon delayed quotes via the account's avg-cost lots). Degrades to
        # nulls offline so the client always renders an honest dash.
        # ------------------------------------------------------------------
        try:
            from portfolio import paper_account
            prices = _live_prices(_account_tickers())
            pnl = paper_account.positions_pnl(prices) if prices else {}
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
            "engine_score": rb.get("engine_score"),
            "research_score": p.get("research_score"),
            "combined": rb.get("combined"),
            "confirmed": rb.get("confirmed"),
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
            "key_risks": p.get("key_risks") or [],
        })
    return JSONResponse({"papers": out})


@router.get("/api/trades")
def api_trades() -> JSONResponse:
    """Open/closed position summaries PLUS a complete per-fill blotter (`history`):
    every individual buy/sell, with realized P&L on sells and live unrealized P&L
    on still-open buy remainders."""
    try:
        from portfolio import position_log, trade_history
        prices = _live_prices(_account_tickers())
        return JSONResponse({
            "open": position_log.open_positions(),
            "closed": position_log.closed_positions(),
            "history": trade_history.history(prices),
        })
    except Exception as exc:
        return JSONResponse({"open": [], "closed": [], "history": [], "error": str(exc)})


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


@router.get("/api/activity")
def api_activity() -> JSONResponse:
    """Reverse-chronological activity timeline (cap 60).

    Assembles events from:
      - positions_ledger history entries  (kind "trade")
      - decisions[] in latest.json        (kind "decision")
      - research note files               (kind "research")
      - runs table via latest.json as_of  (kind "run")
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
                    events.append({
                        "ts": ts,
                        "kind": "trade",
                        "title": f"{ev.upper()} {ticker}{w_str}",
                        "detail": (
                            f"{entry.get('sleeve', '')} sleeve | "
                            f"{'open' if entry.get('still_open') else 'closed'}"
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
                events.append({
                    "ts": d.get("logged_at") or asof or "",
                    "kind": "decision",
                    "title": f"Decision: {d.get('lean', 'watch').upper()} {d.get('subject', '?')}",
                    "detail": (d.get("thesis") or "")[:200],
                })
            # top-level run event
            if asof:
                regime = (portfolio.get("regime") or {})
                events.append({
                    "ts": asof,
                    "kind": "run",
                    "title": f"Book rebuilt — {asof}",
                    "detail": (
                        f"Quad: {regime.get('quad_name') or regime.get('quad')} | "
                        f"gross={portfolio.get('gross', 0)*100:.1f}% "
                        f"cash={portfolio.get('cash', 0)*100:.1f}%"
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
                events.append({
                    "ts": note["date"],
                    "kind": "research",
                    "title": note["title"],
                    "detail": (
                        (f"Tickers: {tickers_str} | " if tickers_str else "")
                        + (note.get("body_md") or "")[:160]
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

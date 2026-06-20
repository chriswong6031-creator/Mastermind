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


@router.get("/api/portfolio")
def api_portfolio() -> JSONResponse:
    path = _data() / "portfolio" / "latest.json"
    if not path.exists():
        return JSONResponse({"error": "no book yet"}, status_code=404)
    try:
        payload = json.loads(path.read_text())
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
        # strip internal sort key, cap at 30
        out = [{k: v for k, v in n.items() if k != "_sort_key"} for n in deduped[:30]]
        return JSONResponse(out)
    except Exception:
        return JSONResponse([])


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

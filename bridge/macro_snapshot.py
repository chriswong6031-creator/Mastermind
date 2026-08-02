"""Macro-Dashboard snapshot bridge — export a static, point-in-time mirror of the
Mastermind dashboard for the public Macro Dashboard (GitHub Pages) to render.

The Macro daily workflow runs on GitHub-hosted runners and CANNOT reach this local
server, so the snapshot can only flow ONE way: this module assembles a single
self-contained JSON for every published book and writes it into the macro repo's
``site/mastermind/`` tree (reached through the ``vendor/macro`` symlink). A macro-side
page (``site/mastermind.html`` + ``mastermind.js``) fetches that JSON client-side.

This reuses the SAME code that backs the live ``/api/*`` endpoints (``portfolio.registry``
+ each book's ``latest.json`` + ``portfolio.paper_account.performance``) so the snapshot
matches the live dashboard — no duplicated logic. See ``bridge/build_portfolio.py`` for
the contract this complements.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import bot  # noqa: F401  -> vendor/macro bootstrap (sys.path + .env)

_ROOT = Path(__file__).resolve().parent.parent

SCHEMA = "mastermind_snapshot.v1"
LIVE_URL = "http://localhost:8000"

# Keep the published JSON lean: the live "Evaluated — Not Held" panel shows the full
# rejected list, but the static snapshot only needs a representative head + the count.
_REJECTED_HEAD = 20

# RUL-CL-6b BRIDGE law (macro repo config/ruling_graph.yml site_privacy_tokens) bans
# held-book/fill economics on any public site/ path, and the macro FB-R13 guard
# (scripts/check_private_boundary.py) key-scans the shipped snapshot on every CI run.
# The public snapshot shows the book's composition and reasoning — tickers, weights,
# verdicts, theses, lifecycle metadata — never fill economics (cost basis, entry/current
# prices, dollar values, share counts). Those stay on the authenticated live dashboard.
# WHITELIST, not blacklist, so upstream latest.json additions stay private by default.
# (37 cost_basis keys sat on the public macro main for weeks before macro PR #4209
# surfaced them, 2026-08-01 — the projection below is the source-side fix.)
_PUBLIC_POSITION_KEYS = (
    "ticker", "name", "theme_id", "sleeve", "group", "venue", "stage",
    "weight", "verdict", "conviction", "rationale", "rs_pctile",
    "opened_at", "held_days", "pending",
    "thesis_id", "thesis_full", "research", "confluence", "size_stage",
    "retained", "time_stop_by", "entry_levels",
)

# Pending orders queue real share counts (portfolio/paper_account.py); only the
# intent is public.
_PUBLIC_PENDING_ORDER_KEYS = (
    "ticker", "name", "side", "status", "sleeve", "verdict", "note",
)


def _project(rows: Any, keys: tuple[str, ...]) -> list[dict]:
    """Project a list of dicts onto a public key whitelist (RUL-CL-6b)."""
    out: list[dict] = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            out.append({k: row[k] for k in keys if k in row})
    return out


def _default_dest() -> Path:
    """The macro repo's site/ dir, reached via the vendor/macro symlink (or pinned clone)."""
    return _ROOT / "vendor" / "macro" / "site"


def _performance(portfolio_id: str) -> dict:
    """Equity curve + return summary for a book; safe-empty on any error."""
    try:
        from portfolio import paper_account
        return paper_account.performance(portfolio_id=portfolio_id)
    except Exception as exc:  # never let one book sink the snapshot
        return {"series": [], "note": f"performance unavailable: {exc}"}


def _book(portfolio_id: str) -> dict | None:
    """Assemble one book's snapshot entry, or None if it has no published latest.json
    (e.g. the self-directed book uses a different contract)."""
    try:
        from portfolio import registry
    except Exception:
        return None
    meta = registry.get(portfolio_id)
    latest_path = registry.data_dir(portfolio_id) / "latest.json"
    if not latest_path.exists():
        return None
    try:
        latest = json.loads(latest_path.read_text())
    except Exception:
        return None

    rejected = latest.get("rejected") or []
    return {
        "id": portfolio_id,
        "name": meta.get("name", portfolio_id),
        "tagline": meta.get("tagline", ""),
        "kind": meta.get("kind", ""),
        "manager": meta.get("manager", ""),
        "benchmark": meta.get("benchmark", "SPY"),
        "as_of": latest.get("as_of"),
        "market_status": latest.get("market_status"),
        "gross": latest.get("gross"),
        "cash": latest.get("cash"),
        "regime": latest.get("regime"),
        "sleeves": latest.get("sleeves"),
        "positions": _project(latest.get("positions"), _PUBLIC_POSITION_KEYS),
        "decisions": latest.get("decisions") or [],
        "pending_orders": _project(latest.get("pending_orders"), _PUBLIC_PENDING_ORDER_KEYS),
        "research_held": latest.get("research_held") or [],
        "track_record": latest.get("track_record"),
        "rejected": rejected[:_REJECTED_HEAD],
        "rejected_count": len(rejected),
        "disclaimer": latest.get("disclaimer"),
        "zh": latest.get("zh"),
        "performance": _performance(portfolio_id),
    }


def build() -> dict:
    """Return the full mastermind_snapshot.v1 payload across every published book."""
    try:
        from portfolio import registry
        ids = registry.ids()
        default_book = registry.DEFAULT_ID
    except Exception:
        ids, default_book = ["flagship"], "flagship"

    books = [b for b in (_book(pid) for pid in ids) if b is not None]
    # Default to the flagship if present, else the first book that published.
    if not any(b["id"] == default_book for b in books) and books:
        default_book = books[0]["id"]

    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "default_book": default_book,
        "live_url": LIVE_URL,
        "books": books,
        "disclaimer": "Paper-only / display-only. Accountability, not alpha. Not investment advice.",
        "zh": {"disclaimer": "仅纸面/仅展示；问责而非阿尔法。非投资建议。"},
    }


def write(dest_site_dir: Path | str | None = None) -> Path:
    """Build the snapshot and write it to ``<dest>/mastermind/mastermind_snapshot.json``.

    Also writes ``<dest>/mastermind/nw_feedback.json`` best-effort (never-raise) as a
    sibling artifact for NW integration (bridge/nw_feedback.py, schema mastermind_nw_feedback.v1).

    ``dest_site_dir`` defaults to the macro repo's ``site/`` via the vendor symlink.
    Returns the written path (snapshot only; feedback path is a side-effect).
    """
    dest = Path(dest_site_dir) if dest_site_dir else _default_dest()
    out_dir = dest / "mastermind"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "mastermind_snapshot.json"
    payload = build()
    out.write_text(json.dumps(payload, indent=2, default=str, ensure_ascii=False))

    # NW feedback artifact — best-effort, never allowed to fail the snapshot write.
    try:
        from bridge import nw_feedback as _nwf
        _nwf.write(dest)
    except Exception as _exc:  # noqa: BLE001
        pass  # feedback write failure is non-fatal; snapshot already written above

    return out

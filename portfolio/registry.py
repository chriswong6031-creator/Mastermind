"""Portfolio registry — the single source of truth for the books Mastermind manages.

Mastermind began as ONE paper book (the engine-gated *flagship*). It now harnesses
MULTIPLE independent portfolios, each with its own NAV / equity curve / blotter,
surfaced behind a tab switcher in the dashboard. A new portfolio TYPE is added by
appending an entry to ``PORTFOLIOS`` here and pointing a builder at it.

Path convention (chosen to preserve back-compat):
  * ``flagship`` (the original gated book) keeps its legacy home: ``data/portfolio/``.
  * every other portfolio lives under ``data/portfolios/<id>/``.

The per-portfolio store modules (``paper_account``, ``position_log``, ``trade_history``)
and the write-back ``bridge`` resolve their state files through ``data_dir(portfolio_id)``:
``None`` or the default id → the legacy dir (so the existing test fixtures that patch the
module-global path constants keep redirecting it); any other id → ``data/portfolios/<id>``.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# The original, engine-gated book. Its id resolves to the legacy data/portfolio/ dir.
DEFAULT_ID = "flagship"

# Ordered — this drives the dashboard tab order.
PORTFOLIOS: list[dict] = [
    {
        "id": "flagship",
        "name": "Flagship",
        "tagline": "Engine-gated · doctrine-disciplined",
        "kind": "gated",          # the deterministic, research-gated, sleeve-structured book
        "manager": "engine",
        "starting_nav": 1_000_000.0,
        "legacy": True,           # state lives in data/portfolio/ (not data/portfolios/flagship)
    },
    {
        "id": "autonomous",
        "name": "Autonomous Brain",
        "tagline": "Free-form · Opus-managed · daily",
        "kind": "autonomous",     # an Opus Brain trades freely; no gate, no research paper
        "manager": "brain",
        "starting_nav": 1_000_000.0,
        "legacy": False,
    },
]

_BY_ID = {p["id"]: p for p in PORTFOLIOS}


def ids() -> list[str]:
    return [p["id"] for p in PORTFOLIOS]


def all_portfolios() -> list[dict]:
    """A copy of the registry — safe to mutate by callers."""
    return [dict(p) for p in PORTFOLIOS]


def get(portfolio_id: str | None) -> dict:
    """Metadata for a portfolio (falls back to the default if unknown/None)."""
    return dict(_BY_ID.get(portfolio_id or DEFAULT_ID, _BY_ID[DEFAULT_ID]))


def is_known(portfolio_id: str | None) -> bool:
    return (portfolio_id or DEFAULT_ID) in _BY_ID


def data_dir(portfolio_id: str | None = None) -> Path:
    """The per-portfolio state directory.

    ``flagship``/``None`` → the legacy ``data/portfolio/``; everything else →
    ``data/portfolios/<id>/`` (an unknown id is sandboxed there too, never the legacy dir).
    """
    pid = portfolio_id or DEFAULT_ID
    meta = _BY_ID.get(pid)
    if meta is not None and meta.get("legacy"):
        return _ROOT / "data" / "portfolio"
    return _ROOT / "data" / "portfolios" / pid


def starting_nav(portfolio_id: str | None = None) -> float:
    return float(get(portfolio_id).get("starting_nav", 1_000_000.0))

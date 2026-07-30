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
        "benchmark": "SPY",       # equity-curve comparison line
        "legacy": True,           # state lives in data/portfolio/ (not data/portfolios/flagship)
    },
    {
        "id": "heavyweight",
        "name": "Heavyweight",
        "tagline": "Concentrated · presses Flagship's best",
        "kind": "heavyweight",    # an Opus Brain concentrating the firm's best ideas: universe =
                                  # union of published books (flagship, autonomous, etf; self_directed
                                  # excluded per ruling R1), one-per-cluster, 5–50% rails (bot/heavyweight.py)
        "manager": "brain",
        "starting_nav": 1_000_000.0,
        "benchmark": "SPY",
        "legacy": False,
    },
    {
        "id": "autonomous",
        "name": "US Brain",
        "tagline": "Free-form · Codex-first AI · daily",
        "kind": "autonomous",     # shared-provider AI trades freely; no gate, no research paper
        "manager": "brain",
        "starting_nav": 1_000_000.0,
        "benchmark": "SPY",
        "legacy": False,
    },
    {
        "id": "etf",
        "name": "ETF Brain",
        "tagline": "US-listed ETFs only · Codex-first AI · daily · doctrine + guardrails",
        "kind": "etf_brain",      # shared-provider AI rotates across US ETFs (index/sector/factor/duration/
                                  # cash) under an ETF-adapted doctrine + hard risk guardrails (bot/etf.py).
                                  # The ETF-only allowlist is enforced in the trusted layer via
                                  # portfolio.etf_universe (not registry.venues — that gates market venue).
        "manager": "brain",
        "starting_nav": 1_000_000.0,
        "benchmark": "SPY",
        "legacy": False,
    },
    {
        "id": "china",
        "name": "CN Brain",
        "tagline": "Mainland A-shares only · Codex-first AI · daily",
        "kind": "china_brain",    # shared-provider AI over the macro China A-share desks; holds
                                  # ONLY mainland A-shares (*.SS / *.SZ), marked natively in CNY (bot/china.py).
        "manager": "brain",
        "starting_nav": 1_000_000.0,
        "benchmark": "FXI",       # iShares China Large-Cap — comparison line (marked in the book ccy)
        "currency": "CNY",        # base currency — A-shares quote CNY
        "venues": ["A-share"],    # tradeable universe: mainland A-shares (Shanghai / Shenzhen) only
        "legacy": False,
    },
    {
        "id": "hk",
        "name": "HK Brain",
        "tagline": "Hong Kong only · Codex-first AI · daily",
        "kind": "hk_brain",       # shared-provider AI over the HK buy board; holds ONLY Hong-Kong
                                  # listed names (*.HK), marked natively in HKD (no cross-FX — bot/hk.py).
        "manager": "brain",
        "starting_nav": 1_000_000.0,
        "benchmark": "FXI",       # iShares China Large-Cap — comparison line (marked in the book ccy)
        "currency": "HKD",        # base currency — HK names quote HKD, so no cross-currency conversion
        "venues": ["HK"],         # tradeable universe: Hong Kong listings only
        "legacy": False,
    },
    {
        "id": "self_directed",
        "name": "Self-Directed",
        "tagline": "Your book · you place the trades",
        "kind": "self_directed",  # YOU trade it by hand; served by portfolio.self_directed (its own
                                  # engine + state under data/portfolio/self_directed/, not paper_account)
        "manager": "you",
        "starting_nav": 1_000_000.0,
        "benchmark": "SPY",
        "legacy": False,
    },
]

# Benchmark fallback for any portfolio missing an explicit one (back-compat: the US books).
_DEFAULT_BENCHMARK = "SPY"

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


def benchmark(portfolio_id: str | None = None) -> str:
    """The equity-curve comparison symbol for a book (default 'SPY' for the US books)."""
    return str(get(portfolio_id).get("benchmark") or _DEFAULT_BENCHMARK)


def currency(portfolio_id: str | None = None) -> str:
    """The book's base/display currency ('USD' default; 'HKD' for hk, 'CNY' for china)."""
    return str(get(portfolio_id).get("currency") or "USD")


def venues(portfolio_id: str | None = None) -> list[str]:
    """The venues a book may trade — e.g. ['HK'] or ['A-share']. Empty = unrestricted (US books)."""
    return list(get(portfolio_id).get("venues") or [])

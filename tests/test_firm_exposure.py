"""Firm-level cross-book exposure MONITOR — offline tests.

Seeds synthetic published books (latest.json) into a tmp store via the registry._ROOT redirect the
other book tests use, then asserts the read-only aggregator FLAGS a name held by enough books,
leaves a single-book name unflagged, degrades to an honest stub on empty/missing books and NEVER
raises, and honours the env threshold overrides. No network, no live book is touched.
"""
from __future__ import annotations

import json

import pytest

from portfolio import firm_exposure, registry


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """Isolate per-id portfolio state to a tmp root (registry.data_dir derives off _ROOT), and point
    the module's own _ROOT at the tmp tree too (so the absent sector snapshot is honestly omitted)."""
    monkeypatch.setattr(registry, "_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(firm_exposure, "_ROOT", tmp_path, raising=False)
    return tmp_path


def _seed(pid: str, positions: list[dict], *, nav: float = 1_000_000.0,
          currency: str | None = None) -> None:
    """Write a minimal published latest.json for book `pid` into the tmp store."""
    d = registry.data_dir(pid)
    d.mkdir(parents=True, exist_ok=True)
    doc = {"schema": "portfolio.v1", "portfolio_id": pid, "as_of": "2026-06-23",
           "nav": nav, "positions": positions}
    if currency:
        doc["currency"] = currency
    (d / "latest.json").write_text(json.dumps(doc))


# --------------------------------------------------------------------------- #
# the core flag: a name held by >= N_BOOKS is flagged with the right n_books
# --------------------------------------------------------------------------- #
def test_pileup_flagged_at_three_books(iso):
    # NVDA in 3 USD books; AAPL in just 1 → only NVDA should be flagged as a pile-up.
    _seed("flagship",    [{"ticker": "NVDA", "weight": 0.10}, {"ticker": "AAPL", "weight": 0.05}])
    _seed("heavyweight", [{"ticker": "NVDA", "weight": 0.20}])
    _seed("autonomous",  [{"ticker": "NVDA", "weight": 0.06}, {"ticker": "MSFT", "weight": 0.04}])

    s = firm_exposure.summary()

    assert s["n_books"] == 3
    # NVDA is flagged, n_books == 3, and lists every holding book
    nvda = next((f for f in s["flags"] if f.get("ticker") == "NVDA"), None)
    assert nvda is not None
    assert nvda["n_books"] == 3
    assert set(nvda["books_holding"]) == {"flagship", "heavyweight", "autonomous"}
    # AAPL (1 book, sub-cap weight) is NOT flagged
    assert not any(f.get("ticker") == "AAPL" for f in s["flags"])
    # and it appears unflagged in the top-exposures table
    aapl = next((e for e in s["top_exposures"] if e["ticker"] == "AAPL"), None)
    assert aapl is not None and aapl["flagged"] is False and aapl["n_books"] == 1


def test_single_book_name_not_flagged(iso):
    _seed("flagship",   [{"ticker": "TSLA", "weight": 0.05}])
    _seed("autonomous", [{"ticker": "AMD", "weight": 0.05}])
    s = firm_exposure.summary()
    # nothing is held by >=3 books and no name clears the default 8% firm cap → no name flags
    assert not any(f["kind"] == "name" for f in s["flags"])
    assert s["n_books"] == 2


# --------------------------------------------------------------------------- #
# the over-weight single-name flag (firm weight cap), independent of book count
# --------------------------------------------------------------------------- #
def test_overweight_name_flagged_even_in_one_book(iso):
    # one book, but a huge weight → the firm-weight cap fires even though n_books == 1
    _seed("heavyweight", [{"ticker": "BIGCO", "weight": 0.40}])
    s = firm_exposure.summary()
    f = next((x for x in s["flags"] if x.get("ticker") == "BIGCO"), None)
    assert f is not None
    assert "firm weight" in f["reason"]


# --------------------------------------------------------------------------- #
# empty / missing books → honest stub, never raises
# --------------------------------------------------------------------------- #
def test_no_books_is_honest_stub(iso):
    s = firm_exposure.summary()      # nothing seeded
    assert s["n_books"] == 0
    assert s["books"] == []
    assert s["flags"] == []
    assert s["top_exposures"] == []
    assert "note" in s and isinstance(s["note"], str)


def test_book_with_no_positions_is_skipped(iso):
    _seed("flagship", [])                                   # empty positions → skipped
    _seed("autonomous", [{"ticker": "NVDA", "weight": 0.1}])
    s = firm_exposure.summary()
    assert [b["id"] for b in s["books"]] == ["autonomous"]
    assert s["n_books"] == 1


def test_never_raises_on_garbage(iso):
    # a corrupt latest.json must not blow up the read-only monitor
    d = registry.data_dir("china")
    d.mkdir(parents=True, exist_ok=True)
    (d / "latest.json").write_text("{ this is not json")
    _seed("flagship", [{"ticker": "NVDA", "weight": 0.1}])
    s = firm_exposure.summary()       # should degrade past the corrupt book
    assert s["n_books"] == 1
    assert [b["id"] for b in s["books"]] == ["flagship"]


# --------------------------------------------------------------------------- #
# threshold env overrides
# --------------------------------------------------------------------------- #
def test_min_books_env_override(iso, monkeypatch):
    # NVDA in only 2 books; default min_books=3 → not flagged as a pile-up
    _seed("flagship",   [{"ticker": "NVDA", "weight": 0.04}])
    _seed("autonomous", [{"ticker": "NVDA", "weight": 0.04}])
    s = firm_exposure.summary()
    assert not any(f["kind"] == "name" for f in s["flags"])
    # lower the bar to 2 books → now flagged
    monkeypatch.setenv("FIRM_MIN_BOOKS", "2")
    s2 = firm_exposure.summary()
    nvda = next((f for f in s2["flags"] if f.get("ticker") == "NVDA"), None)
    assert nvda is not None and nvda["n_books"] == 2


def test_name_max_env_override(iso, monkeypatch):
    _seed("flagship", [{"ticker": "MIDCO", "weight": 0.06}])     # one book, 6% weight
    # default name_max 8% → not flagged
    assert not any(f.get("ticker") == "MIDCO" for f in firm_exposure.summary()["flags"])
    # drop the cap to 5% → now over the line
    monkeypatch.setenv("FIRM_NAME_MAX", "0.05")
    assert any(f.get("ticker") == "MIDCO" for f in firm_exposure.summary()["flags"])


# --------------------------------------------------------------------------- #
# weight derivation from market_value / nav when no explicit weight is published
# --------------------------------------------------------------------------- #
def test_weight_derived_from_market_value(iso):
    _seed("flagship", [{"ticker": "NODE", "market_value": 250_000.0}], nav=1_000_000.0)
    s = firm_exposure.summary()
    node = next((e for e in s["top_exposures"] if e["ticker"] == "NODE"), None)
    assert node is not None
    assert node["firm_weight"] == pytest.approx(0.25, abs=1e-6)


# --------------------------------------------------------------------------- #
# cross-currency honesty: a CNY book + USD books → equal-book mean unless FX converts,
# but the monitor must still aggregate and never raise, and label the method in `note`.
# --------------------------------------------------------------------------- #
def test_cross_currency_aggregates_honestly(iso):
    _seed("flagship",   [{"ticker": "NVDA", "weight": 0.10}])
    _seed("autonomous", [{"ticker": "NVDA", "weight": 0.10}])
    _seed("china",      [{"ticker": "600519.SS", "weight": 0.20}], currency="CNY")
    s = firm_exposure.summary()
    # books all loaded, and the note honestly states the aggregation basis (USD-clean or equal-book)
    assert s["n_books"] == 3
    assert isinstance(s["currency_clean"], bool)
    assert "firm" in s["note"].lower()
    # NVDA still aggregates across the two USD books with a sane firm weight
    nvda = next((e for e in s["top_exposures"] if e["ticker"] == "NVDA"), None)
    assert nvda is not None and nvda["firm_weight"] > 0

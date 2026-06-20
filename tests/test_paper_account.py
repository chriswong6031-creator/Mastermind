"""Paper account unit tests — offline/fast, no network, no LLM.

Tests cover:
  - rebalance() buys to target dollar value
  - a price move changes NAV correctly
  - sell reduces shares
  - performance() returns the contract shape
  - max_drawdown computed correctly
  - cash floored at 0 (no leverage)
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Generator
from unittest import mock

import pytest

import bot  # noqa: F401  -> vendor/macro onto sys.path

# ---------------------------------------------------------------------------
# Fixtures: redirect all file paths to a temp dir so tests are isolated
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_account(tmp_path: Path) -> Generator[None, None, None]:
    """Redirect paper_account file paths into a fresh temp directory."""
    from portfolio import paper_account

    with (
        mock.patch.object(paper_account, "_DATA", tmp_path),
        mock.patch.object(paper_account, "_ACCOUNT_PATH", tmp_path / "account.json"),
        mock.patch.object(paper_account, "_FILLS_PATH", tmp_path / "fills.jsonl"),
        mock.patch.object(paper_account, "_NAV_PATH", tmp_path / "nav_history.jsonl"),
    ):
        yield


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_PRICES_1 = {"AAPL": 200.0, "MSFT": 400.0, "SPY": 500.0}
_PRICES_2 = {"AAPL": 220.0, "MSFT": 440.0, "SPY": 550.0}  # +10% across the board
_PRICES_DOWN = {"AAPL": 180.0, "MSFT": 360.0, "SPY": 450.0}  # -10%


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_rebalance_buys_to_target_dollar(tmp_account: None) -> None:
    from portfolio import paper_account

    weights = {"AAPL": 0.4, "MSFT": 0.4}
    paper_account.rebalance(weights, _PRICES_1, "2026-01-02")

    state = paper_account._load_account()
    # should have bought both tickers
    assert "AAPL" in state["positions"]
    assert "MSFT" in state["positions"]

    aapl_target_dollar = 0.4 * paper_account._STARTING_NAV
    aapl_target_shares = aapl_target_dollar / _PRICES_1["AAPL"]
    assert abs(state["positions"]["AAPL"]["shares"] - aapl_target_shares) < 0.01

    # cash should be ~20% of starting NAV (80% deployed)
    expected_cash = 0.2 * paper_account._STARTING_NAV
    assert abs(state["cash"] - expected_cash) < 1.0


def test_nav_changes_with_price_move(tmp_account: None) -> None:
    from portfolio import paper_account

    weights = {"AAPL": 0.5, "MSFT": 0.5}
    paper_account.rebalance(weights, _PRICES_1, "2026-01-02")
    nav_before = paper_account.nav(_PRICES_1)

    # prices up 10%
    nav_after = paper_account.nav(_PRICES_2)
    assert nav_after > nav_before
    # roughly: invested portion (~$1M) * 1.10 = net gain ~$100k
    assert abs(nav_after - nav_before - 100_000.0) < 500.0


def test_sell_reduces_shares(tmp_account: None) -> None:
    from portfolio import paper_account

    # buy AAPL 50%
    paper_account.rebalance({"AAPL": 0.5}, _PRICES_1, "2026-01-02")
    shares_before = paper_account._load_account()["positions"]["AAPL"]["shares"]

    # cut AAPL to 25%
    paper_account.rebalance({"AAPL": 0.25}, _PRICES_1, "2026-01-03")
    shares_after = paper_account._load_account()["positions"]["AAPL"]["shares"]

    assert shares_after < shares_before
    assert abs(shares_after - shares_before / 2) < 0.1  # roughly halved


def test_fills_recorded(tmp_account: None, tmp_path: Path) -> None:
    from portfolio import paper_account

    paper_account.rebalance({"AAPL": 0.3, "MSFT": 0.3}, _PRICES_1, "2026-01-02")
    fills = paper_account._load_jsonl(paper_account._FILLS_PATH)
    assert len(fills) >= 2
    tickers = {f["ticker"] for f in fills}
    assert "AAPL" in tickers
    assert "MSFT" in tickers
    assert all(f["side"] == "buy" for f in fills)


def test_cash_never_goes_negative(tmp_account: None) -> None:
    from portfolio import paper_account

    # try to deploy 200% — should be clamped to <= 1.0
    oversized = {"AAPL": 1.0, "MSFT": 1.0}
    paper_account.rebalance(oversized, _PRICES_1, "2026-01-02")
    state = paper_account._load_account()
    assert state["cash"] >= -0.01  # effectively 0 or positive


def test_mark_appends_nav_history(tmp_account: None) -> None:
    from portfolio import paper_account

    paper_account.rebalance({"AAPL": 0.5}, _PRICES_1, "2026-01-02")
    paper_account.mark(_PRICES_1, "2026-01-02")
    rows = paper_account._load_jsonl(paper_account._NAV_PATH)
    assert len(rows) == 1
    assert rows[0]["nav"] > 0
    assert rows[0]["date"] == "2026-01-02"
    assert rows[0].get("spy_nav") is not None  # SPY benchmark initialised


def test_performance_contract_shape(tmp_account: None) -> None:
    from portfolio import paper_account
    from unittest import mock
    from datetime import date, timedelta

    # Use dates that sit within the SPY window we'll mock: 5 days ago through today
    today = date.today()
    d0 = (today - timedelta(days=4)).isoformat()
    d1 = (today - timedelta(days=3)).isoformat()
    d2 = (today - timedelta(days=2)).isoformat()
    d3 = (today - timedelta(days=1)).isoformat()

    # Fake SPY history covering the same 5-day span
    fake_spy_history = [
        (d0, 400.0), (d1, 410.0), (d2, 405.0), (d3, 415.0), (today.isoformat(), 420.0),
    ]

    # inception set to d1 so d0 is pre-inception and d1..today are realized
    with mock.patch.object(paper_account, "_INCEPTION_DATE", d1), \
         mock.patch.object(paper_account, "_load_spy_history", return_value=fake_spy_history):
        paper_account.rebalance({"AAPL": 0.5, "MSFT": 0.3}, _PRICES_1, d1)
        paper_account.mark(_PRICES_1, d1)
        paper_account.mark(_PRICES_2, d2)
        paper_account.mark(_PRICES_DOWN, d3)

        perf = paper_account.performance()

    # required top-level keys
    required_keys = {
        "inception_date", "starting_nav", "current_nav", "cash", "invested",
        "total_return_pct", "vs_spy_pct", "day_change_pct", "max_drawdown_pct",
        "realized_since", "series", "note",
    }
    assert required_keys.issubset(perf.keys()), f"missing keys: {required_keys - perf.keys()}"

    assert perf["starting_nav"] == 1_000_000
    assert perf["current_nav"] > 0
    assert isinstance(perf["series"], list)
    assert len(perf["series"]) >= 3

    # each series item has required keys
    for item in perf["series"]:
        assert "date" in item
        assert "nav" in item
        assert "kind" in item
        # kind must never be "hypothetical" — no backfill of our allocation
        assert item["kind"] in ("pre_inception", "realized"), (
            f"Unexpected kind={item['kind']!r} — hypothetical backfill must be removed"
        )

    # realized rows are tagged correctly (we have 3 mark() calls above, d1/d2/d3)
    realized = [s for s in perf["series"] if s["kind"] == "realized"]
    assert len(realized) >= 3


def test_no_hypothetical_series_points(tmp_account: None) -> None:
    """CRITICAL: no series point may ever have kind=='hypothetical'."""
    from portfolio import paper_account
    from datetime import date, timedelta

    today = date.today()
    d0 = (today - timedelta(days=1)).isoformat()

    paper_account.rebalance({"AAPL": 0.5, "MSFT": 0.3}, _PRICES_1, d0)
    paper_account.mark(_PRICES_1, d0)

    # Even with a latest.json present, performance() must not emit hypothetical rows
    paper_account._DATA.mkdir(parents=True, exist_ok=True)
    latest_path = paper_account._DATA / "latest.json"
    import json
    latest_path.write_text(json.dumps({
        "positions": [
            {"ticker": "AAPL", "weight": 0.5},
            {"ticker": "MSFT", "weight": 0.3},
        ]
    }))

    perf = paper_account.performance()
    hypothetical = [s for s in perf["series"] if s["kind"] == "hypothetical"]
    assert hypothetical == [], (
        f"Found {len(hypothetical)} hypothetical points — should be zero"
    )


def test_pre_inception_nav_is_flat(tmp_account: None) -> None:
    """Pre-inception series points must have nav == starting_nav ($1,000,000)."""
    from portfolio import paper_account
    from unittest import mock
    import pandas as pd

    # Inject a fake SPY history spanning dates before and after inception
    # inception is today; fake history includes dates from 5 days ago through today
    from datetime import date, timedelta
    today = date.today()
    dates = [today - timedelta(days=i) for i in range(4, -1, -1)]  # oldest first

    fake_spy = pd.Series(
        [400.0, 405.0, 410.0, 408.0, 412.0],
        index=pd.to_datetime(dates),
        name="SPY",
    )

    def fake_fetch(ticker: str):
        if ticker == "SPY":
            return fake_spy
        return None

    with mock.patch.object(paper_account, "_fetch_price_series", side_effect=fake_fetch):
        # Set inception to "today" (dates[4]) and add one realized mark
        inception = today.isoformat()
        with mock.patch.object(paper_account, "_INCEPTION_DATE", inception):
            # Write one realized NAV row for today
            paper_account.mark({"SPY": 412.0}, today.isoformat())
            perf = paper_account.performance()

    series = perf["series"]
    assert len(series) > 0, "series must not be empty when SPY history is available"

    pre = [s for s in series if s["kind"] == "pre_inception"]
    realized = [s for s in series if s["kind"] == "realized"]

    # Pre-inception nav must be flat at $1M
    for pt in pre:
        assert pt["nav"] == 1_000_000, (
            f"Pre-inception nav={pt['nav']} on {pt['date']} — expected 1,000,000"
        )

    # SPY nav must vary (real history, not flat)
    if len(series) >= 2:
        spy_vals = [s["spy_nav"] for s in series if s.get("spy_nav") is not None]
        if len(spy_vals) >= 2:
            assert len(set(spy_vals)) > 1, "spy_nav must vary — should reflect real history"


def test_spy_nav_varies(tmp_account: None) -> None:
    """spy_nav across the series must not be a single constant value."""
    from portfolio import paper_account
    from unittest import mock
    import pandas as pd
    from datetime import date, timedelta

    today = date.today()
    dates = [today - timedelta(days=i) for i in range(9, -1, -1)]
    # Prices deliberately move around
    prices = [400.0, 410.0, 405.0, 420.0, 415.0, 430.0, 425.0, 440.0, 435.0, 450.0]
    fake_spy = pd.Series(prices, index=pd.to_datetime(dates), name="SPY")

    def fake_fetch(ticker: str):
        if ticker == "SPY":
            return fake_spy
        return None

    paper_account.mark({"SPY": 450.0}, today.isoformat())

    with mock.patch.object(paper_account, "_fetch_price_series", side_effect=fake_fetch):
        perf = paper_account.performance()

    spy_vals = [s["spy_nav"] for s in perf["series"] if s.get("spy_nav") is not None]
    assert len(spy_vals) >= 2, "Need at least 2 SPY data points to check variance"
    assert len(set(spy_vals)) > 1, "spy_nav must vary — should reflect real S&P history, not be flat"


def test_realized_uses_nav_history(tmp_account: None) -> None:
    """Realized series points must use actual nav from nav_history, not repriced weights."""
    from portfolio import paper_account
    from unittest import mock
    from datetime import date, timedelta

    today = date.today()
    d0 = (today - timedelta(days=2)).isoformat()
    d1 = (today - timedelta(days=1)).isoformat()

    # inception set to d0 so both d0 and d1 are realized
    fake_spy_history = [(d0, 400.0), (d1, 410.0), (today.isoformat(), 420.0)]

    with mock.patch.object(paper_account, "_INCEPTION_DATE", d0), \
         mock.patch.object(paper_account, "_load_spy_history", return_value=fake_spy_history):
        paper_account.rebalance({"AAPL": 0.5}, _PRICES_1, d0)
        paper_account.mark(_PRICES_1, d0)   # nav ~= $1M (positions at cost)
        paper_account.mark(_PRICES_2, d1)   # prices up 10% -> nav ~$1.05M

        perf = paper_account.performance()

    realized = [s for s in perf["series"] if s["kind"] == "realized"]

    # The realized nav values should differ (price moved), not be a flat $1M
    realized_navs = [s["nav"] for s in realized]
    assert len(realized_navs) >= 2
    # At least one realized nav should differ from starting nav
    assert any(abs(v - 1_000_000) > 1.0 for v in realized_navs), (
        "Realized navs are all $1M — they should reflect actual marked NAV from nav_history"
    )


def test_max_drawdown_computed(tmp_account: None) -> None:
    from portfolio import paper_account

    # up then down — should detect a drawdown
    paper_account.rebalance({"AAPL": 0.5}, _PRICES_1, "2026-01-02")
    paper_account.mark(_PRICES_1, "2026-01-02")
    paper_account.mark(_PRICES_2, "2026-01-03")  # up
    paper_account.mark(_PRICES_DOWN, "2026-01-04")  # below starting price

    perf = paper_account.performance()
    # max drawdown should be negative (a loss from peak)
    assert perf["max_drawdown_pct"] <= 0.0


def test_performance_safe_on_empty(tmp_account: None) -> None:
    """performance() must not raise even with no data."""
    from portfolio import paper_account

    perf = paper_account.performance()
    assert perf["starting_nav"] == 1_000_000
    assert isinstance(perf["series"], list)
    assert isinstance(perf["note"], str)


def test_api_performance_route_never_500() -> None:
    """The /api/performance FastAPI route must always return 2xx."""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/api/performance")
    assert r.status_code == 200
    data = r.json()
    assert "starting_nav" in data
    assert data["starting_nav"] == 1_000_000

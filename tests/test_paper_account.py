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


# ---------------------------------------------------------------------------
# no-trade band: don't manufacture de-minimis rebalancing trims/adds
# ---------------------------------------------------------------------------

def test_no_trade_band_suppresses_tiny_drift(tmp_account: None) -> None:
    """Re-stating the SAME weight against a slightly drifted price must NOT produce a
    fractional trim/add — the Brain held the name; the band leaves it alone."""
    from portfolio import paper_account

    paper_account.rebalance({"AAPL": 0.5}, _PRICES_1, "2026-01-02")
    shares_before = paper_account._load_account()["positions"]["AAPL"]["shares"]
    fills_before = len(paper_account._load_jsonl(paper_account._FILLS_PATH))

    # AAPL ticks +0.5% (200 -> 201); the Brain re-states the SAME 0.5 weight. The implied
    # snap-to-target is well under 1% of NAV, so nothing should trade.
    paper_account.rebalance({"AAPL": 0.5}, {"AAPL": 201.0}, "2026-01-03")

    shares_after = paper_account._load_account()["positions"]["AAPL"]["shares"]
    fills_after = len(paper_account._load_jsonl(paper_account._FILLS_PATH))
    assert shares_after == shares_before          # untouched
    assert fills_after == fills_before            # no new fill recorded


def test_no_trade_band_allows_meaningful_trim(tmp_account: None) -> None:
    """A trim large enough to clear the band still executes — the band only kills noise."""
    from portfolio import paper_account

    paper_account.rebalance({"AAPL": 0.5}, _PRICES_1, "2026-01-02")
    shares_before = paper_account._load_account()["positions"]["AAPL"]["shares"]

    # 0.5 -> 0.4 is a 10%-of-NAV move, far above the 1% band -> trades.
    paper_account.rebalance({"AAPL": 0.4}, _PRICES_1, "2026-01-03")
    shares_after = paper_account._load_account()["positions"]["AAPL"]["shares"]
    assert shares_after < shares_before
    assert abs(shares_after - 0.8 * shares_before) < 0.1


def test_no_trade_band_does_not_block_new_entry(tmp_account: None) -> None:
    """A brand-new position below the band is a deliberate open, not noise -> always executes."""
    from portfolio import paper_account

    # MSFT is a fresh 0.5%-of-NAV starter (under the 1% band) opened alongside the AAPL anchor.
    paper_account.rebalance({"AAPL": 0.5, "MSFT": 0.005}, _PRICES_1, "2026-01-02")
    assert "MSFT" in paper_account._load_account()["positions"]


def test_no_trade_band_does_not_block_full_exit(tmp_account: None) -> None:
    """Dropping a name from the target fully exits it regardless of size — never banded."""
    from portfolio import paper_account

    paper_account.rebalance({"AAPL": 0.5, "MSFT": 0.005}, _PRICES_1, "2026-01-02")
    assert "MSFT" in paper_account._load_account()["positions"]

    # MSFT (~0.5% of NAV, under the band) is dropped from the target -> must fully exit.
    paper_account.rebalance({"AAPL": 0.5}, _PRICES_1, "2026-01-03")
    assert "MSFT" not in paper_account._load_account()["positions"]


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
        "total_return_pct", "vs_benchmark_pct", "vs_spy_pct", "benchmark",
        "benchmark_name", "benchmark_as_of", "day_change_pct", "max_drawdown_pct",
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


def test_regional_benchmark_migration_preserves_account_and_resets_only_baseline(
        tmp_path: Path, monkeypatch) -> None:
    """A legacy FXI-normalized regional account switches to CSI 300 without touching capital."""
    from portfolio import paper_account, registry

    regional = tmp_path / "china"
    monkeypatch.setattr(registry, "data_dir", lambda portfolio_id=None: regional)
    legacy = {
        "inception_date": "2026-07-01",
        "starting_nav": 1_000_000.0,
        "cash": 500_000.0,
        "positions": {"600519.SS": {"shares": 1_000.0, "avg_cost": 500.0}},
        "spy_shares": 1_000_000.0 / 30.0,
        "spy_inception_price": 30.0,
    }
    paper_account._save_account(legacy, "china")

    paper_account.mark(
        {"600519.SS": 500.0, "000300.SS": 4_000.0},
        "2026-07-02",
        portfolio_id="china",
    )

    migrated = paper_account._load_account("china")
    row = paper_account._load_jsonl(paper_account._paths("china")["nav"])[-1]
    assert migrated["cash"] == legacy["cash"]
    assert migrated["positions"]["600519.SS"]["shares"] == 1_000.0
    assert migrated["benchmark_symbol"] == "000300.SS"
    assert migrated["spy_inception_price"] == 4_000.0
    assert migrated["spy_shares"] == pytest.approx(250.0)
    assert row["nav"] == pytest.approx(1_000_000.0)
    assert row["spy_nav"] == pytest.approx(1_000_000.0)
    assert row["benchmark"] == "000300.SS"


def test_china_performance_uses_native_index_history_not_legacy_fxi(
        tmp_path: Path, monkeypatch) -> None:
    from portfolio import paper_account, registry

    regional = tmp_path / "china"
    monkeypatch.setattr(registry, "data_dir", lambda portfolio_id=None: regional)
    paper_account._save_account({
        "inception_date": "2026-07-01",
        "starting_nav": 1_000_000.0,
        "cash": 1_000_000.0,
        "positions": {},
        "spy_shares": 1_000_000.0 / 30.0,  # legacy FXI normalization
        "spy_inception_price": 30.0,
    }, "china")
    nav_path = paper_account._paths("china")["nav"]
    nav_path.write_text(json.dumps({
        "date": "2026-07-01", "nav": 1_000_000.0, "cash": 1_000_000.0,
        "spy_nav": 1_100_000.0,  # deliberately misleading legacy FXI value
    }) + "\n")
    history = [("2026-07-01", 4_000.0), ("2026-07-02", 4_200.0)]
    monkeypatch.setattr(paper_account, "_load_spy_history",
                        lambda window=91, symbol="SPY": history if symbol == "000300.SS" else [])

    perf = paper_account.performance("china")

    assert perf["benchmark"] == "000300.SS"
    assert perf["benchmark_name"] == "CSI 300"
    assert perf["benchmark_as_of"] == "2026-07-02"
    assert perf["total_return_pct"] == 0.0
    assert perf["vs_benchmark_pct"] == pytest.approx(-5.0)
    assert perf["vs_spy_pct"] == perf["vs_benchmark_pct"]  # compatibility alias
    assert perf["series"][-1]["spy_nav"] == pytest.approx(1_050_000.0)


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


def test_queue_orders_when_closed_does_not_fill(tmp_account: None) -> None:
    """Queuing a buy while the market is closed must NOT move cash or positions —
    it only records a PENDING order sized at the estimated (prev-close) price."""
    from portfolio import paper_account

    pending = paper_account.queue_orders(
        {"AAPL": 0.4, "MSFT": 0.4}, _PRICES_1, "2026-06-21", fill_after="2026-06-22",
    )
    state = paper_account._load_account()
    assert state["positions"] == {}                      # nothing bought
    assert abs(state["cash"] - paper_account._STARTING_NAV) < 1e-6  # full cash intact
    assert paper_account._load_jsonl(paper_account._FILLS_PATH) == []  # no fills

    assert {o["ticker"] for o in pending} == {"AAPL", "MSFT"}
    aapl = next(o for o in pending if o["ticker"] == "AAPL")
    assert aapl["side"] == "buy" and aapl["status"] == "pending"
    assert aapl["est_price"] == _PRICES_1["AAPL"]
    assert abs(aapl["shares"] - (0.4 * paper_account._STARTING_NAV) / _PRICES_1["AAPL"]) < 0.01
    assert aapl["fill_after"] == "2026-06-22"
    assert paper_account.load_pending() == pending       # persisted


def test_queue_orders_skips_phantom_zero_share_buy(tmp_account: None) -> None:
    """A book already AT its target weight must not queue a 'BUY 0 / $0' phantom.

    Regression: when held ≈ target, the residual ``target_shares - held`` can be a
    sub-rounding float (> the 1e-9 guard but < 0.5e-6), so it slipped past the guard
    yet ``round(.., 6)`` collapsed it to 0 shares / $0 — an inert order that cluttered
    the dashboard (e.g. "IWM BUY 0 $295.59 $0 at open")."""
    from portfolio import paper_account

    px = 295.59
    nav = paper_account._STARTING_NAV
    target = 0.125 * nav / px
    held = target - 2e-7                          # sub-rounding residue: > 1e-9, < 0.5e-6
    state = paper_account._load_account()
    state["positions"]["IWM"] = {"shares": held, "avg_cost": px}
    paper_account._save_account(state)

    pending = paper_account.queue_orders(
        {"IWM": 0.125}, {"IWM": px}, "2026-06-22",
        nav_base=nav, fill_after="2026-06-23",
    )
    assert pending == []                          # already at target — no phantom queued
    assert paper_account.load_pending() == []
    # and a real top-up (held well below target) is still queued, with non-zero shares/value
    state["positions"]["IWM"]["shares"] = target / 2
    paper_account._save_account(state)
    pending = paper_account.queue_orders(
        {"IWM": 0.125}, {"IWM": px}, "2026-06-22", nav_base=nav, fill_after="2026-06-23",
    )
    assert len(pending) == 1
    assert pending[0]["shares"] > 0 and pending[0]["est_value"] > 0


def test_fill_pending_executes_at_market_price(tmp_account: None) -> None:
    """At the open, pending orders fill for their queued share count at the REAL
    market price (here +10% vs the prev-close estimate), then the queue clears."""
    from portfolio import paper_account

    paper_account.queue_orders({"AAPL": 0.5}, _PRICES_1, "2026-06-21", fill_after="2026-06-22")
    want = paper_account.load_pending()[0]["shares"]

    fills = paper_account.fill_pending(_PRICES_2, "2026-06-22")   # AAPL 200 -> 220
    assert len(fills) == 1
    assert fills[0]["ticker"] == "AAPL"
    assert fills[0]["price"] == _PRICES_2["AAPL"]                 # filled at market, not estimate
    assert fills[0].get("from_pending") is True

    state = paper_account._load_account()
    assert abs(state["positions"]["AAPL"]["shares"] - want) < 1e-6  # full share count filled
    assert paper_account.load_pending() == []                    # queue drained
    # cash spent at the higher market price
    assert abs(state["cash"] - (paper_account._STARTING_NAV - want * _PRICES_2["AAPL"])) < 1.0


def test_fill_pending_keeps_unpriceable_orders_queued(tmp_account: None) -> None:
    from portfolio import paper_account

    # ZZZZ is a non-existent ticker: no market price and no price-store fallback
    paper_account.queue_orders({"AAPL": 0.3, "ZZZZ": 0.3},
                               {"AAPL": 200.0, "ZZZZ": 50.0}, "2026-06-21")
    # only AAPL has a market price at the open; ZZZZ stays queued (can't fill)
    fills = paper_account.fill_pending({"AAPL": 200.0}, "2026-06-22")
    assert {f["ticker"] for f in fills} == {"AAPL"}
    assert {o["ticker"] for o in paper_account.load_pending()} == {"ZZZZ"}


# ---------------------------------------------------------------------------
# market-closed FULL rebalance: sells are queued too (the flagship-never-sold fix)
# ---------------------------------------------------------------------------

def test_queue_orders_queues_sell_for_dropped_name(tmp_account: None) -> None:
    """A held name absent from the target book must queue a side='sell' full exit —
    the core fix: the market-closed path can now represent an exit, not just buys."""
    from portfolio import paper_account

    # seed a two-name book, then rebalance to a target that DROPS MSFT entirely.
    paper_account.rebalance({"AAPL": 0.4, "MSFT": 0.4}, _PRICES_1, "2026-01-02")
    held_msft = paper_account._load_account()["positions"]["MSFT"]["shares"]

    pending = paper_account.queue_orders(
        {"AAPL": 0.4}, _PRICES_1, "2026-06-21", fill_after="2026-06-22",
    )
    sells = [o for o in pending if o["side"] == "sell"]
    assert len(sells) == 1
    msft_sell = sells[0]
    assert msft_sell["ticker"] == "MSFT"
    assert msft_sell["status"] == "pending"
    assert abs(msft_sell["shares"] - held_msft) < 1e-6      # full exit of the held line
    assert msft_sell["fill_after"] == "2026-06-22"
    # queuing changes nothing yet — no fill, cash/positions intact
    assert "MSFT" in paper_account._load_account()["positions"]


def test_queue_orders_queues_sell_for_reduced_weight(tmp_account: None) -> None:
    """A held name whose target weight is materially REDUCED queues a partial sell."""
    from portfolio import paper_account

    paper_account.rebalance({"AAPL": 0.5}, _PRICES_1, "2026-01-02")
    held = paper_account._load_account()["positions"]["AAPL"]["shares"]

    # 0.5 -> 0.3 is a 20%-of-NAV trim, far above the band -> a partial sell is queued.
    pending = paper_account.queue_orders(
        {"AAPL": 0.3}, _PRICES_1, "2026-06-21", fill_after="2026-06-22",
    )
    sells = [o for o in pending if o["side"] == "sell"]
    assert len(sells) == 1
    # sold ~40% of the line (0.5 -> 0.3)
    assert abs(sells[0]["shares"] - 0.4 * held) < 1e-3
    assert sells[0]["shares"] < held                        # a trim, not a full exit


def test_queue_orders_no_trade_band_suppresses_subband_trim(tmp_account: None) -> None:
    """A sub-band trim of a name still in the target must NOT queue a sell (no churn)."""
    from portfolio import paper_account

    paper_account.rebalance({"AAPL": 0.5}, _PRICES_1, "2026-01-02")

    # AAPL restated at 0.5 with the price ticked +0.5% — the implied trim is well under the
    # 1% band, so nothing should be queued (neither a sell nor a buy).
    pending = paper_account.queue_orders(
        {"AAPL": 0.5}, {"AAPL": 201.0}, "2026-06-21", fill_after="2026-06-22",
    )
    assert [o for o in pending if o["side"] == "sell"] == []
    assert pending == []                                    # no buy either — purely banded


def test_fill_pending_sells_before_buys_frees_cash(tmp_account: None) -> None:
    """Sells fill FIRST; the freed cash then funds the buys — assert cash conservation.

    Deploy the whole book so cash is ~0, then queue a rotation (exit MSFT, open GOOG).
    The GOOG buy is only affordable because the MSFT sell settles first and frees the cash."""
    from portfolio import paper_account

    # fully deploy: AAPL 50% + MSFT 50% -> cash ~0
    paper_account.rebalance({"AAPL": 0.5, "MSFT": 0.5}, _PRICES_1, "2026-01-02")
    cash_after_deploy = paper_account._load_account()["cash"]
    assert cash_after_deploy < 1000.0                       # essentially no dry powder

    prices = {"AAPL": 200.0, "MSFT": 400.0, "GOOG": 100.0}
    nav_before = paper_account.nav(prices)

    # queue a rotation: drop MSFT entirely, open GOOG at ~40% — unaffordable without the sell.
    paper_account.queue_orders(
        {"AAPL": 0.5, "GOOG": 0.4}, prices, "2026-06-21", fill_after="2026-06-22",
    )
    pend = paper_account.load_pending()
    assert any(o["side"] == "sell" and o["ticker"] == "MSFT" for o in pend)
    assert any(o["side"] == "buy" and o["ticker"] == "GOOG" for o in pend)

    fills = paper_account.fill_pending(prices, "2026-06-22")
    # the sell fill must come before the GOOG buy in the returned order (sells-first phase)
    sides = [(f["ticker"], f["side"]) for f in fills]
    assert ("MSFT", "sell") in sides
    assert ("GOOG", "buy") in sides
    assert sides.index(("MSFT", "sell")) < sides.index(("GOOG", "buy"))

    state = paper_account._load_account()
    assert "MSFT" not in state["positions"]                 # exited
    assert "GOOG" in state["positions"]                     # opened with the freed cash
    assert state["positions"]["GOOG"]["shares"] > 0

    # cash conservation: NAV is unchanged by the rotation (same marks, no leverage, no leak).
    nav_after = paper_account.nav(prices)
    assert abs(nav_after - nav_before) < 1.0
    assert state["cash"] >= -0.01                           # never negative
    assert paper_account.load_pending() == []               # queue drained


def test_fill_pending_legacy_buys_only_still_fills(tmp_account: None) -> None:
    """Backward compat: a pending file with legacy buy orders that carry NO 'side' key
    must still fill as buys (missing side defaults to 'buy')."""
    from portfolio import paper_account

    # write a legacy buy-only pending file by hand (no 'side' field at all)
    legacy = [{
        "id": "2026-06-21-AAPL-buy", "ticker": "AAPL", "shares": 100.0,
        "est_price": 200.0, "est_value": 20000.0, "weight": 0.02,
        "placed_asof": "2026-06-21", "fill_after": "2026-06-22", "status": "pending",
    }]
    paper_account._save_pending(legacy)
    assert "side" not in paper_account.load_pending()[0]    # genuinely legacy

    fills = paper_account.fill_pending({"AAPL": 200.0}, "2026-06-22")
    assert len(fills) == 1
    assert fills[0]["ticker"] == "AAPL" and fills[0]["side"] == "buy"
    assert abs(fills[0]["shares"] - 100.0) < 1e-6
    assert paper_account._load_account()["positions"]["AAPL"]["shares"] == 100.0
    assert paper_account.load_pending() == []


def test_queue_orders_nav_base_uses_current_nav_not_1m(tmp_account: None) -> None:
    """Regression for the hardcoded nav_base=$1M: with nav_base=None the weights must
    size against the CURRENT marked NAV, not the $1M inception NAV."""
    from portfolio import paper_account

    # Force the account NAV well below $1M so the two bases differ sharply. Start from a
    # deployed book, then crater its mark so current NAV ~= $400k while starting NAV is $1M.
    state = paper_account._load_account()
    state["cash"] = 0.0
    state["positions"] = {"AAPL": {"shares": 2000.0, "avg_cost": 200.0}}
    paper_account._save_account(state)

    low_prices = {"AAPL": 200.0}                            # NAV = 2000 * 200 = $400,000
    current_nav = paper_account.nav(low_prices)
    assert abs(current_nav - 400_000.0) < 1.0

    # queue a fresh 10% GOOG open with nav_base=None -> should size against $400k, not $1M.
    pending = paper_account.queue_orders(
        {"AAPL": 1.0, "GOOG": 0.1}, {"AAPL": 200.0, "GOOG": 100.0},
        "2026-06-21", nav_base=None, fill_after="2026-06-22",
    )
    goog = next(o for o in pending if o["ticker"] == "GOOG")
    # 10% of the CURRENT $400k NAV / $100 = 400 shares (NOT 10% of $1M = 1000 shares).
    assert abs(goog["shares"] - 400.0) < 1.0
    assert goog["shares"] < 1000.0 - 1.0                    # decisively not the $1M-based size


def test_closed_market_build_then_open_fill_converges(tmp_account: None) -> None:
    """End-to-end mini: a closed-market build queues sells+buys against a NEW target book,
    and the next-open fill converges the account toward that target (drops the zombie name,
    opens the new one)."""
    from portfolio import paper_account

    # existing (stale) book: AAPL + MSFT fully deployed.
    paper_account.rebalance({"AAPL": 0.5, "MSFT": 0.5}, _PRICES_1, "2026-01-02")
    assert set(paper_account._load_account()["positions"]) == {"AAPL", "MSFT"}

    # market CLOSED: the desk decides a new book — keep AAPL, drop MSFT, add GOOG.
    prices = {"AAPL": 200.0, "MSFT": 400.0, "GOOG": 100.0}
    paper_account.queue_orders(
        {"AAPL": 0.5, "GOOG": 0.4}, prices, "2026-06-21", nav_base=None,
        fill_after="2026-06-22",
    )
    # nothing filled yet
    assert set(paper_account._load_account()["positions"]) == {"AAPL", "MSFT"}

    # next OPEN: fill the queue.
    paper_account.fill_pending(prices, "2026-06-22")

    held = set(paper_account._load_account()["positions"])
    assert "MSFT" not in held                               # zombie name exited
    assert held == {"AAPL", "GOOG"}                         # converged toward the target book


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

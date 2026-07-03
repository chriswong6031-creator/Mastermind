"""Self-Directed book (third portfolio) — unit tests. Offline/fast: no network, no
LLM, no vendor data. Prices, market state, and `now` are all injected so the engine's
fill / queue / settle / weight / FIFO / thesis logic is exercised deterministically.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Generator
from unittest import mock

import pytest


@pytest.fixture()
def sd(tmp_path: Path) -> "Generator":
    """Redirect every self_directed state file into a fresh temp dir + yield the module."""
    from portfolio import self_directed as sd_mod
    with (
        mock.patch.object(sd_mod, "_DATA", tmp_path),
        mock.patch.object(sd_mod, "_ACCOUNT_PATH", tmp_path / "account.json"),
        mock.patch.object(sd_mod, "_FILLS_PATH", tmp_path / "fills.jsonl"),
        mock.patch.object(sd_mod, "_PENDING_PATH", tmp_path / "pending.json"),
        mock.patch.object(sd_mod, "_THESES_PATH", tmp_path / "theses.json"),
    ):
        yield sd_mod


# ---------------------------------------------------------------------------
# order routing: fill now (open) vs queue (closed)
# ---------------------------------------------------------------------------

def test_buy_fills_when_market_open(sd) -> None:
    r = sd.place_order("AAPL", "buy", 1000, price=100.0, market_open=True)
    assert r["ok"] and r["status"] == "filled"
    assert r["fill"]["shares"] == 1000 and r["fill"]["price"] == 100.0
    acct = sd._load_account()
    assert acct["positions"]["AAPL"]["shares"] == 1000
    assert abs(acct["cash"] - (sd._STARTING_NAV - 100_000.0)) < 1e-6


def test_buy_by_dollar_amount(sd) -> None:
    # $25,000 of a $125 stock -> 200 shares, cash down $25k
    r = sd.place_order("AAPL", "buy", notional=25_000.0, price=125.0, market_open=True)
    assert r["ok"] and r["status"] == "filled"
    assert abs(r["fill"]["shares"] - 200.0) < 1e-6
    acct = sd._load_account()
    assert abs(acct["positions"]["AAPL"]["shares"] - 200.0) < 1e-6
    assert abs(acct["cash"] - (sd._STARTING_NAV - 25_000.0)) < 1e-6


def test_sell_by_dollar_amount(sd) -> None:
    sd.place_order("AAPL", "buy", 100, price=100.0, market_open=True)
    # sell $3,000 worth at $150 -> 20 shares
    r = sd.place_order("AAPL", "sell", notional=3_000.0, price=150.0, market_open=True)
    assert r["ok"] and r["status"] == "filled"
    assert abs(r["fill"]["shares"] - 20.0) < 1e-6
    assert abs(sd._load_account()["positions"]["AAPL"]["shares"] - 80.0) < 1e-6


def test_pending_dollar_order_settles_to_shares_at_open(sd) -> None:
    r = sd.place_order("AAPL", "buy", notional=5_000.0, market_open=False)
    assert r["ok"] and r["status"] == "pending"
    assert sd._load_pending()[0].get("notional") == 5_000.0
    settled = sd.settle_pending(market_open=True, prices={"AAPL": 100.0})
    assert len(settled) == 1 and settled[0]["status"] == "filled"
    assert abs(sd._load_account()["positions"]["AAPL"]["shares"] - 50.0) < 1e-6   # $5k / $100


def test_order_requires_shares_or_notional(sd) -> None:
    r = sd.place_order("AAPL", "buy", market_open=True, price=100.0)
    assert r["ok"] is False and ("shares" in r["error"].lower() or "amount" in r["error"].lower())


def test_buy_clamped_to_cash_no_leverage(sd) -> None:
    # ask for $10M of stock with $1M cash -> clamp to cash/px shares, cash floored at 0
    r = sd.place_order("AAPL", "buy", 100_000, price=100.0, market_open=True)
    assert r["ok"] and r["status"] == "filled"
    acct = sd._load_account()
    assert abs(acct["positions"]["AAPL"]["shares"] - sd._STARTING_NAV / 100.0) < 1e-6
    assert acct["cash"] >= -1e-6 and acct["cash"] < 1.0


def test_buy_rejected_insufficient_cash(sd) -> None:
    sd.place_order("AAPL", "buy", 10_000, price=100.0, market_open=True)   # spends all cash
    r = sd.place_order("MSFT", "buy", 1, price=100.0, market_open=True)
    assert r["ok"] is False and "cash" in r["error"].lower()


def test_sell_bounded_to_shares_held(sd) -> None:
    sd.place_order("AAPL", "buy", 100, price=100.0, market_open=True)
    r = sd.place_order("AAPL", "sell", 250, price=120.0, market_open=True)   # ask > held
    assert r["ok"] and r["status"] == "filled"
    assert r["fill"]["shares"] == 100          # clamped to the 100 held
    acct = sd._load_account()
    assert "AAPL" not in acct["positions"]     # fully exited
    # proceeds credited: cash = 1M - 100*100 (buy) + 100*120 (sell)
    assert abs(acct["cash"] - (sd._STARTING_NAV - 10_000.0 + 12_000.0)) < 1e-6


def test_sell_with_no_position_rejected(sd) -> None:
    r = sd.place_order("AAPL", "sell", 10, price=100.0, market_open=True)
    assert r["ok"] is False and "position" in r["error"].lower()


def test_order_queues_when_market_closed(sd) -> None:
    r = sd.place_order("AAPL", "buy", 50, price=100.0, market_open=False)
    assert r["ok"] and r["status"] == "pending"
    pending = sd._load_pending()
    assert len(pending) == 1 and pending[0]["ticker"] == "AAPL" and pending[0]["side"] == "buy"
    # nothing has transacted yet
    acct = sd._load_account()
    assert acct["positions"] == {} and acct["cash"] == sd._STARTING_NAV


def test_sell_queue_rejected_when_no_shares(sd) -> None:
    r = sd.place_order("AAPL", "sell", 5, market_open=False)
    assert r["ok"] is False
    assert sd._load_pending() == []


# ---------------------------------------------------------------------------
# pending settlement at the next open
# ---------------------------------------------------------------------------

def test_pending_settles_at_open_price(sd) -> None:
    sd.place_order("AAPL", "buy", 50, market_open=False)
    assert sd._market_open  # sanity: callable exists
    settled = sd.settle_pending(market_open=True, prices={"AAPL": 110.0})
    assert len(settled) == 1 and settled[0]["status"] == "filled"
    assert settled[0]["filled_price"] == 110.0
    acct = sd._load_account()
    assert acct["positions"]["AAPL"]["shares"] == 50
    assert abs(acct["cash"] - (sd._STARTING_NAV - 50 * 110.0)) < 1e-6
    # queue is now empty and the fill is logged as queued
    assert sd._load_pending() == []
    fills = sd._load_fills()
    assert any(f.get("queued") for f in fills)


def test_pending_does_not_settle_while_closed(sd) -> None:
    sd.place_order("AAPL", "buy", 50, market_open=False)
    settled = sd.settle_pending(market_open=False, prices={"AAPL": 110.0})
    assert settled == []
    assert len(sd._load_pending()) == 1


def test_pending_sell_with_no_shares_cancelled_on_settle(sd) -> None:
    # a queued sell whose position is gone by the open has nothing to fill -> it is dropped as
    # 'cancelled', not errored. (Simulate the lot being gone by popping it directly, so the test
    # is deterministic regardless of whether ambient price lookups succeed.)
    sd.place_order("AAPL", "buy", 10, price=100.0, market_open=True)
    sd.place_order("AAPL", "sell", 10, market_open=False)     # queued sell of the 10 held
    state = sd._load_account(); state["positions"].pop("AAPL", None); sd._save_account(state)
    settled = sd.settle_pending(market_open=True, prices={"AAPL": 110.0})
    assert len(settled) == 1 and settled[0]["status"] == "cancelled"


def test_cancel_pending_order(sd) -> None:
    r = sd.place_order("NVDA", "buy", 5, market_open=False)
    oid = r["order"]["order_id"]
    assert sd.cancel_order(oid) is True
    assert sd._load_pending() == []
    assert sd.cancel_order(oid) is False        # already gone


# ---------------------------------------------------------------------------
# book() — live weights + allocation scorecard
# ---------------------------------------------------------------------------

def test_book_weights_and_scorecard(sd) -> None:
    sd.place_order("AAPL", "buy", 1000, price=100.0, market_open=True)   # $100k
    sd.place_order("MSFT", "buy", 500, price=200.0, market_open=True)    # $100k
    b = sd.book(prices={"AAPL": 100.0, "MSFT": 200.0}, market_open=False)
    assert abs(b["nav"] - 1_000_000.0) < 1e-6
    assert abs(b["invested"] - 200_000.0) < 1e-6
    by = {p["ticker"]: p for p in b["positions"]}
    assert abs(by["AAPL"]["weight"] - 0.10) < 1e-6
    assert abs(by["MSFT"]["weight"] - 0.10) < 1e-6
    a = b["allocation"]
    assert a["n_positions"] == 2
    assert abs(a["gross"] - 0.20) < 1e-6
    assert abs(a["cash_pct"] - 0.80) < 1e-6
    assert abs(a["largest_weight"] - 0.10) < 1e-6


def test_book_marks_unrealized_pnl(sd) -> None:
    sd.place_order("AAPL", "buy", 100, price=100.0, market_open=True)
    b = sd.book(prices={"AAPL": 130.0}, market_open=False)
    p = b["positions"][0]
    assert p["current_price"] == 130.0
    assert abs(p["unrealized_pnl"] - 3000.0) < 1e-6      # (130-100)*100
    assert abs(p["unrealized_pct"] - 30.0) < 1e-6
    assert abs(b["allocation"]["total_unrealized_pnl"] - 3000.0) < 1e-6


def test_empty_book_is_all_cash(sd) -> None:
    b = sd.book(market_open=False)
    assert b["positions"] == []
    assert abs(b["cash"] - 1_000_000.0) < 1e-6
    assert b["allocation"]["cash_pct"] == 1.0
    assert b["allocation"]["n_positions"] == 0


# ---------------------------------------------------------------------------
# history() — FIFO realized P&L blotter
# ---------------------------------------------------------------------------

def test_history_fifo_realized_pnl(sd) -> None:
    sd.place_order("AAPL", "buy", 10, price=100.0, market_open=True)
    sd.place_order("AAPL", "sell", 4, price=150.0, market_open=True)
    h = sd.history(market_open=False)
    sells = [r for r in h["history"] if r["action"] == "sell"]
    assert len(sells) == 1
    assert abs(sells[0]["realized_pnl"] - 200.0) < 1e-6      # (150-100)*4
    assert abs(sells[0]["realized_pct"] - 50.0) < 1e-6
    assert abs(h["realized_total"] - 200.0) < 1e-6
    assert h["n_closed"] == 1 and h["n_buys"] == 1 and h["win_rate"] == 1.0
    # the still-open remainder (6 sh) is marked to a live price for unrealized
    buys = [r for r in h["history"] if r["action"] == "buy"]
    assert buys[0]["still_open"] is True and buys[0]["open_shares"] == 6


# ---------------------------------------------------------------------------
# conviction theses
# ---------------------------------------------------------------------------

def test_thesis_save_and_surface_in_book(sd) -> None:
    sd.place_order("AAPL", "buy", 10, price=100.0, market_open=True)
    saved = sd.set_thesis("aapl", "  binding constraint is HBM; hold while RS leads.  ")
    assert saved and saved["note"].startswith("binding constraint")
    b = sd.book(prices={"AAPL": 100.0}, market_open=False)
    p = b["positions"][0]
    assert p["thesis"].startswith("binding constraint")
    assert p["thesis_updated_at"]
    # clearing the note removes it
    assert sd.set_thesis("AAPL", "   ") is None
    b2 = sd.book(prices={"AAPL": 100.0}, market_open=False)
    assert b2["positions"][0]["thesis"] is None


# ---------------------------------------------------------------------------
# input validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ticker,side,shares,err", [
    ("AAPL", "buy", 0, "positive"),
    ("AAPL", "buy", -5, "positive"),
    ("AAPL", "hold", 5, "side"),
    ("", "buy", 5, "ticker"),
])
def test_order_validation(sd, ticker, side, shares, err) -> None:
    r = sd.place_order(ticker, side, shares, price=100.0, market_open=True)
    assert r["ok"] is False and err in r["error"].lower()


# ---------------------------------------------------------------------------
# market clock
# ---------------------------------------------------------------------------

def test_market_clock_sessions() -> None:
    from portfolio import market_clock as mc
    # 2026-06-22 is a Monday (a session day)
    assert mc.is_open(datetime(2026, 6, 22, 11, 0)) is True       # mid-session
    assert mc.is_open(datetime(2026, 6, 22, 9, 0)) is False       # pre-market
    assert mc.is_open(datetime(2026, 6, 22, 16, 30)) is False     # after close
    # 2026-06-20 is a Saturday
    assert mc.is_open(datetime(2026, 6, 20, 11, 0)) is False
    # 2026-12-25 is Christmas (holiday) — closed even though it's a Friday
    assert mc.is_open(datetime(2026, 12, 25, 11, 0)) is False
    st = mc.status(datetime(2026, 6, 22, 9, 0))
    assert st["session"] == "pre" and st["is_open"] is False
    assert st["next_open"].startswith("2026-06-22T09:30")


def test_market_clock_next_open_skips_weekend() -> None:
    from portfolio import market_clock as mc
    # Friday after the close -> next open is Monday
    no = mc.next_open(datetime(2026, 6, 19, 17, 0))
    # 2026-06-19 is Juneteenth (holiday, Friday); 20/21 weekend -> next session Mon 22nd
    assert no.date().isoformat() == "2026-06-22"


# ---------------------------------------------------------------------------
# W-L / L1 — the mark seam + the injected marking layer (phantom-zero-return fix)
# ---------------------------------------------------------------------------

@pytest.fixture()
def sd_nav(tmp_path: Path) -> "Generator":
    """Same as `sd`, plus the nav_history path redirected (the new mark seam)."""
    from portfolio import self_directed as sd_mod
    with (
        mock.patch.object(sd_mod, "_DATA", tmp_path),
        mock.patch.object(sd_mod, "_ACCOUNT_PATH", tmp_path / "account.json"),
        mock.patch.object(sd_mod, "_FILLS_PATH", tmp_path / "fills.jsonl"),
        mock.patch.object(sd_mod, "_PENDING_PATH", tmp_path / "pending.json"),
        mock.patch.object(sd_mod, "_THESES_PATH", tmp_path / "theses.json"),
        mock.patch.object(sd_mod, "_NAV_PATH", tmp_path / "nav_history.jsonl"),
    ):
        yield sd_mod


def test_injected_resolver_is_consulted_first(sd) -> None:
    """set_price_resolver installs the ONE marking layer as _current_price's first source."""
    try:
        sd.set_price_resolver(lambda t: {"AAPL": 321.0}.get(t))
        assert sd._current_price("AAPL") == 321.0
        # a name the resolver can't price falls through to the legacy path (None here → None)
        assert sd._current_price("ZZZZ") in (None,)
    finally:
        sd.set_price_resolver(None)
    # cleared → the resolver is no longer consulted
    assert sd._price_resolver is None


def test_injected_mark_gives_a_non_zero_return(sd_nav) -> None:
    """THE fix: with no live quote the OLD path fell back to avg_cost → a phantom ZERO return.
    Through the injected marking layer the book now shows the real move."""
    # buy 1000 AAPL @ $100 while the market is open (fills immediately)
    sd_nav.place_order("AAPL", "buy", 1000, price=100.0, market_open=True)
    # the marking layer says AAPL is now $130 (a real +30% mark), SPY priced for the bench line
    sd_nav.set_price_resolver(lambda t: {"AAPL": 130.0, "SPY": 500.0}.get(t))
    try:
        row = sd_nav.mark(asof="2026-06-02")
    finally:
        sd_nav.set_price_resolver(None)
    # 900k cash + 1000*130 = 1,030,000 → +3% on the $1M book (NOT the flat avg_cost mark)
    assert abs(row["nav"] - 1_030_000.0) < 1.0
    assert row["nav"] > sd_nav._STARTING_NAV                # non-zero, positive return
    # persisted to nav_history and idempotent per date
    rows = [__import__("json").loads(l) for l in
            (sd_nav._NAV_PATH.read_text().splitlines())]
    assert len(rows) == 1 and rows[0]["date"] == "2026-06-02"
    sd_nav.mark(asof="2026-06-02", prices={"AAPL": 130.0, "SPY": 500.0})  # re-mark same day
    rows = [__import__("json").loads(l) for l in (sd_nav._NAV_PATH.read_text().splitlines())]
    assert len(rows) == 1, "mark() must be idempotent per date"


def test_mark_prices_override_wins(sd_nav) -> None:
    sd_nav.place_order("AAPL", "buy", 1000, price=100.0, market_open=True)
    row = sd_nav.mark(prices={"AAPL": 150.0, "SPY": 500.0}, asof="2026-06-03")
    assert abs(row["nav"] - 1_050_000.0) < 1.0             # 900k + 1000*150

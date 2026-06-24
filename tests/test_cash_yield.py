"""4% money-market cash sweep — offline tests for paper_account.accrue_cash_yield.

Idle cash earns a configurable annualized yield (default 4%), accrued one trading-day at a time,
IDEMPOTENT per (book, date) so the daily mark job can call it freely without double-accruing. No
network, no LLM, no vendor engine.
"""
from __future__ import annotations

from pathlib import Path
from typing import Generator
from unittest import mock

import pytest

import bot  # noqa: F401  -> vendor/macro onto sys.path

_STEP = 1.0 + 0.04 / 252


@pytest.fixture()
def tmp_account(tmp_path: Path) -> Generator[None, None, None]:
    from portfolio import paper_account
    with (
        mock.patch.object(paper_account, "_DATA", tmp_path),
        mock.patch.object(paper_account, "_ACCOUNT_PATH", tmp_path / "account.json"),
        mock.patch.object(paper_account, "_FILLS_PATH", tmp_path / "fills.jsonl"),
        mock.patch.object(paper_account, "_NAV_PATH", tmp_path / "nav_history.jsonl"),
    ):
        yield


def _seed(cash: float) -> None:
    from portfolio import paper_account
    st = paper_account._load_account()
    st["cash"] = cash
    paper_account._save_account(st)


def test_default_rate_is_4pct(tmp_account: None) -> None:
    from portfolio import paper_account
    assert paper_account._cash_yield_rate() == pytest.approx(0.04)


def test_accrues_one_trading_day(tmp_account: None) -> None:
    from portfolio import paper_account
    _seed(1_000_000.0)
    out = paper_account.accrue_cash_yield("2026-01-02", annual_rate=0.04)
    assert out == pytest.approx(1_000_000.0 * _STEP, abs=0.01)
    st = paper_account._load_account()
    assert st["cash"] == out
    assert st["cash_yield_through"] == "2026-01-02"


def test_idempotent_same_date(tmp_account: None) -> None:
    from portfolio import paper_account
    _seed(1_000_000.0)
    a = paper_account.accrue_cash_yield("2026-01-02", annual_rate=0.04)
    b = paper_account.accrue_cash_yield("2026-01-02", annual_rate=0.04)  # SAME date -> no-op
    assert a == b                                   # no double-accrual
    assert paper_account._load_account()["cash"] == a


def test_accrues_again_on_a_new_date(tmp_account: None) -> None:
    from portfolio import paper_account
    _seed(1_000_000.0)
    a = paper_account.accrue_cash_yield("2026-01-02", annual_rate=0.04)
    b = paper_account.accrue_cash_yield("2026-01-05", annual_rate=0.04)  # new date -> accrues again
    assert b > a == pytest.approx(1_000_000.0 * _STEP, abs=0.01)


def test_zero_cash_is_noop(tmp_account: None) -> None:
    from portfolio import paper_account
    _seed(0.0)
    assert paper_account.accrue_cash_yield("2026-01-02", annual_rate=0.04) == 0.0  # no crash


def test_rate_configurable_via_env(tmp_account: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from portfolio import paper_account
    monkeypatch.setenv("CASH_YIELD_ANNUAL", "0.10")
    _seed(1_000_000.0)
    out = paper_account.accrue_cash_yield("2026-01-02")     # picks up the env rate (10%)
    assert out == pytest.approx(1_000_000.0 * (1 + 0.10 / 252), abs=0.01)


def test_nav_reflects_accrued_cash(tmp_account: None) -> None:
    from portfolio import paper_account
    _seed(1_000_000.0)
    paper_account.accrue_cash_yield("2026-01-02", annual_rate=0.04)
    assert paper_account.nav({"SPY": 500.0}) == pytest.approx(1_000_000.0 * _STEP, abs=0.01)

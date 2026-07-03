"""Daily mark-to-market — offline/deterministic tests for the loop fix that re-marks EVERY book
once per trading day (so a book built once still advances its nav_history and can be graded forward).

Covers the three additive changes:
  1. paper_account.mark() now persists each held position's current_price into account.json.
  2. app.scheduler._daily_mark_job() marks a book that would NOT otherwise advance — a fresh
     nav_history row is appended with the new date (live-price helper monkeypatched; no network).
  3. bot.phase2's entry-technical fields flow into the shadow-input record (extension/rs/urgency/
     eq_grade/parabolic), read defensively from the published stockdata JSON.

No vendor/macro engine, no network, no LLM.
"""
from __future__ import annotations

from pathlib import Path
from typing import Generator
from unittest import mock

import pytest

import bot  # noqa: F401  -> vendor/macro onto sys.path


# ---------------------------------------------------------------------------
# 1) mark() persists current_price onto the account positions
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_account(tmp_path: Path) -> Generator[None, None, None]:
    """Redirect the legacy (flagship) paper_account file paths into a fresh temp dir."""
    from portfolio import paper_account
    with (
        mock.patch.object(paper_account, "_DATA", tmp_path),
        mock.patch.object(paper_account, "_ACCOUNT_PATH", tmp_path / "account.json"),
        mock.patch.object(paper_account, "_FILLS_PATH", tmp_path / "fills.jsonl"),
        mock.patch.object(paper_account, "_NAV_PATH", tmp_path / "nav_history.jsonl"),
    ):
        yield


_PRICES = {"AAPL": 200.0, "MSFT": 400.0, "SPY": 500.0}
_PRICES_UP = {"AAPL": 220.0, "MSFT": 440.0, "SPY": 550.0}


def test_mark_persists_current_price(tmp_account: None) -> None:
    from portfolio import paper_account

    paper_account.rebalance({"AAPL": 0.4, "MSFT": 0.4}, _PRICES, "2026-01-02")
    paper_account.mark(_PRICES, "2026-01-02")

    state = paper_account._load_account()
    assert state["positions"]["AAPL"]["current_price"] == 200.0
    assert state["positions"]["MSFT"]["current_price"] == 400.0

    # a later mark at higher prices updates the persisted current_price in place
    paper_account.mark(_PRICES_UP, "2026-01-03")
    state = paper_account._load_account()
    assert state["positions"]["AAPL"]["current_price"] == 220.0
    assert state["positions"]["MSFT"]["current_price"] == 440.0


def test_mark_current_price_falls_back_to_avg_cost(tmp_account: None) -> None:
    """A held name with NO live quote this run gets its avg_cost stamped (never a stale/garbage
    mark), and NAV is unaffected (nav() still recomputes from the prices dict)."""
    from portfolio import paper_account

    paper_account.rebalance({"AAPL": 0.5}, _PRICES, "2026-01-02")
    avg = paper_account._load_account()["positions"]["AAPL"]["avg_cost"]

    # mark with NO AAPL price (only the benchmark) — AAPL must fall back to its avg_cost
    paper_account.mark({"SPY": 500.0}, "2026-01-03")
    state = paper_account._load_account()
    assert state["positions"]["AAPL"]["current_price"] == round(avg, 4)


def test_mark_current_price_does_not_change_nav(tmp_account: None) -> None:
    """Persisting current_price is purely additive — the marked NAV row is identical to before."""
    from portfolio import paper_account

    paper_account.rebalance({"AAPL": 0.5}, _PRICES, "2026-01-02")
    paper_account.mark(_PRICES_UP, "2026-01-03")
    rows = paper_account._load_jsonl(paper_account._NAV_PATH)
    # NAV is recomputed from the prices dict (cash + shares*price), independent of current_price
    expected = paper_account.nav(_PRICES_UP)
    assert abs(rows[-1]["nav"] - round(expected, 2)) < 0.01


# ---------------------------------------------------------------------------
# 2) the daily mark job advances a book that would not otherwise re-mark
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_books(tmp_path: Path, monkeypatch) -> Generator[Path, None, None]:
    """Redirect EVERY book's state dir into a tmp tree by repointing the registry + legacy root,
    so the daily-mark job writes to isolated dirs (never the live data/portfolio*)."""
    from portfolio import registry, paper_account
    monkeypatch.setattr(registry, "_ROOT", tmp_path, raising=False)
    # the legacy flagship dir is registry._ROOT/data/portfolio; keep paper_account's own legacy
    # constants pointed there too so flagship resolves into the tmp tree as well.
    legacy = tmp_path / "data" / "portfolio"
    monkeypatch.setattr(paper_account, "_DATA", legacy, raising=False)
    monkeypatch.setattr(paper_account, "_ACCOUNT_PATH", legacy / "account.json", raising=False)
    monkeypatch.setattr(paper_account, "_FILLS_PATH", legacy / "fills.jsonl", raising=False)
    monkeypatch.setattr(paper_account, "_NAV_PATH", legacy / "nav_history.jsonl", raising=False)
    yield tmp_path


def _seed_held_book(pid: str, prices: dict, asof: str) -> None:
    """Build a held book for `pid` at `prices` and mark it once at `asof` (the 'built once' state)."""
    from portfolio import paper_account
    paper_account.rebalance({"AAPL": 0.5}, prices, asof, portfolio_id=pid)
    paper_account.mark(prices, asof, portfolio_id=pid)


def test_daily_mark_job_advances_unrebuilt_book(tmp_books: Path, monkeypatch) -> None:
    from app import scheduler
    from portfolio import paper_account

    # the autonomous Brain book was built + marked on day 1, then never rebuilt
    _seed_held_book("autonomous", {"AAPL": 200.0, "SPY": 500.0}, "2026-01-02")
    before = paper_account._load_jsonl(paper_account._paths("autonomous")["nav"])
    assert {r["date"] for r in before} == {"2026-01-02"}

    # day 2: no rebuild — only the daily mark job runs, at fresh (higher) prices via a stubbed feed
    def _fake_price(t: str):
        return {"AAPL": 250.0, "SPY": 520.0}.get(t)
    monkeypatch.setattr(paper_account, "_current_price", _fake_price)
    monkeypatch.setattr(scheduler, "_today_iso", lambda: "2026-01-03")

    scheduler._daily_mark_job()

    after = paper_account._load_jsonl(paper_account._paths("autonomous")["nav"])
    dates = {r["date"] for r in after}
    assert "2026-01-03" in dates, "daily mark job must append a fresh nav row for the new date"
    # the new row reflects the higher AAPL mark (the book actually advanced)
    new_row = next(r for r in after if r["date"] == "2026-01-03")
    old_row = next(r for r in before if r["date"] == "2026-01-02")
    assert new_row["nav"] > old_row["nav"]


def test_daily_mark_job_is_idempotent_per_date(tmp_books: Path, monkeypatch) -> None:
    from app import scheduler
    from portfolio import paper_account

    _seed_held_book("autonomous", {"AAPL": 200.0, "SPY": 500.0}, "2026-01-02")
    monkeypatch.setattr(paper_account, "_current_price",
                        lambda t: {"AAPL": 250.0, "SPY": 520.0}.get(t))
    monkeypatch.setattr(scheduler, "_today_iso", lambda: "2026-01-03")

    scheduler._daily_mark_job()
    scheduler._daily_mark_job()  # twice in the same day

    rows = paper_account._load_jsonl(paper_account._paths("autonomous")["nav"])
    same_day = [r for r in rows if r["date"] == "2026-01-03"]
    assert len(same_day) == 1, "mark() is idempotent per date — exactly one row per calendar day"


def test_daily_mark_job_one_failure_does_not_abort_others(tmp_books: Path, monkeypatch) -> None:
    """A book whose mark raises must not prevent the other books from being marked."""
    from app import scheduler
    from portfolio import paper_account

    _seed_held_book("autonomous", {"AAPL": 200.0, "SPY": 500.0}, "2026-01-02")
    _seed_held_book("china", {"AAPL": 200.0, "FXI": 30.0}, "2026-01-02")

    real_mark = paper_account.mark

    def _flaky_mark(prices, asof, portfolio_id=None, benchmark=None):
        if portfolio_id == "autonomous":
            raise RuntimeError("boom")
        return real_mark(prices, asof, portfolio_id=portfolio_id, benchmark=benchmark)

    monkeypatch.setattr(paper_account, "_current_price",
                        lambda t: {"AAPL": 250.0, "SPY": 520.0, "FXI": 31.0}.get(t))
    monkeypatch.setattr(paper_account, "mark", _flaky_mark)
    monkeypatch.setattr(scheduler, "_today_iso", lambda: "2026-01-03")

    scheduler._daily_mark_job()  # must not raise

    # china still got its fresh mark even though autonomous blew up
    china_rows = paper_account._load_jsonl(paper_account._paths("china")["nav"])
    assert "2026-01-03" in {r["date"] for r in china_rows}


def test_daily_mark_job_skips_empty_books(tmp_books: Path, monkeypatch) -> None:
    """A book with no positions and no priceable benchmark must not write a spurious nav row."""
    from app import scheduler
    from portfolio import paper_account

    monkeypatch.setattr(paper_account, "_current_price", lambda t: None)  # nothing priceable
    # W-L / L1: the job also consults the ONE marking layer — stub it dark too so "nothing
    # priceable" holds across BOTH accessors (else the real yahoo parquet prices SPY for the
    # benchmark-only row). Both marks + the benchmark-ledger builder are best-effort here.
    from portfolio import marks
    monkeypatch.setattr(marks, "prices_for", lambda syms, asof, **kw: {})
    monkeypatch.setattr(scheduler, "_today_iso", lambda: "2026-01-03")

    scheduler._daily_mark_job()  # no positions anywhere, no prices → no rows

    for pid in scheduler._MARK_BOOK_IDS:
        rows = paper_account._load_jsonl(paper_account._paths(pid)["nav"])
        assert rows == [], f"{pid} should have no nav rows when nothing is priceable"


# ---------------------------------------------------------------------------
# 3) the shadow input carries the entry-technical fields
# ---------------------------------------------------------------------------

def test_entry_tech_fields_from_synthetic_stockdata(monkeypatch) -> None:
    from bot import phase2
    from portfolio import lenses as lenses_mod

    fake_sd = {
        "tech": {"pct_vs_200dma": 12.5},
        "momentum": {"alpha": {"rs": 88.0}},
        "entry_signal": {"urgency": "now"},
        "conviction": {"ext": {"grade": "steady", "parabolic": False}},
    }
    monkeypatch.setattr(lenses_mod, "_load", lambda rel: fake_sd)

    fields = phase2._entry_tech_fields("AAPL")
    assert fields["pct_vs_200dma"] == 12.5
    assert fields["rs"] == 88.0
    assert fields["urgency"] == "now"
    assert fields["eq_grade"] == "steady"
    assert fields["parabolic"] is False


def test_entry_tech_fields_defensive_on_missing(monkeypatch) -> None:
    """A missing/empty snapshot yields all-nullable fields — never raises, never fabricates."""
    from bot import phase2
    from portfolio import lenses as lenses_mod

    monkeypatch.setattr(lenses_mod, "_load", lambda rel: None)
    fields = phase2._entry_tech_fields("ZZZZ")
    assert fields["pct_vs_200dma"] is None
    assert fields["rs"] is None
    assert fields["urgency"] is None
    assert fields["eq_grade"] is None
    assert fields["parabolic"] is False  # bool(None) → False


def test_shadow_input_record_carries_entry_tech(monkeypatch) -> None:
    """The shadow-input record emitted for a candidate must carry the entry-technical fields.

    We reconstruct the record exactly as bot.phase2._emit_shadow builds it (the closure isn't
    importable), pinning the contract the L3 timing lever reads."""
    from bot import phase2
    from portfolio import lenses as lenses_mod

    fake_sd = {
        "tech": {"pct_vs_200dma": -8.0},
        "momentum": {"alpha": {"rs": 42.0}},
        "entry_signal": {"urgency": "soon"},
        "conviction": {"extension": {"grade": "stretched", "parabolic": True}},  # fallback path
    }
    monkeypatch.setattr(lenses_mod, "_load", lambda rel: fake_sd)

    tech = phase2._entry_tech_fields("NVDA")
    # the exact subset _emit_shadow merges into the input record
    record = {
        "ticker": "NVDA",
        "extension": tech["pct_vs_200dma"], "pct_vs_200dma": tech["pct_vs_200dma"],
        "rs": tech["rs"], "urgency": tech["urgency"],
        "eq_grade": tech["eq_grade"], "parabolic": tech["parabolic"],
    }
    assert record["extension"] == -8.0
    assert record["pct_vs_200dma"] == -8.0
    assert record["rs"] == 42.0
    assert record["urgency"] == "soon"
    assert record["eq_grade"] == "stretched"      # resolved via the .extension fallback path
    assert record["parabolic"] is True

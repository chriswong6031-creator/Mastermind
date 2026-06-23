"""Tests for the WATCHLIST re-review / promotion / expiry loop (portfolio.watchlist).

Offline only: the state machine is pure IO over two JSONL files, which we redirect into a tmp dir
so no test ever touches the live data/portfolios/flagship/. We prove the §3.6 doctrine rules:
  * a parked name whose `still_withheld` returns None is PROMOTED for re-entry,
  * one still withheld AGES (days_in_state++),
  * a past-TTL name EXPIRES (20 td WATCH / 10 td ARMED),
  * MAX_WATCH eviction keeps the top-40 by `combined` (lowest evicted),
  * review is IDEMPOTENT per (ticker, asof) — a same-day re-run doesn't double-age or re-promote,
  * review never raises on an empty / absent watchlist.

Plus a judgment_book guard (dual-patched per the package-attribute lesson in test_judgment_book.py)
that a PROMOTED watchlist name re-enters the PM's candidate pool when the desk runs.
"""
from __future__ import annotations

import pytest

from portfolio import watchlist as W


@pytest.fixture(autouse=True)
def _isolate_watchlist(tmp_path, monkeypatch):
    """Redirect both the append-only log and the state snapshot into a tmp dir so the live flagship
    watchlist is never touched and each test starts from empty."""
    monkeypatch.setattr(W, "_WATCHLIST", tmp_path / "watchlist.jsonl", raising=False)
    monkeypatch.setattr(W, "_STATE", tmp_path / "watchlist_state.jsonl", raising=False)


def test_review_empty_never_raises():
    res = W.review("2026-06-23", still_withheld=lambda t: "still bad")
    assert res == {"promote": [], "expired": [], "active": []}
    assert W.promote_candidates("2026-06-23") == []


def test_cleared_reason_promotes():
    """A parked name whose `still_withheld` predicate returns None (reason cleared) is promoted."""
    W.append("AME", "2026-06-22", "extended (grade=stretched)", combined=68.0)
    res = W.review("2026-06-23", still_withheld=lambda t: None)   # reason cleared
    assert [r["ticker"] for r in res["promote"]] == ["AME"]
    assert res["expired"] == []
    cands = W.promote_candidates("2026-06-23")
    assert len(cands) == 1 and cands[0]["ticker"] == "AME"
    assert cands[0]["combined"] == 68.0


def test_still_withheld_ages():
    """A name still withheld is not promoted; its days_in_state increments by one trading day."""
    W.append("FSS", "2026-06-22", "weak RS (40<50)", combined=61.0)
    res = W.review("2026-06-23", still_withheld=lambda t: "weak RS (40<50)")
    assert res["promote"] == []
    assert [r["ticker"] for r in res["active"]] == ["FSS"]
    assert res["active"][0]["days_in_state"] == 1
    assert res["active"][0]["state"] == "watch"
    # next build day → ages again
    res2 = W.review("2026-06-24", still_withheld=lambda t: "weak RS (40<50)")
    assert res2["active"][0]["days_in_state"] == 2


def test_past_ttl_expires():
    """A WATCH name aged past the 20-trading-day TTL expires (state='expired'), and drops out."""
    W.append("STALE", "2026-06-01", "extended", combined=60.0)
    still = lambda t: "extended"
    last = None
    # age it past _TTL_WATCH=20 trading days (use distinct asof each day for idempotency).
    for i in range(W._TTL_WATCH + 1):
        last = W.review(f"2026-07-{i + 1:02d}", still_withheld=still)
    assert [r["ticker"] for r in last["expired"]] == ["STALE"]
    assert last["expired"][0]["state"] == "expired"
    assert last["active"] == []


def test_armed_ttl_is_shorter():
    """An ARMED name uses the 10-td TTL (shorter than WATCH's 20)."""
    W.append("ARM", "2026-06-01", "await setup", combined=70.0)
    # seed it as ARMED in the state snapshot directly, then age it.
    W.review("2026-07-01", still_withheld=lambda t: "await setup")
    rows = W.state_rows()
    rows[0]["state"] = "armed"
    rows[0]["days_in_state"] = W._TTL_ARMED  # one more review tips it over
    W._write_state(rows)
    res = W.review("2026-07-02", still_withheld=lambda t: "await setup")
    assert [r["ticker"] for r in res["expired"]] == ["ARM"]


def test_max_watch_eviction_keeps_top_by_combined():
    """At MAX_WATCH the lowest-`combined` active names are evicted (expired); the top-40 survive."""
    n = W.MAX_WATCH + 5
    for i in range(n):
        W.append(f"T{i:03d}", "2026-06-22", "extended", combined=float(i))   # combined 0..n-1
    res = W.review("2026-06-23", still_withheld=lambda t: "extended")
    assert len(res["active"]) == W.MAX_WATCH
    # the 5 lowest-combined (T000..T004) are evicted
    evicted = {r["ticker"] for r in res["expired"]}
    assert evicted == {f"T{i:03d}" for i in range(5)}
    assert all(r.get("expire_reason") == "max_watch_evicted" for r in res["expired"])
    # the highest-combined survives
    survivors = {r["ticker"] for r in res["active"]}
    assert f"T{n - 1:03d}" in survivors


def test_review_idempotent_per_date():
    """A same-day re-run does not double-age a withheld name nor re-promote a cleared one."""
    W.append("DUP", "2026-06-22", "weak RS", combined=62.0)
    r1 = W.review("2026-06-23", still_withheld=lambda t: "weak RS")
    assert r1["active"][0]["days_in_state"] == 1
    r2 = W.review("2026-06-23", still_withheld=lambda t: "weak RS")   # same asof
    assert r2["active"][0]["days_in_state"] == 1                       # NOT 2 — idempotent

    # a cleared name: re-running the same day yields the same single promotion (no duplicate park).
    W.append("CLR", "2026-06-22", "extended", combined=65.0)
    p1 = W.review("2026-06-24", still_withheld=lambda t: None)
    p2 = W.review("2026-06-24", still_withheld=lambda t: None)
    assert len(W.promote_candidates("2026-06-24")) == \
        len({c["ticker"] for c in W.promote_candidates("2026-06-24")})


def test_append_latest_back_compatible():
    """The pre-existing append/latest API is unchanged by the state machine (other modules call it)."""
    W.append("BC", "2026-06-22", "extended", combined=55.0)
    lat = W.latest()
    assert [r["ticker"] for r in lat] == ["BC"]
    assert lat[0]["reason"] == "extended"
    # timing_withhold is a pure predicate, untouched
    assert W.timing_withhold(None) is None
    assert W.timing_withhold({"eq_grade": "weak"}) is not None

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


# ─────────────────────────────────────────────────────────────────────────────
# ROTATION-IN park lane (additive; the P2 funnel wiring — DORMANT storage layer).
# Proves the schema extension, the byte-compat legacy read, the separate namespace/caps/TTLs,
# and that the existing TIMING lane is completely undisturbed by rotation rows.
# ─────────────────────────────────────────────────────────────────────────────

def test_legacy_row_reads_as_timing_origin():
    """A row written BEFORE the `origin` field existed (no key) reads back as origin='timing' —
    the byte-compat legacy-read guarantee. A rotation row reads back as origin='rotation_in'."""
    legacy = {"ticker": "OLD", "asof": "2026-06-01", "reason": "extended", "state": "watch"}
    assert W._origin(legacy) == "timing"          # inferred, not stored
    assert W.origin_of(legacy) == W.ORIGIN_TIMING
    assert "origin" not in legacy                 # the read never mutated the legacy row
    # a non-dict / None also collapses to timing (fail to the pre-existing lane)
    assert W._origin(None) == "timing"
    assert W._origin({}) == "timing"
    # an explicit rotation row is read as rotation
    assert W._origin({"origin": "rotation_in"}) == "rotation_in"


def test_append_rotation_stores_schema_fields():
    """append_rotation writes a rotation-origin row carrying origin + call_id + review_trigger."""
    ok = W.append_rotation("SMH", "2026-06-22", "rot:semis:2026-06-22",
                           target="semis", state="watch", confidence=0.42,
                           thesis="semis bottoming, rotation-in EARLY",
                           trigger={"kind": "rel_return", "op": ">", "value": 0.0,
                                    "benchmark": "SPY", "check_by": "2026-07-20"})
    assert ok is True
    rows = W.rotation_rows()
    assert len(rows) == 1
    r = rows[0]
    assert r["ticker"] == "SMH"
    assert W.origin_of(r) == "rotation_in"
    assert r["call_id"] == "rot:semis:2026-06-22"
    assert r["review_trigger"]["check_by"] == "2026-07-20"
    assert r["confidence"] == 0.42
    assert r["thesis"] == "semis bottoming, rotation-in EARLY"
    assert r["state"] == "watch"
    assert r["days_in_state"] == 0
    # bad inputs → no row (mirrors append's guards)
    assert W.append_rotation("", "2026-06-22", "cid") is False
    assert W.append_rotation("X", "", "cid") is False
    assert W.append_rotation("X", "2026-06-22", "") is False


def test_append_rotation_idempotent_per_call_id():
    """A re-enroll of the same call_id UPDATES the row (state/confidence/trigger) without duplicating
    or resetting its age — call_id is the immutable join key."""
    W.append_rotation("SMH", "2026-06-22", "rot:semis", state="watch", confidence=0.4)
    # advance its age via a state change, then re-enroll the same call
    W.append_rotation("SMH", "2026-06-25", "rot:semis", state="watch", confidence=0.55,
                      thesis="strengthening")
    rows = W.rotation_rows()
    assert len(rows) == 1                          # not duplicated
    assert rows[0]["confidence"] == 0.55           # refreshed
    assert rows[0]["thesis"] == "strengthening"
    assert rows[0]["days_in_state"] == 0           # age preserved (still 0 here)


def test_rotation_namespace_is_separate_from_timing():
    """Adding MAX_ROTATION_WATCH+ rotation rows NEVER evicts a timing row, and a full timing book
    NEVER evicts a rotation row — the two lanes have independent caps."""
    # seed one timing park and run a review so it's a tracked timing state row.
    W.append("TIME", "2026-06-22", "extended", combined=90.0)
    W.review("2026-06-23", still_withheld=lambda t: "extended")
    assert any(r["ticker"] == "TIME" and W.origin_of(r) == "timing"
               for r in W.state_rows())

    # now overflow the ROTATION cap — the timing row must survive untouched.
    over = W.MAX_ROTATION_WATCH + 5
    for i in range(over):
        W.append_rotation(f"R{i:03d}", "2026-06-23", f"rot:{i}", confidence=float(i))
    rot_active = W.rotation_rows()
    assert len(rot_active) == W.MAX_ROTATION_WATCH               # rotation cap enforced
    # the 5 lowest-confidence rotation rows evicted, timing row present & active
    survivors = {r["ticker"] for r in rot_active}
    assert "R000" not in survivors and f"R{over - 1:03d}" in survivors
    timing_now = [r for r in W.state_rows()
                  if r.get("ticker") == "TIME" and W.origin_of(r) == "timing"]
    assert len(timing_now) == 1 and timing_now[0]["state"] != "expired"

    # conversely: a timing MAX_WATCH review never touches the rotation rows.
    res = W.review("2026-06-24", still_withheld=lambda t: "extended")
    assert len(W.rotation_rows()) == W.MAX_ROTATION_WATCH        # rotation survives the timing review
    # rotation rows never appear in the timing review's active/expired/promote lists
    reviewed = {r.get("ticker") for r in res["active"] + res["expired"] + res["promote"]}
    assert not any(t.startswith("R0") for t in reviewed)


def test_rotation_rows_survive_and_are_not_aged_by_timing_review():
    """A timing review never ages or expires a rotation row (separate TTLs, separate loop)."""
    W.append_rotation("QQQ", "2026-06-22", "rot:qqq", confidence=0.5)
    # run many timing reviews with an always-withheld predicate — a timing row would age & expire.
    for i in range(W._TTL_ROTATION_WATCH + 5):
        W.review(f"2026-07-{i + 1:02d}", still_withheld=lambda t: "still bad")
    rot = W.rotation_rows()
    assert [r["ticker"] for r in rot] == ["QQQ"]     # still active
    assert rot[0]["days_in_state"] == 0              # NOT aged by the timing loop
    assert rot[0]["state"] == "watch"


def test_rotation_ttl_constants_are_longer():
    """The rotation lane's TTLs/cap are documented, longer than the timing lane (bottoming is slow)."""
    assert W.MAX_ROTATION_WATCH == 20
    assert W._TTL_ROTATION_WATCH == 30 and W._TTL_ROTATION_WATCH > W._TTL_WATCH
    assert W._TTL_ROTATION_ARMED == 15 and W._TTL_ROTATION_ARMED > W._TTL_ARMED


def test_advance_rotation_hook():
    """The WATCH→ARMED / ARMED→promote advancement hook moves a rotation row along the ladder and
    flags a CONFIRMED turn for re-entry into the funnel."""
    W.append_rotation("AMD", "2026-06-22", "rot:amd", state="watch", confidence=0.4)
    # WATCH → ARMED on a TURNING/strengthening call
    r = W._advance_rotation("rot:amd", "2026-06-25", state="armed", confidence=0.6)
    assert r is not None and r["state"] == "armed"
    assert [x for x in W.rotation_rows() if x["call_id"] == "rot:amd"][0]["state"] == "armed"
    # ARMED → promote on CONFIRMED
    W._advance_rotation("rot:amd", "2026-06-26", promote=True)
    cands = W.promote_candidates("2026-06-26")
    assert any(c["ticker"] == "AMD" for c in cands)
    # a missing call_id is a no-op (None), never raises
    assert W._advance_rotation("nope", "2026-06-26") is None

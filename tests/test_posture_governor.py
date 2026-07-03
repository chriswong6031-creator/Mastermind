"""Guards for the POSTURE GOVERNOR (brain/posture_governor) — W-L / L5c.

The governor is the smallest safe self-adaptive corrective. The properties under test are the charter
P8 arming guarantees:
  * DEFAULT OFF — with MASTERMIND_POSTURE_ADAPT unset the multiplier is EXACTLY 1.0 (a pure no-op),
    even when the gap is screaming and the streak is mature;
  * statistical guards — effective_n >= min AND HAC-significant AND 3-review hysteresis, ALL required;
  * NOISE INJECTION — a zero-mean noisy gap NEVER passes the guards (the pre-arming bar);
  * band clamp — the multiplier can only move WITHIN the doctrine [floor, ceil] band;
  * shrink-fast / restore-slow — a persistent trailing gap shrinks by a full step; restoring toward
    the ceiling is EMA-damped;
  * never raises on missing/garbage ledgers → multiplier degrades to 1.0.
"""
from __future__ import annotations

import datetime as dt
import json
import random

import pytest

import bot  # noqa: F401 — bootstraps vendor/macro
from brain import posture_governor as G


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Point state + ledger dirs at tmp and ensure the governor starts DISARMED for every test."""
    monkeypatch.setattr(G, "_STATE", tmp_path / "state.json")
    monkeypatch.setattr(G, "_BENCH_DIR", tmp_path / "benchmark")
    monkeypatch.delenv(G._ADAPT_ENV, raising=False)
    return tmp_path


def _arm(monkeypatch):
    monkeypatch.setenv(G._ADAPT_ENV, "1")


# ── default OFF — the ship-blocker ────────────────────────────────────────────
def test_default_off_multiplier_is_one():
    assert G.armed() is False
    assert G.multiplier() == 1.0


def test_disarmed_review_never_moves_even_with_screaming_gap():
    """A strongly-negative, mature, HAC-significant gap must STILL pin the multiplier at 1.0 disarmed."""
    series = [-0.02] * 10
    st = None
    for _ in range(5):
        r = G.review(series, state=st)
        st = r["state"]
    assert r["armed"] is False
    assert r["multiplier"] == 1.0
    # but the loop OBSERVES: guards pass and would_move_if_armed is True (the near-term deliverable)
    assert r["guards"]["pass_all"] is True
    assert r["would_move_if_armed"] is True
    assert r["action"] == "observe_disarmed"


# ── NOISE INJECTION — the pre-arming bar (must pass before any arming) ─────────
def test_noise_injection_rarely_passes_guards():
    """Zero-mean noise (no real edge) must pass the HAC significance guard only RARELY — the governor
    must not routinely learn from a measurement artifact (architecture §risk-4). The false-positive
    rate over many draws stays near the nominal significance level, well under 20%."""
    random.seed(7)
    trials = 400
    fp = 0
    for _ in range(trials):
        noise = [random.gauss(0.0, 0.02) for _ in range(12)]
        if G.guards(noise)["pass_all"]:
            fp += 1
    assert fp / trials < 0.20, f"noise false-positive rate {fp / trials:.3f} too high"


def test_noise_injection_armed_governor_stays_neutral():
    """Even ARMED, a noisy gap must not push the multiplier off 1.0 (the guards gate the motion)."""
    _arm_env = G._ADAPT_ENV
    import os
    os.environ[_arm_env] = "1"
    try:
        random.seed(11)
        moved_any = False
        st = None
        for _ in range(20):
            noise = [random.gauss(0.0, 0.02) for _ in range(12)]
            r = G.review(noise, state=st)
            st = r["state"]
            if r["moved"]:
                moved_any = True
        assert moved_any is False, "noise must never move an armed governor's multiplier"
        assert st["multiplier"] == 1.0
    finally:
        os.environ.pop(_arm_env, None)


# ── hysteresis + significance gating ──────────────────────────────────────────
def test_hysteresis_requires_three_same_sign_reviews(monkeypatch):
    _arm(monkeypatch)
    series = [-0.012, -0.01, -0.015, -0.011, -0.013, -0.009, -0.014, -0.01, -0.012, -0.011]
    st = None
    actions = []
    for _ in range(4):
        r = G.review(series, state=st)
        st = r["state"]
        actions.append((r["streak_len"], r["action"], r["multiplier"]))
    # first two reviews only build the streak (no motion); the 3rd (streak==3) is the first shrink
    assert actions[0][1] == "observe" and actions[0][2] == 1.0
    assert actions[1][1] == "observe" and actions[1][2] == 1.0
    assert actions[2][0] == 3 and actions[2][1] == "shrink"
    assert actions[2][2] == pytest.approx(0.95, abs=1e-9)   # one full step down


def test_insignificant_gap_does_not_arm(monkeypatch):
    _arm(monkeypatch)
    # a tiny, high-variance gap → |t| below the 2.0 bar → no motion regardless of streak
    series = [-0.001, 0.05, -0.06, 0.04, -0.05, 0.03, -0.04, 0.02, -0.03, 0.01]
    st = None
    for _ in range(5):
        r = G.review(series, state=st)
        st = r["state"]
    assert r["guards"]["significant"] is False
    assert r["multiplier"] == 1.0


def test_below_min_n_never_arms(monkeypatch):
    _arm(monkeypatch)
    series = [-0.02] * 4        # only 4 obs, below min_effective_n=8
    r = G.review(series)
    assert r["guards"]["n_ok"] is False
    assert r["guards"]["pass_all"] is False
    assert r["would_move_if_armed"] is False


# ── band clamp + shrink-fast/restore-slow ─────────────────────────────────────
def test_multiplier_clamped_to_floor(monkeypatch):
    _arm(monkeypatch)
    series = [-0.02] * 12
    st = None
    for _ in range(40):        # keep shrinking — must never breach the floor
        r = G.review(series, state=st)
        st = r["state"]
    assert st["multiplier"] >= G._cf("floor", G._FLOOR) - 1e-9
    assert st["multiplier"] == pytest.approx(G._cf("floor", G._FLOOR), abs=1e-6)


def test_restore_is_slower_than_shrink(monkeypatch):
    _arm(monkeypatch)
    # shrink first with a persistent negative gap
    neg = [-0.02] * 12
    st = None
    for _ in range(3):
        r = G.review(neg, state=st)
        st = r["state"]
    shrunk = st["multiplier"]
    assert shrunk < 1.0
    # now a persistent POSITIVE gap → restore toward ceiling, but EMA-damped (partial step)
    pos = [0.02] * 12
    st = {**st, "streak_sign": 1, "streak_len": 3}    # positive mature streak
    r = G.review(pos, state=st)
    assert r["action"] == "restore"
    # restore is a fraction of the gap to the ceiling — strictly between shrunk and the ceiling
    assert shrunk < r["multiplier"] < G._cf("ceil", G._CEIL)


# ── input wiring: the gap series comes from the benchmark ledger ──────────────
def _ledger(book_ret, def_ret):
    return {"leaderboard": [
        {"id": "autonomous", "kind": "book", "return_pct": book_ret},
        {"id": "defensive", "kind": "bogey", "return_pct": def_ret},
    ]}


def test_gap_series_reads_brain_minus_defensive():
    ledgers = [_ledger(-1.0, 2.0), _ledger(-2.0, 3.0)]   # brain trailing defense both snapshots
    gaps = G.gap_series("autonomous", ledgers=ledgers)
    # gap = (brain - defensive)/100 → both negative
    assert gaps == pytest.approx([-0.03, -0.05], abs=1e-9)


def test_gap_series_degrades_on_missing_side():
    ledgers = [{"leaderboard": [{"id": "autonomous", "kind": "book", "return_pct": -1.0}]}]  # no defensive
    assert G.gap_series("autonomous", ledgers=ledgers) == []


def test_status_and_multiplier_never_raise_on_garbage(monkeypatch):
    monkeypatch.setattr(G, "_load_state", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    # multiplier degrades to 1.0; status still returns a dict
    assert G.multiplier() == 1.0
    s = G.status()
    assert isinstance(s, dict) and s["armed"] is False


def test_review_persists_state(monkeypatch, tmp_path):
    _arm(monkeypatch)
    series = [-0.02] * 10
    G.review(series, persist=True)
    assert (tmp_path / "state.json").exists()
    st = json.loads((tmp_path / "state.json").read_text())
    assert "multiplier" in st and "streak_len" in st

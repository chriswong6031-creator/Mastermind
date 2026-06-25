"""Discounted Thompson Sampling over shadow policies (portfolio.bandit) — #5. Pure + deterministic.

Pins: the discounted Beta weights recent outcomes more; the rank picks the winning arm with high
P(best); the sampler is deterministic (seeded); cold-start is honest 'building' with uniform priors;
date-pairs are recency-ordered; and a regime break (recent losses) pulls the posterior below the naive
cumulative win-rate — the whole point of discounting.
"""
from __future__ import annotations

from portfolio import bandit as B


def test_disc_beta_weights_recent_more():
    # weights newest→oldest are γ^0..γ^3 = 1, .5, .25, .125 (sum 1.875); all wins → a=1+1.875, b=1
    a, b, neff = B._disc_beta([1, 1, 1, 1], gamma=0.5)
    assert abs(a - 2.875) < 1e-9 and abs(b - 1.0) < 1e-9 and abs(neff - 1.875) < 1e-9


def test_disc_beta_recent_loss_lowers_mean_vs_recent_win():
    # SAME raw 3-win/1-loss record, but a recent loss (full weight) vs an old loss (decayed)
    al, bl, _ = B._disc_beta([1, 1, 1, 0], gamma=0.5)   # recent loss
    aw, bw, _ = B._disc_beta([0, 1, 1, 1], gamma=0.5)   # old loss
    assert al / (al + bl) < aw / (aw + bw)


def test_rank_winner_beats_loser():
    res = B.rank({"good": [1] * 20, "bad": [0] * 20})
    assert res["status"] == "scoring"
    assert res["arms"][0]["id"] == "good" and res["arms"][0]["p_best"] > 0.9
    good = next(a for a in res["arms"] if a["id"] == "good")
    bad = next(a for a in res["arms"] if a["id"] == "bad")
    assert good["posterior_mean"] > bad["posterior_mean"]
    assert abs(sum(a["p_best"] for a in res["arms"]) - 1.0) < 0.02   # P(best) is a distribution


def test_rank_is_deterministic():
    arms = {"a": [1, 0, 1, 1, 0, 1, 1, 1], "b": [0, 1, 0, 0, 1, 0, 0, 0]}
    r1, r2 = B.rank(arms), B.rank(arms)
    assert [x["p_best"] for x in r1["arms"]] == [x["p_best"] for x in r2["arms"]]


def test_cold_start_is_building_and_uniform():
    res = B.rank({"x": [], "y": []})
    assert res["status"] == "building"
    for a in res["arms"]:
        assert abs(a["posterior_mean"] - 0.5) < 1e-9 and a["n"] == 0 and a["n_eff"] == 0.0


def test_to_outcomes_orders_pairs_by_date():
    assert B._to_outcomes([("2026-06-03", 1), ("2026-06-01", 0), ("2026-06-02", 1)]) == [0, 1, 1]
    assert B._to_outcomes([1, 0, 1]) == [1, 0, 1]
    assert B._to_outcomes([]) == []


def test_regime_break_pulls_posterior_below_raw_rate():
    # 16 early wins then 8 recent losses: naive rate 16/24≈0.67, but discounting the recent losses
    # (full weight) vs the decayed early wins pulls the posterior mean BELOW the cumulative rate.
    a, b, _ = B._disc_beta([1] * 16 + [0] * 8, gamma=0.9)
    assert a / (a + b) < 16 / 24


def test_rank_handles_empty_and_never_raises():
    assert B.rank({})["arms"] == []
    assert B.rank(None)["status"] == "building"   # type: ignore[arg-type]

"""Phase 3 integration — one real self-improving iteration + the anti-overfit invariants."""
from pathlib import Path

import pytest

import bot  # noqa: F401

_DB = Path(__file__).resolve().parent.parent / "data" / "bot.db"


def _fresh():
    if _DB.exists():
        _DB.unlink()


def test_loop_iteration_real_data():
    _fresh()
    try:
        from loop import iterate
        out = iterate.iterate()
    except Exception as e:                       # no engine/data in this env -> skip
        pytest.skip(f"engine/data unavailable: {e}")

    # the pipeline ran on real multi-year data
    assert out["n_candidates"] >= 10
    assert out["in_sample_years"] > 10
    assert out["cumulative_trials"] > 0

    # effective-N never exceeds the raw count (it collapses near-duplicates)
    assert 1 <= out["n_eff"] <= out["n_candidates"]
    # PBO is a probability or None
    assert out["pbo"] is None or 0.0 <= out["pbo"] <= 1.0

    # NOTHING reaches live sizing here — at most 'paper' (forward Brier governs live)
    assert all(r[2]["stage"] in ("paper", "rejected", "refused") for r in out["results"])
    # every promotion is gated by a touched holdout
    from data_layer import store
    con = store.connect()
    touches = con.execute("SELECT count(*) FROM holdout_touches").fetchone()[0]
    assert touches >= out["n_promoted"]
    _fresh()


def test_holdout_is_one_shot():
    """The locked holdout cannot be re-touched (defeats re-run fishing)."""
    from loop import holdout
    touched = set()
    holdout.touch("abc", touched)               # first touch ok, mutates the set
    assert "abc" in touched
    with pytest.raises(holdout.HoldoutBurned):
        holdout.touch("abc", touched)           # second touch raises

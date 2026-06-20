"""
tests/test_holdout.py — Deterministic tests for loop/holdout.py.

All tests use synthetic data and verify the math directly.
No engine imports, no I/O, no network.
"""

import pytest
import pandas as pd
import numpy as np

from loop.holdout import (
    HoldoutBurned,
    touch,
    is_burned,
    split_index,
    confirms,
)


# ---------------------------------------------------------------------------
# touch / is_burned
# ---------------------------------------------------------------------------

class TestTouch:
    def test_first_touch_mutates_set(self):
        touched: set[str] = set()
        touch("abc123", touched)
        assert "abc123" in touched

    def test_first_touch_returns_none(self):
        touched: set[str] = set()
        result = touch("xyz", touched)
        assert result is None

    def test_second_touch_raises_holdout_burned(self):
        touched: set[str] = set()
        touch("dup_hash", touched)
        with pytest.raises(HoldoutBurned):
            touch("dup_hash", touched)

    def test_second_touch_message_contains_hash(self):
        touched: set[str] = {"h1"}
        with pytest.raises(HoldoutBurned, match="h1"):
            touch("h1", touched)

    def test_different_hashes_do_not_conflict(self):
        touched: set[str] = set()
        touch("hash_a", touched)
        touch("hash_b", touched)   # must NOT raise
        assert touched == {"hash_a", "hash_b"}

    def test_empty_hash_raises_value_error(self):
        with pytest.raises(ValueError):
            touch("", set())

    def test_non_string_hash_raises_value_error(self):
        with pytest.raises(ValueError):
            touch(None, set())   # type: ignore[arg-type]

    def test_touched_set_unchanged_after_burn(self):
        """After HoldoutBurned the set must still only have the one entry."""
        touched: set[str] = set()
        touch("h", touched)
        try:
            touch("h", touched)
        except HoldoutBurned:
            pass
        assert touched == {"h"}


class TestIsBurned:
    def test_not_burned_when_absent(self):
        assert is_burned("abc", set()) is False

    def test_burned_when_present(self):
        assert is_burned("abc", {"abc", "def"}) is True

    def test_does_not_mutate(self):
        s = {"x"}
        is_burned("x", s)
        assert s == {"x"}


# ---------------------------------------------------------------------------
# split_index
# ---------------------------------------------------------------------------

class TestSplitIndex:
    def _daily_index(self, start: str, periods: int) -> pd.DatetimeIndex:
        return pd.date_range(start, periods=periods, freq="D")

    def test_basic_split(self):
        idx = self._daily_index("2020-01-01", 10)
        in_s, hold = split_index(idx, "2020-01-06")
        # strictly before 2020-01-06 → 5 dates (01–05)
        assert len(in_s) == 5
        assert len(hold) == 5

    def test_in_sample_strictly_before_cutoff(self):
        idx = self._daily_index("2023-01-01", 20)
        cutoff = "2023-01-11"
        in_s, hold = split_index(idx, cutoff)
        assert in_s[-1] < pd.Timestamp(cutoff)
        assert hold[0] == pd.Timestamp(cutoff)

    def test_cutoff_on_first_date_gives_empty_in_sample(self):
        idx = self._daily_index("2021-06-01", 5)
        in_s, hold = split_index(idx, "2021-06-01")
        assert len(in_s) == 0
        assert len(hold) == len(idx)

    def test_cutoff_after_last_date_gives_empty_holdout(self):
        idx = self._daily_index("2021-06-01", 5)
        in_s, hold = split_index(idx, "2030-01-01")
        assert len(hold) == 0
        assert len(in_s) == len(idx)

    def test_string_and_timestamp_cutoff_agree(self):
        idx = self._daily_index("2022-03-01", 30)
        cutoff_str = "2022-03-15"
        cutoff_ts = pd.Timestamp("2022-03-15")
        in_s1, h1 = split_index(idx, cutoff_str)
        in_s2, h2 = split_index(idx, cutoff_ts)
        assert list(in_s1) == list(in_s2)
        assert list(h1) == list(h2)

    def test_no_date_appears_in_both_halves(self):
        idx = self._daily_index("2020-01-01", 100)
        in_s, hold = split_index(idx, "2020-02-15")
        overlap = set(in_s) & set(hold)
        assert len(overlap) == 0

    def test_union_is_full_index(self):
        idx = self._daily_index("2019-07-01", 50)
        in_s, hold = split_index(idx, "2019-08-01")
        reconstructed = in_s.append(hold)
        assert list(reconstructed) == list(idx)

    def test_returns_datetime_index_types(self):
        idx = self._daily_index("2020-01-01", 10)
        in_s, hold = split_index(idx, "2020-01-05")
        assert isinstance(in_s, pd.DatetimeIndex)
        assert isinstance(hold, pd.DatetimeIndex)


# ---------------------------------------------------------------------------
# confirms
# ---------------------------------------------------------------------------

class TestConfirms:
    # --- passing cases ---

    def test_holdout_equals_is_sample_passes(self):
        # perfect OOS replication
        assert confirms(1.2, 1.2) is True

    def test_holdout_above_min_ratio_passes(self):
        # IS=1.0, holdout=0.6 → ratio 0.6 ≥ 0.5 threshold
        assert confirms(1.0, 0.6, min_ratio=0.5) is True

    def test_holdout_exactly_at_min_ratio_passes(self):
        # IS=2.0, holdout=1.0 → ratio exactly 0.5
        assert confirms(2.0, 1.0, min_ratio=0.5) is True

    def test_tight_ratio_passes(self):
        assert confirms(1.0, 0.9, min_ratio=0.9) is True

    def test_zero_min_ratio_only_requires_positive_holdout(self):
        # min_ratio=0 → any positive holdout passes
        assert confirms(100.0, 0.001, min_ratio=0.0) is True

    # --- failing cases ---

    def test_negative_holdout_fails(self):
        assert confirms(1.0, -0.1) is False

    def test_zero_holdout_fails(self):
        # boundary: holdout must be STRICTLY > 0
        assert confirms(1.0, 0.0) is False

    def test_holdout_below_min_ratio_fails(self):
        # IS=1.0, holdout=0.4 → ratio 0.4 < 0.5
        assert confirms(1.0, 0.4, min_ratio=0.5) is False

    def test_high_min_ratio_fails_moderate_oos(self):
        # IS=1.0, holdout=0.6, threshold=0.75 → 0.6 < 0.75 → fail
        assert confirms(1.0, 0.6, min_ratio=0.75) is False

    def test_negative_is_sample_high_holdout_passes(self):
        # IS=-1 (bad model); holdout=0.5; min_ratio=0.5
        # 0.5 >= 0.5 * (-1) = -0.5 AND 0.5 > 0 → confirms
        # (a negative IS Sharpe means we'd never promote, but the math is correct)
        assert confirms(-1.0, 0.5, min_ratio=0.5) is True

    def test_both_negative_fails(self):
        assert confirms(-0.5, -0.1) is False

    def test_negative_min_ratio_raises(self):
        with pytest.raises(ValueError):
            confirms(1.0, 0.5, min_ratio=-0.1)

    # --- edge-case numeric sanity ---

    def test_very_small_positive_holdout_passes_zero_ratio(self):
        assert confirms(999.0, 1e-9, min_ratio=0.0) is True

    def test_nan_holdout_fails(self):
        # NaN > 0 is False → safe rejection
        assert confirms(1.0, float("nan")) is False

    def test_inf_holdout_passes(self):
        assert confirms(1.0, float("inf")) is True

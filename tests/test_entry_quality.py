"""Guards for the ADVISORY entry-quality annotator (portfolio.entry_quality).

Fully offline: no vendor/macro engine, no network, no store. Every external read is INJECTED
(`series`, `stockdata`, `pulse`), so these tests pin the pure logic and the fail-open contract
without touching prod state. The load-bearing properties:

  * ADVISORY / SUBTRACT-NOTHING: annotate() only ever returns a note dict — never a weight, size,
    fill, or gate outcome. (The whole module is read-only by construction; this test pins the shape.)
  * FAIL-OPEN (charter P2): with NO inputs the verdict is 'unknown' — a missing signal informs
    nothing and can never manufacture a veto/withhold.
  * CHASE detection: a name at the top of its own recent range that just ripped is flagged 'chase'
    (the buy-high failure mode the existing 200dma-only brakes miss).
  * EXTENDED vs PULLBACK: stretched-but-not-fresh reads 'extended'; near the bottom of the range
    reads 'clean' (a dip is not a chase).
  * SECTOR heat: a cooling/broken sector_pulse theme reads 'cooling_sector'; extended + no tailwind
    composes to 'wait'; heating + clean_entry stays 'clean' (the entry the doctrine wants).
"""
from __future__ import annotations

import pytest

from portfolio import entry_quality as eq


# ── a tiny stand-in for a pandas close-price Series (ascending) ────────────────
# entry_quality only calls len(), .iloc[], .mean(), .min(), .max() — a thin wrapper over a list
# reproduces that surface so the test needs no pandas fixture data.
class _Series:
    def __init__(self, vals):
        self._v = [float(x) for x in vals]

    def __len__(self):
        return len(self._v)

    @property
    def iloc(self):
        return self

    def __getitem__(self, k):
        # supports iloc[-1], iloc[-11], and slice iloc[-20:] / iloc[-60:]
        if isinstance(k, slice):
            return _Series(self._v[k])
        return self._v[k]

    def mean(self):
        return sum(self._v) / len(self._v) if self._v else 0.0

    def min(self):
        return min(self._v)

    def max(self):
        return max(self._v)


def _flat(n=80, level=100.0):
    """A flat series — no extension, mid-range: the clean baseline."""
    return _Series([level] * n)


def _ripped(n=80, base=100.0, rip=0.30):
    """A series that spent 60d flat then ripped `rip` over the last ~10 sessions to a fresh high."""
    vals = [base] * (n - 10)
    step = (base * rip) / 10.0
    for i in range(1, 11):
        vals.append(base + step * i)
    return _Series(vals)


# ── FAIL-OPEN: no inputs → 'unknown', informs nothing, vetoes nothing ─────────
def test_no_inputs_is_unknown(monkeypatch):
    # W8: a live vendor checkout would let the default loaders find real data — pin them dead so
    # this stays the NO-INPUTS contract regardless of environment.
    from portfolio import lenses as _lenses
    monkeypatch.setattr(_lenses, "_closes", lambda t: None)
    monkeypatch.setattr(_lenses, "_load", lambda rel: None)
    out = eq.annotate("NVDA", series=None, stockdata=None, pulse=None)
    assert out["verdict"] == "unknown"
    assert out["notes"] == []
    assert out["sources"] == []


def test_short_series_fails_open():
    # < 21 points → no fast metrics; with no other source the verdict is 'unknown' (never a veto).
    out = eq.annotate("X", series=_Series([100.0] * 5))
    assert out["verdict"] == "unknown"
    assert out["metrics"]["pct_vs_20dma"] is None


def test_return_shape_is_advisory_only():
    # the ONLY outputs are the advisory fields — never a weight/size/fill/gate key.
    out = eq.annotate("X", series=_flat())
    assert set(out) == {"ticker", "verdict", "notes", "metrics", "sources", "as_of"}
    for forbidden in ("weight", "size", "fill", "shares", "gate", "size_authority", "action"):
        assert forbidden not in out
    assert isinstance(out["notes"], list) and isinstance(out["metrics"], dict)


# ── CHASE: top of the range + fresh rip → the buy-high failure mode ───────────
def test_fresh_rip_to_range_top_is_chase():
    out = eq.annotate("HOT", series=_ripped(rip=0.30))
    assert out["verdict"] == "chase"
    assert any("CHASE" in n for n in out["notes"])
    m = out["metrics"]
    assert m["range_pctile_60d"] is not None and m["range_pctile_60d"] >= 90.0
    assert m["ret_10d"] is not None and m["ret_10d"] > 0.18


def test_flat_series_is_clean():
    # flat = mid/low of a zero-width range, no rip, no extension → a fine place to start.
    out = eq.annotate("CALM", series=_flat())
    assert out["verdict"] == "clean"
    assert "price_series" in out["sources"]


def test_pullback_low_in_range_is_not_a_chase():
    # a series that ROSE then pulled back sits low in its 60d range → clean (a dip, not a chase).
    vals = [90.0] * 10 + [130.0] * 55 + [102.0] * 15   # ran up, now well off the range high
    out = eq.annotate("DIP", series=_Series(vals))
    assert out["verdict"] in ("clean", "extended")     # never 'chase'
    assert out["verdict"] != "chase"


# ── EXTENDED: stretched-but-not-fresh, from published slow fields ─────────────
def test_far_above_200dma_is_extended():
    out = eq.annotate("STR", series=_flat(),
                      stockdata={"tech": {"pct_vs_200dma": 45.0, "off_52w_high_pct": -20.0}})
    assert out["verdict"] == "extended"
    assert any("200dma" in n for n in out["notes"])


def test_pinned_at_52w_high_is_extended():
    # F1: pinned within 6% of the 52w high (but not a fresh rip) → 'extended', not a false 'clean'.
    out = eq.annotate("TOP", series=_flat(),
                      stockdata={"tech": {"pct_vs_200dma": 12.0, "off_52w_high_pct": -2.0}})
    assert out["verdict"] == "extended"


def test_moderate_stockdata_is_clean():
    out = eq.annotate("OK", series=_flat(),
                      stockdata={"tech": {"pct_vs_200dma": 8.0, "off_52w_high_pct": -18.0}})
    assert out["verdict"] == "clean"


# ── SECTOR heat via sector_pulse.json (the new shared product) ────────────────
def _pulse(theme_id, heat, clean=None):
    return {"schema": 1, "as_of": "2026-07-03", "region": "US",
            "themes": [{"id": theme_id, "label": theme_id, "heat": heat,
                        "clean_entry": {"flag": clean, "quality": "b"}, "rank_delta_5d": 0}]}


def test_cooling_sector_is_flagged():
    out = eq.annotate("SEMI", series=_flat(), theme_id="ai_semis",
                      pulse=_pulse("ai_semis", "cooling"))
    assert out["verdict"] == "cooling_sector"
    assert "sector_pulse" in out["sources"]
    assert any("cooling" in n for n in out["notes"])


def test_broken_sector_is_cooling_verdict():
    out = eq.annotate("SEMI", series=_flat(), theme_id="ai_semis",
                      pulse=_pulse("ai_semis", "broken"))
    assert out["verdict"] == "cooling_sector"


def test_extended_plus_cooling_composes_to_wait():
    # the worst entry: stretched AND no sector tailwind → 'wait' (a composed advisory).
    out = eq.annotate("SEMI", series=_flat(), theme_id="ai_semis",
                      stockdata={"tech": {"pct_vs_200dma": 40.0}},
                      pulse=_pulse("ai_semis", "cooling"))
    assert out["verdict"] == "wait"


def test_heating_clean_entry_stays_clean():
    out = eq.annotate("SEMI", series=_flat(), theme_id="ai_semis",
                      pulse=_pulse("ai_semis", "heating", clean=True))
    assert out["verdict"] == "clean"
    assert any("doctrine wants" in n for n in out["notes"])


def test_unknown_theme_in_pulse_fails_open():
    # a theme not present in sector_pulse → no heat contribution, verdict falls to the price read.
    out = eq.annotate("X", series=_flat(), theme_id="not_a_theme", pulse=_pulse("other", "cooling"))
    assert out["verdict"] == "clean"           # cooling was for a DIFFERENT theme → ignored
    assert "sector_pulse" not in out["sources"]


def test_malformed_pulse_never_raises():
    for bad in (None, {}, {"schema": 99, "themes": [{"id": "x", "heat": "cooling"}]}, {"themes": "nope"}):
        out = eq.annotate("X", series=_flat(), theme_id="x", pulse=bad)
        assert out["verdict"] in ("clean", "unknown", "extended")   # never raises, never a veto


# ── the composed CHASE beats a merely-extended slow read ──────────────────────
def test_chase_takes_precedence_over_extended():
    # a fresh rip to the range top AND far above the 200dma → 'chase' (the acute read wins).
    out = eq.annotate("HOT", series=_ripped(rip=0.35),
                      stockdata={"tech": {"pct_vs_200dma": 50.0}})
    assert out["verdict"] == "chase"


# ── production-path derivation (audit fix: phase2 passes neither theme_id nor pulse) ──
def test_theme_id_derived_from_stockdata_block(monkeypatch):
    """The phase2 call site passes no theme_id/pulse — annotate must derive the theme id
    from the stockdata sector_pulse block and load the vendored whole-board pulse itself,
    so the cooling_sector verdict is reachable in production (audit: it never fired)."""
    from portfolio import lenses

    def _fake_load(rel):
        assert rel == "site/basketdata/sector_pulse.json"
        return _pulse("ai_semis", "cooling")

    monkeypatch.setattr(lenses, "_load", _fake_load)
    out = eq.annotate("SEMI", series=_flat(),
                      stockdata={"tech": {}, "sector_pulse": {"theme_id": "ai_semis"}})
    assert out["verdict"] == "cooling_sector"
    assert "sector_pulse" in out["sources"]


def test_derivation_fails_open_without_stockdata_block(monkeypatch):
    from portfolio import lenses
    monkeypatch.setattr(lenses, "_load", lambda rel: None)
    out = eq.annotate("SEMI", series=_flat(), stockdata={"tech": {}})
    assert out["verdict"] in ("clean", "unknown")     # no pulse leg, no crash, no veto
    assert "sector_pulse" not in out["sources"]

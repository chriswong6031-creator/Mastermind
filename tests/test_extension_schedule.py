"""W2.2 — GRADED EXTENSION SCHEDULE on conviction entries (portfolio/conviction.py).

The brake is a graded ENTRY-size multiplier off pct_vs_200dma, read from the extension lens row:
    < 30% vs 200dma  → ext_mult 1.0  (no brake)
    >= 30%           → ext_mult = _INITIAL_SIZE_FRACTION  (initial-size only)
    >= 45%           → ext_mult 0.0  (no NEW add — the name is not sized this build)
    parabolic        → UNCHANGED hard veto upstream (never reaches sizing) — not re-tested here
    HELD name        → ext_mult 1.0  (an extension read is an entry brake, never an exit)
    missing ext data → ext_mult 1.0  (W0 fail-closed already blocks degraded names; no double-punish)

These are pinned two ways: the pure ``_ext_mult`` unit (deterministic, no vendor data) and an
end-to-end ``build()`` check (with lenses.full monkeypatched) proving the multiplier COMPOSES with —
does not replace — the confluence/confirmation sizing, and that held names are untouched.
"""
from __future__ import annotations

import pytest

from portfolio import conviction, lenses


# ── fixtures ──────────────────────────────────────────────────────────────────
def _rows(pct_vs_200dma=None, *, grade=None, parabolic=False, extra=None):
    """A minimal rows list carrying an extension lens row (the only row _ext_mult reads) plus the
    trend+sector_rs bull rows so a confirmed-leader path is exercised where needed."""
    rows = [
        {"lens": "extension",
         "value": {"grade": grade, "parabolic": parabolic, "pct_vs_200dma": pct_vs_200dma}},
        {"lens": "trend", "direction": "bull"},
        {"lens": "sector_rs", "direction": "bull"},
    ]
    if extra:
        rows.extend(extra)
    return rows


# ── the pure multiplier (deterministic, no vendor data) ───────────────────────
def test_below_moderate_is_unbraked():
    assert conviction._ext_mult(_rows(pct_vs_200dma=0.0), is_held=False) == 1.0
    assert conviction._ext_mult(_rows(pct_vs_200dma=29.9), is_held=False) == 1.0


def test_moderate_band_is_initial_only():
    m = conviction._ext_mult(_rows(pct_vs_200dma=30.0), is_held=False)
    assert m == conviction._INITIAL_SIZE_FRACTION
    assert conviction._ext_mult(_rows(pct_vs_200dma=44.9), is_held=False) == conviction._INITIAL_SIZE_FRACTION


def test_no_add_band_zeroes_new_adds():
    assert conviction._ext_mult(_rows(pct_vs_200dma=45.0), is_held=False) == 0.0
    assert conviction._ext_mult(_rows(pct_vs_200dma=120.0), is_held=False) == 0.0


def test_held_name_is_exempt_from_the_brake():
    # a held/leading name is NEVER trimmed on how far it has run — an extension read is an entry
    # brake, not an exit signal (masterplan §0: no exit/veto on a held/leading name).
    assert conviction._ext_mult(_rows(pct_vs_200dma=120.0), is_held=True) == 1.0
    assert conviction._ext_mult(_rows(pct_vs_200dma=45.0), is_held=True) == 1.0


def test_missing_extension_data_does_not_brake():
    # no extension row at all → 1.0 (do not double-punish partial data; W0 already blocks degraded).
    assert conviction._ext_mult([{"lens": "trend", "direction": "bull"}], is_held=False) == 1.0
    # extension row present but pct_vs_200dma is None (partial read) → 1.0.
    assert conviction._ext_mult(_rows(pct_vs_200dma=None), is_held=False) == 1.0
    # a non-numeric value must never raise and never brake.
    assert conviction._ext_mult(_rows(pct_vs_200dma="n/a"), is_held=False) == 1.0


def test_thresholds_come_from_doctrine(monkeypatch):
    moderate, no_add = conviction._ext_schedule()
    assert (moderate, no_add) == (30.0, 45.0)     # the shipped doctrine values


# ── end-to-end: the multiplier composes with confluence/confirmation sizing ───
def _fake_full(rows, *, size_authority="up", confluence=0.5):
    return {
        "rows": rows,
        "synthesis": {
            "size_authority": size_authority, "confluence": confluence,
            "vetoes": [], "bull": 3, "bear": 0,
            "data_degraded": False, "stockdata_present": True,
            "price_downtrend": False, "divergences": [],
        },
    }


def test_extended_new_add_is_sized_down_not_dropped_from_gate(monkeypatch):
    """A confirmed leader that has run 35% above its 200dma still PASSES the gate (it is not a veto),
    but its weight is scaled by the initial-size fraction relative to an un-extended twin."""
    def _full(t, kind="name"):
        pv = {"CLEAN": 5.0, "EXT": 35.0}[t]
        return _fake_full(_rows(pct_vs_200dma=pv), confluence=0.5)
    monkeypatch.setattr(conviction, "candidates", lambda: ["CLEAN", "EXT"])
    monkeypatch.setattr(lenses, "full", _full)
    sized, _rej = conviction.build(0.30, name_cap=0.08, held=set())
    w = {p["ticker"]: p["weight"] for p in sized}
    assert "CLEAN" in w and "EXT" in w
    # both share the same confluence → same confluence-weighted base; EXT is braked to the initial
    # fraction, so its weight is exactly _INITIAL_SIZE_FRACTION of CLEAN's (compose, not replace).
    assert w["EXT"] == pytest.approx(w["CLEAN"] * conviction._INITIAL_SIZE_FRACTION, rel=1e-6)
    ext = next(p for p in sized if p["ticker"] == "EXT")
    assert ext["ext_mult"] == conviction._INITIAL_SIZE_FRACTION
    assert ext["size_stage"] == "initial"


def test_far_extended_new_add_is_not_sized(monkeypatch):
    """A NEW add >= 45% above 200dma is zeroed (ext_mult 0.0) → it falls out of the weight>0 book."""
    monkeypatch.setattr(conviction, "candidates", lambda: ["MOON"])
    monkeypatch.setattr(lenses, "full",
                        lambda t, kind="name": _fake_full(_rows(pct_vs_200dma=80.0), confluence=0.6))
    sized, _rej = conviction.build(0.30, name_cap=0.08, held=set())
    assert "MOON" not in {p["ticker"] for p in sized}    # >=45% NEW add takes no size


def test_held_far_extended_name_is_not_trimmed_by_the_brake(monkeypatch):
    """The SAME 80%-extended name, if HELD, is NOT zeroed — the brake is entry-only. It survives at a
    real (unbraked-by-extension) weight so a held leader is never churned on how far it has run."""
    monkeypatch.setattr(conviction, "candidates", lambda: ["MOON"])
    monkeypatch.setattr(lenses, "full",
                        lambda t, kind="name": _fake_full(_rows(pct_vs_200dma=80.0), confluence=0.6))
    sized, _rej = conviction.build(0.30, name_cap=0.08, held={"MOON"})
    moon = {p["ticker"]: p for p in sized}
    assert "MOON" in moon and moon["MOON"]["weight"] > 0     # held → not zeroed by the extension brake
    assert moon["MOON"]["ext_mult"] == 1.0


def test_unextended_build_is_byte_identical_to_pre_brake(monkeypatch):
    """Regression: with every name under the moderate threshold, ext_mult is 1.0 everywhere and the
    book is exactly what it was before the brake existed (the compose-with degrade path)."""
    monkeypatch.setattr(conviction, "candidates", lambda: ["AAAX", "BBBX"])
    monkeypatch.setattr(lenses, "full",
                        lambda t, kind="name": _fake_full(_rows(pct_vs_200dma=10.0), confluence=0.5))
    sized, _rej = conviction.build(0.30, name_cap=0.08, held=set())
    assert {p["ticker"] for p in sized} == {"AAAX", "BBBX"}
    assert all(p["ext_mult"] == 1.0 for p in sized)

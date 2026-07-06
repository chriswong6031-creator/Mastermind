"""Tests for portfolio.firm_allocator — MW5 Lane A (docket M3).

Covers:
  - equal-weight base when lifecycle missing (degradation)
  - active-return tilt (positive tilt increases weight)
  - probation penalty (half weight)
  - retired-recommendation → weight 0
  - correlation penalty (lower-graded book in noisy-mirror pair loses _CORR_PENALTY factor)
  - mandate breach penalty
  - weights sum to 1.0 over eligible books
  - clip invariant: tilt z is bounded
  - regional caveat flag on china/hk books
  - build_latest / latest_artifact (persistence round-trip)
  - endpoint shape via the web router
  - SHADOW GUARANTEE: firm_allocator is imported ONLY by allowed modules (grep ratchet)
"""
from __future__ import annotations

import json

import pytest

import bot  # noqa: F401 — bootstraps vendor/macro
from portfolio import firm_allocator as FA


# ---------------------------------------------------------------------------
# lifecycle/benchmark fixture builders
# ---------------------------------------------------------------------------

def _lifecycle(
    states: dict[str, str] | None = None,
    grades: dict[str, dict] | None = None,
    ortho: dict[str, float] | None = None,
) -> dict:
    """Build a minimal lifecycle artifact for injection."""
    return {
        "asof": "2026-07-06",
        "states": states or {},
        "grades": grades or {},
        "orthogonality": ortho or {},
    }


def _grade(active_vs_spy: float, series: list[float] | None = None) -> dict:
    g: dict = {"active_vs_spy": active_vs_spy}
    if series is not None:
        g["active_vs_spy_series"] = series
    return g


_EMPTY_BENCH: dict = {}
_EMPTY_PKTS: dict = {}

# The 6 non-SD books the allocator grades
_BOOK_IDS = ["flagship", "heavyweight", "autonomous", "etf", "china", "hk"]


# ---------------------------------------------------------------------------
# 1. weights sum to 1
# ---------------------------------------------------------------------------

def test_weights_sum_to_1_equal_weight():
    """No lifecycle → equal-weight fallback; weights must sum to 1 (1e-5 float tolerance after
    round+renorm).  Tightened from 1e-4 to 1e-5: round(w,6)+renorm guarantees sum within 1e-5."""
    art = FA._compute({}, _EMPTY_BENCH, _EMPTY_PKTS, book_ids=_BOOK_IDS)
    total = sum(art["books"][b]["shadow_weight"] for b in _BOOK_IDS)
    assert abs(total - 1.0) < 1e-5  # round(w,6) + renorm pass keeps this tight


def test_weights_sum_to_1_with_tilt():
    """With active-return tilts, weights still sum to 1 (1e-5 float tolerance)."""
    lc = _lifecycle(
        states={b: "active" for b in _BOOK_IDS},
        grades={
            # varying series so stdev > 0 and tilts are non-trivial
            "flagship":    _grade(0.01, [0.005, 0.008, 0.01, 0.012, 0.015]),
            "heavyweight": _grade(-0.005, [-0.002, -0.004, -0.005, -0.006, -0.008]),
            "autonomous":  _grade(0.02, [0.015, 0.018, 0.020, 0.022, 0.025]),
            "etf":         _grade(0.003, [0.001, 0.002, 0.003, 0.004, 0.005]),
            "china":       _grade(0.00, [-0.001, 0.000, 0.001, -0.001, 0.001]),
            "hk":          _grade(0.00, [0.001, -0.001, 0.002, -0.002, 0.000]),
        },
    )
    art = FA._compute(lc, _EMPTY_BENCH, _EMPTY_PKTS, book_ids=_BOOK_IDS)
    total = sum(art["books"][b]["shadow_weight"] for b in _BOOK_IDS)
    assert abs(total - 1.0) < 1e-5


# ---------------------------------------------------------------------------
# 2. equal-weight degradation when lifecycle missing
# ---------------------------------------------------------------------------

def test_equal_weight_when_lifecycle_missing():
    art = FA._compute({}, _EMPTY_BENCH, _EMPTY_PKTS, book_ids=["flagship", "autonomous"])
    w_f = art["books"]["flagship"]["shadow_weight"]
    w_a = art["books"]["autonomous"]["shadow_weight"]
    assert abs(w_f - 0.5) < 1e-9
    assert abs(w_a - 0.5) < 1e-9
    # flags must say fallback
    assert any("lifecycle_artifact_missing" in f for f in art["books"]["flagship"]["flags"])


# ---------------------------------------------------------------------------
# 3. positive tilt increases relative weight
# ---------------------------------------------------------------------------

def test_positive_tilt_book_gets_higher_weight():
    """A book with a consistent positive active return should outweigh a flat book."""
    lc = _lifecycle(
        states={"flagship": "active", "autonomous": "active"},
        grades={
            "flagship": _grade(0.01, [0.005, 0.008, 0.01, 0.012, 0.01]),
            "autonomous": _grade(0.0, [0.0] * 5),
        },
    )
    art = FA._compute(lc, _EMPTY_BENCH, _EMPTY_PKTS, book_ids=["flagship", "autonomous"])
    assert art["books"]["flagship"]["shadow_weight"] > art["books"]["autonomous"]["shadow_weight"]


def test_negative_active_return_reduces_weight():
    """A book whose last active return is below its own mean gets a sub-1 z → lower tilt.
    Must use a varying series (stdev=0 → z=0 → no discrimination)."""
    lc = _lifecycle(
        states={"flagship": "active", "autonomous": "active"},
        grades={
            # flagship: series trending down → last value well below mean → z < 0
            "flagship": _grade(-0.01, [0.005, 0.003, 0.001, -0.005, -0.010]),
            # autonomous: flat near zero → z ≈ 0
            "autonomous": _grade(0.0, [-0.001, 0.000, 0.001, -0.001, 0.001]),
        },
    )
    art = FA._compute(lc, _EMPTY_BENCH, _EMPTY_PKTS, book_ids=["flagship", "autonomous"])
    assert art["books"]["flagship"]["shadow_weight"] < art["books"]["autonomous"]["shadow_weight"]


# ---------------------------------------------------------------------------
# 4. probation penalty
# ---------------------------------------------------------------------------

def test_probation_halves_relative_weight():
    """A book on probation should receive roughly half the weight of an equal-return active book."""
    lc = _lifecycle(
        states={"flagship": "active", "autonomous": "probation"},
        grades={
            "flagship": _grade(0.0, [0.0] * 5),
            "autonomous": _grade(0.0, [0.0] * 5),
        },
    )
    art = FA._compute(lc, _EMPTY_BENCH, _EMPTY_PKTS, book_ids=["flagship", "autonomous"])
    # With ALPHA=0.3, z=0 → tilt=1.0 for both; probation halves autonomous tilt to 0.5.
    # flagship: 1.0 / (1.0 + 0.5) = 0.667; autonomous: 0.5 / 1.5 = 0.333
    assert art["books"]["flagship"]["shadow_weight"] > art["books"]["autonomous"]["shadow_weight"]
    ratio = art["books"]["flagship"]["shadow_weight"] / art["books"]["autonomous"]["shadow_weight"]
    assert abs(ratio - 2.0) < 0.01
    assert "probation:half_weight_penalty" in art["books"]["autonomous"]["flags"]


# ---------------------------------------------------------------------------
# 5. retired-recommendation → weight 0
# ---------------------------------------------------------------------------

def test_retired_recommendation_weight_zero():
    lc = _lifecycle(
        states={"flagship": "active", "autonomous": "retired-recommendation"},
        grades={},
    )
    art = FA._compute(lc, _EMPTY_BENCH, _EMPTY_PKTS, book_ids=["flagship", "autonomous"])
    assert art["books"]["autonomous"]["shadow_weight"] == 0.0
    assert "retired_recommendation:weight_zero" in art["books"]["autonomous"]["flags"]
    # flagship gets all weight
    assert abs(art["books"]["flagship"]["shadow_weight"] - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# 6. correlation penalty
# ---------------------------------------------------------------------------

def test_correlation_penalty_applied_to_lower_graded_book():
    """When corr > 0.80, the lower-graded book takes a penalty."""
    lc = _lifecycle(
        states={"flagship": "active", "autonomous": "active"},
        grades={
            "flagship": _grade(0.01),
            "autonomous": _grade(0.005),
        },
        ortho={"flagship:autonomous": 0.90},  # above threshold
    )
    art = FA._compute(lc, _EMPTY_BENCH, _EMPTY_PKTS, book_ids=["flagship", "autonomous"])
    # autonomous has lower grade → gets _CORR_PENALTY factor
    assert "correlation_penalty_applied" in art["books"]["autonomous"]["flags"]
    # flagship NOT penalized
    assert "correlation_penalty_applied" not in art["books"]["flagship"]["flags"]
    # pair recorded
    assert len(art["firm"]["correlation_pairs_penalized"]) == 1


def test_correlation_below_threshold_no_penalty():
    lc = _lifecycle(
        states={"flagship": "active", "autonomous": "active"},
        grades={"flagship": _grade(0.01), "autonomous": _grade(0.005)},
        ortho={"flagship:autonomous": 0.70},
    )
    art = FA._compute(lc, _EMPTY_BENCH, _EMPTY_PKTS, book_ids=["flagship", "autonomous"])
    assert len(art["firm"]["correlation_pairs_penalized"]) == 0
    assert "correlation_penalty_applied" not in art["books"]["autonomous"]["flags"]


def test_insufficient_n_corr_entry_ignored():
    """ortho 'insufficient-n' string values must not produce a penalty."""
    lc = _lifecycle(
        states={"flagship": "active", "autonomous": "active"},
        grades={"flagship": _grade(0.01), "autonomous": _grade(0.005)},
        ortho={"flagship:autonomous": "insufficient-n"},
    )
    art = FA._compute(lc, _EMPTY_BENCH, _EMPTY_PKTS, book_ids=["flagship", "autonomous"])
    assert len(art["firm"]["correlation_pairs_penalized"]) == 0


# ---------------------------------------------------------------------------
# 6b. orthogonality: REAL book_lifecycle artifact shape (nested matrix)
# ---------------------------------------------------------------------------
# brain.book_lifecycle.review() emits:
#   {"orthogonality": {"matrix": {"flagship": {"autonomous": {"corr": ..., "n_pairs": ..., "status": ...}}},
#                       "noisy_mirror_flags": [...], "reference_book": "flagship", "books": [...]}}
# Verify that firm_allocator reads the nested shape (not just the flat test-fixture shape).

def _ortho_nested(book_a: str, book_b: str, corr: float, status: str = "scoring") -> dict:
    """Build a lifecycle orthogonality block in the REAL brain.book_lifecycle shape."""
    return {
        "matrix": {
            book_a: {
                book_b: {"corr": corr, "n_pairs": 10, "status": status}
            }
        },
        "noisy_mirror_flags": (
            [{"book": book_b, "vs": book_a, "corr": corr, "n_pairs": 10}]
            if corr >= 0.80 else []
        ),
        "reference_book": "flagship",
        "books": [book_a, book_b],
    }


def test_corr_penalty_real_lifecycle_shape():
    """Penalty fires when orthogonality is in the REAL nested book_lifecycle shape.

    This pins finding 6: firm_allocator must read matrix[a][b].corr, not flat "{a}:{b}" keys.
    """
    lc = {
        "asof": "2026-07-06",
        "states": {"flagship": "active", "autonomous": "active"},
        "grades": {
            "flagship": _grade(0.01),
            "autonomous": _grade(0.005),
        },
        "orthogonality": _ortho_nested("flagship", "autonomous", corr=0.90),
    }
    art = FA._compute(lc, _EMPTY_BENCH, _EMPTY_PKTS, book_ids=["flagship", "autonomous"])
    # autonomous (lower grade) must be penalized
    assert "correlation_penalty_applied" in art["books"]["autonomous"]["flags"]
    assert "correlation_penalty_applied" not in art["books"]["flagship"]["flags"]
    assert len(art["firm"]["correlation_pairs_penalized"]) == 1


def test_corr_no_penalty_real_lifecycle_insufficient_n():
    """Penalty does NOT fire when status='insufficient-n' in the nested matrix."""
    lc = {
        "asof": "2026-07-06",
        "states": {"flagship": "active", "autonomous": "active"},
        "grades": {
            "flagship": _grade(0.01),
            "autonomous": _grade(0.005),
        },
        "orthogonality": _ortho_nested("flagship", "autonomous", corr=0.90, status="insufficient-n"),
    }
    art = FA._compute(lc, _EMPTY_BENCH, _EMPTY_PKTS, book_ids=["flagship", "autonomous"])
    assert len(art["firm"]["correlation_pairs_penalized"]) == 0
    assert "correlation_penalty_applied" not in art["books"]["autonomous"]["flags"]


def test_corr_no_penalty_real_lifecycle_below_threshold():
    """Penalty does NOT fire when corr is below 0.80 in the nested matrix."""
    lc = {
        "asof": "2026-07-06",
        "states": {"flagship": "active", "autonomous": "active"},
        "grades": {"flagship": _grade(0.01), "autonomous": _grade(0.005)},
        "orthogonality": _ortho_nested("flagship", "autonomous", corr=0.70),
    }
    art = FA._compute(lc, _EMPTY_BENCH, _EMPTY_PKTS, book_ids=["flagship", "autonomous"])
    assert len(art["firm"]["correlation_pairs_penalized"]) == 0


# ---------------------------------------------------------------------------
# 7. mandate breach penalty
# ---------------------------------------------------------------------------

def test_mandate_breach_penalty_reduces_weight():
    lc = _lifecycle(
        states={"flagship": "active", "autonomous": "active"},
        grades={"flagship": _grade(0.0), "autonomous": _grade(0.0)},
    )
    pkts = {
        "flagship": {"computed": True, "breaches": ["universe: AAPL outside union"]},
        "autonomous": {"computed": True, "breaches": []},
    }
    art = FA._compute(lc, _EMPTY_BENCH, pkts, book_ids=["flagship", "autonomous"])
    # flagship has 1 breach → penalty factor 0.80
    assert art["books"]["flagship"]["shadow_weight"] < art["books"]["autonomous"]["shadow_weight"]
    assert "mandate_breach_penalty" in art["books"]["flagship"]["flags"]
    assert "flagship" in art["firm"]["mandate_breach_books"]


def test_mandate_breach_floor():
    """Two breaches: penalty floor is 0.50."""
    lc = _lifecycle(
        states={"flagship": "active", "autonomous": "active"},
        grades={"flagship": _grade(0.0), "autonomous": _grade(0.0)},
    )
    pkts = {
        "flagship": {"computed": True, "breaches": ["b1", "b2", "b3", "b4", "b5"]},
        "autonomous": {"computed": True, "breaches": []},
    }
    art = FA._compute(lc, _EMPTY_BENCH, pkts, book_ids=["flagship", "autonomous"])
    # even with 5 breaches, flagship should still have > 0 weight (floor 0.50 of tilt)
    assert art["books"]["flagship"]["shadow_weight"] > 0.0


# ---------------------------------------------------------------------------
# 8. z-clip invariant (tilt factor bounded)
# ---------------------------------------------------------------------------

def test_tilt_positive_for_extreme_winner():
    """Even with extreme active return, weight remains within [0, 1]."""
    lc = _lifecycle(
        states={"flagship": "active"},
        grades={"flagship": _grade(0.10, [0.10] * 10)},
    )
    art = FA._compute(lc, _EMPTY_BENCH, _EMPTY_PKTS, book_ids=["flagship"])
    w = art["books"]["flagship"]["shadow_weight"]
    assert 0.0 <= w <= 1.0


# ---------------------------------------------------------------------------
# 9. regional caveat flag
# ---------------------------------------------------------------------------

def test_regional_books_get_proxy_bogey_flag():
    lc = _lifecycle(states={"china": "active", "hk": "active"}, grades={})
    art = FA._compute(lc, _EMPTY_BENCH, _EMPTY_PKTS, book_ids=["china", "hk"])
    assert "proxy_bogey:active_return_z_uncertain" in art["books"]["china"]["flags"]
    assert "proxy_bogey:active_return_z_uncertain" in art["books"]["hk"]["flags"]


def test_non_regional_books_no_proxy_flag():
    lc = _lifecycle(states={"flagship": "active"}, grades={})
    art = FA._compute(lc, _EMPTY_BENCH, _EMPTY_PKTS, book_ids=["flagship"])
    assert "proxy_bogey:active_return_z_uncertain" not in art["books"]["flagship"]["flags"]


# ---------------------------------------------------------------------------
# 10. advisory_only flag always set
# ---------------------------------------------------------------------------

def test_advisory_only_always_true():
    art = FA._compute({}, _EMPTY_BENCH, _EMPTY_PKTS, book_ids=_BOOK_IDS)
    assert art.get("advisory_only") is True


def test_formula_version_present():
    art = FA._compute({}, _EMPTY_BENCH, _EMPTY_PKTS, book_ids=["flagship"])
    assert art.get("formula_version") == FA._FORMULA_VERSION


# ---------------------------------------------------------------------------
# 11. build_latest / latest_artifact persistence round-trip
# ---------------------------------------------------------------------------

def test_build_latest_persists_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(FA, "_OUT_DIR", tmp_path / "allocator")
    monkeypatch.setattr(FA, "_latest_lifecycle", lambda: {})
    monkeypatch.setattr(FA, "_latest_benchmark", lambda: {})
    monkeypatch.setattr(FA, "_latest_mandate_packet", lambda _bid: {})

    art = FA.build_latest()
    assert art.get("advisory_only") is True

    # file was written
    files = list((tmp_path / "allocator").glob("*.json"))
    assert len(files) == 1
    on_disk = json.loads(files[0].read_text())
    assert on_disk["advisory_only"] is True


def test_latest_artifact_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(FA, "_OUT_DIR", tmp_path / "empty_dir")
    assert FA.latest_artifact() is None


def test_latest_artifact_returns_most_recent(tmp_path, monkeypatch):
    d = tmp_path / "allocator"
    d.mkdir()
    (d / "2026-07-05.json").write_text(json.dumps({"asof": "2026-07-05", "advisory_only": True}))
    (d / "2026-07-06.json").write_text(json.dumps({"asof": "2026-07-06", "advisory_only": True}))
    monkeypatch.setattr(FA, "_OUT_DIR", d)
    art = FA.latest_artifact()
    assert art["asof"] == "2026-07-06"


def test_build_latest_never_raises_on_corrupt_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(FA, "_OUT_DIR", tmp_path / "allocator")

    def _bad_lifecycle():
        raise RuntimeError("disk error")

    monkeypatch.setattr(FA, "_latest_lifecycle", _bad_lifecycle)
    art = FA.build_latest()
    assert isinstance(art, dict)
    assert art.get("advisory_only") is True


# ---------------------------------------------------------------------------
# 12. endpoint shape
# ---------------------------------------------------------------------------

def test_api_firm_allocator_endpoint(tmp_path, monkeypatch):
    """Verify the endpoint returns a valid JSON response with advisory_only=True."""
    monkeypatch.setattr(FA, "_OUT_DIR", tmp_path / "allocator")
    monkeypatch.setattr(FA, "_latest_lifecycle", lambda: {})
    monkeypatch.setattr(FA, "_latest_benchmark", lambda: {})
    monkeypatch.setattr(FA, "_latest_mandate_packet", lambda _bid: {})

    from fastapi.testclient import TestClient
    from app.web import router
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/api/firm_allocator")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("advisory_only") is True


def test_api_firm_allocator_rebuild_flag(tmp_path, monkeypatch):
    """rebuild=true forces a fresh computation even when an artifact exists."""
    monkeypatch.setattr(FA, "_OUT_DIR", tmp_path / "allocator")
    monkeypatch.setattr(FA, "_latest_lifecycle", lambda: {})
    monkeypatch.setattr(FA, "_latest_benchmark", lambda: {})
    monkeypatch.setattr(FA, "_latest_mandate_packet", lambda _bid: {})

    from fastapi.testclient import TestClient
    from app.web import router
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, raise_server_exceptions=False)

    resp = client.get("/api/firm_allocator?rebuild=true")
    assert resp.status_code == 200
    assert resp.json().get("advisory_only") is True


# ---------------------------------------------------------------------------
# 13. SHADOW GUARANTEE — grep ratchet
#     firm_allocator must only be imported by allowed modules
# ---------------------------------------------------------------------------

def test_shadow_guarantee_grep(tmp_path):
    """firm_allocator must NOT be imported by any book-sizing or bot-logic module.

    Allowed importers (exhaustive list):
      - app/web.py (the endpoint)
      - tests/test_firm_allocator.py (this file)

    Everything else is forbidden. This is the shadow guarantee: no allocation
    path depends on firm_allocator, ensuring display-only status is grep-provable.
    """
    import subprocess
    import sys
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent

    # Grep for any import of firm_allocator across the project
    result = subprocess.run(
        ["grep", "-r", "--include=*.py", "-l", "firm_allocator", str(repo_root)],
        capture_output=True, text=True,
    )
    files_importing = [
        Path(f.strip()) for f in result.stdout.strip().splitlines() if f.strip()
    ]

    ALLOWED_RELATIVE = {
        "app/web.py",
        "tests/test_firm_allocator.py",
        "portfolio/firm_allocator.py",  # the module itself
    }
    allowed_abs = {repo_root / rel for rel in ALLOWED_RELATIVE}

    violations = [str(f) for f in files_importing if f not in allowed_abs]
    assert violations == [], (
        f"firm_allocator imported by unauthorized module(s) — "
        f"shadow guarantee violated:\n  " + "\n  ".join(violations)
    )

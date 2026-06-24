"""Offline unit tests for the Desk observability API (app/web.py /api/desk/*).

These call the route functions DIRECTLY (no server / TestClient) against a tmp data dir seeded
with synthetic desk artifacts. They assert each endpoint:
  * returns a JSONResponse of the expected shape when artifacts are present, and
  * degrades to the graceful empty / "building" form when artifacts are absent — never raises.

The desk endpoints read four artifact families:
  * committee / gate_officer / risk_officer        — under web._data() (monkeypatched)
  * portfolio.watchlist                            — its own JSONL path root (monkeypatched)
  * brain.calibration / brain.attribution / brain.cio — their own path roots (monkeypatched)
so we redirect every root at a tmp tree and seed only what each test needs.
"""
import json

import pytest
from fastapi.responses import JSONResponse

from app import web


def _body(resp):
    """Decode a JSONResponse body back into a Python object."""
    assert isinstance(resp, JSONResponse)
    return json.loads(resp.body)


@pytest.fixture
def desk_data(tmp_path, monkeypatch):
    """Point every desk artifact root at an isolated tmp tree and return the data/ dir.

    web._data() drives the committee / gate_officer / risk_officer scans; the watchlist,
    calibration, attribution and cio modules each hold their own absolute path roots that we
    redirect here so a test never reads (or writes) the live data tree.
    """
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(web, "_data", lambda: data, raising=True)

    # watchlist — its own JSONL path
    try:
        from portfolio import watchlist
        monkeypatch.setattr(watchlist, "_WATCHLIST",
                            data / "portfolios" / "flagship" / "watchlist.jsonl", raising=False)
    except Exception:
        pass
    # calibration / attribution / cio — redirect their file roots into the tmp tree
    try:
        from brain import calibration
        monkeypatch.setattr(calibration, "_PATH", data / "brain" / "calibration.json", raising=False)
    except Exception:
        pass
    try:
        from brain import attribution
        out = data / "brain" / "attribution"
        monkeypatch.setattr(attribution, "_OUT", out, raising=False)
        monkeypatch.setattr(attribution, "_ROLLUP", out / "_rollup.json", raising=False)
    except Exception:
        pass
    try:
        from brain import cio
        monkeypatch.setattr(cio, "_OUT", data / "brain" / "cio", raising=False)
        # the CIO note prose is Opus-written; force the deterministic fallback (no network in tests)
        monkeypatch.setattr(cio, "_narrate", lambda rep: None, raising=False)
    except Exception:
        pass
    return data


def _seed_committee(data, asof="2026-06-20"):
    base = data / "committee" / asof
    (base / "_FLAGSHIP").mkdir(parents=True, exist_ok=True)
    (base / "_FLAGSHIP" / "strategist.json").write_text(json.dumps({
        "agent": "strategist",
        "input": {"regime": {"quad": "Q1", "quad_name": "Goldilocks",
                             "liquidity_overlay": "contracting", "cycle_tag": "mid"}},
        "verdict": {
            "confirmed_themes": [
                {"theme": "power_grid", "stage": "confirmed", "leadership": 0.82,
                 "names": ["AEIS", "HUBB", "ETN", "TLN"], "why": "confirmed bottleneck layer"},
            ],
            "backdrop_stance": "risk_on", "supportive": True,
            "watch_emerging": ["non_ai_tech (DELL +66%)"],
            "crowding_flags": ["memory_storage late-stage blow-off"],
            "rationale": "Q1 Goldilocks risk-on, participate in confirmed leadership.",
            "calibration_multiplier": 1.0,
        },
    }))
    # one per-name committee folder
    tk = base / "EME"
    tk.mkdir(parents=True, exist_ok=True)
    (tk / "forge.json").write_text(json.dumps({
        "agent": "forge", "combined": 76, "confirmed": True,
        "viability": "compelling", "size_mult": 0.9}))
    (tk / "sentinel.json").write_text(json.dumps({
        "agent": "sentinel", "stance": "CONDITIONAL", "confidence": 0.62,
        "strongest_bear": "extended momentum name mid-pullback"}))
    (tk / "nexus.json").write_text(json.dumps({
        "agent": "nexus", "action": "trim", "scale": 0.66, "lean": "add",
        "rationale": "SENTINEL conditional support — reduced size."}))
    return asof


def _seed_gate(data, asof="2026-06-20"):
    d = data / "gate_officer" / asof
    d.mkdir(parents=True, exist_ok=True)
    (d / "decisions.json").write_text(json.dumps({
        "agent": "gate_officer", "scope": "FLAGSHIP",
        "result": {"decisions": [
            {"ticker": "HUBB", "action": "withhold", "scale": 0.0, "reason": "cycle_blocked"},
            {"ticker": "EME", "action": "trim", "scale": 0.8, "reason": "over cap"},
        ]},
    }))


def _seed_risk(data, asof="2026-06-20"):
    d = data / "risk_officer" / asof
    d.mkdir(parents=True, exist_ok=True)
    (d / "decisions.json").write_text(json.dumps({
        "agent": "risk_officer", "scope": "FLAGSHIP",
        "result": {"decisions": []},
    }))


# ───────────────────────────── strategist ─────────────────────────────
def test_strategist_present(desk_data):
    asof = _seed_committee(desk_data)
    body = _body(web.api_desk_strategist())
    assert body["status"] == "scoring"
    assert body["asof"] == asof
    assert body["backdrop_stance"] == "risk_on"
    assert body["regime"]["quad_name"] == "Goldilocks"
    assert body["confirmed_themes"][0]["theme"] == "power_grid"
    assert body["confirmed_themes"][0]["names"] == ["AEIS", "HUBB", "ETN", "TLN"]
    assert body["crowding_flags"]


def test_strategist_asof_override(desk_data):
    _seed_committee(desk_data, asof="2026-06-18")
    asof = _seed_committee(desk_data, asof="2026-06-20")
    # no asof → most-recent
    assert _body(web.api_desk_strategist())["asof"] == asof
    # explicit asof
    assert _body(web.api_desk_strategist(asof="2026-06-18"))["asof"] == "2026-06-18"


def test_strategist_building_when_absent(desk_data):
    body = _body(web.api_desk_strategist())
    assert body["status"] == "building"
    assert body["asof"] is None


# ───────────────────────────── decisions ─────────────────────────────
def test_decisions_join(desk_data):
    asof = _seed_committee(desk_data)
    _seed_gate(desk_data)
    _seed_risk(desk_data)
    body = _body(web.api_desk_decisions())
    assert body["status"] == "scoring"
    assert body["asof"] == asof
    rows = {r["ticker"]: r for r in body["decisions"]}
    # EME has full committee chain + a gate trim
    eme = rows["EME"]
    assert eme["forge"]["confirmed"] is True
    assert eme["sentinel"]["stance"] == "CONDITIONAL"
    assert eme["nexus"]["action"] == "trim"
    assert eme["gate"]["action"] == "trim"
    # HUBB appears via the gate withhold even with no committee folder
    assert rows["HUBB"]["gate"]["action"] == "withhold"
    assert rows["HUBB"]["forge"] is None


def test_decisions_building_when_absent(desk_data):
    body = _body(web.api_desk_decisions())
    assert body["status"] == "building"
    assert body["decisions"] == []


# ───────────────────────────── watchlist ─────────────────────────────
def test_watchlist_dedups_latest(desk_data):
    from portfolio import watchlist
    watchlist.append("ZZZ", "2026-06-18", reason="early")
    watchlist.append("ZZZ", "2026-06-20", reason="latest reason")
    watchlist.append("AAA", "2026-06-19", reason="parked")
    body = _body(web.api_desk_watchlist())
    rows = {r["ticker"]: r for r in body["watchlist"]}
    assert rows["ZZZ"]["asof"] == "2026-06-20"
    assert rows["ZZZ"]["reason"] == "latest reason"
    assert rows["AAA"]["reason"] == "parked"


def test_watchlist_empty_when_absent(desk_data):
    body = _body(web.api_desk_watchlist())
    assert body["watchlist"] == []
    assert body["book"] == "flagship"


# ───────────────────────────── scorecard ─────────────────────────────
def test_scorecard_present(desk_data):
    (desk_data / "brain").mkdir(parents=True, exist_ok=True)
    (desk_data / "brain" / "calibration.json").write_text(json.dumps({
        "as_of": "2026-06-20", "min_n": 12, "floor": 0.5,
        "agents": {
            "forge": {"n": 0, "reliability": None, "multiplier": 1.0, "status": "building"},
            "gate": {"n": 14, "reliability": 0.6, "multiplier": 0.7, "status": "scoring"},
        },
    }))
    attr = desk_data / "brain" / "attribution"
    attr.mkdir(parents=True, exist_ok=True)
    (attr / "_rollup.json").write_text(json.dumps({
        "updated": "2026-06-20",
        "seats": {"gate": {"attributed_bps": -42.5, "n": 3, "mean_bps": -14.2},
                  "forge": {"attributed_bps": 110.0, "n": 5, "mean_bps": 22.0}},
    }))
    body = _body(web.api_desk_scorecard())
    assert body["status"] == "scoring"
    seats = {s["seat"]: s for s in body["seats"]}
    assert seats["gate"]["multiplier"] == 0.7
    assert seats["gate"]["n"] == 14
    assert seats["gate"]["attributed_bps"] == -42.5
    assert seats["forge"]["attributed_bps"] == 110.0
    # cio block is always present (deterministic fallback note)
    assert body["cio"] is not None
    assert "note_md" in body["cio"]


def test_scorecard_building_when_absent(desk_data):
    body = _body(web.api_desk_scorecard())
    # no calibration/attribution → no seats with data, but never raises
    assert body["status"] in ("building", "scoring")
    assert isinstance(body["seats"], list)
    assert "cio" in body

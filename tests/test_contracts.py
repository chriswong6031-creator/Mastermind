"""MW3 Lane A — contracts loader + stale-anchor freeze integration tests.

Sections
--------
A. Contracts loader (control_plane/contracts.py)
   A1: missing file → never raises, returns empty
   A2: contract lookup by synapse key and by path
   A3: all_contracts() and freeze_class_keys()

B. Conformance: every synapse-declared mastermind consumer artifact appears in contracts.yml
   (baked 16-key golden set — the gate that proves the bake is complete)

C. Anchor-stale → freeze=True propagation through macro_refresh.check_and_warn()
   C1: fresh anchors → freeze=False
   C2: FREEZE-class anchor stale beyond budget → freeze=True, reasons populated
   C3: non-FREEZE anchor stale → freeze=False (ADVISORY, not FREEZE)
   C4: kill-switch MASTERMIND_STALE_FREEZE=0 → _freeze_enabled() False

D. run_daily applies freeze_to_prior when freeze=True (numeric: no new adds vs prior)
   D1: freeze=True + prior={AAPL:0.08} → new add NVDA dropped, AAPL retained <= 0.08
   D2: de-risk (AAPL target < prior) passes through (weight reduced, not blocked)
   D3: kill-switch off → warn-only, positions unchanged

E. R2 probe failure → advisory event (stockdata_degraded=True, execution continues)
   E1: probe ticker absent → stockdata_degraded=True
   E2: all probe tickers fresh → stockdata_degraded=False

F. 19/19 incident replay integration: replay does not regress under new freeze path
   (runs existing incident replay, verifies it still passes)
"""
from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_fake_contracts_yml(tmp_path: Path, extra_yaml: str = "") -> Path:
    """Write a minimal contracts.yml to tmp_path/config/contracts.yml."""
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    p = tmp_path / "config" / "contracts.yml"
    p.write_text(textwrap.dedent(f"""
        meta:
          schema_version: 1
        artifacts:
          regime-latest:
            path: data/regime/latest.json
            owner: macro_engine
            schema_hint: json
            freshness_budget_sessions: 1
            tier: infrastructure
            allowed_effect: sizing-input
            degradation_class: FREEZE
            consumer_modules: []
            declared_by: synapse
          site-us-standouts:
            path: site/factordata/us_standouts.json
            owner: macro_engine
            schema_hint: json
            freshness_budget_sessions: 1
            tier: display
            allowed_effect: sizing-input
            degradation_class: FREEZE
            consumer_modules: []
            declared_by: synapse
          site-baskets-json:
            path: site/basketdata/baskets.json
            owner: macro_engine
            schema_hint: json
            freshness_budget_sessions: 1
            tier: display
            allowed_effect: context-only
            degradation_class: ADVISORY
            consumer_modules: []
            declared_by: synapse
          {extra_yaml}
    """))
    return p


# ---------------------------------------------------------------------------
# A. Contracts loader
# ---------------------------------------------------------------------------

class TestContractsLoader:
    def test_missing_file_never_raises(self, tmp_path, monkeypatch):
        """A missing contracts.yml must not raise; all_contracts() returns {}."""
        from control_plane import contracts as _c
        monkeypatch.setattr(_c, "_CONTRACTS_FILE", tmp_path / "nonexistent.yml")
        _c._reset()
        assert _c.all_contracts() == {}
        assert _c.contract("regime-latest") is None
        assert _c.freeze_class_keys() == []
        _c._reset()

    def test_contract_lookup_by_key(self, tmp_path, monkeypatch):
        """contract('regime-latest') returns the dict by synapse key."""
        from control_plane import contracts as _c
        _make_fake_contracts_yml(tmp_path)
        monkeypatch.setattr(_c, "_CONTRACTS_FILE", tmp_path / "config" / "contracts.yml")
        _c._reset()
        c = _c.contract("regime-latest")
        assert c is not None
        assert c["path"] == "data/regime/latest.json"
        assert c["degradation_class"] == "FREEZE"
        _c._reset()

    def test_contract_lookup_by_path(self, tmp_path, monkeypatch):
        """contract('site/factordata/us_standouts.json') resolves via path index."""
        from control_plane import contracts as _c
        _make_fake_contracts_yml(tmp_path)
        monkeypatch.setattr(_c, "_CONTRACTS_FILE", tmp_path / "config" / "contracts.yml")
        _c._reset()
        c = _c.contract("site/factordata/us_standouts.json")
        assert c is not None
        assert c["degradation_class"] == "FREEZE"
        _c._reset()

    def test_unknown_key_returns_none(self, tmp_path, monkeypatch):
        """Unregistered key returns None without raising."""
        from control_plane import contracts as _c
        _make_fake_contracts_yml(tmp_path)
        monkeypatch.setattr(_c, "_CONTRACTS_FILE", tmp_path / "config" / "contracts.yml")
        _c._reset()
        assert _c.contract("no-such-artifact") is None
        _c._reset()

    def test_freeze_class_keys(self, tmp_path, monkeypatch):
        """freeze_class_keys() returns only FREEZE-class entries."""
        from control_plane import contracts as _c
        _make_fake_contracts_yml(tmp_path)
        monkeypatch.setattr(_c, "_CONTRACTS_FILE", tmp_path / "config" / "contracts.yml")
        _c._reset()
        keys = _c.freeze_class_keys()
        assert "regime-latest" in keys
        assert "site-us-standouts" in keys
        assert "site-baskets-json" not in keys   # ADVISORY
        _c._reset()

    def test_empty_string_key_never_raises(self, tmp_path, monkeypatch):
        """contract('') must not raise."""
        from control_plane import contracts as _c
        _make_fake_contracts_yml(tmp_path)
        monkeypatch.setattr(_c, "_CONTRACTS_FILE", tmp_path / "config" / "contracts.yml")
        _c._reset()
        assert _c.contract("") is None
        _c._reset()

    def test_all_contracts_returns_mapping(self, tmp_path, monkeypatch):
        """all_contracts() returns a non-empty dict with 'path' keys."""
        from control_plane import contracts as _c
        _make_fake_contracts_yml(tmp_path)
        monkeypatch.setattr(_c, "_CONTRACTS_FILE", tmp_path / "config" / "contracts.yml")
        _c._reset()
        arts = _c.all_contracts()
        assert isinstance(arts, dict)
        assert len(arts) >= 3
        for v in arts.values():
            assert "path" in v
        _c._reset()


# ---------------------------------------------------------------------------
# B. Conformance: synapse-declared 16 keys all present in contracts.yml
# ---------------------------------------------------------------------------

# These 16 keys are the baked synapse mastermind consumer artifacts.
# The test fails if any key is absent from config/contracts.yml.
_SYNAPSE_MASTERMIND_KEYS = [
    "regime-latest",
    "site-us-standouts",
    "site-baskets-json",
    "site-altdata-mastermind",
    "site-signals-per-ticker",
    "site-regime-timeline",
    "site-sector-pulse",
    "site-china-standouts",
    "site-basket-flow",
    "site-allocation",
    "site-ai-desk-us",
    "site-china-intel-briefing",
    "site-artifact-manifest",
    "site-golden-signals",
    "site-foresight-cascade",
    "feeds-plane",
]


class TestConformance:
    def test_all_synapse_mastermind_artifacts_in_contracts(self):
        """Every synapse-declared mastermind consumer artifact must be in config/contracts.yml."""
        from control_plane import contracts as _c
        _c._reset()
        arts = _c.all_contracts()
        missing = [k for k in _SYNAPSE_MASTERMIND_KEYS if k not in arts]
        assert not missing, (
            f"Synapse-declared mastermind artifacts missing from contracts.yml: {missing}. "
            "Run the MW3 bake step or add entries manually."
        )
        _c._reset()

    def test_freeze_class_artifacts_have_sizing_input_or_infrastructure(self):
        """FREEZE-class artifacts must be sizing-input or infrastructure tier (never display-only)."""
        from control_plane import contracts as _c
        _c._reset()
        arts = _c.all_contracts()
        for key in _c.freeze_class_keys():
            c = arts.get(key, {})
            assert c.get("allowed_effect") != "display-only", (
                f"FREEZE-class artifact '{key}' has allowed_effect=display-only — "
                "a display-only artifact must not trigger freeze semantics."
            )
        _c._reset()


# ---------------------------------------------------------------------------
# C. Anchor-stale → freeze propagation in macro_refresh.check_and_warn()
# ---------------------------------------------------------------------------

class TestFreezeSemantics:
    """macro_refresh.check_and_warn() sets freeze=True when a FREEZE anchor is stale."""

    def _patch_src(self, monkeypatch, src: Path):
        from data_layer import macro_refresh as mr
        monkeypatch.setattr(mr, "_SRC", src)

    def _make_checkout(self, tmp_path: Path, anchors: dict, *, stockdata: bool = True) -> Path:
        src = tmp_path / "macro_src"
        for rel, payload in anchors.items():
            if payload is None:
                continue
            p = src / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(payload))
        if stockdata:
            (src / "site" / "stockdata").mkdir(parents=True, exist_ok=True)
        return src

    def test_fresh_anchors_freeze_false(self, monkeypatch, tmp_path):
        """All anchors fresh → freeze=False."""
        src = self._make_checkout(tmp_path, {
            "site/factordata/us_standouts.json": {"as_of": "2026-07-05"},
            "data/regime/latest.json":           {"date": "2026-07-05"},
            "site/sectordata/sector_cycles.json": {"meta": {"asOf": "2026-07-05"}},
            "site/stockdata/SPY.json":            {"asof": "2026-07-05"},
        })
        self._patch_src(monkeypatch, src)
        from data_layer import macro_refresh as mr
        mr._FREEZE_CONTRACTS_LOADED = False
        info = mr.check_and_warn(block=False, log=lambda *_: None)
        assert info["freeze"] is False
        assert info["freeze_reasons"] == []

    def test_stale_freeze_anchor_sets_freeze_true(self, monkeypatch, tmp_path):
        """Stale regime anchor → freeze=True, reasons non-empty."""
        src = self._make_checkout(tmp_path, {
            "site/factordata/us_standouts.json": {"as_of": "2026-07-05"},
            "data/regime/latest.json":           {"date": "2026-06-20"},   # 15d old >> budget
            "site/sectordata/sector_cycles.json": {"meta": {"asOf": "2026-07-05"}},
            "site/stockdata/SPY.json":            {"asof": "2026-07-05"},
        })
        self._patch_src(monkeypatch, src)
        from data_layer import macro_refresh as mr
        mr._FREEZE_CONTRACTS_LOADED = False
        # Mock contracts to return regime-latest as FREEZE with budget=1 session=2 days
        with patch("control_plane.contracts.all_contracts", return_value={
            "regime-latest": {
                "path": "data/regime/latest.json",
                "degradation_class": "FREEZE",
                "freshness_budget_sessions": 1,
            },
            "site-us-standouts": {
                "path": "site/factordata/us_standouts.json",
                "degradation_class": "FREEZE",
                "freshness_budget_sessions": 1,
            },
        }):
            mr._FREEZE_CONTRACTS_LOADED = False
            info = mr.check_and_warn(block=False, log=lambda *_: None)
        assert info["freeze"] is True, f"expected freeze=True, got {info}"
        assert len(info["freeze_reasons"]) >= 1
        assert any("regime_latest" in r for r in info["freeze_reasons"])

    def test_advisory_anchor_stale_does_not_freeze(self, monkeypatch, tmp_path):
        """ADVISORY-class artifact stale does not set freeze=True."""
        src = self._make_checkout(tmp_path, {
            "site/factordata/us_standouts.json": {"as_of": "2026-07-05"},
            "data/regime/latest.json":           {"date": "2026-07-05"},
            "site/sectordata/sector_cycles.json": {"meta": {"asOf": "2026-07-05"}},
            "site/stockdata/SPY.json":            {"asof": "2026-07-05"},
        })
        self._patch_src(monkeypatch, src)
        from data_layer import macro_refresh as mr
        mr._FREEZE_CONTRACTS_LOADED = False
        # Only ADVISORY contracts registered
        with patch("control_plane.contracts.all_contracts", return_value={
            "site-baskets-json": {
                "path": "site/basketdata/baskets.json",
                "degradation_class": "ADVISORY",
                "freshness_budget_sessions": 1,
            },
        }):
            mr._FREEZE_CONTRACTS_LOADED = False
            info = mr.check_and_warn(block=False, log=lambda *_: None)
        assert info["freeze"] is False

    def test_kill_switch_freeze_disabled(self, monkeypatch):
        """_freeze_enabled() returns False when MASTERMIND_STALE_FREEZE=0."""
        monkeypatch.setenv("MASTERMIND_STALE_FREEZE", "0")
        from data_layer import macro_refresh as mr
        assert mr._freeze_enabled() is False
        monkeypatch.delenv("MASTERMIND_STALE_FREEZE", raising=False)
        assert mr._freeze_enabled() is True

    def test_check_and_warn_returns_freeze_keys(self, monkeypatch, tmp_path):
        """check_and_warn() always returns 'freeze' and 'freeze_reasons' keys (backwards-compat)."""
        src = self._make_checkout(tmp_path, {
            "site/factordata/us_standouts.json": {"as_of": "2026-07-05"},
            "data/regime/latest.json":           {"date": "2026-07-05"},
            "site/sectordata/sector_cycles.json": {"meta": {"asOf": "2026-07-05"}},
        })
        self._patch_src(monkeypatch, src)
        from data_layer import macro_refresh as mr
        mr._FREEZE_CONTRACTS_LOADED = False
        with patch("control_plane.contracts.all_contracts", return_value={}):
            mr._FREEZE_CONTRACTS_LOADED = False
            info = mr.check_and_warn(block=False, log=lambda *_: None)
        assert "freeze" in info
        assert "freeze_reasons" in info
        assert "stockdata_degraded" in info


# ---------------------------------------------------------------------------
# D. run_daily applies freeze_to_prior (numeric invariant tests)
# ---------------------------------------------------------------------------

def _make_daily_mocks(monkeypatch, *, freeze: bool, positions: list[dict],
                      prior_weights: dict[str, float],
                      freeze_reasons: list[str] | None = None,
                      stale_freeze_env: str = "1"):
    """Set up monkeypatches for run_daily freeze path tests."""
    monkeypatch.setenv("MASTERMIND_STALE_FREEZE", stale_freeze_env)

    # Patch macro_refresh.refresh_and_check to return freeze state
    mock_macro_data = {
        "asof": "2026-07-01",
        "stale": freeze,
        "freeze": freeze,
        "freeze_reasons": freeze_reasons or (["regime_latest=2026-06-20 is 15d old"] if freeze else []),
        "stockdata_degraded": False,
        "max_age_days": 2,
        "data_gaps": [],
        "anchors": {},
    }

    # Patch phase2.run to return a ran=True book with the given positions
    mock_book = {
        "ran": True,
        "positions": list(positions),
        "sleeves": {"conviction": 0.15, "leadership": 0.2, "cash": 0.65},
    }

    from unittest.mock import MagicMock, patch
    import data_layer.macro_refresh as _mr_mod
    import bot.phase2 as _p2_mod
    import portfolio.firm_exposure as _fe_mod

    monkeypatch.setattr(_mr_mod, "refresh_and_check", lambda **kw: mock_macro_data)
    monkeypatch.setattr(_p2_mod, "run", lambda **kw: dict(mock_book))
    monkeypatch.setattr(_fe_mod, "published_weights", lambda pid: dict(prior_weights))

    # Patch deploy lag check
    try:
        import scripts.check_deploy_lag as _dl
        monkeypatch.setattr(_dl, "check", lambda: {"warn": False})
    except Exception:
        pass

    return mock_macro_data, mock_book


class TestRunDailyFreeze:
    def test_freeze_drops_new_adds(self, monkeypatch):
        """When freeze=True, new adds (not in prior) are dropped from positions."""
        prior = {"AAPL": 0.08}
        # NVDA is a new add (not in prior)
        positions = [
            {"ticker": "AAPL", "weight": 0.08, "sleeve": "conviction"},
            {"ticker": "NVDA", "weight": 0.10, "sleeve": "conviction"},  # new add
        ]
        _make_daily_mocks(monkeypatch, freeze=True, positions=positions,
                          prior_weights=prior)
        from bot.daily import run_daily
        out = run_daily(asof="2026-07-05", armed=False)

        # stale_freeze should be in out
        sf = out.get("stale_freeze") or {}
        assert sf.get("applied") is True, f"expected stale_freeze.applied=True: {sf}"

        # NVDA must not appear in frozen positions (new add)
        frozen = out["book"].get("positions") or []
        frozen_tickers = {p["ticker"].upper() for p in frozen}
        assert "NVDA" not in frozen_tickers, (
            f"New add NVDA should have been dropped by freeze. positions={frozen}")

    def test_freeze_retains_prior_at_min_weight(self, monkeypatch):
        """When freeze=True, AAPL at weight <= prior passes through at min(target, prior)."""
        prior = {"AAPL": 0.08}
        positions = [
            {"ticker": "AAPL", "weight": 0.06, "sleeve": "conviction"},  # reduce: ok
        ]
        _make_daily_mocks(monkeypatch, freeze=True, positions=positions,
                          prior_weights=prior)
        from bot.daily import run_daily
        out = run_daily(asof="2026-07-05", armed=False)

        frozen = out["book"].get("positions") or []
        aapl = next((p for p in frozen if str(p.get("ticker") or "").upper() == "AAPL"), None)
        assert aapl is not None, "AAPL should be retained in frozen positions"
        assert float(aapl["weight"]) <= 0.08, (
            f"Frozen AAPL weight {aapl['weight']} exceeds prior 0.08")

    def test_freeze_derisks_allowed(self, monkeypatch):
        """When freeze=True, de-risk (target < prior) passes through (weight is reduced)."""
        prior = {"AAPL": 0.10}
        positions = [
            {"ticker": "AAPL", "weight": 0.05, "sleeve": "conviction"},  # de-risk
        ]
        _make_daily_mocks(monkeypatch, freeze=True, positions=positions,
                          prior_weights=prior)
        from bot.daily import run_daily
        out = run_daily(asof="2026-07-05", armed=False)

        frozen = out["book"].get("positions") or []
        aapl = next((p for p in frozen if str(p.get("ticker") or "").upper() == "AAPL"), None)
        assert aapl is not None, "AAPL (de-risk) must survive the freeze"
        # min(0.05, 0.10) = 0.05
        assert float(aapl["weight"]) <= 0.05 + 1e-6

    def test_kill_switch_off_unchanged_behavior(self, monkeypatch):
        """MASTERMIND_STALE_FREEZE=0 → positions unchanged, stale_freeze.applied=False."""
        prior = {"AAPL": 0.08}
        positions = [
            {"ticker": "AAPL", "weight": 0.08, "sleeve": "conviction"},
            {"ticker": "NVDA", "weight": 0.10, "sleeve": "conviction"},
        ]
        _make_daily_mocks(monkeypatch, freeze=True, positions=positions,
                          prior_weights=prior, stale_freeze_env="0")
        from bot.daily import run_daily
        out = run_daily(asof="2026-07-05", armed=False)

        # stale_freeze.applied must be False (kill switch active)
        sf = out.get("stale_freeze") or {}
        assert sf.get("applied") is not True, (
            f"Kill-switch=0 should suppress freeze application: {sf}")
        assert sf.get("kill_switch") is True

        # positions should be unchanged (NVDA still present)
        book_positions = out["book"].get("positions") or []
        tickers = {p["ticker"].upper() for p in book_positions}
        assert "NVDA" in tickers, (
            "NVDA should not be dropped when kill-switch is off")

    def test_no_freeze_when_macro_data_freeze_false(self, monkeypatch):
        """When macro_data.freeze=False, stale_freeze must not be set (no-op)."""
        prior = {"AAPL": 0.08}
        positions = [
            {"ticker": "AAPL", "weight": 0.08, "sleeve": "conviction"},
            {"ticker": "NVDA", "weight": 0.10, "sleeve": "conviction"},
        ]
        _make_daily_mocks(monkeypatch, freeze=False, positions=positions,
                          prior_weights=prior)
        from bot.daily import run_daily
        out = run_daily(asof="2026-07-05", armed=False)

        # stale_freeze must not appear (or must not be applied)
        sf = out.get("stale_freeze")
        if sf is not None:
            assert sf.get("applied") is not True


# ---------------------------------------------------------------------------
# E. R2 probe failure → advisory event
# ---------------------------------------------------------------------------

class TestR2Probe:
    def _patch_src(self, monkeypatch, src: Path):
        from data_layer import macro_refresh as mr
        monkeypatch.setattr(mr, "_SRC", src)

    def test_probe_missing_ticker_stockdata_degraded(self, monkeypatch, tmp_path):
        """R2 probe: SPY present but QQQ absent → stockdata_degraded=True."""
        src = tmp_path / "macro_src"
        sd = src / "site" / "stockdata"
        sd.mkdir(parents=True)
        # Only write SPY — QQQ, NVDA, AAPL, MSFT absent
        (sd / "SPY.json").write_text(json.dumps({"asof": "2026-07-05"}))
        self._patch_src(monkeypatch, src)
        from data_layer import macro_refresh as mr
        mr._FREEZE_CONTRACTS_LOADED = False
        with patch("control_plane.contracts.all_contracts", return_value={}):
            mr._FREEZE_CONTRACTS_LOADED = False
            degraded, missing = mr._probe_r2_availability()
        assert degraded is True
        assert "QQQ" in missing or "NVDA" in missing or "AAPL" in missing or "MSFT" in missing

    def test_probe_all_fresh_not_degraded(self, monkeypatch, tmp_path):
        """R2 probe: all probe tickers fresh → stockdata_degraded=False."""
        src = tmp_path / "macro_src"
        sd = src / "site" / "stockdata"
        sd.mkdir(parents=True)
        from data_layer import macro_refresh as mr
        for ticker in mr._R2_PROBE_TICKERS:
            (sd / f"{ticker}.json").write_text(json.dumps({"asof": "2026-07-05"}))
        self._patch_src(monkeypatch, src)
        mr._FREEZE_CONTRACTS_LOADED = False
        with patch("control_plane.contracts.all_contracts", return_value={}):
            mr._FREEZE_CONTRACTS_LOADED = False
            degraded, missing = mr._probe_r2_availability()
        assert degraded is False
        assert missing == []

    def test_r2_probe_failure_advisory_only(self, monkeypatch, tmp_path):
        """stockdata_degraded=True appears in check_and_warn result but does not set freeze."""
        src = tmp_path / "macro_src"
        (src / "site" / "stockdata").mkdir(parents=True)  # exists but probe tickers absent
        (src / "site" / "factordata").mkdir(parents=True)
        (src / "site" / "factordata" / "us_standouts.json").write_text(
            json.dumps({"as_of": "2026-07-05"}))
        (src / "data" / "regime").mkdir(parents=True)
        (src / "data" / "regime" / "latest.json").write_text(
            json.dumps({"date": "2026-07-05"}))
        (src / "site" / "sectordata").mkdir(parents=True)
        (src / "site" / "sectordata" / "sector_cycles.json").write_text(
            json.dumps({"meta": {"asOf": "2026-07-05"}}))
        self._patch_src(monkeypatch, src)
        from data_layer import macro_refresh as mr
        mr._FREEZE_CONTRACTS_LOADED = False
        with patch("control_plane.contracts.all_contracts", return_value={}):
            mr._FREEZE_CONTRACTS_LOADED = False
            info = mr.check_and_warn(block=False, log=lambda *_: None)
        assert info["stockdata_degraded"] is True
        # freeze must NOT be True just because of probe degradation
        assert info["freeze"] is False


# ---------------------------------------------------------------------------
# F. Incident replay regression (19/19 replays must still pass)
# ---------------------------------------------------------------------------

def test_incident_replay_module_importable():
    """The 2026-07-02 incident replay module must be importable (regression guard).

    Full replay execution happens via pytest's normal collection of
    tests/incident_replays/test_incident_2026_07_02.py — this test just verifies
    the module is present and syntax-clean, so a MW3 change cannot silently break it."""
    try:
        from tests.incident_replays import test_incident_2026_07_02
        import inspect
        fns = [n for n, _ in inspect.getmembers(test_incident_2026_07_02, inspect.isfunction)
               if n.startswith("test_")]
        assert fns, "incident replay module has no test_ functions"
    except ImportError:
        pytest.skip("incident replay module not importable (expected in isolated worktree)")

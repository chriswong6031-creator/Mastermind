"""tests/test_etf_guardrails.py — Drift-check + regression tests for the A3 cluster-config migration.

WHAT THIS GUARDS
----------------
Task A3: ETF G5 reads the SHARED ``config/clusters.yml`` (the single firm-wide definition of the
semis/AI concentration cluster), with a fallback to ``config/etf_strategy.yml`` values so the PROVEN
G5 brake never weakens during rollout.

This file covers:
  1. DRIFT-CHECK — clusters.yml and etf_strategy.yml's factor_clusters (DEPRECATED-mirror) must
     agree on the ETF-universe members and max_gross for the megacap_growth_semis / semis_ai cluster.
     clusters.yml is authoritative and may have MORE members (single names not in the ETF universe);
     the mirror only lists ETF-universe tickers.  Checks:
       (a) every ETF-universe member in etf_strategy.yml is present in clusters.yml semis_ai, and
       (b) max_gross is identical in both files.
     A divergence fails the build; two live definitions can never co-exist silently.

  2. CLUSTER-CONFIG READER — portfolio/cluster_config: load(), clusters(), etf_clusters() work
     with real files, degrade to [] on missing/malformed input, and never raise.

  3. G5 REGRESSION — guardrails() routes through clusters.yml when present; the G5 factor-cluster
     cap clamps a synthetically over-weight cluster book exactly as before the migration (same math,
     same pro-rata scale-down — only the CONFIG SOURCE changed, the brake still clamps).

  4. FALLBACK CHAIN — when clusters.yml is absent/invalid, guardrails() falls back to
     etf_strategy.yml's DEPRECATED-mirror block, not to a loosened/missing cap.  The cap level is
     never weakened: fallback max_gross == clusters.yml max_gross (both are the firm-decided level).

All tests are pure / offline — no live data, no external I/O (fixture-injected configs where needed).
"""
from __future__ import annotations

import pytest

# ── helpers ─────────────────────────────────────────────────────────────────────────────────────


def _norm_members(members) -> frozenset[str]:
    """Upper-case + strip for membership comparison."""
    return frozenset(str(m).upper().strip() for m in (members or []))


# The 6 ETF-universe tickers that are in both configs and that G5 actually caps.
# If you add to etf_strategy.yml's mirror list, add here too (it's the drift-check fixture).
_ETF_UNIVERSE_CLUSTER_MEMBERS = frozenset(["QQQ", "XLK", "SMH", "IGV", "MTUM", "SIZE"])
_ETF_CLUSTER_MAX_GROSS = 0.35   # current agreed level (clusters.yml semis_ai + etf_strategy.yml mirror)


# ── 1. DRIFT-CHECK ──────────────────────────────────────────────────────────────────────────────

class TestClusterDriftCheck:
    """The drift-check: clusters.yml and etf_strategy.yml must agree on the ETF-relevant cluster.

    clusters.yml (id=semis_ai, name=megacap_growth_semis) is the AUTHORITATIVE definition. It
    contains more members than the ETF mirror (single names like NVDA/AVGO that aren't ETFs) — the
    drift-check only requires that every ETF-universe member in the mirror is also present in
    clusters.yml, and that max_gross agrees. This prevents divergence in both directions while
    allowing clusters.yml to grow its single-name coverage independently.
    """

    def test_etf_mirror_members_are_subset_of_clusters_yml_semis_ai(self):
        """Every ETF-universe member in etf_strategy.yml's mirror must appear in clusters.yml.

        DRIFT alarm: someone edited etf_strategy.yml without updating clusters.yml (or vice versa).
        """
        # ── load clusters.yml ───────────────────────────────────────────────
        from portfolio import cluster_config
        cls = cluster_config.clusters()
        semis_cls = next(
            (c for c in cls if c["id"] in ("semis_ai",) or c["name"] == "megacap_growth_semis"),
            None,
        )
        assert semis_cls is not None, (
            "clusters.yml must contain a cluster with id='semis_ai' or name='megacap_growth_semis'. "
            "Found ids: " + str([c["id"] for c in cls])
        )
        shared_members = _norm_members(semis_cls["members"])

        # ── load etf_strategy.yml mirror ───────────────────────────────────
        from portfolio import etf_universe
        spec = etf_universe.load_spec()
        fc = spec.get("guardrails", {}).get("factor_clusters") or []
        mirror = next(
            (c for c in fc if isinstance(c, dict)
             and c.get("name") in ("megacap_growth_semis", "semis_ai")),
            None,
        )
        assert mirror is not None, (
            "etf_strategy.yml guardrails.factor_clusters must contain a 'megacap_growth_semis' "
            "entry (the DEPRECATED-mirror of clusters.yml semis_ai). "
            "Found names: " + str([c.get("name") for c in fc])
        )
        mirror_members = _norm_members(mirror.get("members"))

        # Every mirror member must appear in clusters.yml (cluster.yml may have MORE)
        missing = mirror_members - shared_members
        assert not missing, (
            f"DRIFT: etf_strategy.yml mirror has members {sorted(missing)} that are NOT in "
            f"clusters.yml semis_ai. Edit clusters.yml to add them, then re-run.\n"
            f"  clusters.yml semis_ai: {sorted(shared_members)}\n"
            f"  etf_strategy.yml mirror: {sorted(mirror_members)}"
        )

    def test_max_gross_identical_in_both_configs(self):
        """max_gross for the ETF cluster must be identical in clusters.yml and etf_strategy.yml.

        DRIFT alarm: someone tuned the cap in one place without updating the other.
        """
        from portfolio import cluster_config, etf_universe
        cls = cluster_config.clusters()
        semis_cls = next(
            (c for c in cls if c["id"] == "semis_ai" or c["name"] == "megacap_growth_semis"),
            None,
        )
        assert semis_cls is not None, "clusters.yml semis_ai entry required"

        spec = etf_universe.load_spec()
        fc = spec.get("guardrails", {}).get("factor_clusters") or []
        mirror = next(
            (c for c in fc if isinstance(c, dict)
             and c.get("name") in ("megacap_growth_semis", "semis_ai")),
            None,
        )
        assert mirror is not None, "etf_strategy.yml mirror entry required"

        assert semis_cls["max_gross"] == pytest.approx(float(mirror["max_gross"]), abs=1e-9), (
            f"DRIFT: clusters.yml semis_ai max_gross={semis_cls['max_gross']} "
            f"!= etf_strategy.yml megacap_growth_semis max_gross={mirror['max_gross']}. "
            "Edit one to match the other, then re-run."
        )

    def test_etf_universe_members_all_in_clusters_yml(self):
        """The 6 canonical ETF-universe cluster members are present in clusters.yml semis_ai.

        This confirms the hard-coded _ETF_UNIVERSE_CLUSTER_MEMBERS fixture above stays in sync
        with what the test suite actually expects. If clusters.yml removes one of these 6, this
        test fails immediately (it's not hidden inside a dict-subtraction).
        """
        from portfolio import cluster_config
        cls = cluster_config.clusters()
        semis_cls = next((c for c in cls if c["id"] == "semis_ai"), None)
        assert semis_cls is not None
        shared = _norm_members(semis_cls["members"])
        missing = _ETF_UNIVERSE_CLUSTER_MEMBERS - shared
        assert not missing, (
            f"clusters.yml semis_ai is missing ETF-universe members: {sorted(missing)}. "
            f"These are required for the ETF G5 cap to work."
        )


# ── 2. CLUSTER-CONFIG READER ────────────────────────────────────────────────────────────────────

class TestClusterConfigReader:
    """portfolio/cluster_config: reader contract + degrade semantics."""

    def test_load_returns_dict(self):
        from portfolio import cluster_config
        d = cluster_config.load()
        assert isinstance(d, dict)

    def test_clusters_returns_normalised_list(self):
        from portfolio import cluster_config
        cls = cluster_config.clusters()
        assert isinstance(cls, list)
        for c in cls:
            assert isinstance(c["members"], list) and c["members"]
            assert all(m == m.upper() for m in c["members"])
            assert isinstance(c["max_gross"], float)
            assert "id" in c and "name" in c and "kind" in c

    def test_semis_ai_cluster_present(self):
        from portfolio import cluster_config
        ids = {c["id"] for c in cluster_config.clusters()}
        assert "semis_ai" in ids

    def test_etf_clusters_subset_has_correct_shape(self):
        """etf_clusters() returns the G5-compatible shape [{name, members, max_gross}]."""
        from portfolio import cluster_config
        etf = cluster_config.etf_clusters()
        assert isinstance(etf, list)
        for c in etf:
            assert set(c.keys()) >= {"name", "members", "max_gross"}
            assert isinstance(c["members"], list)
            assert isinstance(c["max_gross"], float)

    def test_missing_file_degrades_to_empty(self, monkeypatch, tmp_path):
        """A missing clusters.yml returns {} from load() and [] from clusters()."""
        from portfolio import cluster_config
        monkeypatch.setattr(cluster_config, "_CACHE", None)
        monkeypatch.setattr(cluster_config, "_SPEC_PATH", tmp_path / "nonexistent.yml")
        assert cluster_config.load() == {}
        assert cluster_config.clusters() == []
        assert cluster_config.etf_clusters() == []
        monkeypatch.setattr(cluster_config, "_CACHE", None)  # reset

    def test_corrupt_yaml_degrades_to_empty(self, monkeypatch, tmp_path):
        """A malformed YAML returns {} / [] and never raises."""
        from portfolio import cluster_config
        bad = tmp_path / "clusters.yml"
        bad.write_text(": : : bad yaml [[[\n")
        monkeypatch.setattr(cluster_config, "_CACHE", None)
        monkeypatch.setattr(cluster_config, "_SPEC_PATH", bad)
        d = cluster_config.load()
        assert isinstance(d, dict)
        monkeypatch.setattr(cluster_config, "_CACHE", None)

    def test_bad_cluster_entry_skipped(self, monkeypatch, tmp_path):
        """A cluster entry missing members or max_gross is silently skipped."""
        import yaml
        from portfolio import cluster_config
        spec = {"clusters": [
            {"id": "bad_no_members", "name": "x", "max_gross": 0.4},          # missing members
            {"id": "bad_no_max_gross", "name": "y", "members": ["SPY"]},      # missing max_gross
            {"id": "good", "name": "z", "members": ["SPY", "QQQ"], "max_gross": 0.5},
        ]}
        f = tmp_path / "clusters.yml"
        f.write_text(yaml.dump(spec))
        monkeypatch.setattr(cluster_config, "_CACHE", None)
        monkeypatch.setattr(cluster_config, "_SPEC_PATH", f)
        cls = cluster_config.clusters()
        assert len(cls) == 1 and cls[0]["id"] == "good"
        monkeypatch.setattr(cluster_config, "_CACHE", None)

    def test_members_are_upper_cased(self, monkeypatch, tmp_path):
        """Members are normalised to upper-case regardless of YAML case."""
        import yaml
        from portfolio import cluster_config
        spec = {"clusters": [
            {"id": "x", "name": "x", "members": ["qqq", "Xlk", "SMH"], "max_gross": 0.4},
        ]}
        f = tmp_path / "clusters.yml"
        f.write_text(yaml.dump(spec))
        monkeypatch.setattr(cluster_config, "_CACHE", None)
        monkeypatch.setattr(cluster_config, "_SPEC_PATH", f)
        cls = cluster_config.clusters()
        assert cls[0]["members"] == ["QQQ", "XLK", "SMH"]
        monkeypatch.setattr(cluster_config, "_CACHE", None)


# ── 3. G5 REGRESSION ────────────────────────────────────────────────────────────────────────────

class TestG5Regression:
    """G5 still clamps a synthetically over-weight cluster book correctly after the migration.

    The config source changed (etf_strategy.yml → clusters.yml), but the mathematical outcome is
    the same: over-weight combined cluster → pro-rata scale-down to max_gross.

    The current firm-decided cap is 0.35 (clusters.yml semis_ai + etf_strategy.yml mirror both
    set this value).  Tests use this level; _FIXED_GUARDRAILS_V2 in test_etf_book.py uses 0.40
    and is NOT changed (it monkeypatches _guardrails() so it is isolated from live config).
    """

    # G5 cluster fixture (same shape as etf_universe._DEFAULT_FACTOR_CLUSTERS)
    _CLUSTER_035 = {"name": "megacap_growth_semis",
                    "members": ["QQQ", "XLK", "SMH", "IGV", "MTUM", "SIZE"],
                    "max_gross": _ETF_CLUSTER_MAX_GROSS}  # 0.35

    def _guardrails_with(self, cluster: dict) -> dict:
        return {
            "max_single_weight": 0.35,
            "min_trade": 0.015,
            "offensive_cap": {"stressed": 0.55, "elevated": 0.80},
            "overextension": {"pct_vs_200d_cap": 40.0, "max_weight": 0.08},
            "factor_clusters": [cluster],
        }

    def test_g5_clamps_overweight_cluster(self, monkeypatch):
        """G5 via clusters.yml path: QQQ+SMH+MTUM=50% is above 0.35 → scaled to 0.35; SGOV untouched."""
        from bot import etf
        from brain import etf_board
        monkeypatch.setattr(etf_board, "etf_trend", lambda t: {})
        monkeypatch.setattr(etf, "_guardrails", lambda: self._guardrails_with(self._CLUSTER_035))
        target  = {"QQQ": 0.20, "SMH": 0.15, "MTUM": 0.15, "SGOV": 0.30}
        prices  = {"QQQ": 740.0, "SMH": 668.0, "MTUM": 345.0, "SGOV": 100.0}
        adj, notes = etf._apply_guardrails(target, prices, {"state": "calm"})
        cluster_gross = adj["QQQ"] + adj["SMH"] + adj["MTUM"]
        assert cluster_gross == pytest.approx(0.35, abs=1e-3), (
            f"cluster gross {cluster_gross:.4f} should equal max_gross=0.35")
        # pro-rata: each scaled by 0.35/0.50
        assert adj["QQQ"]  == pytest.approx(0.20 * (0.35 / 0.50), abs=1e-3)
        assert adj["SMH"]  == pytest.approx(0.15 * (0.35 / 0.50), abs=1e-3)
        assert adj["MTUM"] == pytest.approx(0.15 * (0.35 / 0.50), abs=1e-3)
        assert adj["SGOV"] == 0.30, "SGOV (outside cluster) must not be touched"
        assert any("factor cap" in n for n in notes)

    def test_g5_no_cap_when_under_max_gross(self, monkeypatch):
        """G5 is a NO-OP when combined cluster weight is already below max_gross."""
        from bot import etf
        from brain import etf_board
        monkeypatch.setattr(etf_board, "etf_trend", lambda t: {})
        monkeypatch.setattr(etf, "_guardrails", lambda: self._guardrails_with(self._CLUSTER_035))
        # QQQ=10%, SMH=10% = 20% combined, well below 0.35
        target = {"QQQ": 0.10, "SMH": 0.10, "SGOV": 0.30}
        prices = {"QQQ": 740.0, "SMH": 668.0, "SGOV": 100.0}
        adj, notes = etf._apply_guardrails(target, prices, {"state": "calm"})
        assert adj["QQQ"] == 0.10 and adj["SMH"] == 0.10   # untouched
        assert not any("factor cap" in n for n in notes)

    def test_guardrails_routes_through_clusters_yml(self):
        """guardrails()['factor_clusters'] comes from clusters.yml when the file is present.

        We verify the routing actually happened: the returned entry matches clusters.yml semis_ai
        (not the in-code _DEFAULT_FACTOR_CLUSTERS which has max_gross=0.40).
        """
        from portfolio import etf_universe, cluster_config
        shared = next(
            (c for c in cluster_config.clusters() if c["id"] == "semis_ai"),
            None,
        )
        assert shared is not None, "clusters.yml must have semis_ai entry for this test"

        g = etf_universe.guardrails()
        fc = g.get("factor_clusters") or []
        etf_entry = next(
            (c for c in fc if c.get("name") in ("megacap_growth_semis", "semis_ai")),
            None,
        )
        assert etf_entry is not None, (
            "guardrails()['factor_clusters'] missing megacap_growth_semis / semis_ai. "
            "Got: " + str([c.get("name") for c in fc])
        )
        assert etf_entry["max_gross"] == pytest.approx(shared["max_gross"], abs=1e-9), (
            "guardrails() routed through clusters.yml but max_gross doesn't match"
        )
        # All ETF-universe cluster members must be in the returned entry
        returned_members = _norm_members(etf_entry["members"])
        assert _ETF_UNIVERSE_CLUSTER_MEMBERS <= returned_members, (
            "guardrails() cluster entry is missing ETF-universe members: "
            + str(sorted(_ETF_UNIVERSE_CLUSTER_MEMBERS - returned_members))
        )

    def test_g5_fallback_when_clusters_yml_absent(self, monkeypatch):
        """When clusters.yml is absent, guardrails() falls back to etf_strategy.yml values.

        The cap level on fallback must equal the firm-decided level (_ETF_CLUSTER_MAX_GROSS) — the
        brake never weakens AND never silently disappears when the shared config isn't available.
        """
        from portfolio import etf_universe
        monkeypatch.setattr(etf_universe, "_clusters_from_shared_config", lambda: [])
        g = etf_universe.guardrails()
        fc = g.get("factor_clusters") or []
        etf_entry = next(
            (c for c in fc if c.get("name") in ("megacap_growth_semis", "semis_ai")),
            None,
        )
        assert etf_entry is not None, (
            "Fallback to etf_strategy.yml failed: no megacap_growth_semis in factor_clusters. "
            "Got: " + str([c.get("name") for c in fc])
        )
        # Fallback must match the firm-decided level (never loosen)
        assert etf_entry["max_gross"] <= _ETF_CLUSTER_MAX_GROSS + 1e-9, (
            f"Fallback weakened the brake! max_gross={etf_entry['max_gross']} "
            f"> firm level {_ETF_CLUSTER_MAX_GROSS}"
        )
        assert _ETF_UNIVERSE_CLUSTER_MEMBERS <= _norm_members(etf_entry["members"]), (
            "Fallback cluster entry is missing required ETF-universe members"
        )

    def test_g5_end_to_end_with_live_guardrails(self, monkeypatch):
        """End-to-end: live guardrails() → G5 cap fires on a synthetic over-weight book.

        Uses real guardrails() (which now routes through clusters.yml) so we prove the full chain
        from file load → normalisation → G5 cap in _apply_guardrails.  The cap level is the live
        firm-decided value (_ETF_CLUSTER_MAX_GROSS = 0.35).
        """
        from bot import etf
        from brain import etf_board
        from portfolio import etf_universe
        monkeypatch.setattr(etf_board, "etf_trend", lambda t: {})
        monkeypatch.setattr(etf, "_guardrails", etf_universe.guardrails)
        # 50% combined in one cluster: well above 0.35 → must be capped
        target = {"QQQ": 0.20, "SMH": 0.15, "MTUM": 0.15, "SGOV": 0.30}
        prices = {"QQQ": 740.0, "SMH": 668.0, "MTUM": 345.0, "SGOV": 100.0}
        adj, notes = etf._apply_guardrails(target, prices, {"state": "calm"})
        cluster_gross = adj["QQQ"] + adj["SMH"] + adj["MTUM"]
        # The cap must have fired: gross < input gross and <= max_gross
        assert cluster_gross < 0.50 - 1e-6, "G5 should have reduced the combined cluster gross"
        assert cluster_gross <= _ETF_CLUSTER_MAX_GROSS + 1e-6, (
            f"cluster gross {cluster_gross:.4f} exceeds max_gross={_ETF_CLUSTER_MAX_GROSS}")
        assert adj["SGOV"] == 0.30
        assert any("factor cap" in n for n in notes)


# ── 4. FALLBACK CHAIN (unit-level) ──────────────────────────────────────────────────────────────

class TestFallbackChain:
    """_clusters_from_shared_config() returns [] on every failure mode; guardrails() degrades
    correctly through the three-step chain (clusters.yml → etf_strategy.yml → in-code default)."""

    def test_clusters_from_shared_config_returns_list_on_real_file(self):
        from portfolio import etf_universe
        result = etf_universe._clusters_from_shared_config()
        assert isinstance(result, list)
        assert result, "_clusters_from_shared_config() returned [] with clusters.yml present"

    def test_clusters_from_shared_config_empty_on_import_error(self, monkeypatch):
        """If cluster_config.etf_clusters() raises (e.g. import/parse error) → return []."""
        from portfolio import etf_universe
        import portfolio.cluster_config as cc
        # Patch etf_clusters to raise
        original = cc.etf_clusters
        def _boom():
            raise RuntimeError("simulated error")
        monkeypatch.setattr(cc, "etf_clusters", _boom)
        result = etf_universe._clusters_from_shared_config()
        assert result == []
        monkeypatch.setattr(cc, "etf_clusters", original)

    def test_clusters_from_shared_config_empty_when_empty_list_returned(self, monkeypatch):
        from portfolio import etf_universe
        import portfolio.cluster_config as cc
        monkeypatch.setattr(cc, "etf_clusters", lambda: [])
        result = etf_universe._clusters_from_shared_config()
        assert result == []

    def test_guardrails_always_has_factor_clusters(self, monkeypatch):
        """guardrails() always returns non-empty factor_clusters through all fallback paths."""
        from portfolio import etf_universe
        # Path 1: clusters.yml present (normal case) → non-empty
        g1 = etf_universe.guardrails()
        assert g1["factor_clusters"], "factor_clusters must be non-empty with clusters.yml present"

        # Path 2: clusters.yml absent → etf_strategy.yml fallback
        monkeypatch.setattr(etf_universe, "_clusters_from_shared_config", lambda: [])
        g2 = etf_universe.guardrails()
        assert g2["factor_clusters"], "factor_clusters must be non-empty on etf_strategy.yml fallback"

        # Path 3: both absent → in-code default
        monkeypatch.setattr(etf_universe, "_clusters_from_shared_config", lambda: [])
        monkeypatch.setattr(etf_universe, "load_spec", lambda: {})
        g3 = etf_universe.guardrails()
        assert g3["factor_clusters"], "factor_clusters must be non-empty on in-code default fallback"

    def test_guardrails_never_loosens_cap_through_fallbacks(self, monkeypatch):
        """The cap level must be <= firm level through every fallback path."""
        from portfolio import etf_universe

        def _max_gross(g: dict) -> float | None:
            fc = g.get("factor_clusters") or []
            etf_e = next((c for c in fc if c.get("name") in ("megacap_growth_semis", "semis_ai")),
                         None)
            return etf_e["max_gross"] if etf_e else None

        # All three paths must not exceed the firm-decided level
        g1 = etf_universe.guardrails()
        mg1 = _max_gross(g1)
        assert mg1 is not None and mg1 <= _ETF_CLUSTER_MAX_GROSS + 1e-9, (
            f"Path 1 (clusters.yml): max_gross={mg1} > firm level {_ETF_CLUSTER_MAX_GROSS}")

        monkeypatch.setattr(etf_universe, "_clusters_from_shared_config", lambda: [])
        g2 = etf_universe.guardrails()
        mg2 = _max_gross(g2)
        assert mg2 is not None and mg2 <= _ETF_CLUSTER_MAX_GROSS + 1e-9, (
            f"Path 2 (etf_strategy.yml): max_gross={mg2} > firm level {_ETF_CLUSTER_MAX_GROSS}")

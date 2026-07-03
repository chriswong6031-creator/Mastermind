"""tests/test_sleeves_caps.py — W3 A2: enforce_book_caps v2 (name-cap-binds-every-sleeve + cluster pass).

WHAT THIS GUARDS
----------------
The W3 rebuild of ``portfolio.sleeves.enforce_book_caps``:

  1. The LEADERSHIP name-cap EXEMPTION is DEAD. Every position — every sleeve, single names, sector /
     thematic ETFs (SMH included, no carve-out) — is bound by name_cap 0.08, EXCEPT the broad-index
     allowlist (SPY/QQQ/VTI/RSP/IWM/DIA) which is capped at the looser broad_index_cap 0.15.
  2. The CLUSTER pass (new Stage-6.2 layer): aggregate weight by ``cluster_fn(ticker)`` ACROSS BOTH
     sleeves and scale any over-cap cluster DOWN pro-rata (freed weight -> cash, never redistributed).
     breach kind:'cluster', code 'D6'. semis_ai cap 0.35; default 0.40; singletons can never breach.
  3. theme_id is DISPLAY-ONLY (the old per-theme_id cap loop — structurally defeated because leadership
     legs set theme_id=ticker so a correlated cohort never summed — is GONE).
  4. The REPLAY FALSIFIER of the audit-era live book (leadership SMH/XLK/MTUM/IWM @0.125 + conviction
     tech summing IT ~0.426) + the calm-tape no-op guarantee.

Cluster identity is dependency-injected (``cluster_fn`` / ``cluster_cap_fn`` kwargs) so this suite passes
even before task A1's ``fragility_chain.cluster_id`` + ``config/clusters.yml`` land. One integration
test exercises the real default lookup (which degrades to singleton clusters when A1 is absent).
"""
from __future__ import annotations

import bot  # noqa: F401

from portfolio import sleeves


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _p(ticker, weight, *, sleeve="conviction", theme_id=None):
    return {"ticker": ticker, "theme_id": theme_id or ticker, "sleeve": sleeve, "weight": weight}


# An INJECTED cluster map mirroring what A1's fragility_chain.cluster_id() is contracted to return:
# the crowded semis/AI cohort collapses to one cluster; sector ETFs to a sector:* tier; anything else
# is its own singleton. Broad indices are NOT clustered here (they never over-concentrate a theme).
_CLUSTER_MAP = {
    "SMH": "semis_ai", "XLK": "semis_ai", "MTUM": "semis_ai",
    "NVDA": "semis_ai", "AVGO": "semis_ai", "AMD": "semis_ai", "SMCI": "semis_ai",
    "XLV": "sector:health", "XLF": "sector:financials", "XLI": "sector:industrials",
    "IWM": "broad_index", "SPY": "broad_index", "QQQ": "broad_index",
}


def _cluster_fn(t):
    """Injected ticker->cluster_id; UNKNOWN names return None so the code degrades them to singletons."""
    return _CLUSTER_MAP.get(str(t).upper().strip())


# ---------------------------------------------------------------------------
# LAYER 1 — the leadership name-cap exemption DIES
# ---------------------------------------------------------------------------

class TestNameCapBindsEverySleeve:
    def test_smh_leadership_leg_capped_to_name_cap(self):
        """SMH in the leadership sleeve at 0.125 is clamped to 0.08 — NO carve-out (the old
        'if sleeve==leadership: continue' exemption is dead)."""
        pos = [_p("SMH", 0.125, sleeve="leadership")]
        out = sleeves.enforce_book_caps(pos, cluster_fn=_cluster_fn)
        assert pos[0]["weight"] == 0.08
        assert any(b["kind"] == "name" and b["subject"] == "SMH" and b["cap"] == 0.08
                   for b in out["breaches"])

    def test_sector_etf_leadership_leg_capped(self):
        """A plain sector ETF (XLK) in the leadership sleeve is bound by the 0.08 name cap too."""
        pos = [_p("XLK", 0.11, sleeve="leadership")]
        out = sleeves.enforce_book_caps(pos, cluster_fn=_cluster_fn)
        assert pos[0]["weight"] == 0.08
        assert any(b["kind"] == "name" and b["subject"] == "XLK" for b in out["breaches"])

    def test_broad_index_allowed_to_15pct(self):
        """A broad, internally-diversified index (IWM) is allowed up to 0.15, NOT clamped to 0.08."""
        pos = [_p("IWM", 0.14, sleeve="leadership")]
        out = sleeves.enforce_book_caps(pos, cluster_fn=_cluster_fn)
        assert pos[0]["weight"] == 0.14                       # under 0.15 → untouched
        assert not any(b["kind"] == "name" for b in out["breaches"])

    def test_broad_index_over_15pct_capped_to_15(self):
        pos = [_p("SPY", 0.22, sleeve="leadership")]
        out = sleeves.enforce_book_caps(pos, cluster_fn=_cluster_fn)
        assert pos[0]["weight"] == 0.15
        assert any(b["kind"] == "name" and b["subject"] == "SPY" and b["cap"] == 0.15
                   for b in out["breaches"])

    def test_conviction_single_name_still_name_capped(self):
        pos = [_p("NVDA", 0.14, sleeve="conviction")]
        out = sleeves.enforce_book_caps(pos, cluster_fn=_cluster_fn)
        assert pos[0]["weight"] == 0.08
        assert any(b["kind"] == "name" and b["subject"] == "NVDA" for b in out["breaches"])


# ---------------------------------------------------------------------------
# LAYER 2 — the cluster pass (cross-sleeve, pro-rata scale-down)
# ---------------------------------------------------------------------------

class TestClusterPass:
    def test_semis_ai_cluster_trims_to_cap_prorata(self):
        """SMH+XLK+MTUM (leadership) + NVDA+AVGO (conviction) all in semis_ai. After the name cap each
        leg ≤0.08; the cluster still sums >0.35 → pro-rata scale-down to exactly 0.35."""
        pos = [_p("SMH", 0.125, sleeve="leadership"), _p("XLK", 0.125, sleeve="leadership"),
               _p("MTUM", 0.125, sleeve="leadership"),
               _p("NVDA", 0.07, sleeve="conviction"), _p("AVGO", 0.07, sleeve="conviction")]
        out = sleeves.enforce_book_caps(pos, cluster_fn=_cluster_fn)
        semis = sum(p["weight"] for p in out["positions"])    # every leg is in semis_ai here
        assert semis <= 0.35 + 1e-9
        assert abs(semis - 0.35) < 1e-6                        # trimmed to exactly the cap
        assert any(b["kind"] == "cluster" and b["subject"] == "semis_ai" and b["cap"] == 0.35
                   and b["code"] == "D6" for b in out["breaches"])

    def test_prorata_preserves_relative_weights(self):
        """Pro-rata: every member is scaled by the SAME factor (cap / cluster_gross)."""
        pos = [_p("SMH", 0.08, sleeve="leadership"), _p("XLK", 0.08, sleeve="leadership"),
               _p("MTUM", 0.08, sleeve="leadership"), _p("NVDA", 0.08, sleeve="conviction"),
               _p("AVGO", 0.08, sleeve="conviction")]                 # 0.40 gross, cap 0.35
        out = sleeves.enforce_book_caps(pos, cluster_fn=_cluster_fn)
        scale = 0.35 / 0.40
        for p in out["positions"]:
            assert abs(p["weight"] - 0.08 * scale) < 1e-9

    def test_default_cap_for_non_semis_cluster(self):
        """A cluster with no per-cluster override uses the 0.40 default. Under it → no breach."""
        pos = [_p("XLV", 0.08, sleeve="leadership"), _p("XLF", 0.08, sleeve="leadership")]
        out = sleeves.enforce_book_caps(pos, cluster_fn=lambda t: "sector:defensive")
        assert not any(b["kind"] == "cluster" for b in out["breaches"])  # 0.16 < 0.40

    def test_default_cap_breaches_at_040(self):
        pos = [_p(f"N{i}", 0.08, sleeve="conviction") for i in range(6)]   # 0.48 in one cluster
        out = sleeves.enforce_book_caps(pos, cluster_fn=lambda t: "sector:foo")
        total = sum(p["weight"] for p in out["positions"])
        assert abs(total - 0.40) < 1e-6
        assert any(b["kind"] == "cluster" and b["subject"] == "sector:foo" and b["cap"] == 0.40
                   for b in out["breaches"])

    def test_cluster_cap_fn_override_wins(self):
        """An injected cluster_cap_fn (A1's clusters.yml lookup) takes precedence over doctrine defaults."""
        pos = [_p("A", 0.08, sleeve="conviction"), _p("B", 0.08, sleeve="conviction")]
        out = sleeves.enforce_book_caps(pos, cluster_fn=lambda t: "custom",
                                        cluster_cap_fn=lambda cid: 0.10)   # tight 0.10 cap
        total = sum(p["weight"] for p in out["positions"])
        assert abs(total - 0.10) < 1e-6
        assert any(b["kind"] == "cluster" and b["cap"] == 0.10 for b in out["breaches"])


# ---------------------------------------------------------------------------
# INVARIANT — unknown name degrades to a SINGLETON cluster (never grouped, never falsely breaches)
# ---------------------------------------------------------------------------

class TestSingletonInvariant:
    def test_unknown_name_is_own_cluster(self):
        """An UNKNOWN ticker (cluster_fn → None) becomes its own singleton cluster: it still gets the
        name cap but can NEVER be spuriously grouped with peers into a cluster breach."""
        pos = [_p("WEIRDCO", 0.075, sleeve="conviction"),
               _p("ODDNAME", 0.075, sleeve="conviction")]
        out = sleeves.enforce_book_caps(pos, cluster_fn=lambda t: None)
        # each is its own cluster ≤ its (name-cap-floored) cluster cap → no cluster breach
        assert not any(b["kind"] == "cluster" for b in out["breaches"])
        assert all(p["weight"] == 0.075 for p in out["positions"])   # under the 0.08 name cap → untouched

    def test_singleton_cluster_cap_floored_at_name_cap(self):
        """A singleton's cluster cap is floored at the name cap, so a name at exactly the name cap can
        never breach its own cluster (the name pass already bounds it)."""
        pos = [_p("LONE", 0.08, sleeve="conviction")]
        out = sleeves.enforce_book_caps(pos, cluster_fn=lambda t: "LONE")
        assert not any(b["kind"] == "cluster" for b in out["breaches"])
        assert pos[0]["weight"] == 0.08

    def test_cluster_fn_raising_degrades_to_singleton(self):
        def _boom(_t):
            raise RuntimeError("cluster map corrupt")
        pos = [_p("AAA", 0.06, sleeve="conviction"), _p("BBB", 0.06, sleeve="conviction")]
        out = sleeves.enforce_book_caps(pos, cluster_fn=_boom)
        assert not any(b["kind"] == "cluster" for b in out["breaches"])   # each → singleton, no group


# ---------------------------------------------------------------------------
# theme_id is DISPLAY-ONLY (no more per-theme_id cap)
# ---------------------------------------------------------------------------

class TestThemeIdDisplayOnly:
    def test_shared_theme_id_no_longer_capped_as_theme(self):
        """Two names sharing theme_id='ai' but in DIFFERENT clusters are NOT theme-capped. The old
        0.25 theme cap is gone; only name + cluster caps bind. (Here both are under name+cluster caps.)"""
        pos = [_p("X1", 0.07, sleeve="conviction", theme_id="ai"),
               _p("X2", 0.07, sleeve="conviction", theme_id="ai")]
        out = sleeves.enforce_book_caps(
            pos, cluster_fn=lambda t: {"X1": "c1", "X2": "c2"}[t])
        assert not any(b["kind"] == "theme" for b in out["breaches"])     # no 'theme' breaches ever
        assert all(p["weight"] == 0.07 for p in out["positions"])          # 0.14 theme sum uncapped now


# ---------------------------------------------------------------------------
# REPLAY FALSIFIER — the audit-era live book + calm-tape no-op
# ---------------------------------------------------------------------------

class TestAuditEraReplayFalsifier:
    def _audit_book(self):
        """The audit-era live book shape: leadership SMH/XLK/MTUM/IWM @0.125 each + conviction tech
        names summing Information Technology to ~0.426 of book (the state that carried IT=42.6% with
        safety.json breaches:[] — nothing fired)."""
        lead = [_p(t, 0.125, sleeve="leadership") for t in ("SMH", "XLK", "MTUM", "IWM")]
        # conviction tech cohort in semis_ai; ~0.176 so leadership semis (SMH+XLK+MTUM=0.375) + these
        # push the correlated IT cohort well past any single-theme cap. (IWM is a broad index, separate.)
        conv = [_p("NVDA", 0.05, sleeve="conviction"), _p("AVGO", 0.05, sleeve="conviction"),
                _p("AMD", 0.04, sleeve="conviction"), _p("SMCI", 0.036, sleeve="conviction")]
        return lead + conv

    def test_audit_book_now_binds(self):
        book = self._audit_book()
        gross_before = sum(p["weight"] for p in book)            # capture BEFORE the in-place mutation
        out = sleeves.enforce_book_caps(book, cluster_fn=_cluster_fn)
        by = {p["ticker"]: p for p in out["positions"]}

        # (a) SMH personally clamped to the name cap — the largest single risk line is gone.
        assert by["SMH"]["weight"] <= 0.08 + 1e-9

        # (b) IWM (broad index) allowed up to 0.15 — NOT clamped to 0.08.
        assert by["IWM"]["weight"] == 0.125
        assert not any(b["kind"] == "name" and b["subject"] == "IWM" for b in out["breaches"])

        # (c) the semis_ai cluster is trimmed to <= 0.35 and emits a D6 kind:'cluster' breach.
        semis = sum(p["weight"] for p in out["positions"] if _cluster_fn(p["ticker"]) == "semis_ai")
        assert semis <= 0.35 + 1e-9
        assert any(b["kind"] == "cluster" and b["subject"] == "semis_ai" and b["code"] == "D6"
                   for b in out["breaches"])

        # (d) freed weight is NOT redistributed: the total book weight only ever DROPS (subtract-only).
        gross_after = sum(p["weight"] for p in out["positions"])
        assert gross_after < gross_before                        # strictly reduced (freed → cash)

    def test_calm_tape_book_passes_unchanged(self):
        """The W2 calm-tape fixture shape (4 sector ETFs, distinct clusters) sized BELOW every threshold
        passes through the cap stack UNCHANGED — the caps no-op below thresholds (anti-shrink invariant).
        Mirrors tests/test_phase2.py::TestW2CalmTapeInvariance's byte-identical guarantee, at the book
        layer: no name breach (each ≤0.08), no cluster breach (each cluster ≤0.40)."""
        golden = [_p(t, 0.07, sleeve="leadership") for t in ("XLK", "XLV", "XLF", "XLI")]
        book = [dict(g) for g in golden]
        out = sleeves.enforce_book_caps(
            book, cluster_fn=lambda t: f"sector:{t}")             # each ETF its own sector cluster
        assert out["breaches"] == []                              # nothing fires below thresholds
        assert book == golden                                     # byte-identical pass-through


# ---------------------------------------------------------------------------
# W-I Task 5(a) — BALLAST asset class: SGOV/BIL/SHY/USFR cap-exempt + cluster-excluded
# ---------------------------------------------------------------------------

class TestBallastCapExempt:
    """The W3 name-cap false-positive: SGOV at 12% of the ETF book was trimmed to 0.08 (−$39.2k
    of T-bill ballast) because the name cap couldn't distinguish a concentrated equity bet from
    a cash-equivalent position. T-bill ETFs are NOT concentrated bets: they ARE the rotation dry
    powder. W-I Task 5(a) exempts them: ballast_cap 0.40, excluded from cluster aggregation."""

    def test_sgov_not_trimmed_by_name_cap(self):
        """SGOV at 12% of book (the ETF-book incident weight) must NOT be trimmed to 0.08 —
        it is ballast, not a concentrated equity bet."""
        pos = [_p("SGOV", 0.12, sleeve="conviction")]
        out = sleeves.enforce_book_caps(pos, cluster_fn=_cluster_fn)
        assert pos[0]["weight"] == 0.12    # untouched — under ballast_cap 0.40
        assert not any(b["subject"] == "SGOV" for b in out["breaches"])

    def test_bil_usfr_shy_also_exempt(self):
        """All four T-bill ETFs are exempt from the 0.08 name cap."""
        for tkr in ("BIL", "USFR", "SHY"):
            pos = [_p(tkr, 0.20, sleeve="conviction")]
            out = sleeves.enforce_book_caps(pos, cluster_fn=_cluster_fn)
            assert pos[0]["weight"] == 0.20, f"{tkr} should not be trimmed by the name cap"
            assert not any(b["subject"] == tkr for b in out["breaches"])

    def test_ballast_cap_bites_when_truly_over(self):
        """The ballast cap (0.40) still bites when a ballast position is genuinely over it
        (degenerate edge case — a book should never hold >40% T-bills). Keeps the invariant."""
        pos = [_p("SGOV", 0.50, sleeve="conviction")]
        out = sleeves.enforce_book_caps(pos, cluster_fn=_cluster_fn)
        assert pos[0]["weight"] == 0.40    # clamped to ballast_cap, not 0.08
        assert any(b["subject"] == "SGOV" and b["cap"] == 0.40 for b in out["breaches"])

    def test_ballast_excluded_from_cluster_aggregation(self):
        """SGOV MUST NOT be counted in the cluster pass: adding a T-bill to a semis_ai cluster
        run-total would cause a spurious cluster breach against equity factor caps."""
        # A semis book right at the semis_ai cap (0.35) + a large SGOV position —
        # the cluster pass must NOT treat SGOV as semis_ai weight.
        pos = [
            _p("SMH",  0.08, sleeve="leadership"),
            _p("XLK",  0.08, sleeve="leadership"),
            _p("MTUM", 0.08, sleeve="leadership"),
            _p("NVDA", 0.07, sleeve="conviction"),    # semis_ai total = 0.31 (under 0.35)
            _p("SGOV", 0.12, sleeve="conviction"),    # ballast — must be invisible to the cluster
        ]
        out = sleeves.enforce_book_caps(pos, cluster_fn=_cluster_fn)
        # no cluster breach on semis_ai (SGOV not counted)
        assert not any(b["kind"] == "cluster" and b["subject"] == "semis_ai"
                       for b in out["breaches"])
        # SGOV weight untouched
        sgov = next(p for p in out["positions"] if p["ticker"] == "SGOV")
        assert sgov["weight"] == 0.12

    def test_ballast_does_not_shield_equity_from_cluster_cap(self):
        """Adding ballast must NEVER shield the equity cluster from the cluster cap: the equity
        legs must still be trimmed if THEY are over the cluster cap (ballast just doesn't add to it)."""
        pos = [
            _p("SMH",  0.08, sleeve="leadership"),
            _p("XLK",  0.08, sleeve="leadership"),
            _p("MTUM", 0.08, sleeve="leadership"),
            _p("NVDA", 0.08, sleeve="conviction"),
            _p("AVGO", 0.08, sleeve="conviction"),    # semis_ai = 0.40 > cap 0.35 → cluster breach
            _p("SGOV", 0.15, sleeve="conviction"),    # ballast alongside them
        ]
        out = sleeves.enforce_book_caps(pos, cluster_fn=_cluster_fn)
        # cluster breach on semis_ai fires
        assert any(b["kind"] == "cluster" and b["subject"] == "semis_ai"
                   for b in out["breaches"])
        # semis_ai trimmed to <= 0.35
        semis_wt = sum(p["weight"] for p in out["positions"]
                       if _cluster_fn(p["ticker"]) == "semis_ai")
        assert semis_wt <= 0.35 + 1e-9
        # SGOV untouched by the equity cluster trim
        sgov = next(p for p in out["positions"] if p["ticker"] == "SGOV")
        assert sgov["weight"] == 0.15

    def test_w3_replay_with_ballast_sgov_preserved(self):
        """W3 replay update (counterfactual §4b): the audit-era book with SGOV at 0.12.
        Old: name cap trimmed SGOV 0.12→0.08 (−$39.2k false positive). New: SGOV preserved at 0.12."""
        book = [_p(t, 0.125, sleeve="leadership") for t in ("SMH", "XLK", "MTUM", "IWM")]
        book += [_p("NVDA", 0.05, sleeve="conviction"), _p("AVGO", 0.05, sleeve="conviction"),
                 _p("AMD",  0.04, sleeve="conviction"), _p("SMCI", 0.036, sleeve="conviction"),
                 _p("SGOV", 0.12, sleeve="conviction")]   # ETF book ballast (the incident false positive)
        out = sleeves.enforce_book_caps(book, cluster_fn=_cluster_fn)
        by = {p["ticker"]: p for p in out["positions"]}

        # SMH still clamped to 0.08 by name cap (unchanged)
        assert by["SMH"]["weight"] <= 0.08 + 1e-9
        # semis_ai cluster still trimmed
        semis_wt = sum(p["weight"] for p in out["positions"]
                       if _cluster_fn(p["ticker"]) == "semis_ai")
        assert semis_wt <= 0.35 + 1e-9
        # SGOV NOT trimmed — the false positive is fixed
        assert by["SGOV"]["weight"] == 0.12
        # no name breach on SGOV
        assert not any(b["kind"] == "name" and b["subject"] == "SGOV" for b in out["breaches"])


# ---------------------------------------------------------------------------
# INTEGRATION — the real default lookup (degrades to singletons before A1 lands)
# ---------------------------------------------------------------------------

class TestRealClusterLookupIntegration:
    def test_default_cluster_fn_degrades_safe(self):
        """With NO injected cluster_fn, enforce_book_caps lazily imports fragility_chain.cluster_id.
        Whether or not A1 has landed it, the name cap must still bind and the call must never raise."""
        pos = [_p("SMH", 0.125, sleeve="leadership"), _p("NVDA", 0.14, sleeve="conviction")]
        out = sleeves.enforce_book_caps(pos)                      # real default path
        assert all(p["weight"] <= 0.08 + 1e-9 for p in out["positions"])
        assert any(b["kind"] == "name" for b in out["breaches"])

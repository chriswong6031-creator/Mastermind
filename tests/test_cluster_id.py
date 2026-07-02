"""W3 A1 — ONE cluster identity: portfolio.fragility_chain.cluster_id() + config/clusters.yml.

The 0.25 theme cap keyed on per-instrument theme_id (leadership legs set theme_id=ticker), so a
0.79-0.94-correlated cohort (SMH/XLK/MTUM) never SUMMED — the live book carried IT = 42.6% with
breaches:[]. cluster_id() is the single firm-wide 3-tier resolver the book/firm cluster caps key on
instead: (a) explicit clusters.yml membership → the cluster id; (b) GICS sector → 'sector:<Name>';
(c) SINGLETON 'name:<TICKER>' (unknown degrades to its own cluster — never spuriously grouped).

Offline / fixture-injected (no live market state asserted — R2 mutates under us; we assert INTENT:
the correlated cohort collapses to one id, an unknown name stays a singleton). Includes the feasible
STABILITY FALSIFIER (the architecture wants 0 relabels absent a config edit): run cluster_id over the
union of every ticker in the live published books TWICE with a cache reset between and assert identical
output, assert the explicit tier resolves ≥ the chain members, and a dict-order-shuffle determinism
guard.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from portfolio import fragility_chain as FC

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "w3_books"

# whether clusters.yml is actually loadable in this checkout (PyYAML present + file parseable).
# When it is NOT, the explicit tier is empty and everything degrades to sector:/name: — the
# membership-specific assertions skip, but the degrade-safe + determinism guards still run.
try:
    from portfolio import cluster_config
    _HAS_CLUSTERS = bool(cluster_config.clusters())
except Exception:  # noqa: BLE001
    _HAS_CLUSTERS = False


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Every test starts from a cold cache so cross-test state never leaks (the falsifier relies on
    a genuine re-read)."""
    FC._reset_cluster_cache()
    yield
    FC._reset_cluster_cache()


# ── tier (a) — explicit clusters.yml membership binds the correlated cohort ─────────────────────

@pytest.mark.skipif(not _HAS_CLUSTERS, reason="clusters.yml not loadable (no PyYAML / absent)")
def test_semis_cohort_collapses_to_one_cluster():
    # THE fix: SMH/XLK/MTUM (leadership legs that set theme_id=ticker) + the single-name semis now
    # share ONE identity, so the book cluster cap can finally see the 0.79-0.94 cohort sum.
    cohort = ["SMH", "XLK", "MTUM", "NVDA", "AMD", "MU", "AVGO", "TSM", "ANET"]
    ids = {FC.cluster_id(t) for t in cohort}
    assert ids == {"semis_ai"}, f"semis cohort must be ONE cluster, got {ids}"


@pytest.mark.skipif(not _HAS_CLUSTERS, reason="clusters.yml not loadable")
def test_membership_is_case_insensitive_and_wins_over_sector():
    assert FC.cluster_id("smh") == FC.cluster_id("SMH") == "semis_ai"
    # explicit membership (tier a) beats an injected sector (tier b) — a member is never re-tiered.
    assert FC.cluster_id("NVDA", sector="Health Care") == "semis_ai"


@pytest.mark.skipif(not _HAS_CLUSTERS, reason="clusters.yml not loadable")
def test_distinct_clusters_stay_distinct():
    # megacap platforms and long-duration software are NOT folded into semis_ai (over-coarsening).
    assert FC.cluster_id("MSFT") == "megacap_platform"
    assert FC.cluster_id("GOOGL") == "megacap_platform"
    assert FC.cluster_id("ARKK") == "long_duration_tech"
    assert FC.cluster_id("JPM") == "rate_sensitive"
    assert FC.cluster_id("XOM") == "commodity_inflation"
    assert FC.cluster_id("NVDA") != FC.cluster_id("MSFT")


@pytest.mark.skipif(not _HAS_CLUSTERS, reason="clusters.yml not loadable")
def test_cluster_cap_reads_from_clusters_yml():
    # the tighter semis cap vs the default; a singleton/sector id has no explicit cap (uses caller default).
    assert FC.cluster_cap("semis_ai") == 0.35
    assert FC.cluster_cap("megacap_platform") == 0.40
    assert FC.cluster_cap("name:ZZZZ") is None
    assert FC.cluster_cap("sector:Information Technology") is None
    assert FC.cluster_cap("name:ZZZZ", default=0.40) == 0.40


# ── tier (b) — GICS sector when there is no explicit membership ─────────────────────────────────

def test_injected_sector_tier_for_non_member():
    # a non-member with an injected sector groups by sector (no stockdata read needed).
    assert FC.cluster_id("FOOBAR_NOT_A_MEMBER", sector="Health Care") == "sector:Health Care"
    # blank/whitespace injected sector is ignored (falls through to snapshot then singleton).
    assert FC.cluster_id("ZZ_UNMAPPED_XYZ", sector="   ") == "name:ZZ_UNMAPPED_XYZ"


# ── tier (c) — SINGLETON: unknown degrades to its own cluster, never grouped, never un-capped ────

def test_unknown_name_is_a_singleton_cluster():
    a = FC.cluster_id("ZZ_UNMAPPED_A")
    b = FC.cluster_id("ZZ_UNMAPPED_B")
    assert a == "name:ZZ_UNMAPPED_A"
    assert b == "name:ZZ_UNMAPPED_B"
    assert a != b                       # two unknowns are NOT spuriously grouped together
    # the malformed/empty row still gets a stable singleton sentinel (never raises).
    assert FC.cluster_id("") == "name:?"
    assert FC.cluster_id(None) == "name:?"
    assert FC.cluster_id("   ") == "name:?"


def test_never_raises_on_garbage():
    for junk in [None, "", "   ", 12345, "😀", "A" * 500, "a\nb"]:
        try:
            out = FC.cluster_id(junk)  # type: ignore[arg-type]
        except Exception as e:  # noqa: BLE001
            pytest.fail(f"cluster_id raised on {junk!r}: {e}")
        assert isinstance(out, str) and out


# ── the live-books union: fixture loader ────────────────────────────────────────────────────────

def _live_book_tickers() -> list[str]:
    """Union of every ticker across the trimmed published-book fixtures (data/portfolio/latest.json +
    data/portfolios/*/latest.json, copied into tests/fixtures/w3_books/). Sorted for determinism."""
    tks: set[str] = set()
    for f in sorted(_FIXTURES.glob("*.json")):
        try:
            d = json.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        for r in (d.get("positions") or []):
            if isinstance(r, dict) and r.get("ticker"):
                tks.add(str(r["ticker"]).upper().strip())
    return sorted(tks)


def test_fixtures_present():
    # the falsifier is only meaningful if the fixtures were copied in.
    assert _FIXTURES.exists(), f"missing fixtures dir {_FIXTURES}"
    tks = _live_book_tickers()
    assert len(tks) >= 10, f"expected a non-trivial union of live-book tickers, got {tks}"


# ── STABILITY FALSIFIER — 0 relabels over the live-book union absent a config edit ──────────────

def test_stability_no_relabel_across_cache_reset():
    """The architecture demands 0 relabels absent a config edit. Feasible version: resolve the whole
    live-book union, reset the cache (forces a genuine clusters.yml + index rebuild), resolve again,
    assert byte-identical output. A relabel here means the identity is non-deterministic — the exact
    failure the single-map firebreak exists to prevent."""
    tks = _live_book_tickers()
    first = {t: FC.cluster_id(t) for t in tks}
    FC._reset_cluster_cache()
    second = {t: FC.cluster_id(t) for t in tks}
    assert first == second, (
        "cluster_id RELABELED across a cache reset (non-deterministic identity): "
        f"{ {k: (first[k], second[k]) for k in first if first[k] != second[k]} }"
    )


@pytest.mark.skipif(not _HAS_CLUSTERS, reason="clusters.yml not loadable")
def test_explicit_tier_covers_chain_members():
    """The explicit-cluster tier must resolve AT LEAST the fragility-chain members to a real (non-
    singleton) cluster — the chains were a seed source, so nothing they mapped may fall through to a
    singleton. Asserts explicit ≥ chains (superset), the property the seed step guarantees."""
    from portfolio import fragility_chain as FCC
    chain_members: set[str] = set()
    for c in FCC.all_chains().values():
        chain_members |= set(c["tickers"]) | set(c["proxies"])
    fell_through = sorted(t for t in chain_members
                          if FC.cluster_id(t).startswith(("name:", "sector:")))
    assert not fell_through, (
        "these fragility-chain members were NOT covered by an explicit clusters.yml cluster "
        f"(the seed sources must be a subset of the explicit tier): {fell_through}"
    )


def test_determinism_under_dict_order_shuffle(monkeypatch):
    """Identity must not depend on dict iteration order. We shuffle the ORDER of the normalised cluster
    list the resolver indexes and assert every live-book ticker keeps the SAME id — a ticker's cluster
    is a property of membership, not of which cluster happened to be iterated first (except for a name
    deliberately listed in two clusters, where 'first in file order wins' is the documented, stable
    rule — none of the live-book names are multiply-listed, so shuffling is a clean determinism probe)."""
    try:
        from portfolio import cluster_config
    except Exception:  # noqa: BLE001
        pytest.skip("cluster_config unavailable")
    base = cluster_config.clusters()
    if not base:
        pytest.skip("clusters.yml not loadable")

    tks = _live_book_tickers()
    baseline = {t: FC.cluster_id(t) for t in tks}

    # sanity: no live-book ticker is a member of >1 cluster (so order genuinely shouldn't matter).
    seen: dict[str, str] = {}
    for c in base:
        for m in c["members"]:
            if m in {t for t in tks}:
                assert m not in seen or seen[m] == c["id"], f"{m} multiply-listed across clusters"
                seen[m] = c["id"]

    reversed_clusters = list(reversed(base))
    monkeypatch.setattr(cluster_config, "clusters", lambda: reversed_clusters)
    FC._reset_cluster_cache()          # note: also re-clears cluster_config cache, but clusters() is patched
    shuffled = {t: FC.cluster_id(t) for t in tks}
    assert shuffled == baseline, (
        "cluster_id changed under cluster-order shuffle (order-dependent identity): "
        f"{ {k: (baseline[k], shuffled[k]) for k in baseline if baseline[k] != shuffled[k]} }"
    )


# ── degrade-safe: absent clusters.yml → everything singleton, caps still bind per-name ──────────

def test_degrades_to_singleton_when_reader_empty(monkeypatch):
    """No explicit map (missing/corrupt clusters.yml or no PyYAML) → every name is its OWN cluster;
    caps still bind per-name, they just stop grouping. The invariant: a data outage may only COARSEN
    identity, never un-cap or spuriously group."""
    try:
        from portfolio import cluster_config
    except Exception:  # noqa: BLE001
        pytest.skip("cluster_config unavailable")
    monkeypatch.setattr(cluster_config, "clusters", lambda: [])
    FC._reset_cluster_cache()
    # a name that WOULD be semis_ai now falls to its sector (tier b) or singleton (tier c) — never
    # grouped with peers via the explicit tier.
    assert not FC.cluster_id("SMH").startswith("semis_ai")
    assert FC.cluster_id("ZZ_UNMAPPED_Q") == "name:ZZ_UNMAPPED_Q"
    assert FC.cluster_cap("semis_ai") is None          # no explicit caps either

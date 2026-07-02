"""Firm-level cross-book exposure MONITOR — offline tests.

Seeds synthetic published books (latest.json) into a tmp store via the registry._ROOT redirect the
other book tests use, then asserts the read-only aggregator FLAGS a name held by enough books,
leaves a single-book name unflagged, degrades to an honest stub on empty/missing books and NEVER
raises, and honours the env threshold overrides. No network, no live book is touched.
"""
from __future__ import annotations

import json

import pytest

from portfolio import firm_exposure, registry


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """Isolate per-id portfolio state to a tmp root (registry.data_dir derives off _ROOT), and point
    the module's own _ROOT at the tmp tree too (so the absent sector snapshot is honestly omitted)."""
    monkeypatch.setattr(registry, "_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(firm_exposure, "_ROOT", tmp_path, raising=False)
    return tmp_path


def _seed(pid: str, positions: list[dict], *, nav: float = 1_000_000.0,
          currency: str | None = None) -> None:
    """Write a minimal published latest.json for book `pid` into the tmp store."""
    d = registry.data_dir(pid)
    d.mkdir(parents=True, exist_ok=True)
    doc = {"schema": "portfolio.v1", "portfolio_id": pid, "as_of": "2026-06-23",
           "nav": nav, "positions": positions}
    if currency:
        doc["currency"] = currency
    (d / "latest.json").write_text(json.dumps(doc))


# --------------------------------------------------------------------------- #
# the core flag: a name held by >= N_BOOKS is flagged with the right n_books
# --------------------------------------------------------------------------- #
def test_pileup_flagged_at_three_books(iso):
    # NVDA in 3 USD books; AAPL in just 1 → only NVDA should be flagged as a pile-up.
    _seed("flagship",    [{"ticker": "NVDA", "weight": 0.10}, {"ticker": "AAPL", "weight": 0.05}])
    _seed("heavyweight", [{"ticker": "NVDA", "weight": 0.20}])
    _seed("autonomous",  [{"ticker": "NVDA", "weight": 0.06}, {"ticker": "MSFT", "weight": 0.04}])

    s = firm_exposure.summary()

    assert s["n_books"] == 3
    # NVDA is flagged, n_books == 3, and lists every holding book
    nvda = next((f for f in s["flags"] if f.get("ticker") == "NVDA"), None)
    assert nvda is not None
    assert nvda["n_books"] == 3
    assert set(nvda["books_holding"]) == {"flagship", "heavyweight", "autonomous"}
    # AAPL (1 book, sub-cap weight) is NOT flagged
    assert not any(f.get("ticker") == "AAPL" for f in s["flags"])
    # and it appears unflagged in the top-exposures table
    aapl = next((e for e in s["top_exposures"] if e["ticker"] == "AAPL"), None)
    assert aapl is not None and aapl["flagged"] is False and aapl["n_books"] == 1


def test_single_book_name_not_flagged(iso):
    _seed("flagship",   [{"ticker": "TSLA", "weight": 0.05}])
    _seed("autonomous", [{"ticker": "AMD", "weight": 0.05}])
    s = firm_exposure.summary()
    # nothing is held by >=3 books and no name clears the default 8% firm cap → no name flags
    assert not any(f["kind"] == "name" for f in s["flags"])
    assert s["n_books"] == 2


# --------------------------------------------------------------------------- #
# the over-weight single-name flag (firm weight cap), independent of book count
# --------------------------------------------------------------------------- #
def test_overweight_name_flagged_even_in_one_book(iso):
    # one book, but a huge weight → the firm-weight cap fires even though n_books == 1
    _seed("heavyweight", [{"ticker": "BIGCO", "weight": 0.40}])
    s = firm_exposure.summary()
    f = next((x for x in s["flags"] if x.get("ticker") == "BIGCO"), None)
    assert f is not None
    assert "firm weight" in f["reason"]


# --------------------------------------------------------------------------- #
# empty / missing books → honest stub, never raises
# --------------------------------------------------------------------------- #
def test_no_books_is_honest_stub(iso):
    s = firm_exposure.summary()      # nothing seeded
    assert s["n_books"] == 0
    assert s["books"] == []
    assert s["flags"] == []
    assert s["top_exposures"] == []
    assert "note" in s and isinstance(s["note"], str)


def test_book_with_no_positions_is_skipped(iso):
    _seed("flagship", [])                                   # empty positions → skipped
    _seed("autonomous", [{"ticker": "NVDA", "weight": 0.1}])
    s = firm_exposure.summary()
    assert [b["id"] for b in s["books"]] == ["autonomous"]
    assert s["n_books"] == 1


def test_never_raises_on_garbage(iso):
    # a corrupt latest.json must not blow up the read-only monitor
    d = registry.data_dir("china")
    d.mkdir(parents=True, exist_ok=True)
    (d / "latest.json").write_text("{ this is not json")
    _seed("flagship", [{"ticker": "NVDA", "weight": 0.1}])
    s = firm_exposure.summary()       # should degrade past the corrupt book
    assert s["n_books"] == 1
    assert [b["id"] for b in s["books"]] == ["flagship"]


# --------------------------------------------------------------------------- #
# threshold env overrides
# --------------------------------------------------------------------------- #
def test_min_books_env_override(iso, monkeypatch):
    # NVDA in only 2 books; default min_books=3 → not flagged as a pile-up
    _seed("flagship",   [{"ticker": "NVDA", "weight": 0.04}])
    _seed("autonomous", [{"ticker": "NVDA", "weight": 0.04}])
    s = firm_exposure.summary()
    assert not any(f["kind"] == "name" for f in s["flags"])
    # lower the bar to 2 books → now flagged
    monkeypatch.setenv("FIRM_MIN_BOOKS", "2")
    s2 = firm_exposure.summary()
    nvda = next((f for f in s2["flags"] if f.get("ticker") == "NVDA"), None)
    assert nvda is not None and nvda["n_books"] == 2


def test_name_max_env_override(iso, monkeypatch):
    _seed("flagship", [{"ticker": "MIDCO", "weight": 0.06}])     # one book, 6% weight
    # default name_max 8% → not flagged
    assert not any(f.get("ticker") == "MIDCO" for f in firm_exposure.summary()["flags"])
    # drop the cap to 5% → now over the line
    monkeypatch.setenv("FIRM_NAME_MAX", "0.05")
    assert any(f.get("ticker") == "MIDCO" for f in firm_exposure.summary()["flags"])


# --------------------------------------------------------------------------- #
# weight derivation from market_value / nav when no explicit weight is published
# --------------------------------------------------------------------------- #
def test_weight_derived_from_market_value(iso):
    _seed("flagship", [{"ticker": "NODE", "market_value": 250_000.0}], nav=1_000_000.0)
    s = firm_exposure.summary()
    node = next((e for e in s["top_exposures"] if e["ticker"] == "NODE"), None)
    assert node is not None
    assert node["firm_weight"] == pytest.approx(0.25, abs=1e-6)


# --------------------------------------------------------------------------- #
# cross-currency honesty: a CNY book + USD books → equal-book mean unless FX converts,
# but the monitor must still aggregate and never raise, and label the method in `note`.
# --------------------------------------------------------------------------- #
def test_cross_currency_aggregates_honestly(iso):
    _seed("flagship",   [{"ticker": "NVDA", "weight": 0.10}])
    _seed("autonomous", [{"ticker": "NVDA", "weight": 0.10}])
    _seed("china",      [{"ticker": "600519.SS", "weight": 0.20}], currency="CNY")
    s = firm_exposure.summary()
    # books all loaded, and the note honestly states the aggregation basis (USD-clean or equal-book)
    assert s["n_books"] == 3
    assert isinstance(s["currency_clean"], bool)
    assert "firm" in s["note"].lower()
    # NVDA still aggregates across the two USD books with a sane firm weight
    nvda = next((e for e in s["top_exposures"] if e["ticker"] == "NVDA"), None)
    assert nvda is not None and nvda["firm_weight"] > 0


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# W3 B1 — headroom() + clamp_book(): the BINDING firm-wide cap
# ═══════════════════════════════════════════════════════════════════════════════════════════════
# These use the SAME iso fixture (tmp store via registry._ROOT + firm_exposure._ROOT). SMH/NVDA/XLK are
# explicit config/clusters.yml members of 'semis_ai' (verified in cluster_id resolution), so the cluster
# aggregation is deterministic and independent of the live/mutating stockdata snapshot. A fake ticker
# (ZZZFAKE) has no membership and no stockdata → its own singleton cluster.


# --------------------------------------------------------------------------- #
# (a) firm cluster at 0.28 elsewhere -> this book's semis target clamps to 0.02 headroom
# --------------------------------------------------------------------------- #
def test_headroom_cluster_binds_at_remaining_room(iso):
    # peers (autonomous + etf) together hold 0.28 of the semis_ai cluster; the firm cluster cap is 0.30,
    # so flagship may still hold at most 0.02 in that cluster.
    _seed("autonomous", [{"ticker": "SMH", "weight": 0.18}])
    _seed("etf",        [{"ticker": "XLK", "weight": 0.10}])   # both semis_ai → peer cluster 0.28
    room = firm_exposure.headroom("semis_ai", "flagship")
    assert room == pytest.approx(0.02, abs=1e-9)

    # and clamp_book scales a flagship semis target DOWN to that 0.02 headroom, pro-rata. Both legs are
    # UNDER the 0.10 firm name cap (so the name pass is a no-op) → the cluster pass is pure pro-rata.
    book = [{"ticker": "NVDA", "sleeve": "conviction", "weight": 0.06},
            {"ticker": "MU", "sleeve": "leadership", "weight": 0.04}]   # both semis_ai, own=0.10
    res = firm_exposure.clamp_book(book, "flagship")
    assert res["bound"] is True
    own_after = sum(p["weight"] for p in res["positions"])
    assert own_after == pytest.approx(0.02, abs=1e-4)
    # pro-rata: NVDA kept 0.06/0.10 of the room, MU kept 0.04/0.10
    nvda = next(p for p in res["positions"] if p["ticker"] == "NVDA")
    mu = next(p for p in res["positions"] if p["ticker"] == "MU")
    assert nvda["weight"] == pytest.approx(0.02 * 0.6, abs=1e-4)
    assert mu["weight"] == pytest.approx(0.02 * 0.4, abs=1e-4)


# --------------------------------------------------------------------------- #
# (b) absent peers -> no clamp (headroom +inf; clamp_book is a no-op)
# --------------------------------------------------------------------------- #
def test_headroom_absent_peers_no_clamp(iso):
    # nothing published by any peer → headroom is +inf and clamp_book leaves the book byte-identical.
    import math
    assert math.isinf(firm_exposure.headroom("semis_ai", "flagship"))
    book = [{"ticker": "SMH", "sleeve": "leadership", "weight": 0.40}]   # would breach if a firm cap applied
    res = firm_exposure.clamp_book(book, "flagship")
    assert res["bound"] is False
    assert res["clamped"] == []
    assert res["positions"][0]["weight"] == 0.40   # untouched


# --------------------------------------------------------------------------- #
# (c) corrupt peer file -> no clamp from THAT peer + never raises (the firm view still builds)
# --------------------------------------------------------------------------- #
def test_headroom_corrupt_peer_degrades(iso):
    # autonomous publishes a corrupt file (present but unloadable), etf publishes a real 0.10 semis line.
    d = registry.data_dir("autonomous")
    d.mkdir(parents=True, exist_ok=True)
    (d / "latest.json").write_text("{ not json")
    _seed("etf", [{"ticker": "SMH", "weight": 0.10}])
    # the corrupt peer contributes nothing; the readable peer's 0.10 is honoured → room = 0.30-0.10.
    room = firm_exposure.headroom("semis_ai", "flagship")
    assert room == pytest.approx(0.20, abs=1e-9)
    # and the clamp never raises on the corrupt file
    res = firm_exposure.clamp_book([{"ticker": "NVDA", "weight": 0.25}], "flagship")
    assert res["bound"] is True     # 0.25 > 0.20 room → clamped


def test_headroom_only_corrupt_peers_is_no_clamp(iso):
    # if the ONLY peer file present is corrupt, there is STILL firm data (a book published), so the firm
    # view is real (peer cluster weight 0.0) → headroom is the full cap, NOT +inf.
    d = registry.data_dir("etf")
    d.mkdir(parents=True, exist_ok=True)
    (d / "latest.json").write_text("garbage {[")
    room = firm_exposure.headroom("semis_ai", "flagship")
    assert room == pytest.approx(0.30, abs=1e-9)   # full cap, no peer weight; NOT +inf


# --------------------------------------------------------------------------- #
# (d) the clamp NEVER increases any weight (the core invariant)
# --------------------------------------------------------------------------- #
def test_clamp_never_increases_weight(iso):
    # a heavy peer (0.29 semis) means small firm headroom; every book weight must only shrink or hold.
    _seed("autonomous", [{"ticker": "SMH", "weight": 0.29}])
    book = [{"ticker": "NVDA", "weight": 0.05}, {"ticker": "MU", "weight": 0.01},
            {"ticker": "ZZZFAKE", "weight": 0.30}]   # ZZZFAKE singleton, well over the 0.10 name cap
    before = {p["ticker"]: p["weight"] for p in book}
    res = firm_exposure.clamp_book(book, "flagship")
    for p in res["positions"]:
        assert p["weight"] <= before[p["ticker"]] + 1e-12    # never raised
    # ZZZFAKE (singleton) is bound by the firm NAME cap 0.10, independent of the semis cluster.
    zz = next(p for p in res["positions"] if p["ticker"] == "ZZZFAKE")
    assert zz["weight"] == pytest.approx(0.10, abs=1e-4)


# --------------------------------------------------------------------------- #
# (e) name-level 0.10 firm cap binds INDEPENDENTLY of the cluster cap
# --------------------------------------------------------------------------- #
def test_name_cap_binds_independently_of_cluster(iso):
    # a single peer holds 0.07 of NVDA specifically; the cluster has lots of room, but the NAME cap
    # (0.10) leaves only 0.03 of NVDA for this book — bind on the name even though the cluster is fine.
    _seed("etf", [{"ticker": "NVDA", "weight": 0.07}])
    # NVDA name headroom = 0.10 - 0.07 = 0.03; cluster (semis_ai) headroom = 0.30 - 0.07 = 0.23.
    room = firm_exposure.headroom("NVDA", "flagship")     # raw ticker → min(name, cluster) = 0.03
    assert room == pytest.approx(0.03, abs=1e-9)
    res = firm_exposure.clamp_book([{"ticker": "NVDA", "weight": 0.09}], "flagship")
    assert res["bound"] is True
    assert res["positions"][0]["weight"] == pytest.approx(0.03, abs=1e-4)
    # a clamp record must be tagged 'name' (the binding kind here)
    assert any(c["kind"] == "name" and c["key"] == "NVDA" for c in res["clamped"])


# --------------------------------------------------------------------------- #
# (f) sequential fairness: build order matters — Flagship claims headroom FIRST by fixture
# --------------------------------------------------------------------------- #
def test_sequential_fairness_flagship_first(iso):
    # Simulate the 22:40 order. At Flagship's build time, NO US peer has published yet (fresh day) →
    # Flagship sees +inf headroom and is NOT clamped: it claims the cluster first, by design.
    import math
    assert math.isinf(firm_exposure.headroom("semis_ai", "flagship"))
    flagship_book = [{"ticker": "SMH", "weight": 0.28}]
    res_flag = firm_exposure.clamp_book(flagship_book, "flagship")
    assert res_flag["bound"] is False    # Flagship-first: unclamped

    # Now Flagship has published 0.28 semis. A LATER book (heavyweight) building after it sees that as a
    # peer and is clamped to the remaining 0.02 — the later book yields, not Flagship.
    _seed("flagship", [{"ticker": "SMH", "weight": 0.28}])
    room_hw = firm_exposure.headroom("semis_ai", "heavyweight")
    assert room_hw == pytest.approx(0.02, abs=1e-9)
    res_hw = firm_exposure.clamp_book([{"ticker": "NVDA", "weight": 0.20}], "heavyweight")
    assert res_hw["bound"] is True
    assert res_hw["positions"][0]["weight"] == pytest.approx(0.02, abs=1e-4)


# --------------------------------------------------------------------------- #
# self-exclusion + non-US exclusion + flag gate + mapping shape
# --------------------------------------------------------------------------- #
def test_own_book_not_counted(iso):
    # flagship's OWN published exposure must not count against its own headroom (only peers).
    _seed("flagship", [{"ticker": "SMH", "weight": 0.29}])
    import math
    # no OTHER US book published → +inf (flagship's own 0.29 is excluded).
    assert math.isinf(firm_exposure.headroom("semis_ai", "flagship"))


def test_non_us_books_excluded(iso):
    # china/hk publish semis-cluster-looking tickers; they are non-US (disjoint venue) and must NOT
    # enter the firm aggregation → flagship still sees +inf (no US peer).
    _seed("china", [{"ticker": "SMH", "weight": 0.30}], currency="CNY")
    _seed("hk",    [{"ticker": "NVDA", "weight": 0.30}], currency="HKD")
    import math
    assert math.isinf(firm_exposure.headroom("semis_ai", "flagship"))


def test_flag_gate_default_on_and_optout(iso, monkeypatch):
    assert firm_exposure.caps_enabled() is True            # default ON
    monkeypatch.setenv("MASTERMIND_FIRM_CAPS", "0")
    assert firm_exposure.caps_enabled() is False           # opt-out
    monkeypatch.setenv("MASTERMIND_FIRM_CAPS", "1")
    assert firm_exposure.caps_enabled() is True


def test_clamp_book_mapping_shape(iso):
    # clamp_book accepts (and returns) a {ticker: weight} mapping, not just a list of rows.
    _seed("autonomous", [{"ticker": "SMH", "weight": 0.28}])
    out = firm_exposure.clamp_book({"NVDA": 0.20}, "flagship")
    assert isinstance(out["positions"], dict)
    assert out["positions"]["NVDA"] == pytest.approx(0.02, abs=1e-4)


def test_clamp_duplicate_ticker_rows_distributed_prorata(iso):
    # a name held in BOTH sleeves (two rows) must have the clamped TOTAL distributed pro-rata across the
    # rows — never write the full clamped total to each row (which would double the exposure).
    _seed("autonomous", [{"ticker": "NVDA", "weight": 0.07}])   # firm NVDA name room = 0.10-0.07 = 0.03
    book = [{"ticker": "NVDA", "sleeve": "leadership", "weight": 0.06},
            {"ticker": "NVDA", "sleeve": "conviction", "weight": 0.02}]   # own NVDA total 0.08 → clamp to 0.03
    res = firm_exposure.clamp_book(book, "flagship")
    assert res["bound"] is True
    total = sum(p["weight"] for p in res["positions"])
    assert total == pytest.approx(0.03, abs=1e-4)               # the SUM matches the name headroom
    lead = next(p for p in res["positions"] if p["sleeve"] == "leadership")
    conv = next(p for p in res["positions"] if p["sleeve"] == "conviction")
    assert lead["weight"] == pytest.approx(0.03 * 0.06 / 0.08, abs=1e-4)   # pro-rata by original
    assert conv["weight"] == pytest.approx(0.03 * 0.02 / 0.08, abs=1e-4)


def test_firm_caps_env_override(iso, monkeypatch):
    _seed("autonomous", [{"ticker": "SMH", "weight": 0.20}])
    # default firm cluster cap 0.30 → room 0.10
    assert firm_exposure.headroom("semis_ai", "flagship") == pytest.approx(0.10, abs=1e-9)
    # raise the firm cluster cap to 0.50 → room 0.30
    monkeypatch.setenv("MASTERMIND_FIRM_CLUSTER_CAP", "0.50")
    assert firm_exposure.headroom("semis_ai", "flagship") == pytest.approx(0.30, abs=1e-9)

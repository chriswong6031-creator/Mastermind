"""Conviction sleeve — a name takes paper size only when ALL sides confirm.

Closes the loop: candidate names (Claude's open proposals + the leadership universe) are
each run through the multi-sided decision matrix; a name is sized ONLY if its synthesis
says size_authority == 'up' AND it trips no hard veto (parabolic / Altman distress /
cycle-blocked). Size is confluence-weighted, subtract-only, capped per name. Everything
else is shown but held at 0 — discipline over enthusiasm.
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import bot  # noqa: F401

from portfolio import lenses

log = logging.getLogger(__name__)


def _floor4(x: float) -> float:
    """Truncate toward zero at 4 decimals — used where a cap must never be exceeded by a
    round-to-nearest artifact (the freed sub-cent falls to cash, subtract-only)."""
    return math.floor(max(0.0, x) * 10000) / 10000.0

# W2.3 — the hardcoded 20-name AI/MAG7 `_SHORTLIST` is DEAD. It was a frozen, human-curated bet on
# one cohort (the exact crowding failure the doctrine exists to prevent) that could not respond to
# the cycle. It is replaced by `regime_seed()` below: a DERIVED leadership seed (bottleneck-chain
# order-layer baskets + top-liquidity basket leaders), FILTERED to sectors whose cycle phase is
# entry-favored (Trough/Recovery/Expansion — the only walk-forward-defensible cycle use). No ticker
# literal remains here.

# the fed-in candidate universe: top names from the us_stocks standout board + the top
# stock picks across the thematic baskets. The engine gate (build) filters this down — a
# broad feed in, discipline at the gate.
TOP_US = 100        # top-N from us_stocks.html's standout BUY board (ranked by alpha)
TOP_BASKET = 100    # top-N single-name picks across all thematic baskets (by 20d return)

_V = Path(__file__).resolve().parent.parent / "vendor" / "macro"


class _SizedBook(list):
    """A plain list of sized-position dicts that ALSO carries a `data_health` attribute.

    build() historically returns (sized_list, rejected_list); every caller unpacks that tuple and
    iterates `sized` as a list. To surface the build-wide data-health / fail-closed record WITHOUT
    breaking that contract (add fields, never rename — house rule), `sized` is this list subclass:
    `isinstance(sized, list)` and all list behaviour is unchanged, and `sized.data_health` exposes
    the coverage record for the runlog."""
    data_health: dict | None = None


def _load(rel: str):
    p = _V / rel
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except Exception:
        return None


def _respect_standout_gate() -> bool:
    """Doctrine toggle (P-NEW-2): honour the standout board's own `gate_go` verdict. Default TRUE.
    A missing/unreadable doctrine key degrades to today's behaviour (respect the gate) — the gate
    only ever SKIPS the source, never adds names, so defaulting to respect is invariant-safe."""
    try:
        from bot.doctrine_config import load_doctrine
        v = load_doctrine().get("us_standouts_respect_gate_go")
        return True if v is None else bool(v)
    except Exception:  # noqa: BLE001
        return True


def _us_standouts(n: int = TOP_US) -> list[str]:
    """Top-N tickers from the us_stocks standout BUY board (already rank-ordered by alpha).

    RESPECTS THE BOARD'S OWN GATE (P-NEW-2): the dashboard publishes `gate_go` — its Phase-0 verdict
    on whether the board is a statistically-validated buy list. When gate_go is *explicitly* False
    (present and falsy) the board is a confluence read, NOT standalone alpha, so we DROP its names
    from the conviction universe — they can still arrive via basket picks / intake / open theses on
    their own merits, but the un-validated board no longer directly seeds buys. Invariant-safe:
      * gate_go missing (None) → today's behaviour, ingest (legacy artifacts, degrade-never-raise);
      * gate_go truthy         → today's behaviour, ingest;
      * gate_go explicitly False (and the doctrine toggle is on) → SKIP (never adds, only removes)."""
    d = _load("site/factordata/us_standouts.json") or {}
    buy = d.get("buy") or d.get("standouts") or []
    gate_go = d.get("gate_go")
    if gate_go is False and _respect_standout_gate():
        log.warning("us_standouts gate_go=False (board not statistically validated) — skipping "
                    "%d standout buy names from conviction universe", len(buy))
        return []
    return [r.get("ticker") for r in buy[:n] if isinstance(r, dict) and r.get("ticker")]


# sector-concentration firebreak — PERCENTAGE based (PM directive 2026-06-22, replacing the prior
# count-based SECTOR_MAX_NAMES which had been disabled). No single sector may hold more than
# SECTOR_MAX_FRACTION of the conviction-sleeve budget; an over-weight sector is scaled DOWN
# proportionally (subtract-only — names are down-sized, never churned out) and the freed weight is
# left in cash. This is the crowding / cohort de-gross control: a book that piles a whole homogeneous
# cohort (e.g. every AI-semis leader) into one sector is fragile even when each name scores well in
# isolation. The per-name + book/theme weight caps still bound individual position size on top of it.
SECTOR_MAX_FRACTION = 0.50

# MANUAL HOLD-OUT (operational, 2026-06-22) — names deliberately reversed out of the book by PM
# directive after the AVGO/NVDA forced-override post-mortem (see docs/case_studies). This is NOT a
# scoring penalty or a permanent ban: it is a do-not-AUTO-re-add guard so the daily rebalance does
# not silently re-buy a name the desk just deliberately exited. Remove a ticker here to let the
# engine consider it again on its own merits.
_MANUAL_EXCLUDE = {"NVDA", "AVGO"}

# EXIT HYSTERESIS — to ENTER, a new name must clear the full 'up' gate (confluence > 0.30). To be
# DROPPED, a name we ALREADY hold has to fall below this LOWER floor (or trip a hard exit). The
# asymmetric entry/exit bars stop a name being churned in and out across builds when it wobbles
# around the 0.30 entry line (the NVDA bought-then-immediately-closed problem). RESTORED toward
# entry parity (0.15 -> 0.25, 2026-06-22): the prior 0.15 floor was loosened under the AVGO/NVDA
# override and let a deteriorating held name ride too long; a tight 0.05 band still prevents churn.
_EXIT_CONFLUENCE_FLOOR = 0.25

# CATALYST/CONFIRMATION gates FULL size (doctrine §4.3 "catalyst gates full size" + "own leaders
# without chasing"). A name that clears the gate but lacks price+leadership confirmation (or a
# leading theme) takes only INITIAL size — this fraction of its confluence-weighted target.
_INITIAL_SIZE_FRACTION = 0.7

# ── NEURAL-WEB WHOLE-UNIVERSE CANDIDACY SCAN (P2, flag-gated) ──────────────────────────────────────
# The operator's "review the whole universe via Neural Web" ask: at the candidacy layer of the NW
# decision ladder (MASTERMIND_NW_DECISION >= "candidacy") the whole NW candidate_context is swept and
# every fdr-cleared name with a qualifying bottom_state is fed into candidates() as an ADDITIVE
# candidacy prior — the gate (build) still filters it exactly like any other source. This never sizes
# a name and never touches a held name. STRICTLY ADDITIVE + FAIL-SOFT + DEFAULT-OFF: below candidacy
# mode the scan returns [] and candidates() is byte-identical to today.
#
# NW_UNIVERSE_SCAN_CAP is an UNVERIFIED PRIOR — a defensive bound on how many NW-originated candidacy
# names may enter one build so the gate isn't flooded by a wide/degraded NW artifact. The exact value
# (25) is a starter guess, not a validated number; leader-anticipation is coin-flip (China-basket
# program), so this is a firebreak, not a target.
NW_UNIVERSE_SCAN_CAP = 25


# ── W2.2 GRADED EXTENSION SCHEDULE (entry brake) ──────────────────────────────────────────────────
def _ext_schedule() -> tuple[float, float]:
    """(moderate, no_add) pct-vs-200dma thresholds from doctrine.yml, with safe fallbacks."""
    try:
        from bot.doctrine_config import load_doctrine
        cfg = load_doctrine().get("extension_schedule") or {}
        return (float(cfg.get("moderate_pct_vs_200dma", 30.0)),
                float(cfg.get("no_add_pct_vs_200dma", 45.0)))
    except Exception:  # noqa: BLE001
        return (30.0, 45.0)


def _ext_mult(rows: list[dict], is_held: bool) -> float:
    """The graded extension multiplier for a NEW conviction add, from the extension lens row.

    Schedule (masterplan W2.2), off pct_vs_200dma:
        < moderate (30%)  → 1.0   (no brake)
        >= moderate (30%) → _INITIAL_SIZE_FRACTION   (initial-size only)
        >= no_add  (45%)  → 0.0   (no NEW add)
    The PARABOLIC hard veto is UNCHANGED — it fires upstream (lenses._hard_vetoes → size_authority
    'blocked' → the name never reaches sizing) so it is not re-implemented here.

    HELD names are EXEMPT (return 1.0): an extension read is an ENTRY-timing brake, not an exit
    signal — a held/leading name is never trimmed on how far it has run (masterplan §0). MISSING
    extension data (no row / pct_vs_200dma is None) → 1.0: the W0 fail-closed gate already blocks
    truly-degraded names, so we must not double-punish a name with merely partial (no-extension) data.
    """
    if is_held:
        return 1.0
    ext = next((r for r in rows if r.get("lens") == "extension"), None)
    val = (ext or {}).get("value") or {}
    pv2 = val.get("pct_vs_200dma")
    if not isinstance(pv2, (int, float)):        # missing/partial extension read → no brake
        return 1.0
    moderate, no_add = _ext_schedule()
    if pv2 >= no_add:
        return 0.0
    if pv2 >= moderate:
        return _INITIAL_SIZE_FRACTION
    return 1.0


def _sector_of(t: str) -> str:
    """Normalised sector key for the concentration cap — collapses synonym labels
    ('Technology' / 'Information Technology' -> XLK) via the sector→ETF map so a cohort can't
    dodge the cap by sitting under two spellings of the same sector."""
    d = _load(f"site/stockdata/{t}.json")
    sec = (d or {}).get("sector") or "Unknown"
    return lenses._SECTOR_ETF.get(sec, sec)


def _basket_top_picks(n: int = TOP_BASKET) -> list[str]:
    """Top-N single-name picks across all thematic baskets, ranked by 20-day return.

    Union every basket's members, keep each name's best 20d return, take the top N. The
    extension veto at the gate handles parabolic momentum names, so a momentum-ranked feed
    is safe here."""
    d = _load("site/basketdata/baskets.json") or {}
    best: dict[str, float] = {}
    for b in (d.get("baskets") or []):
        for m in (b.get("members") or []):
            sym = (m.get("symbol") or m.get("ticker") or "").upper()
            if not sym:
                continue
            r = m.get("ret_20d")
            rr = float(r) if isinstance(r, (int, float)) else -1e9
            if sym not in best or rr > best[sym]:
                best[sym] = rr
    ranked = sorted(best.items(), key=lambda kv: kv[1], reverse=True)
    return [sym for sym, _ in ranked[:n]]


# ── W2.3 REGIME SEED (replaces the dead _SHORTLIST) ───────────────────────────────────────────────
# The leadership seed is DERIVED, not curated: it is the doctrine bottleneck-chain order-layer baskets
# (the AI-buildout migration chain — first/second/third order) PLUS the top-liquidity basket leaders,
# each restricted to sectors whose SECTOR-CYCLE phase is entry-favored (Trough/Recovery/Expansion —
# the only walk-forward-defensible cycle use per masterplan §0). This is an ENTRY tilt on NEW
# candidates ONLY; it never touches a held name (a held name in a now-Peak sector is untouched here
# and everywhere else — the refuted cycle veto is NOT reintroduced).

def _seed_cfg() -> dict:
    """regime_seed knobs from doctrine.yml, with safe fallbacks (loader failure never breaks the seed)."""
    try:
        from bot.doctrine_config import load_doctrine
        return dict(load_doctrine().get("regime_seed") or {})
    except Exception:  # noqa: BLE001
        return {}


def _bottleneck_chain_baskets() -> list[str]:
    """The order-layer basket ids from doctrine bottleneck.chains (first→second→third order), in
    chain order. These are the migration-chain baskets the doctrine says lead the AI buildout — a
    config-driven seed source, not a ticker literal. Empty on any config failure (degrade-safe)."""
    try:
        from bot.doctrine_config import load_doctrine
        chains = (load_doctrine().get("bottleneck") or {}).get("chains") or {}
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for chain in chains.values():
        if not isinstance(chain, dict):
            continue
        for layer in ("first_order", "second_order", "third_order"):
            for bid in (chain.get(layer) or []):
                if isinstance(bid, str) and bid not in out:
                    out.append(bid)
    return out


# Coarse basket→sector-ETF map for the cycle filter. `regime_frame.cycles()` is keyed by the 11 GICS
# sector ETF tickers (XLK, XLV, …); baskets carry a `reference.label` that is EITHER a sector ETF
# (used directly) or a thematic/complex ETF (SMH/IGV/QQQ/…) that we fold into its parent sector so the
# cycle phase still applies. Anything not in this map AND not a sector ticker → UNMAPPED (allowed,
# never blocked — the invariant: missing mapping only ever removes a shrink/filter, never imposes one).
_BASKET_ETF_TO_SECTOR = {
    "SMH": "XLK", "IGV": "XLK", "QQQ": "XLK",   # semis / software / nasdaq-100 → Technology
    "XHB": "XLY",                                 # homebuilders → Consumer Discretionary
    "KRE": "XLF",                                 # regional banks → Financials
    # IBIT (crypto) intentionally UNMAPPED → allowed (no sector cycle applies)
}


def _basket_sector_ticker(basket: dict) -> str | None:
    """Best-effort sector-ETF ticker for a basket, for the cycle-phase filter. Reads `reference.label`
    (may be a sector ETF, a thematic ETF, or a composite like 'XLP+XLU' — first token wins). Returns
    None when it cannot map — the caller treats None as UNMAPPED = allowed (never blocked)."""
    ref = (basket.get("reference") or {})
    label = (ref.get("label") or "").strip().upper()
    if not label:
        return None
    first = label.split("+")[0].strip()          # 'XLP+XLU' → 'XLP'
    if first in _BASKET_ETF_TO_SECTOR:
        return _BASKET_ETF_TO_SECTOR[first]
    return first or None                          # a bare sector ticker (XLV/XLK/…) maps to itself


def _basket_leaders(basket: dict, top_n: int, min_last: float) -> list[str]:
    """Top-`top_n` member symbols of a basket by 20d return, above the (usually inert) liquidity
    floor. baskets.json carries no turnover/mcap, so the floor degrades to a `last`-price proxy
    (min_last, default 0 → no-op); the seed's liquidity intent is preserved by sourcing ONLY from
    curated baskets (inherently liquid, data-covered names — the original _SHORTLIST intent)."""
    scored: list[tuple[float, str]] = []
    for m in (basket.get("members") or []):
        sym = (m.get("symbol") or m.get("ticker") or "").upper()
        if not sym:
            continue
        last = m.get("last")
        if min_last and isinstance(last, (int, float)) and last < min_last:
            continue                              # liquidity floor (inert unless configured > 0)
        r = m.get("ret_20d")
        rr = float(r) if isinstance(r, (int, float)) else -1e9
        scored.append((rr, sym))
    scored.sort(key=lambda kv: kv[0], reverse=True)
    return [sym for _, sym in scored[:max(0, int(top_n))]]


def regime_seed() -> list[str]:
    """The DERIVED leadership seed that replaces the dead `_SHORTLIST`.

    Sources (in priority order): the doctrine bottleneck-chain order-layer baskets, then the remaining
    baskets ranked by 20d relative performance (the top-liquidity basket leaders). Each basket's top
    members are taken, but ONLY from baskets whose mapped sector is cycle-entry-favored
    (Trough/Recovery/Expansion). The seed is capped at `regime_seed.max_names`.

    INVARIANT-SAFE degradations (never block on missing data):
      * an UNMAPPED / unknown-sector basket is ALLOWED (a missing mapping can only remove a filter);
      * a STALE sector_cycles file → `cycles()` returns {} → the filter is a no-op → the seed degrades
        to the UNFILTERED basket leaders (today's-behaviour-or-better, never empty on staleness);
      * any I/O / config failure → the seed still emits the unfiltered basket leaders it could read.
    This is an ENTRY seed only — it feeds `candidates()`, which the gate then filters. It never
    touches held names; a held name in a now-Peak sector is unaffected (the refuted veto is NOT back).
    """
    cfg = _seed_cfg()
    max_names = int(cfg.get("max_names", 20) or 20)
    top_n = int(cfg.get("leader_top_n_per_basket", 3) or 3)
    min_last = float(cfg.get("liquidity_min_last", 0.0) or 0.0)

    d = _load("site/basketdata/baskets.json") or {}
    baskets = d.get("baskets") or []
    by_id = {b.get("id"): b for b in baskets if isinstance(b, dict) and b.get("id")}

    # cycle read (may be {} when stale/absent → filter becomes a no-op).
    try:
        from brain import regime_frame
        cyc = regime_frame.cycles() or {}
    except Exception:  # noqa: BLE001 — a cycle-read failure degrades to the unfiltered seed
        cyc = {}

    def _entry_ok(basket: dict) -> bool:
        """A basket passes the cycle filter iff its mapped sector is entry-favored — OR it is
        unmapped / the sector has no cycle row / cycles is empty (all → allowed, never blocked)."""
        if not cyc:                               # stale/absent cycles → no filter (unfiltered seed)
            return True
        sec = _basket_sector_ticker(basket)
        if sec is None or sec not in cyc:         # unmapped / uncovered sector → allowed
            return True
        return bool(cyc[sec].get("entry_favored"))

    # ordering: bottleneck-chain baskets first (the doctrine's declared leaders), then every other
    # basket ranked by 20d relative performance (top-liquidity leaders). De-dup by basket id.
    ordered_ids: list[str] = [bid for bid in _bottleneck_chain_baskets() if bid in by_id]
    rest = [b for b in baskets if isinstance(b, dict) and b.get("id") not in set(ordered_ids)]

    def _rel20(b: dict) -> float:
        rel = (((b.get("perf") or {}).get("20d") or {}).get("rel"))
        return float(rel) if isinstance(rel, (int, float)) else -1e9
    rest.sort(key=_rel20, reverse=True)
    ordered_ids += [b.get("id") for b in rest if b.get("id")]

    seed: list[str] = []
    seen: set[str] = set()
    for bid in ordered_ids:
        basket = by_id.get(bid)
        if not basket or not _entry_ok(basket):
            continue
        for sym in _basket_leaders(basket, top_n, min_last):
            if sym not in seen:
                seen.add(sym)
                seed.append(sym)
                if len(seed) >= max_names:
                    return seed
    return seed


def universe() -> list[str]:
    """The fed-in candidate universe: top us_stocks standouts ∪ top thematic-basket picks."""
    return sorted(set(_us_standouts()) | set(_basket_top_picks()))


def nw_universe_scan() -> list[str]:
    """The Neural-Web WHOLE-UNIVERSE candidacy scan (P2 funnel, flag-gated + fail-soft).

    Below candidacy mode (nw_decision_mode() < "candidacy", the DEFAULT) this returns [] and has ZERO
    effect — candidates() is byte-identical to today. At/above candidacy mode it sweeps the entire NW
    candidate_context and returns every TICKER whose decision_signals()["candidacy"] is non-None (i.e.
    fdr-cleared AND a qualifying bottom_state), capped at NW_UNIVERSE_SCAN_CAP and with the operational
    _MANUAL_EXCLUDE hold-out honoured. Names are ADDITIVE candidacy priors for the gate to filter; the
    scan never sizes and never touches a held name.

    FAIL-SOFT: any absent/stale/malformed NW artifact, or an unexpected error anywhere, degrades to []
    (the additive source simply contributes nothing — never raises into a build). Lazy import so the
    NW leaf is never loaded when the flag is off.
    """
    try:
        from brain import neural_web_context as nwc
        if not nwc._mode_ge(nwc.nw_decision_mode(), "candidacy"):
            return []
        cand_ctx = (nwc.context() or {}).get("candidate_context") or {}
        if not isinstance(cand_ctx, dict):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for tkr in cand_ctx:
            if not isinstance(tkr, str):
                continue
            sym = tkr.upper()
            if sym in seen or sym in _MANUAL_EXCLUDE:
                continue
            try:
                sig = nwc.decision_signals(sym)
            except Exception:  # noqa: BLE001 — one bad row never sinks the scan
                continue
            if isinstance(sig, dict) and sig.get("candidacy") is not None:
                seen.add(sym)
                out.append(sym)
                if len(out) >= NW_UNIVERSE_SCAN_CAP:
                    break
        return out
    except Exception:  # noqa: BLE001 — additive + fail-soft: never raise into candidate assembly
        return []


def candidates() -> list[str]:
    """Conviction candidate pool: the fed-in universe (top us_stocks + top basket picks)
    ∪ open ledger theses (Claude's proposals) ∪ the DERIVED regime seed (W2.3 — bottleneck-chain +
    cycle-favored basket leaders, replacing the dead hardcoded _SHORTLIST) ∪ the unified intake queue
    (radar / alt-data / briefing-corroborated + divergent names the buy board alone misses) ∪ the
    Neural-Web whole-universe candidacy scan (P2 — additive + flag-gated, [] unless NW decision mode
    >= candidacy). The engine gate (build) filters this down — broad feed in, discipline at the gate."""
    try:
        from brain import ledger
        proposed = {t["subject"].upper() for t in ledger.all_theses() if t.get("status") == "open"}
    except Exception:
        proposed = set()
    try:
        from brain import intake
        # only reasonably-corroborated names (score floor) so the gate isn't drowned in noise
        fed_in = set(intake.tickers(limit=60, min_score=0.4))
    except Exception:
        fed_in = set()
    try:
        seed = set(regime_seed())
    except Exception:  # noqa: BLE001 — seed is additive; a failure degrades to the other sources
        seed = set()
    # P2: whole-universe NW candidacy passthrough. Byte-identical when off (nw_universe_scan() == [] →
    # `set() | ...` leaves the union unchanged), deduped by ticker (set union), and still subject to
    # the _MANUAL_EXCLUDE hold-out below like every other source.
    nw_scan = set(nw_universe_scan())
    # W8 §2.3: the US PROPHET feed — entry-endorsed trade plans (entry/trigger/invalidation
    # geometry, tier-gated upstream) as an ADDITIVE candidate source. Inert ([]) when the flag is
    # off / the artifact is absent or stale; the gate still decides like for every other source.
    try:
        from portfolio import prophet_feed
        prophet = set(prophet_feed.candidate_tickers())
    except Exception:  # noqa: BLE001 — additive source; a feed failure contributes nothing
        prophet = set()
    return sorted((seed | set(universe()) | proposed | fed_in | nw_scan | prophet)
                  - _MANUAL_EXCLUDE)


def _entry_gate_enabled() -> bool:
    """W8 master flag: the binding ENTRY + CONTEXT gates on NEW conviction/leadership entries
    (research/FLAGSHIP_V2_DECISION_CORE.md §2.5-2.6). DEFAULT ON per the 2026-07-19 operator
    order; opt out with MASTERMIND_ENTRY_GATE in {0, false, no, off}. Subtract-only: OFF restores
    the pre-W8 buy path exactly (assessments not even computed)."""
    import os
    return os.environ.get("MASTERMIND_ENTRY_GATE", "1").strip().lower() not in (
        "0", "false", "no", "off", "")


def build(budget: float, name_cap: float = 0.08,
          held: set | None = None, asof: str | None = None,
          extra_candidates: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Return (sized_positions, rejected) where rejected contains every evaluated name
    that did NOT make the size gate, with the veto/bear detail that kept it out.

    `held` = tickers already open in the conviction book; they get priority in the sector cap
    (hysteresis) so a name isn't churned in/out across builds when a marginally-higher new name
    appears. `asof` (additive, optional) stamps the W8 entry/context reports; `extra_candidates`
    (additive, optional) carries watchlist promotions back into the pool. Sizing behaviour is
    otherwise unchanged.
    """
    held = {h.upper() for h in (held or set())}
    _w8 = _entry_gate_enabled()
    passed = []
    rejected: list[dict] = []
    n_evaluated = 0
    n_degraded = 0

    # W8 §2.8: watchlist promotions union in HERE (not inside candidates(), whose zero-arg
    # signature is a monkeypatch surface for a dozen tests). Additive; the gate still decides.
    _pool = list(candidates())
    _promoted = {str(x).upper().strip() for x in (extra_candidates or []) if x}
    _pool = sorted(set(_pool) | (_promoted - _MANUAL_EXCLUDE))
    for t in _pool:
        try:
            full = lenses.full(t, "name")
            syn = full["synthesis"]
            rows: list[dict] = full.get("rows", [])
        except Exception:
            continue

        vetoes: list[str] = syn.get("vetoes", [])
        confluence: float = syn.get("confluence", 0.0)
        sa = syn.get("size_authority")
        is_held = t.upper() in held
        # DATA-DEGRADED (fail-closed): the per-name stockdata was absent OR < 2 lenses voted, so the
        # synthesis flagged size_authority='insufficient_data'. Track coverage across the whole build
        # for the >80%-degraded circuit breaker (f). (Also read the explicit flag so a future
        # authority value can't silently bypass this.)
        degraded = (sa == "insufficient_data") or bool(syn.get("data_degraded"))
        n_evaluated += 1
        if degraded:
            n_degraded += 1

        # HARD exits — a held name is dropped IMMEDIATELY on any of these (no hysteresis for a
        # genuinely broken name): a hard veto (parabolic / Altman / cycle-blocked), a CONFIRMED
        # structural downtrend, or size_authority blocked. A fresh falling-knife or a softened
        # sector is NOT a hard exit, so a name we already own rides through a rough week.
        # CRITICAL FREEZE SEMANTICS: a data outage is NOT a hard exit. Missing data must NEVER
        # liquidate the book (the inverse disaster of the fail-open bug). When degraded we suppress
        # price_downtrend as an exit trigger — there is no real price read to trust — so a held name
        # FREEZES (hold, don't churn) rather than being dropped on a phantom/stale signal.
        hard_exit = (bool(vetoes)
                     or (bool(syn.get("price_downtrend")) and not degraded)
                     or (sa == "blocked"))
        entry_ok = (sa == "up") and not vetoes and not degraded    # sa=='up' already implies not-degraded; belt-and-suspenders
        # FREEZE-ON-DEGRADE: a HELD name with degraded data is RETAINED as a hold regardless of the
        # confluence floor (a degraded confluence is untrustworthy — possibly the 1.0 mirage — so it
        # can neither justify nor deny the hold). Only a genuine hard exit (real veto) removes it.
        held_frozen = is_held and degraded and not hard_exit
        hold_ok = held_frozen or (is_held and not hard_exit and confluence > _EXIT_CONFLUENCE_FLOOR)

        # ── W8 ENTRY + CONTEXT gates (NEW entries only; held names NEVER touched here) ──────────
        # The buy triad's second and third axes (design §2.2): a quality-gate passer must ALSO be
        # at a buyable ENTRY (not chase/late_leg/rollover/extended/knife/plan-exhausted) in
        # weather that is not BLOCKED for its cohort. Failing either does not discard the name —
        # it goes to `rejected` carrying a `park` record so phase2 enrolls it on the watchlist
        # with promotion triggers (patience, not forfeit). Fail-open: verdict 'unknown' and any
        # assessor error withhold nothing (charter P2 — the data-health breaker owns outages).
        entry_rep = ctx_rep = None
        ctx_mult = 1.0
        if _w8 and entry_ok and not is_held:
            try:
                from portfolio import context_gate as _ctxg
                from portfolio import entry_engine as _eeng
                entry_rep = _eeng.assess(t, as_of=asof)
                ctx_rep = _ctxg.assess(
                    t, entry_verdict=entry_rep.get("verdict"),
                    entry_tier_ok=bool(entry_rep.get("metrics", {}).get("tier_fresh")
                                       or entry_rep.get("metrics", {}).get("tier_eligible")),
                    as_of=asof)
                ctx_mult = float(ctx_rep.get("entry_mult", 1.0) or 1.0)
            except Exception:  # noqa: BLE001 — assessors are subtract-only; failure = no brake
                entry_rep = ctx_rep = None
                ctx_mult = 1.0
            _entry_bad = bool(entry_rep) and not entry_rep.get("buyable") \
                and entry_rep.get("verdict") != "unknown"
            _ctx_blocked = bool(ctx_rep) and ctx_rep.get("verdict") == "blocked"
            if _entry_bad or _ctx_blocked:
                _why = []
                if _entry_bad:
                    _why.append(f"entry {entry_rep.get('verdict')}")
                if _ctx_blocked:
                    _why.append("context blocked")
                _notes = (entry_rep or {}).get("notes", []) + (ctx_rep or {}).get("reasons", [])
                rejected.append({
                    "ticker": t,
                    "reason": "Parked — " + " + ".join(_why)
                              + (f" ({_notes[0]})" if _notes else ""),
                    "vetoes": [],
                    "bear": _notes[:4],
                    "confluence": round(confluence, 3),
                    "park": {
                        "asof": asof,
                        "entry_verdict": (entry_rep or {}).get("verdict"),
                        "context_verdict": (ctx_rep or {}).get("verdict"),
                        "triggers": ((entry_rep or {}).get("park_triggers")
                                     or (ctx_rep or {}).get("park_triggers")),
                    },
                    "entry_report": entry_rep, "context_report": ctx_rep,
                })
                continue

        if entry_ok or hold_ok:
            # full-size confirmation: a confirmed leader (price + sector leadership) OR a genuine
            # leading theme. Everything else that clears the gate is sized at INITIAL only.
            _dirs = {r["lens"]: r.get("direction") for r in rows}
            confirmed = ((_dirs.get("trend") == "bull" and _dirs.get("sector_rs") == "bull")
                         or _dirs.get("narrative") == "bull")
            # A frozen (data-degraded) held name is confirmed=False — we have NO price/leadership read
            # to justify full size, so it can only carry its existing (initial-fraction) weight.
            if held_frozen:
                confirmed = False
            # FREEZE weight floor: a frozen held name's degraded confluence may be 0 (or the untrusted
            # 1.0 mirage) — either way it must SURVIVE the `weight > 0` filter so the freeze actually
            # HOLDS the position (a 0 weight would silently liquidate it — the very disaster we guard).
            # A tiny positive floor keeps it in the book at minimal size; the sector cap / vol sizing
            # still bound it. Non-frozen entries keep their real confluence unchanged.
            _conf = max(0.01, confluence) if held_frozen else max(0.0, confluence)
            # W2.2 GRADED EXTENSION BRAKE: a graded entry-size multiplier off pct_vs_200dma for a NEW
            # add (held names return 1.0 — an extension read is an entry brake, never an exit). It
            # COMPOSES with (multiplies, does not replace) the confirmation size mult below.
            _emult = _ext_mult(rows, is_held)
            _entry = {"ticker": t, "confluence": _conf,
                      "bull": syn["bull"], "bear": syn["bear"],
                      "retained": bool(hold_ok and not entry_ok), "confirmed": confirmed,
                      "ext_mult": _emult, "ctx_mult": ctx_mult,
                      "divergences": [d["pattern"] for d in syn.get("divergences", [])]}
            # W8: the entry/context reports ride on the position so theses/provenance/research
            # can say WHY NOW (design §2.5). Held names carry none (never assessed).
            if entry_rep is not None:
                _entry["entry_report"] = entry_rep
            if ctx_rep is not None:
                _entry["context_report"] = ctx_rep
            if held_frozen:
                _entry["retained_reason"] = "data_degraded_freeze"
                _entry["data_degraded"] = True
            passed.append(_entry)
        else:
            # Determine a short human-readable rejection reason (most-specific first).
            if vetoes:
                reason = "Vetoed: " + ", ".join(vetoes)
            elif sa == "blocked":
                reason = "Blocked (size_authority=blocked)"
            elif syn.get("price_downtrend"):
                reason = "Downtrend — price rolling over (no falling knives)"
            elif syn.get("price_falling_fast"):
                reason = "Falling knife — sharp recent multi-day decline (await stabilization)"
            elif not syn.get("leadership_ok", True):
                reason = "Lagging sector/commodity — leadership gate (fighting the tape)"
            elif syn.get("weak_asymmetry"):
                _ar = syn.get("asym_ratio")
                reason = ("Weak asymmetry — upside/downside cone "
                          + (f"{_ar:.2f}" if isinstance(_ar, (int, float)) else "?")
                          + " (not asymmetric)")
            elif confluence <= -0.3:
                reason = f"Negative confluence ({confluence:+.2f})"
            else:
                reason = f"Insufficient confluence ({confluence:+.2f}, need >0.30)"

            # ── W8 §2.5 ROTATION-CANDIDATE marking (the D7 positive path) ────────────────────────
            # A strength-rewarding gate structurally under-scores a BOTTOMING name (XLE fell to
            # confluence ~0 at its low). A name that fails ONLY the confluence bar — no veto, no
            # downtrend, non-negative confluence — but whose ENTRY read says base_turn in weather
            # that is not blocked, is marked for the watchlist ROTATION lane (phase2 enrolls it;
            # WATCH→ARMED as tier/stage confirmation builds). ADDITIVE SOURCING ONLY: the mark
            # never sizes; a promoted name still re-runs the full gate on a later build.
            _rot_probe_ok = (_w8 and not is_held and not vetoes and sa not in ("blocked",)
                             and not syn.get("price_downtrend") and not degraded
                             and confluence >= 0.0 and reason.startswith("Insufficient"))
            if _rot_probe_ok:
                try:
                    from portfolio import context_gate as _ctxg
                    from portfolio import entry_engine as _eeng
                    _probe = _eeng.assess(t, as_of=asof)
                    if _probe.get("verdict") == "base_turn":
                        _pctx = _ctxg.assess(t, entry_verdict="base_turn", as_of=asof)
                        if _pctx.get("verdict") != "blocked":
                            rejected.append({
                                "ticker": t, "reason": reason, "vetoes": [],
                                "bear": [], "confluence": round(confluence, 3),
                                "rotation_candidate": True,
                                "entry_report": _probe, "context_report": _pctx,
                            })
                            continue
                except Exception:  # noqa: BLE001 — the probe is additive; failure marks nothing
                    pass

            # Extract bear bullets from the matrix rows (cap at 4).
            bear_pts: list[str] = []
            for r in rows:
                if r.get("direction") == "bear" and len(bear_pts) < 4:
                    lens_name = r.get("lens", "")
                    note = r.get("note") or ""
                    val = r.get("value") or {}
                    if lens_name == "extension":
                        pv2 = val.get("pct_vs_200dma")
                        para = val.get("parabolic")
                        bear_pts.append(
                            f"Extension: grade={val.get('grade')}"
                            + (", parabolic=True" if para else "")
                            + (f", +{pv2:.1f}% vs 200dma" if pv2 is not None else "")
                        )
                    elif lens_name == "valuation":
                        vz = val.get("value_z")
                        bear_pts.append(
                            f"Valuation stretched"
                            + (f" (value_z={vz:.2f})" if vz is not None else "")
                        )
                    elif lens_name == "flows_13f":
                        ns = val.get("n_selling")
                        nb = val.get("n_buying")
                        asof = val.get("as_of")
                        when = f" as of {asof}" if asof else ""
                        bear_pts.append(
                            f"13F distribution tilt ({ns} trimmed vs {nb} added last quarter{when}, lagged)"
                            if ns is not None else "13F smart-money net negative (lagged quarterly snapshot)"
                        )
                    elif lens_name == "quality":
                        acct = val.get("accounting")
                        bear_pts.append(
                            f"Quality / accounting flag: {acct}"
                            if acct else "Quality lens bearish"
                        )
                    elif lens_name == "solvency":
                        az = val.get("altman_zone")
                        bear_pts.append(
                            f"Solvency: Altman zone={az}" if az else "Solvency concern"
                        )
                    elif lens_name == "macro_risk":
                        score = val.get("score")
                        bear_pts.append(
                            f"Macro risk elevated (score={score:.2f})" if score is not None
                            else "Macro risk elevated"
                        )
                    elif lens_name == "conviction":
                        band = val.get("band")
                        bear_pts.append(
                            f"Engine conviction band={band}" if band else "Engine conviction bearish"
                        )
                    elif note:
                        bear_pts.append(f"{lens_name.replace('_', ' ').title()}: {note}")
                    else:
                        bear_pts.append(f"{lens_name.replace('_', ' ').title()} lens bearish")

            rejected.append({
                "ticker": t,
                "reason": reason,
                "vetoes": vetoes,
                "bear": bear_pts,
                "confluence": round(confluence, 3),
            })

    # ── BUILD-LEVEL DATA-HEALTH CIRCUIT BREAKER (fail-closed, book-wide) ─────────────────────────
    # If the OVERWHELMING majority of evaluated candidates are data-degraded (>80%), the feed is
    # broken system-wide — not a single-name gap. On the 2026-07-01 incident this was ~100%. In that
    # state we refuse EVERY new add this build (there is no trustworthy evidence to open on) but KEEP
    # existing holds (freeze, don't churn — missing data must never liquidate the book). A loud
    # data_health record rides out in the return so the runlog shows exactly WHY the book froze.
    _degraded_frac = (n_degraded / n_evaluated) if n_evaluated else 0.0
    _breaker_tripped = n_evaluated > 0 and _degraded_frac > 0.80
    data_health = {
        "degraded": _breaker_tripped,
        "n_evaluated": n_evaluated,
        "n_degraded": n_degraded,
        "degraded_fraction": round(_degraded_frac, 3),
        "threshold": 0.80,
        "action": ("NEW_ADDS_FROZEN — data feed degraded across the candidate universe; "
                   "holding existing book, refusing all new opens this build")
                  if _breaker_tripped else "ok",
    }
    if _breaker_tripped:
        # keep only names ALREADY in the book (a held name that still cleared the gate on its own real
        # data is retained too — key off `held`, not the retained flag, so healthy holds aren't
        # churned out by the breaker); drop every genuinely NEW add. A kept name becomes a HOLD.
        _kept = [p for p in passed if p["ticker"].upper() in held]
        for p in _kept:
            p.setdefault("data_degraded", True)
            p["retained"] = True                       # a breaker-kept name is a HOLD, not a fresh add
            p["retained_reason"] = p.get("retained_reason") or "data_health_freeze"
        passed = _kept

    # NOTE: the sector-concentration firebreak is now a PERCENTAGE cap applied AFTER sizing (see
    # _apply_sector_cap, called below). Every entry-gate passer stays in the book; no single sector
    # may exceed SECTOR_MAX_FRACTION of the budget, so an over-weight cohort is risk-trimmed (scaled
    # down) rather than demoted — held names are never churned out, just sized down.

    # confidence-weighted sizing, then the catalyst/confirmation FULL-vs-INITIAL size gate. The W2.2
    # graded extension brake (ext_mult) is applied LAST, after vol-sizing + the sector cap (see
    # _apply_extension_brake below) — a mid-pipeline multiply here would be silently RENORMALISED away
    # by risk_sizing.apply (the NEW-SIZE-1 haircut-erasure), so the brake must be a terminal subtract
    # whose freed weight goes to CASH (doctrine A6: freed weight is sized cash, never redistributed).
    tot = sum(p["confluence"] for p in passed) or 1.0
    for p in passed:
        base = min(p["confluence"] / tot * budget, name_cap)
        mult = 1.0 if p.get("confirmed") else _INITIAL_SIZE_FRACTION
        p["weight"] = round(base * mult, 4)
        # provisional size_stage; the terminal extension brake re-labels a braked NEW add below.
        p["size_stage"] = "full" if p.get("confirmed") else "initial"
        p["sleeve"] = "conviction"
        # a name kept only by exit-hysteresis (retained, entry gate NOT re-cleared) is a HOLD, not a
        # fresh add — say so honestly so the book/thesis doesn't claim "all sides confirm".
        p["verdict"] = "hold" if p.get("retained") else "add"
        # W2.2 EXTENSION 'NO-ADD' band: a NEW add >= the no_add threshold (ext_mult 0.0) takes ZERO
        # size this build — treat it exactly like a gate rejection so the `weight > 0` filter drops it
        # (held names have ext_mult 1.0 and are unaffected). The GRADED (0<ext_mult<1) band is applied
        # as a terminal subtract AFTER vol-sizing so renorm can't erase it (see _apply_extension_brake).
        _em = p.get("ext_mult", 1.0)
        if isinstance(_em, (int, float)) and _em <= 0.0:
            p["weight"] = 0.0
            p["size_stage"] = "ext_no_add"

    # `sized` is a list (unchanged for every existing caller) that ALSO carries the build-wide
    # data_health record as an attribute, so the runlog can surface WHY the book froze without
    # changing the (sized, rejected) tuple contract every caller already unpacks. Also mirrored onto
    # the first sized dict as a fallback for consumers that only iterate the positions.
    sized = _SizedBook(p for p in passed if p["weight"] > 0)
    sized.data_health = data_health
    if sized:
        sized[0].setdefault("data_health", data_health)
    # VOL-MANAGED RISK SIZING (the validated +0.1-0.15 Sharpe lever): re-weight the book
    # by inverse forecasted vol x the dispersion regime — bet less on high-vol names, more
    # on calm ones, de-gross when selection doesn't pay. Risk lever only; never changes
    # WHICH names are in. Additive + graceful (neutral until the macro field ships).
    try:
        from portfolio import risk_sizing
        risk_sizing.apply(sized, budget, name_cap)
    except Exception:  # noqa: BLE001 — additive, never breaks book construction
        pass
    # W2.2 GRADED EXTENSION BRAKE (subtract): applied AFTER vol-managed sizing so the renorm inside
    # risk_sizing.apply cannot erase it (the NEW-SIZE-1 haircut-erasure), but BEFORE the sector cap so
    # that firebreak stays the FINAL binding pass on the book. Scales a NEW extended add's weight by
    # its ext_mult; the freed weight goes to cash (never redistributed). Held / un-extended names have
    # ext_mult 1.0 → untouched → this pass is a no-op for them.
    _apply_extension_brake(sized)
    # PERCENTAGE sector-concentration firebreak (applied LAST so the <=SECTOR_MAX_FRACTION-per-sector
    # invariant holds in the FINAL book): scale any over-weight sector down proportionally, leaving the
    # freed weight in cash. Subtract-only; never churns.
    _apply_sector_cap(sized, budget)
    # sort rejected worst-confluence first so the most-bearish names surface at top
    rejected.sort(key=lambda x: x["confluence"])
    return sized, rejected


def _apply_extension_brake(sized: list[dict]) -> None:
    """Terminal graded entry-brake subtract (in place). For each position, multiply its FINAL weight
    by its stored ``ext_mult`` (1.0 for held / un-extended names; _INITIAL_SIZE_FRACTION for a NEW
    add in the moderate band) AND — W8 — its ``ctx_mult`` (1.0 unless the context gate said
    'against', then 0.6: a new entry into adverse weather takes reduced size). Both run AFTER
    vol-sizing precisely so the renorm-to-budget inside ``risk_sizing.apply`` cannot silently undo
    the haircut (the NEW-SIZE-1 erasure). Subtract-only: each can only reduce a weight; the freed
    weight is left in cash, never redistributed. The 0.0 (no-add) band is handled upstream, so here
    each multiplier is effectively in (0, 1]."""
    for p in sized:
        em = p.get("ext_mult", 1.0)
        cm = p.get("ctx_mult", 1.0)
        em = em if isinstance(em, (int, float)) else 1.0
        cm = cm if isinstance(cm, (int, float)) else 1.0
        mult = max(0.0, min(1.0, em)) * max(0.0, min(1.0, cm))
        if mult >= 1.0:
            continue
        p["weight"] = round(max(0.0, float(p.get("weight", 0.0))) * mult, 4)
        if 0.0 < em < 1.0:
            p["size_stage"] = "initial"
            p["ext_braked"] = True
        if 0.0 < cm < 1.0:
            p["ctx_braked"] = True


def _apply_sector_cap(sized: list[dict], budget: float,
                      frac: float = SECTOR_MAX_FRACTION) -> None:
    """Percentage sector-concentration firebreak (subtract-only, in place).

    No single sector may hold more than `frac` of the conviction-sleeve `budget`. Any sector over
    the cap has every one of its names scaled DOWN by the same factor so the sector lands exactly at
    the cap; the freed weight is left uninvested (cash), never redistributed (which would just
    re-concentrate elsewhere). Names are down-sized, never dropped — a held position is risk-trimmed,
    not churned out. The catch-all 'Unknown' bucket (untagged names — not a real cohort) is exempt."""
    if not sized or budget <= 0 or frac <= 0:
        return
    cap = frac * budget
    from collections import defaultdict
    by_sec: dict[str, list[dict]] = defaultdict(list)
    for p in sized:
        by_sec[_sector_of(p["ticker"])].append(p)
    for sec, names in by_sec.items():
        if sec == "Unknown":
            continue
        tot = sum(max(0.0, float(p.get("weight", 0.0))) for p in names)
        if tot > cap and tot > 0:
            scale = cap / tot
            for p in names:
                # TRUNCATE (floor) at 4dp, not round-to-nearest: rounding N capped names to the
                # nearest 4th-decimal can sum a hair ABOVE the cap (e.g. 3 × 0.0667 = 0.2001 > 0.20),
                # letting the firebreak be breached by a rounding artifact. Flooring guarantees the
                # capped sector lands AT-OR-BELOW the cap; the sub-cent remainder falls to cash
                # (subtract-only — exactly the freed-weight-to-cash contract this firebreak already has).
                p["weight"] = _floor4(float(p.get("weight", 0.0)) * scale)
                p["sector_capped"] = {"sector": sec, "scaled_to_frac": round(frac, 3)}

"""Prophet feed — an ADDITIVE candidate source + plan-geometry chase guard for the conviction sleeve.

WHAT THIS IS
------------
The Macro Dashboard's "US Prophet" system publishes nightly trade PLANS — each a full
entry/trigger/invalidation/target geometry for one equity, pre-gated upstream by the macro
engine's confluence tiers (insider + earnings-surprise + revisions read together, not a lone
alpha). We vendor its emit on disk at ``vendor/macro/site/prophet/index.json`` (schema
``prophet.index/v1``) and expose it here as ONE more place the conviction gate can DISCOVER a
name, plus a deterministic guard that answers *"given this plan's own geometry, is the current
price still a buyable entry or have we missed / over-chased the move?"*.

WHY IT IS SHAPED THIS WAY — the load-bearing invariants
-------------------------------------------------------
  * ADDITIVE ONLY (charter P2 / doc §2.3, §3). This module SOURCES candidates and DESCRIBES
    geometry. It sizes nothing, gates nothing, and casts no vote. ``candidate_tickers()`` only
    ever *adds* names for the downstream conviction gate to filter — the gate still decides, and
    a Prophet plan can never buy through a failed quality/entry/context assessor. The doctrine is
    explicit that the artifact is DISPLAY-TIER: "no signal has passed a forward ledger gate; the
    word 'validated' is forbidden in user-facing text." Nothing here escalates it past display.
  * FAIL-OPEN EVERYWHERE. Every public function returns an inert default (``{}`` / ``[]`` /
    ``None`` / a ``no_plan`` dict) when the artifact is absent, malformed, stale, or the flag is
    OFF — and NEVER raises. A missing signal must inform nothing; it can never manufacture a
    veto, a withhold, or a fabricated plan. (In a fresh worktree the vendored file is commonly a
    dangling symlink into the R2 data plane and simply absent — that MUST degrade silently.)
  * STALENESS IS A HARD GATE. The plans carry entry/trigger/invalidation PRICES; acting on a
    week-old geometry against today's tape is how you knife into a name whose plan already
    invalidated. Top-level ``asof`` older than ``_STALE_DAYS`` calendar days → the whole feed goes
    inert (``index()`` returns ``{}``), exactly as ``brain/neural_web_context`` treats its context.
  * INJECTABLE FOR TESTS. The artifact path is a module-level ``_ARTIFACT_PATH`` derived from the
    ``_V`` vendor root (same convention as ``portfolio/lenses`` and ``brain/neural_web_context``),
    and the read is cached per-process with an explicit ``_reset_cache()`` — so a test can write a
    fixture to ``tmp_path``, monkeypatch ``_ARTIFACT_PATH``/``_V``, reset the cache, and exercise
    the full logic offline with no vendor engine and no network.

REAL-SCHEMA NOTES (verified against the 2026-07-18 vendor artifact — deviations from the naive
expectation are handled here, NOT papered over):
  * ``targets`` is a FLAT list of prices ``[t1, t2]`` — NOT a list of ``{label, price}`` objects.
    The LABELLED T1/T2 levels live in a SEPARATE ``profit_plan`` array whose items are
    ``{level, label, action, status}`` (the price key is ``level``, not ``price``). So t1/t2 are
    resolved by label from ``profit_plan`` first, falling back to ``targets`` by index order.
  * The signal date field is ``_signal_date`` (underscore-prefixed), and conviction is
    ``_conviction_score`` (underscore-prefixed). We read both, with a bare-name fallback so a
    future un-prefixed emit still resolves.
  * There is NO ``rr_grade`` (nor any letter grade) in the artifact. The nearest analogs are the
    numeric ``_r_unit`` (dollar R-size) and ``management_confidence``. ``rr_grade`` is therefore
    normalized to ``None`` (defensively coerced) — we never fabricate a grade. Downstream should
    treat it as informational-only and absent in this schema version.
  * Observed ``recommended_action`` values: hold | wait | trim | invalidated. Observed ``phase``
    values: pre_trigger | triggered_pre_t1 | overtime | invalidated. The enter/expired/closed/trail
    values named in the program spec are handled for forward-compatibility though absent today.
"""
from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# artifact location — derived from the vendor root, monkeypatchable in tests
# --------------------------------------------------------------------------- #
# Mirrors portfolio/lenses (_V) and brain/neural_web_context (_V + _ARTIFACT_PATH): the vendored
# Macro engine emit lives under <repo>/vendor/macro. In a fresh worktree this may be a dangling
# symlink (the emit lives on the R2 data plane) — every read is guarded so that is simply "absent".
_V = Path(__file__).resolve().parent.parent / "vendor" / "macro"
_ARTIFACT_PATH = _V / "site" / "prophet" / "index.json"

_EXPECTED_SCHEMA = "prophet.index/v1"

# --------------------------------------------------------------------------- #
# module constants — ALL UNVERIFIED PRIORS, tunable on the outcome ledger later
# --------------------------------------------------------------------------- #
# Staleness gate: top-level `asof` older than this many CALENDAR days → the whole feed is inert.
# Matches brain/neural_web_context._STALE_DAYS (the nightly-EOD cadence means >4d = a missed run).
_STALE_DAYS = 4                 # calendar days (unverified prior — tune later)
# Hard cap on how many names the additive source may emit in one build, so a bloated emit can
# never flood the conviction candidate set. 8 is a conservative starter (unverified prior).
MAX_CANDIDATES = 8              # (unverified prior — tune later)
# Chase-room budget for entry_discipline: how far ABOVE `entry` (in R = entry - invalidation units)
# the price may run before the plan reads `extended_vs_plan` — i.e. the move got away from us.
# 0.5R is a starter (unverified prior); the doc §2.1 uses the same 0.5R geometry.
CHASE_ROOM_R = 0.5             # (unverified prior — tune later)

# Age (calendar days vs `_signal_date`) beyond which even an enter/wait plan is too old to SOURCE
# a fresh candidate — a two-week-old "wait" setup is stale as a discovery, though still fine as
# geometry for a name we already surfaced (plan_for imposes no age cap; candidate_tickers does).
_CANDIDATE_MAX_AGE_DAYS = 10   # (unverified prior — tune later)

# Phases at which a plan is NO LONGER a live, actionable geometry. `overtime` is deliberately NOT
# here: an overtime plan (past its horizon, action often `trim`) still informs geometry and is a
# legitimate held-name context, so it stays "active".
_DEAD_PHASES = {"invalidated", "expired", "closed"}

# recommended_action values that make a plan an eligible ADDITIVE candidate source (a name we are
# not yet in, that Prophet says to enter or to watch-for-trigger). hold/trim/trail/invalidated are
# management states for an already-live plan — they inform geometry but do not SOURCE a new name.
_SOURCEABLE_ACTIONS = ("enter", "wait")

# --------------------------------------------------------------------------- #
# flag — default ON (armed by the operator order; opt-out restores the prior no-Prophet path)
# --------------------------------------------------------------------------- #
_FALSY = {"0", "false", "no", "off", ""}


def feed_enabled() -> bool:
    """True unless ``MASTERMIND_PROPHET_FEED`` is explicitly falsy ({0,false,no,off,empty}).

    Default ON (doc §2.3 / §3): the operator order arms the Prophet feed; any of the falsy tokens
    turns it fully inert (``index()`` → {} and every derived function → its inert default), which
    restores the exact pre-Prophet behavior. Fail-soft: an unreadable environ degrades to ON only
    when the value is a non-falsy string; a read error is treated as the default (ON).
    """
    try:
        return os.environ.get("MASTERMIND_PROPHET_FEED", "1").strip().lower() not in _FALSY
    except Exception:  # noqa: BLE001 — fail-soft: never raise out of a flag read
        return True


# --------------------------------------------------------------------------- #
# process-level cache — reset via _reset_cache() for tests / intraday refresh
# --------------------------------------------------------------------------- #
_CACHE: dict[str, Any] | None = None    # None = not yet loaded; {} = absent/malformed/stale/flag-off
_CACHE_LOADED: bool = False


def _reset_cache() -> None:
    """Invalidate the per-process index cache. Tests MUST call this around fixtures (the cache is
    keyed on nothing but process lifetime, so a monkeypatched path/flag needs an explicit reset)."""
    global _CACHE, _CACHE_LOADED
    _CACHE = None
    _CACHE_LOADED = False


# --------------------------------------------------------------------------- #
# internal helpers
# --------------------------------------------------------------------------- #

def _age_days(asof_str: str | None) -> int | None:
    """Calendar days since an ISO ``YYYY-MM-DD`` date (leading 10 chars used). None if unparseable.

    Mirrors brain/neural_web_context._age_days so staleness is computed identically across readers.
    """
    if not asof_str:
        return None
    try:
        asof_date = date.fromisoformat(str(asof_str)[:10])
        return (date.today() - asof_date).days
    except Exception:  # noqa: BLE001
        return None


def _num(v: Any) -> float | None:
    """Coerce to float, or None on anything non-numeric / unparseable. Defensive — the artifact is
    machine-generated but a null / string / missing key must degrade to None, never raise. (bool is
    rejected: a stray True/False in a price field is bad data, not the number 1/0.)"""
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _first(d: dict, *keys: str) -> Any:
    """Return the first present, non-None value among ``keys`` in dict ``d`` (else None). Lets us
    prefer the real underscore-prefixed field (``_signal_date``) with a bare-name fallback."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d.get(k)
    return None


def _is_equity_ticker(asset: Any) -> bool:
    """True iff ``asset`` looks like a plain equity ticker: a non-empty string of uppercase
    alphanumerics, allowing dots and hyphens (e.g. BRK.B, RDS-A). Guards against option OCC
    strings, index slugs, or junk sneaking in as a candidate. Case is normalized by the caller,
    so we require the already-uppercased form to be alnum-plus-[.-]."""
    if not isinstance(asset, str):
        return False
    s = asset.strip()
    if not s or len(s) > 12:
        return False
    core = s.replace(".", "").replace("-", "")
    return core.isalnum() and core.isupper() and core.encode("ascii", "ignore").decode() == core


def _t1_t2(plan: dict) -> tuple[float | None, float | None]:
    """Resolve (t1, t2) prices for a plan.

    REAL SCHEMA: labelled levels live in ``profit_plan`` as ``{level, label, ...}`` (price key is
    ``level``); ``targets`` is a flat, index-ordered price list ``[t1, t2, ...]``. So we take T1/T2
    by label from ``profit_plan`` FIRST, then fall back to ``targets`` by position. Every value is
    coerced float-or-None. Never raises."""
    t1 = t2 = None
    # 1) labelled profit_plan levels (authoritative when present)
    pp = plan.get("profit_plan")
    if isinstance(pp, list):
        for leg in pp:
            if not isinstance(leg, dict):
                continue
            label = str(leg.get("label") or "").upper().strip()
            price = _num(_first(leg, "level", "price"))
            if label == "T1" and t1 is None:
                t1 = price
            elif label == "T2" and t2 is None:
                t2 = price
    # 2) flat targets[] by index order — fallback for whichever label didn't resolve
    tg = plan.get("targets")
    if isinstance(tg, list):
        if t1 is None and len(tg) >= 1:
            t1 = _num(tg[0])
        if t2 is None and len(tg) >= 2:
            t2 = _num(tg[1])
    return t1, t2


def _normalize(plan: dict) -> dict | None:
    """Normalize ONE raw plan dict into the stable shape the sleeve consumes, or None if it is not
    a usable dict. Pure; never raises. (Filtering by direction/phase/asset-shape is the caller's
    job — this only reshapes + defensively coerces.)"""
    if not isinstance(plan, dict):
        return None
    ticker = str(plan.get("asset") or "").upper().strip()
    t1, t2 = _t1_t2(plan)
    signal_date = _first(plan, "_signal_date", "signal_date")
    return {
        "ticker": ticker,
        "plan_id": plan.get("id"),
        "entry": _num(plan.get("entry")),
        "trigger": _num(plan.get("trigger")),
        "invalidation": _num(plan.get("invalidation")),
        "t1": t1,
        "t2": t2,
        # conviction is the underscore-prefixed `_conviction_score`; bare fallback for forward emits
        "conviction": _num(_first(plan, "_conviction_score", "conviction_score", "conviction")),
        "phase": plan.get("phase"),
        "recommended_action": plan.get("recommended_action"),
        "signal_date": signal_date,
        "age_days": _age_days(signal_date),
        # NOTE: no rr_grade exists in prophet.index/v1 — degrades to None, never fabricated.
        "rr_grade": _first(plan, "rr_grade"),
    }


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #

def index() -> dict:
    """Return the cached, parsed Prophet index dict — or ``{}`` when it must be inert.

    ``{}`` (fully inert) is returned when ANY of these hold, each fail-open and silent:
      * the flag ``MASTERMIND_PROPHET_FEED`` is OFF,
      * the artifact file is absent (the common fresh-worktree case — dangling vendor symlink),
      * the JSON is malformed / not a dict / wrong ``schema``,
      * the top-level ``asof`` is missing/unparseable, or older than ``_STALE_DAYS`` calendar days.

    Cached for the process lifetime; call ``_reset_cache()`` to force a fresh read (tests / intraday
    refresh). Never raises — a read/parse error logs at debug and yields ``{}``.
    """
    global _CACHE, _CACHE_LOADED
    if _CACHE_LOADED:
        return _CACHE or {}
    _CACHE_LOADED = True
    _CACHE = {}
    try:
        if not feed_enabled():
            return {}
        if not _ARTIFACT_PATH.exists():
            return {}
        import json  # local import: keep module import cheap / dependency-light
        raw = json.loads(_ARTIFACT_PATH.read_text())
        if not isinstance(raw, dict):
            return {}
        if raw.get("schema") != _EXPECTED_SCHEMA:
            log.debug("prophet_feed: wrong schema %r", raw.get("schema"))
            return {}
        asof = raw.get("asof")
        age = _age_days(asof)
        if age is None:
            log.debug("prophet_feed: asof missing/unparseable (%r)", asof)
            return {}
        if age > _STALE_DAYS:
            log.debug("prophet_feed: stale asof=%s age=%dd > %dd", asof, age, _STALE_DAYS)
            return {}
        _CACHE = raw
        return _CACHE
    except Exception as e:  # noqa: BLE001 — fail-open: never raise into a build
        log.debug("prophet_feed: index load failed (%s)", e)
        _CACHE = {}
        return {}


def plans() -> list[dict]:
    """Return the normalized ACTIVE, LONG, equity plans from the index (``[]`` when inert).

    Filters applied to ``index()["plans"]``:
      * ``direction == "BULL"`` (the sleeve is long-only additive; a bear plan sources nothing),
      * ``phase`` NOT in {invalidated, expired, closed} (dead geometries drop; ``overtime`` stays),
      * ``asset`` is a plain equity ticker (uppercase alnum, dots/hyphens allowed).

    Each element is the normalized shape from ``_normalize`` (numeric fields coerced float-or-None,
    ``age_days`` computed vs ``signal_date``, ``t1``/``t2`` resolved by label). Never raises."""
    try:
        idx = index()
        if not idx:
            return []
        raw_plans = idx.get("plans")
        if not isinstance(raw_plans, list):
            return []
        out: list[dict] = []
        for p in raw_plans:
            if not isinstance(p, dict):
                continue
            if str(p.get("direction") or "").upper() != "BULL":
                continue
            if str(p.get("phase") or "").lower() in _DEAD_PHASES:
                continue
            if not _is_equity_ticker(str(p.get("asset") or "").upper().strip()):
                continue
            norm = _normalize(p)
            if norm is not None:
                out.append(norm)
        return out
    except Exception as e:  # noqa: BLE001 — fail-open
        log.debug("prophet_feed: plans() failed (%s)", e)
        return []


def candidate_tickers() -> list[str]:
    """Return the ADDITIVE candidate tickers Prophet contributes to conviction sourcing (``[]`` when
    inert), sorted by conviction desc, deduped, capped at ``MAX_CANDIDATES``.

    A plan sources a candidate iff it is an active BULL equity plan (see ``plans()``) AND
    ``recommended_action`` ∈ {enter, wait} AND its ``age_days`` ≤ ``_CANDIDATE_MAX_AGE_DAYS`` (a
    stale setup is not a fresh discovery). This is DISCOVERY ONLY — the conviction gate downstream
    still runs quality/entry/context on every name and decides; Prophet can never buy a name in.

    Dedup keeps the FIRST occurrence after the conviction-desc sort, so a ticker with two plans is
    represented by its highest-conviction one. A plan with ``conviction is None`` sorts last."""
    try:
        eligible = [
            p for p in plans()
            if str(p.get("recommended_action") or "").lower() in _SOURCEABLE_ACTIONS
            and p.get("age_days") is not None
            and p["age_days"] <= _CANDIDATE_MAX_AGE_DAYS
            and p.get("ticker")
        ]
        # conviction desc; None conviction sorts last (treated as -inf)
        eligible.sort(key=lambda p: (p.get("conviction") if p.get("conviction") is not None else float("-inf")),
                      reverse=True)
        seen: set[str] = set()
        out: list[str] = []
        for p in eligible:
            t = p["ticker"]
            if t in seen:
                continue
            seen.add(t)
            out.append(t)
            if len(out) >= MAX_CANDIDATES:
                break
        return out
    except Exception as e:  # noqa: BLE001 — fail-open
        log.debug("prophet_feed: candidate_tickers() failed (%s)", e)
        return []


def plan_for(ticker: str) -> dict | None:
    """Return the single most-relevant normalized plan for ``ticker``, or None.

    Selection among that ticker's ACTIVE plans (ANY active phase — a held/triggered/overtime plan
    still informs geometry, so this is NOT limited to enter/wait): FRESHEST first (min
    ``age_days``), tie-broken by HIGHEST ``conviction``. A plan with unknown ``age_days`` sorts
    after any dated plan; unknown conviction tie-breaks lowest. None when no active plan exists (or
    the feed is inert). Never raises."""
    try:
        t = (ticker or "").upper().strip()
        if not t:
            return None
        mine = [p for p in plans() if p.get("ticker") == t]
        if not mine:
            return None
        # freshest (age asc; unknown age → +inf so it sorts last), then conviction desc
        mine.sort(key=lambda p: (
            p.get("age_days") if p.get("age_days") is not None else float("inf"),
            -(p.get("conviction") if p.get("conviction") is not None else float("-inf")),
        ))
        return mine[0]
    except Exception as e:  # noqa: BLE001 — fail-open
        log.debug("prophet_feed: plan_for(%r) failed (%s)", ticker, e)
        return None


def entry_discipline(ticker: str, last_price: float | None) -> dict:
    """The plan-geometry chase guard: given ``ticker``'s freshest plan and the current ``last_price``,
    classify where price sits relative to the plan's own entry/invalidation/target geometry.

    Returns a dict with ``status`` ∈ {no_plan, invalidated, missed_move, extended_vs_plan,
    pre_trigger, within_zone} plus the supporting numbers::

        {"status", "r", "room_r", "plan_id", "entry", "invalidation", "t1"}

    where ``r = entry − invalidation`` (the plan's 1R risk unit) and
    ``room_r = (last_price − entry) / r`` (how far above entry, in R units). ``r`` must be > 0 for a
    coherent long plan; a non-positive or missing R (or a missing plan / price) yields
    ``{"status": "no_plan", ...}`` — fail-open, never a fabricated verdict.

    Status ladder (first match wins), all comparisons None-guarded (any missing field needed for a
    branch degrades that branch out; if the core entry/invalidation is missing → ``no_plan``):
      * ``invalidated``       — price < invalidation (the stop is blown; do not enter).
      * ``missed_move``       — price > t1 (the first target is already through; the move got away).
      * ``extended_vs_plan``  — price > entry + CHASE_ROOM_R·R (ran too far past entry to start).
      * ``pre_trigger``       — phase == "pre_trigger" AND trigger present AND price < trigger
                                (the setup has not confirmed yet — a legitimate WATCH, not a buy).
      * ``within_zone``       — none of the above: price is in the buyable band.

    This is ADVISORY geometry: it withholds/sizes nothing itself; the entry engine + conviction
    gate consume the ``status`` to park or proceed. Never raises."""
    inert = {"status": "no_plan", "r": None, "room_r": None,
             "plan_id": None, "entry": None, "invalidation": None, "t1": None}
    try:
        price = _num(last_price)
        p = plan_for(ticker)
        if p is None or price is None:
            return inert
        entry = p.get("entry")
        invalidation = p.get("invalidation")
        t1 = p.get("t1")
        trigger = p.get("trigger")
        if entry is None or invalidation is None:
            return inert
        r = entry - invalidation
        if r <= 0:                       # not a coherent long risk unit → treat as no plan
            return inert
        room_r = (price - entry) / r
        base = {"r": r, "room_r": room_r, "plan_id": p.get("plan_id"),
                "entry": entry, "invalidation": invalidation, "t1": t1}
        # ladder — first match wins
        if price < invalidation:
            status = "invalidated"
        elif t1 is not None and price > t1:
            status = "missed_move"
        elif price > entry + CHASE_ROOM_R * r:
            status = "extended_vs_plan"
        elif str(p.get("phase") or "").lower() == "pre_trigger" and trigger is not None and price < trigger:
            status = "pre_trigger"
        else:
            status = "within_zone"
        return {"status": status, **base}
    except Exception as e:  # noqa: BLE001 — fail-open
        log.debug("prophet_feed: entry_discipline(%r) failed (%s)", ticker, e)
        return inert


def summary_line(ticker: str) -> str:
    """Return ONE compact human-readable evidence line for ``ticker``'s freshest plan, or "" when
    none. Intended for thesis / prompt-evidence injection so a Prophet-sourced name carries its
    plan geometry as provenance (the "why now", not just "confluence +0.60").

    Example::

        PROPHET TNDM-BULL-20260624: entry 15.28 trig 15.53 inv 12.44 T1 19.54 conv 100 phase pre_trigger age 3d

    Fields with no value are omitted from the line. Never raises."""
    try:
        p = plan_for(ticker)
        if p is None:
            return ""
        pid = p.get("plan_id") or (ticker or "").upper().strip()
        parts: list[str] = [f"PROPHET {pid}:"]

        def _fmt(v: float | None) -> str:
            # trim trailing zeros so 15.28 stays 15.28 and 100.0 → 100
            f = float(v)
            return str(int(f)) if f == int(f) else f"{f:g}"

        if p.get("entry") is not None:
            parts.append(f"entry {_fmt(p['entry'])}")
        if p.get("trigger") is not None:
            parts.append(f"trig {_fmt(p['trigger'])}")
        if p.get("invalidation") is not None:
            parts.append(f"inv {_fmt(p['invalidation'])}")
        if p.get("t1") is not None:
            parts.append(f"T1 {_fmt(p['t1'])}")
        if p.get("conviction") is not None:
            parts.append(f"conv {_fmt(p['conviction'])}")
        if p.get("phase"):
            parts.append(f"phase {p['phase']}")
        if p.get("age_days") is not None:
            parts.append(f"age {p['age_days']}d")
        return " ".join(parts)
    except Exception as e:  # noqa: BLE001 — fail-open
        log.debug("prophet_feed: summary_line(%r) failed (%s)", ticker, e)
        return ""

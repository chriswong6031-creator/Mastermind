"""brain/rotation_intake.py — the SOLE reader of the rotation-calls contract (A3, roadmap §2).

THE COORDINATION SEAM — "they IDENTIFY, we CONSUME."
----------------------------------------------------
A separate session owns the rotation/cycle *identification* engine (the detection science:
labelling sectors / themes / subsectors / names as turning, per-call confidence, cadence). We
own the sole reader — NOTHING else in the firm opens their file — plus schema/freshness
validation, target→member expansion, and (elsewhere) all sizing discipline / the consumption
ledger / grading. See docs/design/rotation/ROTATION_CALLS_CONTRACT.md for the authoritative
contract both sessions build to.

Modelled on brain/neural_web_context.py: fail-soft everywhere (absent / malformed / stale /
wrong-schema → a stable empty read, NEVER raises), a single reader, a process-level cache with
an explicit `_reset_cache()` for tests, and a documented staleness gate. This module never
imports the Macro engine and never re-derives the other session's signal — evidence[] is opaque,
logged verbatim, never recomputed.

THE ABSENCE HANDSHAKE (the load-bearing invariant)
--------------------------------------------------
No file, a stale file, or a malformed file all mean **"no calls today"** — it is NEVER read as
"all clear". `calls()` returns [] in every degraded state; downstream lanes must treat [] as
"the rotation lane is provably inert this build", not as an affirmative risk-on read. This is the
same fail-closed discipline the rest of the bot uses: missing data may coarsen or freeze, never
un-cap or flip direction.

PUBLIC API
----------
* calls() -> list[dict]
      Read + schema-validate + freshness-gate the `rotation_calls.v1` artifact. [] when absent /
      stale / invalid. Process-cached; call _reset_cache() to force a fresh read (tests / intraday).
* synthesize_fallback(asof=None) -> list[dict]
      Deterministic identification-LITE that fills the lane until the real engine ships. Every
      synthesized call is tagged provenance='fallback_synth' and its confidence is capped at 0.5 so
      it can NEVER impersonate a real call. Fail-soft → [].
* active_calls(asof=None) -> list[dict]
      calls() if non-empty, else synthesize_fallback(asof). Each returned call carries an
      `intake_path` tag ('real' | 'fallback_synth') so consumers know which lane fed them.
* expand(call) -> list[dict]
      Map a sector/theme/subsector call to member tickers via the existing basket machinery
      (reuses portfolio.conviction's basket-member semantics; reads site/basketdata fail-soft).
      Ranks members by (name ret_20d − basket rel_20d) when reachable, else returns them unranked.
      A target_kind='ticker' call returns just that ticker.
* active_call_for(ticker) -> dict|None
      The active call (if any) whose members / target include the ticker.
* audit_row() -> dict
      {status, as_of, age_days, n_calls, provenance_mix} for the perception runlog.
* _reset_cache() -> None
      Explicit cache reset for tests.

ARTIFACT LOCATION — NOT YET FINALISED (operator open question, roadmap §4.2)
---------------------------------------------------------------------------
The publication home is an open joint ruling: bot-side `data/rotation/` vs macro-side
`site/rotationdata/`, standalone file vs published inside the NW artifact. Until decided we read
the PRIMARY path (`data/rotation/rotation_calls.json` under the repo root) and, only if the
primary is absent, FALL BACK to the vendor site path
(`vendor/macro/site/rotationdata/rotation_calls.json`). Both are expressed as the module-level
constants `_ARTIFACT_PATH` (primary) and `_ARTIFACT_PATH_FALLBACK` below, so when the home is
decided this is a one-line change.

STALENESS
---------
The freshness budget is 2 sessions (the seam contract). Trading sessions would be the precise
unit, but we deliberately use CALENDAR days as a safe over-tight proxy: `age_days = today −
as_of` in calendar days, and anything with age_days > _STALE_DAYS (2) is treated as absent-stale.
Calendar-day arithmetic is strictly TIGHTER than trading-day arithmetic (a weekend is counted, so
a Friday call is stale by Monday under this proxy) — which is the conservative direction: we would
rather drop a marginally-old call ("no calls today") than trust a stale one. If the seam later
needs the looser trading-day budget, swap `_age_days` for a trading-day counter (see
regime_frame._trading_days_since) — the gate logic is otherwise unchanged.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent

# ── ARTIFACT LOCATION (see module docstring — one-line change when the home is ruled) ──────────
# PRIMARY: bot-side data plane. FALLBACK: vendor macro-site plane, tried only if primary absent.
_ARTIFACT_PATH: Path = _ROOT / "data" / "rotation" / "rotation_calls.json"
_ARTIFACT_PATH_FALLBACK: Path = _ROOT / "vendor" / "macro" / "site" / "rotationdata" / "rotation_calls.json"

# The rotation_tensor artifact — an OPTIONAL input to synthesize_fallback (never a hard dependency).
_TENSOR_PATH: Path = _ROOT / "data" / "market_view" / "rotation_tensor.json"

_EXPECTED_SCHEMA = "rotation_calls.v1"
_STALE_DAYS = 2  # freshness budget: 2 sessions, using calendar days as a safe (tighter) proxy.

# Contract vocabularies — validated against the schema (see the contract doc for the state machine).
_VALID_STATES = frozenset({"EARLY", "TURNING", "CONFIRMED", "FAILED", "EXPIRED"})
_VALID_TARGET_KINDS = frozenset({"sector", "theme", "subsector", "ticker"})
# The two terminal / non-actionable states — active_call_for skips these (a FAILED/EXPIRED call is
# not "active" for candidacy). calls() still RETURNS them (state history is the seam's ground truth);
# it is downstream that must not act on a terminal call.
_TERMINAL_STATES = frozenset({"FAILED", "EXPIRED"})

# fallback_synth cap: a synthesized call may NEVER present as more than a soft EARLY read.
_FALLBACK_MAX_CONFIDENCE = 0.5

# --------------------------------------------------------------------------- #
# process-level cache — reset via _reset_cache() for tests (mirrors neural_web_context)
# --------------------------------------------------------------------------- #
_CACHE: Optional[list[dict]] = None   # None = not yet loaded; [] = empty/absent/stale/invalid
_CACHE_LOADED: bool = False


def _reset_cache() -> None:
    """Invalidate the per-process cache. Tests MUST call this around fixtures."""
    global _CACHE, _CACHE_LOADED
    _CACHE = None
    _CACHE_LOADED = False


# --------------------------------------------------------------------------- #
# internal helpers
# --------------------------------------------------------------------------- #

def _age_days(asof_str: Any) -> Optional[int]:
    """Calendar days since asof_str (YYYY-MM-DD). None if absent/unparseable.

    See the module docstring: calendar days are a deliberately-tighter proxy for the 2-session
    freshness budget. None ("we do not know how old this is") is NEVER treated as fresh.
    """
    if not asof_str:
        return None
    try:
        asof_date = date.fromisoformat(str(asof_str)[:10])
        return (date.today() - asof_date).days
    except Exception:  # noqa: BLE001
        return None


def _resolve_path() -> Optional[Path]:
    """Return the artifact path to read: PRIMARY if it exists, else FALLBACK if IT exists, else None."""
    try:
        if _ARTIFACT_PATH.exists():
            return _ARTIFACT_PATH
        if _ARTIFACT_PATH_FALLBACK.exists():
            return _ARTIFACT_PATH_FALLBACK
    except Exception:  # noqa: BLE001
        return None
    return None


def _load_raw() -> Optional[dict[str, Any]]:
    """Read + JSON-parse the artifact from the resolved path. None on any IO/parse error / absence."""
    path = _resolve_path()
    if path is None:
        return None
    try:
        return json.loads(path.read_text())
    except Exception as e:  # noqa: BLE001
        log.debug("rotation_intake: read failed (%s)", e)
        return None


def _validate_envelope(raw: Any) -> tuple[bool, str]:
    """Validate the artifact ENVELOPE (schema + as_of presence + freshness). (valid, reason).

    Individual calls are validated separately (a bad call is skipped, not fatal) — this gate is
    only about whether the artifact as a whole is trustworthy at all.
    """
    if not isinstance(raw, dict):
        return False, "not a dict"
    if raw.get("schema") != _EXPECTED_SCHEMA:
        return False, f"wrong schema {raw.get('schema')!r}"
    asof = raw.get("as_of")
    if not asof:
        return False, "as_of absent"
    age = _age_days(asof)
    if age is None:
        return False, f"as_of unparseable: {asof!r}"
    if age > _STALE_DAYS:
        return False, f"stale: as_of={asof} age={age}d > {_STALE_DAYS}d"
    return True, "ok"


def _valid_call(call: Any) -> bool:
    """A single call is VALID iff it is a dict with a non-empty call_id + a valid state + target_kind.

    Deliberately minimal — the seam is additive-only under v1, so we validate the JOIN KEY (call_id),
    the state machine, and the target kind, and pass everything else through opaque. A call failing
    this is SKIPPED (logged), never fatal to the whole read.
    """
    if not isinstance(call, dict):
        return False
    cid = call.get("call_id")
    if not cid or not isinstance(cid, str):
        return False
    if call.get("state") not in _VALID_STATES:
        return False
    if call.get("target_kind") not in _VALID_TARGET_KINDS:
        return False
    return True


# --------------------------------------------------------------------------- #
# calls() — the sole reader
# --------------------------------------------------------------------------- #

def calls() -> list[dict]:
    """Return the validated, fresh rotation calls, or [] in every degraded state.

    [] means "no calls today" — the ABSENCE HANDSHAKE — never "all clear". Absent file, stale
    as_of (> 2 sessions old, calendar-day proxy), a wrong/absent schema, or an unparseable artifact
    all collapse to []. Individually-malformed calls (bad/missing call_id, invalid state/target_kind)
    are SKIPPED, not fatal — a single bad row never sinks a good artifact.

    Result is process-cached (like neural_web_context.context()); call _reset_cache() to force a
    fresh read.
    """
    global _CACHE, _CACHE_LOADED
    if _CACHE_LOADED:
        return list(_CACHE or [])
    _CACHE_LOADED = True
    try:
        raw = _load_raw()
        if raw is None:
            _CACHE = []
            return []
        valid, reason = _validate_envelope(raw)
        if not valid:
            log.debug("rotation_intake: envelope rejected (%s) → no calls today", reason)
            _CACHE = []
            return []
        raw_calls = raw.get("calls")
        if not isinstance(raw_calls, list):
            _CACHE = []
            return []
        out: list[dict] = []
        for c in raw_calls:
            if _valid_call(c):
                out.append(c)
            else:
                log.debug("rotation_intake: skipping malformed call %r", c)
        _CACHE = out
        return list(out)
    except Exception as e:  # noqa: BLE001 — fail-soft: never raise into a build
        log.warning("rotation_intake: unexpected error loading calls (%s)", e)
        _CACHE = []
        return []


# --------------------------------------------------------------------------- #
# synthesize_fallback() — deterministic identification-LITE
# --------------------------------------------------------------------------- #

def synthesize_fallback(asof: Optional[str] = None) -> list[dict]:
    """Deterministic identification-LITE that fills the lane until the real engine ships.

    Composed ONLY from what is already available in-repo — this never re-derives the other
    session's science, it only produces provably-inferior placeholder reads so the downstream
    lanes have SOMETHING to consume (and a permanent disagreement check) until the real artifact
    lands. Two independent sources, unioned:

      (1) regime_frame.cycles(): every sector whose phase is entry_favored (Trough/Recovery/
          Expansion) becomes an EARLY sector `rotation_in` call. If osc_slope is present it must be
          > 0 (turning UP) — a missing osc_slope is allowed (the cycles() gate already fresh-gates
          the read; absent slope only ever removes a call, never fabricates one).
      (2) rotation_tensor (if the artifact is present + fresh): each top_pair's `lead` instrument
          becomes an EARLY sector `rotation_in` call (the leg gaining relative strength).

    EVERY synthesized call is tagged provenance='fallback_synth' and confidence is capped at
    _FALLBACK_MAX_CONFIDENCE (0.5) so it can NEVER impersonate a real EARLY→CONFIRMED call. call_ids
    are namespaced 'synth:...' so they never collide with a real engine's join keys. Fail-soft → [].

    `asof` is stamped on the synthesized calls' as_of / first_seen (defaults to today) so the
    fallback read is itself freshness-legible; it does NOT drive any live read here.
    """
    asof_str = str(asof)[:10] if asof else date.today().isoformat()
    out: list[dict] = []
    seen_targets: set[str] = set()

    # (1) cycles() entry_favored ∧ (osc_slope > 0 if available) → EARLY sector calls.
    try:
        from brain import regime_frame
        cyc = regime_frame.cycles() or {}
    except Exception:  # noqa: BLE001 — a cycle-read failure just drops this source
        cyc = {}
    for ticker, row in (cyc.items() if isinstance(cyc, dict) else []):
        try:
            if not isinstance(row, dict) or not row.get("entry_favored"):
                continue
            osc = row.get("osc_slope")
            # osc present → require turning UP; osc absent → allowed (never fabricate a brake/gate).
            if isinstance(osc, (int, float)) and osc <= 0:
                continue
            tk = str(ticker).upper()
            if tk in seen_targets:
                continue
            seen_targets.add(tk)
            out.append(_synth_call(
                target=tk, target_kind="sector", asof=asof_str,
                confidence=0.4,
                evidence=[{"source": "regime_frame.cycles",
                           "value": {"phase": row.get("phase"), "osc_slope": osc,
                                     "pos": row.get("pos")},
                           "note": "entry_favored cycle phase (identification-lite)"}],
            ))
        except Exception:  # noqa: BLE001 — a single bad row never sinks the synth
            continue

    # (2) rotation_tensor top_pairs (if present + fresh) → EARLY sector calls on each `lead`.
    try:
        tensor = _load_tensor()
        for pair in (tensor.get("top_pairs") or []):
            if not isinstance(pair, dict):
                continue
            lead = pair.get("lead")
            if not lead or not isinstance(lead, str):
                continue
            tk = lead.upper()
            if tk in seen_targets:
                continue
            seen_targets.add(tk)
            out.append(_synth_call(
                target=tk, target_kind="sector", asof=asof_str,
                confidence=0.45,
                evidence=[{"source": "rotation_tensor.top_pairs",
                           "value": {"lag": pair.get("lag"),
                                     "R_bps_day": pair.get("R_bps_day"),
                                     "dR_bps_day": pair.get("dR_bps_day"),
                                     "accelerating": pair.get("accelerating")},
                           "note": "leading leg of a top RS-velocity pair (identification-lite)"}],
            ))
    except Exception:  # noqa: BLE001 — tensor is optional; any failure just drops this source
        pass

    return out


def _load_tensor() -> dict[str, Any]:
    """Read the rotation_tensor artifact iff present + fresh (≤ _STALE_DAYS). {} otherwise.

    Reuses the same 2-session calendar-day freshness budget as the calls gate. Reads the tensor's
    flat rs_velocity.top_pairs. A missing / stale / malformed tensor → {} (source simply absent).
    """
    try:
        if not _TENSOR_PATH.exists():
            return {}
        raw = json.loads(_TENSOR_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(raw, dict):
        return {}
    age = _age_days(raw.get("as_of"))
    if age is None or age > _STALE_DAYS:
        return {}
    rs_vel = raw.get("rs_velocity")
    top_pairs = rs_vel.get("top_pairs") if isinstance(rs_vel, dict) else None
    return {"top_pairs": top_pairs or [], "as_of": raw.get("as_of")}


def _synth_call(*, target: str, target_kind: str, asof: str, confidence: float,
                evidence: list[dict]) -> dict:
    """Build one fallback_synth call — always provenance-tagged and confidence-capped."""
    conf = min(float(confidence), _FALLBACK_MAX_CONFIDENCE)
    return {
        "call_id": f"synth:{target_kind}:{target}:{asof}",
        "target_kind": target_kind,
        "target": target,
        "members": None,
        "state": "EARLY",
        "direction": "rotation_in",
        "confidence": conf,
        "horizon_bdays": None,
        "evidence": evidence,
        "falsifier": None,
        "first_seen": asof,
        "state_history": [{"date": asof, "state": "EARLY"}],
        "provenance": "fallback_synth",
        "as_of": asof,
    }


# --------------------------------------------------------------------------- #
# active_calls() — real if present, else fallback
# --------------------------------------------------------------------------- #

def active_calls(asof: Optional[str] = None) -> list[dict]:
    """Return calls() if non-empty, else synthesize_fallback(asof). Tag which lane was used.

    Each returned call carries an `intake_path` key: 'real' when it came from the real artifact,
    'fallback_synth' when synthesized. (Synthesized calls also already carry
    provenance='fallback_synth'; intake_path is the single field consumers switch on regardless of
    source, so a real call that happens to lack a provenance field is still unambiguously tagged.)
    """
    try:
        real = calls()
    except Exception:  # noqa: BLE001
        real = []
    if real:
        return [{**c, "intake_path": "real"} for c in real]
    try:
        synth = synthesize_fallback(asof)
    except Exception:  # noqa: BLE001
        synth = []
    return [{**c, "intake_path": "fallback_synth"} for c in synth]


# --------------------------------------------------------------------------- #
# expand() — target → member tickers (reuses the basket machinery)
# --------------------------------------------------------------------------- #

def expand(call: Any) -> list[dict]:
    """Map a sector / theme / subsector call to member tickers via the existing basket machinery.

    REUSE (not reinvention): member extraction + the (name ret_20d − basket rel_20d) ranking mirror
    portfolio.conviction's basket-member semantics (`_basket_leaders` / `_basket_top_picks`), reading
    the same site/basketdata/baskets.json contract (baskets[].members[].{symbol|ticker, ret_20d};
    baskets[].perf.20d.rel). A missing / unreadable baskets file degrades to [] (fail-soft) — never
    raises, never fabricates members.

    Resolution:
      * target_kind == 'ticker' → return just that ticker (no basket lookup; a name call IS its
        member). Ranked score is None (a single name has no basket-relative rank).
      * otherwise → if the call carries an explicit `members` list, use it verbatim (the engine's
        own membership is authoritative); else match the target to a basket by id / slug / target
        and take its members. Rank each member by (member ret_20d − basket perf.20d.rel) when both
        are reachable; otherwise return members UNRANKED (score None), order-preserved.

    Returns a list of {ticker, score} dicts (score float|None), highest score first when ranked.
    """
    if not isinstance(call, dict):
        return []
    kind = call.get("target_kind")
    target = call.get("target")

    # A ticker call is its own member — no basket expansion.
    if kind == "ticker":
        tk = str(target).upper() if target else None
        return [{"ticker": tk, "score": None}] if tk else []

    try:
        d = _load_baskets()
    except Exception:  # noqa: BLE001
        d = None
    baskets = (d.get("baskets") if isinstance(d, dict) else None) or []

    # (a) explicit members on the call win — the engine's own membership is authoritative.
    explicit = call.get("members")
    if isinstance(explicit, list) and explicit:
        syms = [str(m).upper() for m in explicit if m]
        # try to rank against a matched basket's rel_20d; else unranked.
        basket = _match_basket(baskets, target)
        return _rank_members(syms, basket)

    # (b) else resolve the target to a basket and take its members.
    basket = _match_basket(baskets, target)
    if not basket:
        return []
    syms: list[str] = []
    for m in (basket.get("members") or []):
        if not isinstance(m, dict):
            continue
        sym = (m.get("symbol") or m.get("ticker") or "").upper()
        if sym:
            syms.append(sym)
    return _rank_members(syms, basket)


def _load_baskets() -> Optional[dict[str, Any]]:
    """Read site/basketdata/baskets.json from the vendor macro site. None on any error/absence.

    Uses the SAME relative path portfolio.conviction reads; kept local (rather than importing
    conviction._load) so this module stays free of conviction's heavier import graph (bot, lenses).
    """
    p = _ROOT / "vendor" / "macro" / "site" / "basketdata" / "baskets.json"
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except Exception:  # noqa: BLE001
        return None


def _match_basket(baskets: list, target: Any) -> Optional[dict]:
    """Find the basket matching `target` by id / slug / reference.label / name (case-insensitive)."""
    if not target or not isinstance(baskets, list):
        return None
    t = str(target).strip().lower()
    for b in baskets:
        if not isinstance(b, dict):
            continue
        keys = [b.get("id"), b.get("slug"), b.get("name"), b.get("target"),
                (b.get("reference") or {}).get("label") if isinstance(b.get("reference"), dict) else None]
        for k in keys:
            if isinstance(k, str) and k.strip().lower() == t:
                return b
    return None


def _rank_members(syms: list[str], basket: Optional[dict]) -> list[dict]:
    """Rank member symbols by (member ret_20d − basket rel_20d); unranked (score None) if unreachable.

    Mirrors conviction's basket-member semantics: member ret_20d from baskets[].members[].ret_20d,
    basket rel_20d from baskets[].perf.20d.rel. When the ranking inputs are not reachable (no basket,
    no rel_20d, no member ret_20d) the members are returned UNRANKED in their original order — never
    dropped (missing data coarsens the ranking, it does not remove a member).
    """
    if not syms:
        return []
    if not isinstance(basket, dict):
        return [{"ticker": s, "score": None} for s in syms]

    # basket relative 20d return (the benchmark leg of the differential).
    rel20 = (((basket.get("perf") or {}).get("20d") or {}).get("rel"))
    rel20_f = float(rel20) if isinstance(rel20, (int, float)) else None

    # member ret_20d lookup from the basket's own member rows.
    ret_by_sym: dict[str, float] = {}
    for m in (basket.get("members") or []):
        if not isinstance(m, dict):
            continue
        sym = (m.get("symbol") or m.get("ticker") or "").upper()
        r = m.get("ret_20d")
        if sym and isinstance(r, (int, float)):
            ret_by_sym[sym] = float(r)

    if rel20_f is None or not ret_by_sym:
        # ranking inputs not reachable → unranked, order preserved.
        return [{"ticker": s, "score": None} for s in syms]

    scored: list[dict] = []
    for s in syms:
        r = ret_by_sym.get(s)
        score = (r - rel20_f) if isinstance(r, (int, float)) else None
        scored.append({"ticker": s, "score": score})
    # sort: real scores first (descending), None-scored members trail in original order.
    scored.sort(key=lambda x: (x["score"] is None, -(x["score"] if x["score"] is not None else 0.0)))
    return scored


# --------------------------------------------------------------------------- #
# active_call_for() — the call whose members/target include a ticker
# --------------------------------------------------------------------------- #

def active_call_for(ticker: str) -> Optional[dict]:
    """Return the ACTIVE call (real preferred, else fallback) whose members / target include `ticker`.

    "Active" excludes the terminal states (FAILED / EXPIRED) — a resolved call is not a candidacy
    reason. Matching is against: the call's own target (for a ticker-kind call), the call's explicit
    `members`, and the expand()'d member set for a sector/theme/subsector call. Returns None when no
    active call covers the ticker (the common case). Fail-soft → None.
    """
    if not ticker:
        return None
    tk = str(ticker).upper()
    try:
        for c in active_calls():
            if not isinstance(c, dict):
                continue
            if c.get("state") in _TERMINAL_STATES:
                continue
            # direct target hit (ticker-kind call).
            if c.get("target_kind") == "ticker" and str(c.get("target", "")).upper() == tk:
                return c
            # explicit members.
            members = c.get("members")
            if isinstance(members, list) and any(str(m).upper() == tk for m in members if m):
                return c
            # expanded member set (sector/theme/subsector).
            try:
                if any(row.get("ticker") == tk for row in expand(c)):
                    return c
            except Exception:  # noqa: BLE001 — a bad expand never sinks the scan
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


# --------------------------------------------------------------------------- #
# audit_row() — perception runlog
# --------------------------------------------------------------------------- #

def audit_row() -> dict[str, Any]:
    """Return {status, as_of, age_days, n_calls, provenance_mix} for the runlog.

    status ∈ {present, absent, stale, fallback}:
      * present  — a fresh, valid real artifact with ≥1 valid call.
      * stale    — a real artifact exists but its as_of is older than the 2-session budget.
      * absent   — no artifact (or wrong/absent schema / unparseable) AND no fallback produced.
      * fallback — no usable real calls, but synthesize_fallback produced ≥1 synthesized call.

    n_calls counts the calls that would be consumed (real when present, else synthesized).
    provenance_mix is a {provenance: count} tally over those calls ('real' for real calls that carry
    no provenance field; whatever the call declares otherwise). Flag-independent — always safe.
    """
    try:
        raw = _load_raw()
        # ── real-artifact paths ──────────────────────────────────────────────────────────────
        if raw is not None and isinstance(raw, dict):
            asof = raw.get("as_of")
            age = _age_days(asof)
            valid, _reason = _validate_envelope(raw)
            if valid:
                real = calls()
                if real:
                    return {
                        "status": "present",
                        "as_of": asof,
                        "age_days": age,
                        "n_calls": len(real),
                        "provenance_mix": _provenance_mix(real),
                    }
                # valid+fresh envelope but zero usable calls → fall through to fallback below.
            elif raw.get("schema") == _EXPECTED_SCHEMA and asof and (age is None or age > _STALE_DAYS):
                # a real artifact that is specifically STALE (right schema, present but old as_of).
                return {
                    "status": "stale",
                    "as_of": asof,
                    "age_days": age,
                    "n_calls": 0,
                    "provenance_mix": {},
                }
            # wrong schema / unparseable as_of / no calls → treated as absent, try fallback.

        # ── fallback / absent ────────────────────────────────────────────────────────────────
        synth = synthesize_fallback()
        if synth:
            return {
                "status": "fallback",
                "as_of": synth[0].get("as_of"),
                "age_days": 0,
                "n_calls": len(synth),
                "provenance_mix": _provenance_mix(synth),
            }
        return {"status": "absent", "as_of": None, "age_days": None,
                "n_calls": 0, "provenance_mix": {}}
    except Exception:  # noqa: BLE001
        return {"status": "absent", "as_of": None, "age_days": None,
                "n_calls": 0, "provenance_mix": {}}


def _provenance_mix(calls_list: list[dict]) -> dict[str, int]:
    """Tally {provenance: count} over a call list; a call with no provenance field counts as 'real'."""
    mix: dict[str, int] = {}
    for c in calls_list:
        prov = c.get("provenance") if isinstance(c, dict) else None
        key = str(prov) if prov else "real"
        mix[key] = mix.get(key, 0) + 1
    return mix

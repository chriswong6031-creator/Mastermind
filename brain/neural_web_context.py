"""brain/neural_web_context.py — single reader for the Neural Web → Mastermind bridge (W-NW.1).

Reads ONLY vendor/macro/site/neuralwebdata/mastermind_context.json.  Fail-soft everywhere:
absent / malformed / stale / wrong-schema → stable empty context object, never raises.
Never imports Macro engine modules.

PUBLIC API
----------
* context()            — cached-per-process full artifact dict; {} when absent/stale/invalid.
* candidate(ticker)    — per-ticker advisory context dict; {} when not present.
* market_plane()       — compact dict for the neural_web market_view plane.
* seat_prompt_block(tickers, max_chars=1200) — compact text for prompt injection (NO cortex prose).
* audit_row()          — {status, asof, age_days, n_candidates, gap_notes_count}.
* nw_prompts_enabled() — reads MASTERMIND_NW_CONTEXT; default OFF (text-only prompt injection).
* nw_decision_mode()   — reads MASTERMIND_NW_DECISION; default 'off' (typed decision ladder).
* decision_signals(ticker) — THE typed decision-policy chokepoint; default-OFF inert no-op.
* _reset_context_cache() — explicit cache reset for tests.

FLAGS (independent):
  MASTERMIND_NW_CONTEXT  — default OFF; owns TEXT-ONLY prompt/plane injection.
  MASTERMIND_NW_DECISION — default 'off'; owns the typed decision ladder
                           (off|shadow|candidacy|shrink|vote) — see nw_decision_mode().
Reader + audit rows are flag-independent; prompt injection and decision signals are gated.

STALENESS: as_of age > 4 calendar days → treat as absent-stale.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
# Follow intake._read convention: _V = repo_root / "vendor" / "macro"
_V = _ROOT / "vendor" / "macro"
_ARTIFACT_PATH = _V / "site" / "neuralwebdata" / "mastermind_context.json"

_EXPECTED_SCHEMA = "neural_web_mastermind_context.v1"
_STALE_DAYS = 4   # calendar days

# --------------------------------------------------------------------------- #
# typed decision-policy priors (A5) — ALL UNVERIFIED, tunable module constants
# --------------------------------------------------------------------------- #
# Candidacy scores by NW bottom_state. Unverified priors: leader-anticipation is
# coin-flip per the China-basket program; these are conservative starter weights,
# never authority to size. Only ever *add* a candidate for the gate to filter.
NW_CANDIDACY_SCORES: dict[str, float] = {
    "WATCH": 0.35,          # eyes-only prior — turn not yet confirmed
    "BOTTOMING": 0.50,      # turn in progress
    "CONFIRMED": 0.50,      # confirmed turn — still a starter-grade prior
}
# graph_conflicts count at/above which a NEW entry is shrunk (subtract-only). Unverified.
NW_CONFLICTS_MIN: int = 2
# entry-size multiplier applied when the conflict floor is breached (subtract-only). Unverified.
NW_ENTRY_SHRINK: float = 0.7
# market-plane contradiction_count at/above which the tape is "conflicted", so a
# name with zero graph_conflicts reads as a clean/safe-haven tell. Unverified.
NW_CONTRADICTIONS_MIN: int = 3

# Monotone decision-mode ladder (see nw_decision_mode()). Ordinal only — the values
# are used solely for threshold (>=) comparisons via _mode_ge().
_DECISION_MODE_ORDER: dict[str, int] = {
    "off": 0,
    "shadow": 1,
    "candidacy": 2,
    "shrink": 3,
    "vote": 4,
}
# W8 (2026-07-19, operator-ordered): default 'shrink' — candidacy sourcing (NW may ADD candidates
# for the gate to filter) + subtract-only entry shrink on graph-conflict density. 'vote' stays
# opt-in. Opt out with MASTERMIND_NW_DECISION=off.
_DECISION_MODE_DEFAULT = "shrink"

# --------------------------------------------------------------------------- #
# process-level cache — reset via _reset_context_cache() for tests
# --------------------------------------------------------------------------- #
_CACHE: dict[str, Any] | None = None   # None = not yet loaded; {} = empty/absent
_CACHE_LOADED: bool = False


def _reset_context_cache() -> None:
    """Invalidate the per-process cache.  Tests MUST call this around fixtures."""
    global _CACHE, _CACHE_LOADED
    _CACHE = None
    _CACHE_LOADED = False


# --------------------------------------------------------------------------- #
# flag
# --------------------------------------------------------------------------- #

def nw_prompts_enabled() -> bool:
    """Return True iff MASTERMIND_NW_CONTEXT is set to '1' (default OFF).

    NOTE: this flag owns TEXT-ONLY prompt injection (seat_prompt_block / pm_conviction
    section) and is fully independent of MASTERMIND_NW_DECISION (see nw_decision_mode),
    which owns the typed decision-signal chokepoint.
    """
    # W8 (2026-07-19): default flipped ON — the W-NW.1 arming condition (≥5 consecutive builds with
    # nw_context status=present) matured on schedule (come-back date 2026-07-19) and the operator
    # ordered the NW connection as part of the Flagship v2 program. Text-only prompt injection;
    # opt out with MASTERMIND_NW_CONTEXT=0.
    return os.environ.get("MASTERMIND_NW_CONTEXT", "1").strip().lower() in ("1", "true", "yes")


# --------------------------------------------------------------------------- #
# typed decision-policy chokepoint (A5)
# --------------------------------------------------------------------------- #

def nw_decision_mode() -> str:
    """Return the NW decision mode from MASTERMIND_NW_DECISION (default 'off').

    The five-mode monotone ladder — each mode is a strict superset of the prior's
    authority (use _mode_ge for threshold checks):

        off        nothing; decision_signals() is a byte-identical no-op (inert).
        shadow     compute + log only; signals are still inert to downstream sizing.
        candidacy  NW MAY add candidates (a candidacy prior for the gate to filter).
        shrink     NW may ALSO subtract entry size on graph-conflict density.
        vote       NW may ALSO cast a graded lens vote.

    This flag is SEPARATE from MASTERMIND_NW_CONTEXT, which stays owning text-only
    prompt injection. Unrecognized / empty values fail-soft to 'off'.
    """
    try:
        raw = os.environ.get("MASTERMIND_NW_DECISION")
        if raw is None:
            return _DECISION_MODE_DEFAULT          # ABSENT → the W8 default ('shrink')
        raw = raw.strip().lower()
        # PRESENT but empty/unrecognized → 'off' (inert), NOT the default: a garbled value must
        # never ESCALATE authority (fail-soft means unknown input is inert, W8 default included).
        return raw if raw in _DECISION_MODE_ORDER else "off"
    except Exception:  # noqa: BLE001 — fail-soft: never raise
        return "off"


def _mode_ge(mode: str, threshold: str) -> bool:
    """True iff `mode` is at or above `threshold` on the decision ladder.

    Unrecognized modes/thresholds resolve to 'off' (rank 0), so unknown input is inert.
    """
    m = _DECISION_MODE_ORDER.get(mode, 0)
    t = _DECISION_MODE_ORDER.get(threshold, 0)
    return m >= t


def _inert_signals(mode: str) -> dict[str, Any]:
    """The byte-identical no-op signal dict: everything None/inert."""
    return {
        "candidacy": None,
        "entry_shrink": None,
        "clean_in_conflicted": False,
        "inert": True,
        "mode": mode,
    }


def decision_signals(ticker: str) -> dict[str, Any]:
    """Return TYPED decision signals for `ticker`, gated by nw_decision_mode().

    THE single place that converts raw NW candidate/market fields into decision
    signals, so no downstream consumer reads raw NW fields ad hoc. Fail-soft: any
    absent/stale/malformed context, or an fdr-uncleared name, degrades to the inert
    default (a byte-identical no-op for every caller).

    Return shape::

        {
          "candidacy": {"state": <bottom_state>, "score": <float>, "lean": +1} | None,
          "entry_shrink": 0.7 | None,
          "clean_in_conflicted": <bool>,
          "inert": <bool>,
          "mode": <mode string>,
        }

    Derivation (all fail-soft; absent data → None, never a fabricated signal):
      * off              → fully inert (candidacy/entry_shrink None, clean_in_conflicted False).
      * fdr not cleared  → per-name inert (display-armed-only names never originate a signal).
      * candidacy (mode>=candidacy) → bottom_state WATCH→0.35, BOTTOMING/CONFIRMED→0.50,
                                      lean=+1; any other state → candidacy None.
      * entry_shrink (mode>=shrink) → graph_conflicts>=NW_CONFLICTS_MIN AND fdr ok → NW_ENTRY_SHRINK;
                                      missing/insufficient conflicts → None (never shrink on absence).
      * clean_in_conflicted (mode>=candidacy) → conflicts==0 AND market contradiction_count
                                      >=NW_CONTRADICTIONS_MIN (the safe-haven tell).
    """
    mode = nw_decision_mode()
    try:
        # mode off — byte-identical no-op regardless of row content.
        if not _mode_ge(mode, "shadow"):
            return _inert_signals(mode)

        row = candidate(ticker)
        # per-name inert: absent row, or a name not cleared by the FDR batch.
        kernel = row.get("kernel") if isinstance(row, dict) else None
        fdr_ok = isinstance(kernel, dict) and kernel.get("fdr_cleared") is True
        if not row or not fdr_ok:
            return _inert_signals(mode)

        signals = _inert_signals(mode)
        # From here on the name is fdr-cleared and present → not per-name inert,
        # though individual signals may still be None until their mode arms.
        signals["inert"] = False

        # --- graph_conflicts (shared leg for shrink + clean_in_conflicted) --- #
        conflicts_raw = row.get("graph_conflicts")
        conflicts = len(conflicts_raw) if isinstance(conflicts_raw, list) else 0

        # --- candidacy (mode >= candidacy) --- #
        if _mode_ge(mode, "candidacy"):
            bottom = row.get("bottom") or {}
            state = None
            if isinstance(bottom, dict):
                state = bottom.get("bottom_state") or bottom.get("state")
            score = NW_CANDIDACY_SCORES.get(state) if isinstance(state, str) else None
            if score is not None:
                signals["candidacy"] = {"state": state, "score": score, "lean": +1}

            # --- clean_in_conflicted (available at candidacy+) --- #
            try:
                contradictions = int((market_plane() or {}).get("contradiction_count") or 0)
            except Exception:  # noqa: BLE001
                contradictions = 0
            signals["clean_in_conflicted"] = (
                conflicts == 0 and contradictions >= NW_CONTRADICTIONS_MIN
            )

        # --- entry_shrink (mode >= shrink; subtract-only, never on absence) --- #
        if _mode_ge(mode, "shrink"):
            if conflicts >= NW_CONFLICTS_MIN:
                signals["entry_shrink"] = NW_ENTRY_SHRINK

        return signals
    except Exception as e:  # noqa: BLE001 — fail-soft: never raise into a decision
        log.debug("neural_web_context: decision_signals failed (%s)", e)
        return _inert_signals(mode)


# --------------------------------------------------------------------------- #
# internal helpers
# --------------------------------------------------------------------------- #

def _age_days(asof_str: str | None) -> int | None:
    """Calendar days since asof_str (YYYY-MM-DD).  None if unparseable."""
    if not asof_str:
        return None
    try:
        asof_date = date.fromisoformat(str(asof_str)[:10])
        return (date.today() - asof_date).days
    except Exception:  # noqa: BLE001
        return None


def _load_raw() -> dict[str, Any] | None:
    """Read and JSON-parse the artifact.  Returns None on any IO/parse error."""
    try:
        if not _ARTIFACT_PATH.exists():
            return None
        return json.loads(_ARTIFACT_PATH.read_text())
    except Exception as e:  # noqa: BLE001
        log.debug("neural_web_context: read failed (%s)", e)
        return None


def _validate(raw: Any) -> tuple[bool, str]:
    """Return (valid, reason).  valid=True only when schema+is_context_only+as_of+freshness pass."""
    if not isinstance(raw, dict):
        return False, "not a dict"
    if raw.get("schema") != _EXPECTED_SCHEMA:
        return False, f"wrong schema {raw.get('schema')!r}"
    if not raw.get("is_context_only"):
        return False, "is_context_only not True"
    asof = raw.get("as_of")
    if not asof:
        return False, "as_of absent"
    age = _age_days(asof)
    if age is None:
        return False, f"as_of unparseable: {asof!r}"
    if age > _STALE_DAYS:
        return False, f"stale: as_of={asof} age={age}d > {_STALE_DAYS}d"
    return True, "ok"


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #

def context() -> dict[str, Any]:
    """Return the cached artifact dict.  {} when absent / malformed / stale / wrong-schema.

    Result is cached for the lifetime of the process.  Call _reset_context_cache() to force
    a fresh read (tests, intraday refresh).
    """
    global _CACHE, _CACHE_LOADED
    if _CACHE_LOADED:
        return _CACHE or {}
    _CACHE_LOADED = True
    try:
        raw = _load_raw()
        if raw is None:
            _CACHE = {}
            return {}
        valid, reason = _validate(raw)
        if not valid:
            log.debug("neural_web_context: invalid artifact (%s)", reason)
            _CACHE = {}
            return {}
        _CACHE = raw
        return _CACHE
    except Exception as e:  # noqa: BLE001 — fail-soft: never raise into a build
        log.warning("neural_web_context: unexpected error loading context (%s)", e)
        _CACHE = {}
        return {}


def candidate(ticker: str) -> dict[str, Any]:
    """Return per-ticker advisory context dict; {} when not present or context absent."""
    try:
        c = context()
        if not c:
            return {}
        cc = c.get("candidate_context")
        if not isinstance(cc, dict):
            return {}
        row = cc.get(str(ticker).upper())
        return row if isinstance(row, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def market_plane() -> dict[str, Any]:
    """Return a compact dict for the neural_web market_view plane.

    Shape: {verdict, regime, vol, breadth, liquidity, contradiction_count, asof, stale}
    Returns an empty-stale dict if context is absent/stale.

    ``liquidity`` is distilled fail-soft from the market lobe's liquidity_plumbing block:
    {state, netliq_bn, tga_bn, tga_impulse} — every field None (tga_impulse None, not {})
    when the plumbing block is absent, so the shape is stable for pass-through consumers.
    """
    try:
        c = context()
        if not c:
            return {"stale": True, "asof": None}
        lobes = c.get("lobes") or {}
        market = lobes.get("market") or {}
        contradictions_lobe = lobes.get("contradictions") or {}
        asof = c.get("as_of")
        age = _age_days(asof)
        stale = (age is None) or (age > _STALE_DAYS)

        verdict_raw = market.get("verdict") or {}
        regime_raw = market.get("regime") or {}
        vol_raw = market.get("vol") or {}
        breadth_raw = market.get("breadth") or {}
        contr_summary = (contradictions_lobe.get("summary") or
                         market.get("contradictions") or {})

        # count contradiction records
        contr_count = 0
        try:
            recs = contradictions_lobe.get("records") or []
            contr_count = len(recs) if isinstance(recs, list) else 0
        except Exception:  # noqa: BLE001
            pass

        # W-TSY.1 — Treasury/TGA liquidity distillation (additive; stable shape).
        # Every access .get-chained with isinstance guards; an absent/malformed
        # liquidity_plumbing block degrades to all-None fields, never raises.
        _plumb = market.get("liquidity_plumbing")
        _plumb = _plumb if isinstance(_plumb, dict) else {}
        _headline = _plumb.get("headline")
        _headline = _headline if isinstance(_headline, dict) else {}
        _quantity = _plumb.get("quantity")
        _quantity = _quantity if isinstance(_quantity, dict) else {}
        _treasury = _plumb.get("treasury")
        _treasury = _treasury if isinstance(_treasury, dict) else {}
        _impulse = _treasury.get("tga_impulse")
        liquidity = {
            "state": _headline.get("state"),
            "netliq_bn": _quantity.get("netliq_bn"),
            "tga_bn": _treasury.get("tga_bn"),
            "tga_impulse": ({
                "direction": _impulse.get("direction"),
                "magnitude_bn": _impulse.get("magnitude_bn"),
                "quarter_end_adjacent": _impulse.get("quarter_end_adjacent"),
            } if isinstance(_impulse, dict) else None),
        }

        return {
            "verdict": verdict_raw,
            "regime": {
                "quad": regime_raw.get("quad"),
                "quad_name": regime_raw.get("quad_name"),
                "confidence": regime_raw.get("confidence"),
                "cycle_tag": regime_raw.get("cycle_tag"),
                "transition_state": regime_raw.get("transition_state"),
                "flip_margin": regime_raw.get("flip_margin"),
                "liquidity_overlay": regime_raw.get("liquidity_overlay"),
            },
            "vol": vol_raw,
            "breadth": breadth_raw,
            "liquidity": liquidity,
            "contradiction_count": contr_count,
            "contradiction_summary": contr_summary,
            "asof": asof,
            "stale": stale,
        }
    except Exception:  # noqa: BLE001
        return {"stale": True, "asof": None}


def seat_prompt_block(tickers: list[str], max_chars: int = 1200) -> str:
    """Return compact text lines suitable for prompt injection (bounded to max_chars).

    STRUCTURAL EXCLUSION: cortex memo text is NEVER included — this function reads
    only candidate_context rows and market-level regime/vol/breadth fields.
    It never touches lobes['cortex'] or any memo field.

    Returns empty string when context is absent/stale or flag is OFF.
    """
    try:
        c = context()
        if not c:
            return ""
        asof = c.get("as_of")
        age = _age_days(asof)
        stale = (age is None) or (age > _STALE_DAYS)
        if stale:
            return ""

        lobes = c.get("lobes") or {}
        market = lobes.get("market") or {}
        regime_raw = market.get("regime") or {}
        vol_raw = market.get("vol") or {}
        breadth_raw = market.get("breadth") or {}
        verdict_raw = market.get("verdict") or {}
        candidate_ctx = c.get("candidate_context") or {}

        lines: list[str] = []
        lines.append(f"NW asof={asof}")

        # market-level context
        quad_name = regime_raw.get("quad_name") or ""
        conf = regime_raw.get("confidence")
        cycle_tag = regime_raw.get("cycle_tag") or ""
        trans = regime_raw.get("transition_state") or ""
        liq = regime_raw.get("liquidity_overlay") or ""
        if quad_name or conf is not None:
            lines.append(
                f"Regime: {quad_name} conf={conf} cycle={cycle_tag} "
                f"transition={trans} liquidity={liq}"
            )

        verdict_en = verdict_raw.get("label_en") or verdict_raw.get("verdict") or ""
        if verdict_en:
            lines.append(f"NW verdict: {verdict_en}")

        breadth_label = breadth_raw.get("label_en") or breadth_raw.get("label") or ""
        vol_label = vol_raw.get("label_en") or vol_raw.get("label") or ""
        if breadth_label or vol_label:
            lines.append(f"Breadth: {breadth_label}  Vol: {vol_label}")

        # per-candidate rows — ONLY for tickers in the provided set, NO cortex
        norm_tickers = {t.upper() for t in tickers if t}
        cand_lines: list[str] = []
        for tkr, row in (candidate_ctx.items() if isinstance(candidate_ctx, dict) else []):
            if tkr.upper() not in norm_tickers:
                continue
            if not isinstance(row, dict):
                continue
            parts: list[str] = [tkr]
            bottom = row.get("bottom") or {}
            if bottom:
                bst = bottom.get("bottom_state") or bottom.get("state") or ""
                if bst:
                    parts.append(f"bottom={bst}")
            opts = row.get("options") or {}
            if opts:
                gate_status = opts.get("gate_status") or opts.get("status") or ""
                if gate_status:
                    parts.append(f"options={gate_status}")
            conflicts = row.get("graph_conflicts") or []
            if conflicts and isinstance(conflicts, list):
                parts.append(f"conflicts={len(conflicts)}")
            kernel = row.get("kernel") or {}
            if isinstance(kernel, dict) and kernel.get("fdr_cleared") is False:
                parts.append("kernel=display_armed_only")
            cand_lines.append(" ".join(parts))

        if cand_lines:
            lines.append("Candidates: " + "; ".join(cand_lines[:20]))

        result = "\n".join(lines)
        # hard bound — structural, not filtering
        if len(result) > max_chars:
            result = result[:max_chars]
        return result
    except Exception:  # noqa: BLE001 — fail-soft
        return ""


def audit_row() -> dict[str, Any]:
    """Return {status, asof, age_days, n_candidates, gap_notes_count} for runlog.

    status: 'present' | 'absent' | 'stale'
    This function is flag-independent — always runs to feed the perception runlog.
    """
    try:
        raw = _load_raw()
        if raw is None:
            return {"status": "absent", "asof": None, "age_days": None,
                    "n_candidates": 0, "gap_notes_count": 0}
        asof = raw.get("as_of")
        age = _age_days(asof)

        # check schema first
        if raw.get("schema") != _EXPECTED_SCHEMA or not raw.get("is_context_only") or not asof:
            return {"status": "absent", "asof": asof, "age_days": age,
                    "n_candidates": 0, "gap_notes_count": 0}

        if age is None or age > _STALE_DAYS:
            return {"status": "stale", "asof": asof, "age_days": age,
                    "n_candidates": 0, "gap_notes_count": 0}

        cc = raw.get("candidate_context") or {}
        n_cands = len(cc) if isinstance(cc, dict) else 0
        gap_notes = raw.get("gap_notes") or []
        n_gaps = len(gap_notes) if isinstance(gap_notes, list) else 0

        return {
            "status": "present",
            "asof": asof,
            "age_days": age,
            "n_candidates": n_cands,
            "gap_notes_count": n_gaps,
        }
    except Exception:  # noqa: BLE001
        return {"status": "absent", "asof": None, "age_days": None,
                "n_candidates": 0, "gap_notes_count": 0}

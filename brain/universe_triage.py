"""brain/universe_triage.py — the whole-universe verdict organ (A4, roadmap §1 Group A / §2).

WHY THIS MODULE EXISTS
----------------------
Candidacy today is ~90% momentum-sourced; there is no whole-universe pass that composes the
perception layer into a single, actionable per-sector read. This organ is that pass. It COMPOSES
the existing perception readers — it never re-derives their science — into one per-sector (and
per-theme) verdict:

    { XLV: {phase, osc_slope, entry_favored, late_cycle, tensor_level, tensor_accel,
            nw_stance|null, rotation_in, action: favor|neutral|reduce, why[...]}, ... }

Downstream consumers (NOT wired yet — this is the PRODUCER only): intake weighting (±0.05),
conviction lane filters, leadership reduce-sector suppression, and the strategist / SENTINEL / PM
payloads. Until a consumer wires it, this module is WRITE-ONLY: it produces an artifact + a reader
and changes ZERO trading behaviour.

THE COMPOSITION (never a reinvention)
-------------------------------------
* regime_frame.cycles()  → phase / osc_slope / entry_favored / late_cycle per GICS sector (+SMH).
                           This is ALREADY freshness-gated (>5 trading days → {}); we consume its
                           output, never the JSON.
* rotation_tensor        → per-sector RS level / acceleration (bps/day) + the headline episode.
                           Read from the published artifact (data/market_view/rotation_tensor.json),
                           freshness-gated here; never recomputed.
* neural_web_context     → the per-sector/market NW stance, gated behind nw_decision_mode(). When
                           the NW mode is off or the data is absent, nw_stance is null and it does
                           NOT affect the action (the flag-independent invariant).
* etf_pulse (site/basketdata/etf_pulse.json) → the sector-level momentum pulse (leaders/laggards),
                           read fail-soft, used ONLY to populate `sources_fresh.etf_pulse` for now.

THE INVARIANT (governs every path — mirrors regime_frame / rotation_intake / neural_web_context)
------------------------------------------------------------------------------------------------
FAIL-SOFT everywhere: never raise; a missing input → the corresponding field is null; MISSING DATA
DEGRADES TOWARD NEUTRAL. We never manufacture a 'favor' OR a 'reduce' out of absence — an absent
tensor, an absent NW read, or an absent cycle read can only ever leave a sector at "neutral". Only
AFFIRMATIVE evidence moves a sector off neutral, and even then only through the two documented
rules below.

THE ACTION RULESET (all fail-soft)
----------------------------------
* action = "favor"   when entry_favored AND osc_slope > 0 (a bottoming+rising sector), UNLESS
                      contradicted by strongly-negative tensor acceleration (accel < -_STRONG_NEG_ACCEL).
* action = "reduce"  when late_cycle AND (osc_slope < 0 OR tensor_accel < 0) — a topping+rolling sector.
* action = "neutral" otherwise — including EVERY degraded / unmapped / quiet sector.

`nw_stance` is informational in this producer: it is recorded on the row (when the NW decision mode
is armed and a stance is available) but it does NOT participate in the action rule (subtract/vote
authority for NW is owned by neural_web_context.decision_signals + its own flag ladder — this organ
does not duplicate it). A later consumer step may fold nw_stance into weighting; here it is a tell.

PUBLIC API
----------
* assemble(asof=None) -> dict
      Compose the full artifact (sectors{}, headline_episode, sources_fresh{}, as_of, ...). Never
      raises; degrades every sector toward neutral on missing input.
* write_artifact(art) -> Path
      Atomic (tmp → os.replace) write to data/universe_triage/latest.json. Fail-soft.
* verdicts() -> dict
      THE sole reader: load latest.json, freshness-gate, {} on absent / stale / malformed. Process-
      cached; call _reset_cache() to force a fresh read (tests / intraday).
* sector_action(sector) -> str
      Convenience reader off verdicts(): the action for one sector, or 'neutral' when unmapped/absent.
* favored_sectors() / reduce_sectors() -> list[str]
      Convenience readers off verdicts(): the sectors currently at 'favor' / 'reduce'.
* audit_row() -> dict
      {status, as_of, n_sectors, sources_fresh} for the perception runlog. Flag-independent.
* _reset_cache() -> None
      Explicit cache reset for tests.

STALENESS
---------
The verdicts() reader treats an artifact older than _STALE_DAYS (5 calendar days — a safe, tighter
proxy for the ~5-trading-day cycle budget) as absent-stale and returns {}. An unparseable / missing
as_of is likewise stale. This is the same fail-closed discipline as the rest of the perception layer:
stale data degrades to a no-op ({}), never to an affirmative read.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_ROOT: Path = Path(__file__).resolve().parent.parent

# ── artifact location (bot-side data plane; atomic tmp→replace) ────────────────────────────────
_ARTIFACT_DIR: Path = _ROOT / "data" / "universe_triage"
_ARTIFACT_PATH: Path = _ARTIFACT_DIR / "latest.json"

# The rotation_tensor artifact — an OPTIONAL input (never a hard dependency; absent → tensor null).
_TENSOR_PATH: Path = _ROOT / "data" / "market_view" / "rotation_tensor.json"

# etf_pulse — read only to populate sources_fresh.etf_pulse for now (a coverage tell for consumers).
_ETF_PULSE_PATH: Path = _ROOT / "vendor" / "macro" / "site" / "basketdata" / "etf_pulse.json"

_SCHEMA = "universe_triage.v1"
_STALE_DAYS = 5  # calendar-day proxy for the ~5-trading-day cycle freshness budget.

# The 11 GICS SPDR sector ETFs + the semis bloc — the universe cycles()/rotation_tensor key on.
# Mirrors rotation_tensor.UNIVERSE ordering so the two organs agree on the sector set.
_SECTOR_UNIVERSE: tuple[str, ...] = (
    "XLB", "XLC", "XLE", "XLF", "XLI",
    "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
    "SMH",
)

# The magnitude, in bps/day, at which a NEGATIVE tensor acceleration is "strong" enough to VETO a
# favor call (a bottoming+rising cycle read that the tape is aggressively fading). Unverified prior;
# deliberately conservative — only a clearly-negative accel cancels a favor, never a marginal one.
_STRONG_NEG_ACCEL: float = 5.0  # bps/day

# --------------------------------------------------------------------------- #
# process-level cache — reset via _reset_cache() for tests (mirrors rotation_intake)
# --------------------------------------------------------------------------- #
_CACHE: Optional[dict] = None   # None = not yet loaded; {} = empty/absent/stale/invalid
_CACHE_LOADED: bool = False


def _reset_cache() -> None:
    """Invalidate the per-process verdicts cache. Tests MUST call this around fixtures."""
    global _CACHE, _CACHE_LOADED
    _CACHE = None
    _CACHE_LOADED = False


# --------------------------------------------------------------------------- #
# internal helpers
# --------------------------------------------------------------------------- #

def _age_days(asof_str: Any) -> Optional[int]:
    """Calendar days since asof_str (YYYY-MM-DD). None if absent/unparseable — never treated fresh."""
    if not asof_str:
        return None
    try:
        asof_date = date.fromisoformat(str(asof_str)[:10])
        return (date.today() - asof_date).days
    except Exception:  # noqa: BLE001
        return None


def _as_float(v: Any) -> Optional[float]:
    """Coerce to float, or None on any failure (missing/None/non-numeric)."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _read_cycles() -> dict[str, dict[str, Any]]:
    """Read regime_frame.cycles() fail-soft. {} on any error (already freshness-gated upstream)."""
    try:
        from brain import regime_frame
        cyc = regime_frame.cycles()
        return cyc if isinstance(cyc, dict) else {}
    except Exception:  # noqa: BLE001 — a cycles read failure degrades every sector toward neutral
        return {}


def _read_tensor() -> dict[str, Any]:
    """Read the rotation_tensor artifact iff present + fresh. {} otherwise (source simply absent).

    Returns the flattened planes this organ needs:
      {level: {ticker: float|None}, accel: {ticker: float|None},
       headline_episode: dict|None, as_of: str|None}
    A missing / stale / malformed tensor → {} — the per-sector tensor_* fields then degrade to None
    (which can only ever leave a sector neutral, never manufacture favor/reduce).
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
        return {}  # stale/undated tensor is absent, never trusted
    rs_vel = raw.get("rs_velocity")
    level = rs_vel.get("level_bps_per_day") if isinstance(rs_vel, dict) else None
    accel = rs_vel.get("accel_bps_per_day") if isinstance(rs_vel, dict) else None
    return {
        "level": level if isinstance(level, dict) else {},
        "accel": accel if isinstance(accel, dict) else {},
        "headline_episode": raw.get("headline_episode"),
        "as_of": raw.get("as_of"),
    }


def _read_etf_pulse_fresh() -> bool:
    """True iff etf_pulse.json is present and parseable with a sector block. Best-effort coverage tell.

    Used ONLY to populate sources_fresh.etf_pulse for now — it does not (yet) drive any action.
    Never raises; any miss → False.
    """
    try:
        if not _ETF_PULSE_PATH.exists():
            return False
        raw = json.loads(_ETF_PULSE_PATH.read_text())
        return isinstance(raw, dict) and bool(raw.get("sector"))
    except Exception:  # noqa: BLE001
        return False


def _nw_stance_for(sector: str, *, nw_on: bool) -> Optional[str]:
    """Return the NW stance for a sector ETF, or None when NW is off / absent / undeterminable.

    Flag-gated: when the NW decision mode is 'off' (nw_on False) this ALWAYS returns None and never
    touches the NW reader — so NW cannot affect the artifact when its ladder is disarmed. When armed,
    we surface the per-name decision-signal lean as a coarse stance string:
      * candidacy present (lean +1) → 'favor'
      * clean_in_conflicted (safe-haven tell) → 'clean'
      * otherwise → None (no affirmative stance; NEVER a fabricated 'reduce' from absence).
    This is a TELL recorded on the row; it does NOT participate in the action rule (that authority is
    owned by neural_web_context's own flag ladder). Fail-soft → None on any error.
    """
    if not nw_on:
        return None
    try:
        from brain import neural_web_context as nw
        sig = nw.decision_signals(sector)
        if not isinstance(sig, dict) or sig.get("inert"):
            return None
        cand = sig.get("candidacy")
        if isinstance(cand, dict) and cand.get("lean") == 1:
            return "favor"
        if sig.get("clean_in_conflicted") is True:
            return "clean"
        return None
    except Exception:  # noqa: BLE001 — NW is optional; any failure leaves the stance absent
        return None


def _nw_decision_on() -> bool:
    """True iff the NW decision ladder is armed at 'shadow' or above (else NW is fully disarmed).

    Reads neural_web_context.nw_decision_mode() (default 'off'). At 'off' the NW stance is never read.
    Fail-soft → False (disarmed) on any error.
    """
    try:
        from brain import neural_web_context as nw
        return nw.nw_decision_mode() != "off"
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# the action rule (pure — takes the composed per-sector fields, returns action + why)
# --------------------------------------------------------------------------- #

def _decide_action(
    *,
    entry_favored: Optional[bool],
    late_cycle: Optional[bool],
    osc_slope: Optional[float],
    tensor_accel: Optional[float],
) -> tuple[str, list[str]]:
    """Return (action, why[]) from the composed fields. Pure + fail-soft (missing → neutral).

    THE RULESET (see module docstring):
      * favor   — entry_favored AND osc_slope > 0, UNLESS tensor_accel < -_STRONG_NEG_ACCEL.
      * reduce  — late_cycle AND (osc_slope < 0 OR tensor_accel < 0).
      * neutral — otherwise (including every degraded / absent input).

    Precedence: `reduce` is evaluated FIRST (a topping+rolling sector is the risk-management read and
    must win over any stale favor tell). Absent inputs never satisfy either rule — they fall through
    to neutral, so absence can never manufacture a favor OR a reduce.
    """
    why: list[str] = []

    # --- reduce (evaluated first: the protective read wins) --- #
    # late_cycle is already a 3-way-AND'd bool from cycles() (phase∈{Peak,Downturn} ∧ slope<0 ∧ pos≥70).
    if late_cycle is True:
        rolling = (osc_slope is not None and osc_slope < 0)
        distributing = (tensor_accel is not None and tensor_accel < 0)
        if rolling or distributing:
            why.append("late_cycle")
            if rolling:
                why.append(f"osc_slope<0 ({osc_slope:.4g})")
            if distributing:
                why.append(f"tensor_accel<0 ({tensor_accel:.4g})")
            return "reduce", why

    # --- favor --- #
    if entry_favored is True and osc_slope is not None and osc_slope > 0:
        # Contradiction guard: a strongly-negative tensor acceleration cancels the favor (the tape is
        # aggressively fading the bottoming cycle read). A missing/marginal accel never cancels it.
        if tensor_accel is not None and tensor_accel < -_STRONG_NEG_ACCEL:
            why.append("entry_favored")
            why.append(f"osc_slope>0 ({osc_slope:.4g})")
            why.append(f"tensor_accel strongly negative ({tensor_accel:.4g}) — favor vetoed")
            return "neutral", why
        why.append("entry_favored")
        why.append(f"osc_slope>0 ({osc_slope:.4g})")
        if tensor_accel is not None and tensor_accel > 0:
            why.append(f"tensor_accel>0 ({tensor_accel:.4g})")
        return "favor", why

    return "neutral", why


# --------------------------------------------------------------------------- #
# assemble() — compose the full artifact
# --------------------------------------------------------------------------- #

def assemble(asof: Optional[str] = None) -> dict[str, Any]:
    """Compose the per-sector universe-triage verdict. Never raises — degrades toward neutral.

    Parameters
    ----------
    asof : optional YYYY-MM-DD to stamp on the artifact. When None, the true data date is derived
           from the cycle/tensor reads if available, else today.

    Returns the full artifact dict::

        {
          "schema": "universe_triage.v1",
          "as_of": str,
          "sectors": { "XLV": {phase, osc_slope, entry_favored, late_cycle,
                               tensor_level, tensor_accel, nw_stance, rotation_in,
                               action, why[]}, ... },
          "headline_episode": dict|None,   # from rotation_tensor
          "sources_fresh": {cycles, tensor, nw, etf_pulse},  # what was available this build
          "nw_decision_mode": str,
        }

    Every sector in _SECTOR_UNIVERSE is emitted (never dropped) so consumers can iterate a stable
    key set; a sector with no cycle/tensor data lands at action='neutral' with null fields.
    """
    try:
        cyc = _read_cycles()
        tensor = _read_tensor()
        nw_on = _nw_decision_on()
        etf_pulse_fresh = _read_etf_pulse_fresh()

        tensor_level = tensor.get("level") if isinstance(tensor.get("level"), dict) else {}
        tensor_accel = tensor.get("accel") if isinstance(tensor.get("accel"), dict) else {}

        sectors: dict[str, dict[str, Any]] = {}
        for sec in _SECTOR_UNIVERSE:
            row = cyc.get(sec) if isinstance(cyc.get(sec), dict) else {}

            phase = row.get("phase")
            osc_slope = _as_float(row.get("osc_slope"))
            entry_favored = row.get("entry_favored")
            entry_favored = bool(entry_favored) if entry_favored is not None else None
            late_cycle = row.get("late_cycle")
            late_cycle = bool(late_cycle) if late_cycle is not None else None

            t_level = _as_float(tensor_level.get(sec))
            t_accel = _as_float(tensor_accel.get(sec))

            nw_stance = _nw_stance_for(sec, nw_on=nw_on)

            action, why = _decide_action(
                entry_favored=entry_favored,
                late_cycle=late_cycle,
                osc_slope=osc_slope,
                tensor_accel=t_accel,
            )

            # rotation_in: an affirmative "the tape is rotating INTO this sector" tell — favor action
            # with a confirming (>0) tensor acceleration. Absent tensor → False (never fabricated).
            rotation_in = bool(action == "favor" and t_accel is not None and t_accel > 0)

            sectors[sec] = {
                "phase": phase,
                "osc_slope": osc_slope,
                "entry_favored": entry_favored,
                "late_cycle": late_cycle,
                "tensor_level": t_level,
                "tensor_accel": t_accel,
                "nw_stance": nw_stance,
                "rotation_in": rotation_in,
                "action": action,
                "why": why,
            }

        # true as_of: prefer the caller's asof, else the newest input date, else today.
        as_of = str(asof)[:10] if asof else (tensor.get("as_of") or date.today().isoformat())

        return {
            "schema": _SCHEMA,
            "as_of": as_of,
            "sectors": sectors,
            "headline_episode": tensor.get("headline_episode"),
            "sources_fresh": {
                "cycles": bool(cyc),
                "tensor": bool(tensor),
                "nw": bool(nw_on),
                "etf_pulse": bool(etf_pulse_fresh),
            },
            "nw_decision_mode": _nw_mode_str(),
        }
    except Exception as e:  # noqa: BLE001 — never raise into a build; emit a minimal neutral artifact
        log.warning("universe_triage: assemble failed (%s) — emitting neutral artifact", e)
        return _degrade_artifact(asof)


def _nw_mode_str() -> str:
    """The NW decision mode string for the artifact (best-effort; 'off' on any error)."""
    try:
        from brain import neural_web_context as nw
        return nw.nw_decision_mode()
    except Exception:  # noqa: BLE001
        return "off"


def _degrade_artifact(asof: Optional[str]) -> dict[str, Any]:
    """A minimal all-neutral artifact for the total-failure path (never raises).

    Every sector present, every field null, every action 'neutral' — the honest degraded read.
    """
    sectors = {
        sec: {
            "phase": None, "osc_slope": None, "entry_favored": None, "late_cycle": None,
            "tensor_level": None, "tensor_accel": None, "nw_stance": None,
            "rotation_in": False, "action": "neutral", "why": [],
        }
        for sec in _SECTOR_UNIVERSE
    }
    return {
        "schema": _SCHEMA,
        "as_of": (str(asof)[:10] if asof else date.today().isoformat()),
        "sectors": sectors,
        "headline_episode": None,
        "sources_fresh": {"cycles": False, "tensor": False, "nw": False, "etf_pulse": False},
        "nw_decision_mode": "off",
    }


# --------------------------------------------------------------------------- #
# write_artifact() — atomic output
# --------------------------------------------------------------------------- #

def write_artifact(artifact: dict[str, Any]) -> Optional[Path]:
    """Atomically write the artifact to data/universe_triage/latest.json (tmp → os.replace).

    Fail-soft: returns the path on success, None on any error (a failed write never raises into a
    build — the organ is observability-only, a missing artifact just means the reader returns {}).
    """
    try:
        _ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(artifact, indent=2, default=str)
        fd, tmp_path = tempfile.mkstemp(dir=_ARTIFACT_DIR, prefix=".universe_triage_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
            os.replace(tmp_path, _ARTIFACT_PATH)
        except Exception:  # noqa: BLE001
            try:
                os.unlink(tmp_path)
            except Exception:  # noqa: BLE001
                pass
            raise
        return _ARTIFACT_PATH
    except Exception as e:  # noqa: BLE001 — fail-soft: a write failure is a no-op, never a raise
        log.warning("universe_triage: write_artifact failed (%s)", e)
        return None


# --------------------------------------------------------------------------- #
# verdicts() — the sole reader
# --------------------------------------------------------------------------- #

def verdicts() -> dict[str, Any]:
    """Return the last published, fresh universe-triage artifact, or {} in every degraded state.

    THE SOLE READER of data/universe_triage/latest.json. {} when absent / stale (> _STALE_DAYS) /
    malformed / wrong-schema / undated. {} is the honest "no verdict this build" read — consumers
    must treat it as "no signal", never as an affirmative all-neutral (they iterate `.get('sectors')`
    which is absent, so every sector reads as unmapped = neutral by omission).

    Process-cached (like neural_web_context.context() / rotation_intake.calls()); call _reset_cache()
    to force a fresh read. Never raises.
    """
    global _CACHE, _CACHE_LOADED
    if _CACHE_LOADED:
        return dict(_CACHE or {})
    _CACHE_LOADED = True
    try:
        if not _ARTIFACT_PATH.exists():
            _CACHE = {}
            return {}
        raw = json.loads(_ARTIFACT_PATH.read_text())
        if not isinstance(raw, dict):
            _CACHE = {}
            return {}
        if raw.get("schema") != _SCHEMA:
            log.debug("universe_triage: wrong schema %r → {}", raw.get("schema"))
            _CACHE = {}
            return {}
        age = _age_days(raw.get("as_of"))
        if age is None or age > _STALE_DAYS:
            log.debug("universe_triage: stale/undated as_of=%r → {}", raw.get("as_of"))
            _CACHE = {}
            return {}
        if not isinstance(raw.get("sectors"), dict):
            _CACHE = {}
            return {}
        _CACHE = raw
        return dict(raw)
    except Exception as e:  # noqa: BLE001 — fail-soft: never raise into a build
        log.warning("universe_triage: verdicts read failed (%s)", e)
        _CACHE = {}
        return {}


# --------------------------------------------------------------------------- #
# convenience readers (off verdicts())
# --------------------------------------------------------------------------- #

def sector_action(sector: str) -> str:
    """Return the action for one sector ('favor'|'neutral'|'reduce'), or 'neutral' when unmapped/absent.

    Fail-soft: an absent artifact, an unknown sector, or a malformed row all degrade to 'neutral'
    (the safe, non-affirmative default). Never raises.
    """
    if not sector:
        return "neutral"
    try:
        v = verdicts()
        secs = v.get("sectors") if isinstance(v, dict) else None
        if not isinstance(secs, dict):
            return "neutral"
        row = secs.get(str(sector).upper())
        act = row.get("action") if isinstance(row, dict) else None
        return act if act in ("favor", "neutral", "reduce") else "neutral"
    except Exception:  # noqa: BLE001
        return "neutral"


def _sectors_with_action(action: str) -> list[str]:
    """The sorted list of sector tickers currently at `action`, or [] when absent. Fail-soft."""
    try:
        v = verdicts()
        secs = v.get("sectors") if isinstance(v, dict) else None
        if not isinstance(secs, dict):
            return []
        out = [tk for tk, row in secs.items()
               if isinstance(row, dict) and row.get("action") == action]
        return sorted(out)
    except Exception:  # noqa: BLE001
        return []


def favored_sectors() -> list[str]:
    """The sectors currently at action='favor' (sorted), or [] when the artifact is absent/stale."""
    return _sectors_with_action("favor")


def reduce_sectors() -> list[str]:
    """The sectors currently at action='reduce' (sorted), or [] when the artifact is absent/stale."""
    return _sectors_with_action("reduce")


# --------------------------------------------------------------------------- #
# audit_row() — perception runlog
# --------------------------------------------------------------------------- #

def audit_row() -> dict[str, Any]:
    """Return {status, as_of, n_sectors, sources_fresh} for the runlog. Flag-independent, never raises.

    status ∈ {present, absent, stale}:
      * present — a fresh, valid artifact with a sectors block.
      * stale   — an artifact exists with the right schema but its as_of is older than _STALE_DAYS.
      * absent  — no artifact, wrong/absent schema, undated, or unparseable.
    """
    try:
        if not _ARTIFACT_PATH.exists():
            return {"status": "absent", "as_of": None, "n_sectors": 0, "sources_fresh": {}}
        raw = json.loads(_ARTIFACT_PATH.read_text())
        if not isinstance(raw, dict) or raw.get("schema") != _SCHEMA:
            return {"status": "absent", "as_of": None, "n_sectors": 0, "sources_fresh": {}}
        as_of = raw.get("as_of")
        age = _age_days(as_of)
        secs = raw.get("sectors")
        n_sectors = len(secs) if isinstance(secs, dict) else 0
        sources_fresh = raw.get("sources_fresh") if isinstance(raw.get("sources_fresh"), dict) else {}
        if age is None or age > _STALE_DAYS:
            return {"status": "stale", "as_of": as_of, "n_sectors": n_sectors,
                    "sources_fresh": sources_fresh}
        return {"status": "present", "as_of": as_of, "n_sectors": n_sectors,
                "sources_fresh": sources_fresh}
    except Exception:  # noqa: BLE001
        return {"status": "absent", "as_of": None, "n_sectors": 0, "sources_fresh": {}}


# --------------------------------------------------------------------------- #
# CLI entry point (for the nightly build pipeline — write-only, no consumer wired)
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    asof_arg = sys.argv[1] if len(sys.argv) > 1 else None
    art = assemble(asof=asof_arg)
    out = write_artifact(art)
    n_favor = len(favored_sectors()) if out else 0
    print(f"universe_triage written: {out}  as_of={art['as_of']}  "
          f"sectors={len(art['sectors'])}  sources_fresh={art['sources_fresh']}")

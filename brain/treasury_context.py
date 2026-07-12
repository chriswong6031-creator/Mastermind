"""brain/treasury_context.py — single reader for the Treasury Watch → Mastermind bridge (W-TW.1).

Reads ONLY vendor/macro/site/whdata/treasury_watch.json.  Fail-soft everywhere:
absent / malformed / stale / wrong-schema → stable empty context object, never raises.
Never imports Macro engine modules.

PUBLIC API
----------
* context()            — cached-per-process full artifact dict; {} when absent/stale/invalid.
* snapshot()           — compact flag-independent dict for payload embedding.
* seat_prompt_block(max_chars=700) — compact deterministic text for prompt injection
                          (NO event/impulse prose — see STRUCTURAL EXCLUSION below).
* audit_row()          — {status, asof, age_days, n_events, headline_state}.
* enabled()            — reads MASTERMIND_TREASURY_CONTEXT; default OFF.
* _reset_context_cache() — explicit cache reset for tests.

FLAG: MASTERMIND_TREASURY_CONTEXT defaults OFF (dark ship).  Reader + audit rows are
flag-independent; only prompt injection is gated by callers via enabled().

STALENESS: as_of age > 5 calendar days → treat as absent-stale (TGA data lags
weekends/holidays; 5 days spans a long weekend without going blind for a week).

STRUCTURAL EXCLUSION: LLM/builder-generated prose (event "summary"/"analysis"-like
fields, tga_impulse.summary_en/summary_zh, plumbing.headline_summary_*) is NEVER read
by seat_prompt_block() or snapshot() — only numbers plus the top event's banner_title
(hard-capped at 140 chars), tone and importance may reach a prompt.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
# Follow intake._read convention: _V = repo_root / "vendor" / "macro"
_V = _ROOT / "vendor" / "macro"
_ARTIFACT_PATH = _V / "site" / "whdata" / "treasury_watch.json"

_EXPECTED_SCHEMA = "treasury_watch.v1"
_STALE_DAYS = 5   # calendar days — TGA data lags weekends/holidays
_TITLE_CAP = 140  # hard cap on the one prose-adjacent field (banner_title) in prompts

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

def enabled() -> bool:
    """Return True iff MASTERMIND_TREASURY_CONTEXT is set to 1/true/yes (default OFF)."""
    return os.environ.get("MASTERMIND_TREASURY_CONTEXT", "0").strip().lower() in ("1", "true", "yes")


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


def _num(x: Any) -> float | None:
    """Coerce to float; None on any failure (fail-soft on malformed numeric fields)."""
    try:
        if x is None or isinstance(x, bool):
            return None
        return float(x)
    except Exception:  # noqa: BLE001
        return None


def _ord(n: int) -> str:
    """84 -> '84th', 1 -> '1st' — deterministic English ordinal."""
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def _load_raw() -> dict[str, Any] | None:
    """Read and JSON-parse the artifact.  Returns None on any IO/parse error."""
    try:
        if not _ARTIFACT_PATH.exists():
            return None
        return json.loads(_ARTIFACT_PATH.read_text())
    except Exception as e:  # noqa: BLE001
        log.debug("treasury_context: read failed (%s)", e)
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


def _top_event(c: dict[str, Any]) -> dict[str, Any] | None:
    """Return {title, tone, importance} for the highest-importance LIVE event, or None.

    STRUCTURAL EXCLUSION: reads ONLY banner_title / tone / importance / live from each
    event row — never summary/analysis prose fields.  Unknown extra fields in event
    rows are tolerated and never leak.  banner_title is hard-capped at _TITLE_CAP chars.
    """
    try:
        events = c.get("events")
        if not isinstance(events, list):
            return None
        best: dict[str, Any] | None = None
        best_imp = -1
        for ev in events:
            if not isinstance(ev, dict) or not ev.get("live"):
                continue
            try:
                imp = int(ev.get("importance") or 0)
            except Exception:  # noqa: BLE001
                imp = 0
            if imp > best_imp:
                best_imp = imp
                best = ev
        if best is None:
            return None
        title = str(best.get("banner_title") or "")[:_TITLE_CAP]
        return {"title": title, "tone": best.get("tone"), "importance": best.get("importance")}
    except Exception:  # noqa: BLE001
        return None


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
            log.debug("treasury_context: invalid artifact (%s)", reason)
            _CACHE = {}
            return {}
        _CACHE = raw
        return _CACHE
    except Exception as e:  # noqa: BLE001 — fail-soft: never raise into a build
        log.warning("treasury_context: unexpected error loading context (%s)", e)
        _CACHE = {}
        return {}


def snapshot() -> dict[str, Any]:
    """Return a compact flag-independent dict for payload embedding.

    Shape: {tga_bn, tga_asof, chg_1d_bn, chg_5d_bn, chg_since_quarter_end_bn,
            quarter_end_date, netliq_bn, netliq_chg_20d_bn, netliq_pctile_expanding,
            headline_state, rrp_buffer_state, impulse|None, top_event|None, asof, stale}
    Returns {"stale": True, "asof": None} when context is absent/stale.
    """
    try:
        c = context()
        if not c:
            return {"stale": True, "asof": None}
        asof = c.get("as_of")
        age = _age_days(asof)
        stale = (age is None) or (age > _STALE_DAYS)

        tga = c.get("tga") if isinstance(c.get("tga"), dict) else {}
        nl = c.get("net_liquidity") if isinstance(c.get("net_liquidity"), dict) else {}
        pl = c.get("plumbing") if isinstance(c.get("plumbing"), dict) else {}
        qe = tga.get("quarter_end") if isinstance(tga.get("quarter_end"), dict) else {}

        imp_raw = c.get("tga_impulse")
        impulse: dict[str, Any] | None = None
        if isinstance(imp_raw, dict) and imp_raw.get("active"):
            impulse = {
                "direction": imp_raw.get("direction"),
                "magnitude_bn": imp_raw.get("magnitude_bn"),
                "days": imp_raw.get("days"),
                "quarter_end_adjacent": bool(imp_raw.get("quarter_end_adjacent")),
            }

        return {
            "tga_bn": tga.get("level_bn"),
            "tga_asof": tga.get("asof"),
            "chg_1d_bn": tga.get("chg_1d_bn"),
            "chg_5d_bn": tga.get("chg_5d_bn"),
            "chg_since_quarter_end_bn": qe.get("chg_since_bn"),
            "quarter_end_date": qe.get("date"),
            "netliq_bn": nl.get("netliq_bn"),
            "netliq_chg_20d_bn": nl.get("chg_20d_bn"),
            "netliq_pctile_expanding": nl.get("pctile_expanding"),
            "headline_state": pl.get("headline_state"),
            "rrp_buffer_state": pl.get("rrp_buffer_state"),
            "impulse": impulse,
            "top_event": _top_event(c),
            "asof": asof,
            "stale": stale,
        }
    except Exception:  # noqa: BLE001
        return {"stale": True, "asof": None}


def seat_prompt_block(max_chars: int = 700) -> str:
    """Return compact text lines suitable for prompt injection (bounded to max_chars).

    STRUCTURAL EXCLUSION: event/impulse/plumbing prose is NEVER included — this function
    reads only numeric fields, state enums, and at most the top event's banner_title
    (hard-capped at 140 chars upstream and here), tone and importance.  It never touches
    any "summary"/"analysis"-like field.

    Returns empty string when context is absent/stale.
    """
    try:
        c = context()
        if not c:
            return ""
        asof = c.get("as_of")
        age = _age_days(asof)
        if age is None or age > _STALE_DAYS:
            return ""

        tga = c.get("tga") if isinstance(c.get("tga"), dict) else {}
        nl = c.get("net_liquidity") if isinstance(c.get("net_liquidity"), dict) else {}
        pl = c.get("plumbing") if isinstance(c.get("plumbing"), dict) else {}
        qe = tga.get("quarter_end") if isinstance(tga.get("quarter_end"), dict) else {}

        lines: list[str] = [f"TREASURY asof={asof} (TGA data)"]

        # TGA line — numbers only, mechanism suffix is a deterministic template
        level = _num(tga.get("level_bn"))
        if level is not None:
            parts = [f"TGA ${level:.1f}bn"]
            chg5 = _num(tga.get("chg_5d_bn"))
            if chg5 is not None:
                parts.append(f"Δ5d {chg5:+.1f}")
            chg_qe = _num(qe.get("chg_since_bn"))
            if chg_qe is not None:
                qd = str(qe.get("date") or "")[5:10]
                parts.append(f"Δ since Q-end({qd}) {chg_qe:+.1f}")
            line = " ".join(parts)
            if chg_qe is not None:
                if chg_qe <= -50:
                    line += " -> cash into bank reserves (mechanical liquidity injection)"
                elif chg_qe >= 50:
                    line += " -> cash drained from reserves"
            lines.append(line)

        # NetLiq / plumbing line
        segs: list[str] = []
        netliq = _num(nl.get("netliq_bn"))
        if netliq is not None:
            s = f"NetLiq ${netliq:.0f}bn"
            chg20 = _num(nl.get("chg_20d_bn"))
            if chg20 is not None:
                s += f" Δ20d {chg20:+.1f}"
            pct = _num(nl.get("pctile_expanding"))
            if pct is not None:
                p = pct * 100.0 if 0.0 <= pct <= 1.0 else pct
                s += f" ({_ord(int(round(p)))} pctile expanding)"
            segs.append(s)
        rrp_state = pl.get("rrp_buffer_state")
        if rrp_state:
            segs.append(f"RRP buffer: {rrp_state}")
        headline = pl.get("headline_state")
        if headline:
            segs.append(f"plumbing: {headline}")
        if segs:
            lines.append(" | ".join(segs))

        # Impulse line — numeric fields only, NEVER summary_en/summary_zh
        imp = c.get("tga_impulse")
        if isinstance(imp, dict) and imp.get("active"):
            s = f"Impulse: {imp.get('direction') or 'impulse'}"
            mag = _num(imp.get("magnitude_bn"))
            if mag is not None:
                s += f" {mag:+.1f}bn"
            if imp.get("days") is not None:
                s += f"/{imp.get('days')}bd"
            if imp.get("since"):
                s += f" since {imp.get('since')}"
            if imp.get("quarter_end_adjacent"):
                s += " (quarter-end adjacent)"
            lines.append(s)

        # Event line — banner_title (capped) + tone + importance only
        top = _top_event(c)
        if top is not None:
            title = str(top.get("title") or "")[:_TITLE_CAP]
            lines.append(f"Event: {title} (tone={top.get('tone')} imp={top.get('importance')})")

        result = "\n".join(lines)
        # hard bound — structural, not filtering
        if len(result) > max_chars:
            result = result[:max_chars]
        return result
    except Exception:  # noqa: BLE001 — fail-soft
        return ""


def audit_row() -> dict[str, Any]:
    """Return {status, asof, age_days, n_events, headline_state} for runlog.

    status: 'present' | 'absent' | 'stale'
    This function is flag-independent — always runs to feed the perception runlog.
    """
    try:
        raw = _load_raw()
        if raw is None or not isinstance(raw, dict):
            return {"status": "absent", "asof": None, "age_days": None,
                    "n_events": 0, "headline_state": None}
        asof = raw.get("as_of")
        age = _age_days(asof)

        # check schema first
        if raw.get("schema") != _EXPECTED_SCHEMA or not raw.get("is_context_only") or not asof:
            return {"status": "absent", "asof": asof, "age_days": age,
                    "n_events": 0, "headline_state": None}

        if age is None or age > _STALE_DAYS:
            return {"status": "stale", "asof": asof, "age_days": age,
                    "n_events": 0, "headline_state": None}

        events = raw.get("events") or []
        n_events = len(events) if isinstance(events, list) else 0
        pl = raw.get("plumbing") if isinstance(raw.get("plumbing"), dict) else {}

        return {
            "status": "present",
            "asof": asof,
            "age_days": age,
            "n_events": n_events,
            "headline_state": pl.get("headline_state"),
        }
    except Exception:  # noqa: BLE001
        return {"status": "absent", "asof": None, "age_days": None,
                "n_events": 0, "headline_state": None}

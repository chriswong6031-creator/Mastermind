"""brain/regime_frame.py — the single regime reader (architecture Stage 1, W1).

WHY THIS MODULE EXISTS
----------------------
Before W1 every bot that needed the macro regime had its own private ``_regime_dict()``
copy pasted from the same three-liner.  Five copies × two fields each = ten places where
the contract between the macro-dashboard JSON and the Brain prompts could silently drift.
The architecture mandates ONE reader; this is it.

INVARIANT (governs all paths here)
-----------------------------------
Missing / stale / corrupt data may coarsen identity, freeze the book, or shrink size —
it may NEVER un-cap, raise authority, or flip direction.  Concretely: every missing field
returns None (not a permissive default), every I/O error returns an empty dict, and
lens_row() degrades to {quad: None, quad_name: None, liquidity_overlay: None} rather than
inventing a regime.

PUBLIC API
----------
* ``frame(region='us')``   -> full enriched dict (quad … flag_confidence_decay)
* ``lens_row(region='us')``-> {quad, quad_name, liquidity_overlay} — byte-identical to
                              today's five _regime_dict() copies; the golden-output test
                              asserts this.
* ``freshness(region='us')``-> int | None — calendar days between the regime file's
                              ``date`` field and today; None if unknown.

REGION ROUTING
--------------
``region='us'``    -> vendor/macro/data/regime/latest.json
``region='china'`` -> vendor/macro/data/china_regime/latest.json
``region='hk'``    -> vendor/macro/data/china_regime/latest.json  (HK uses China's frame)

No other regions are supported yet; unknown regions degrade to empty dict (safe).

ADDING NEW CONSUMERS (W2+)
--------------------------
Read frame() for the enriched fields.  Never add a new consumer of the raw JSON path —
import this module instead.  Do NOT add new fields to lens_row() without bumping a golden
test — that dict feeds LLM prompts and key-order drift would silently change outputs.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# The module lives at brain/regime_frame.py; the repo root is two levels up.
_ROOT: Path = Path(__file__).resolve().parent.parent

# Map region tokens to the relative JSON paths under vendor/macro/data/.
_REGION_PATHS: dict[str, Path] = {
    "us":     _ROOT / "vendor" / "macro" / "data" / "regime"       / "latest.json",
    "china":  _ROOT / "vendor" / "macro" / "data" / "china_regime" / "latest.json",
    "hk":     _ROOT / "vendor" / "macro" / "data" / "china_regime" / "latest.json",
}

# Fields carried through to frame() unchanged from the raw JSON.
# Any field NOT in the raw dict becomes None — never a KeyError.
_FRAME_FIELDS = (
    "quad",
    "quad_name",
    "liquidity_overlay",
    "confidence",
    "transition_state",
    "contradicting",
    "flip_condition",
    "flip_margin",           # synthesised below if absent
    "flag_confidence_decay", # synthesised below if absent
    "date",
)


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

def _read_raw(region: str) -> dict[str, Any]:
    """Read and parse the regime JSON for *region*.

    Returns an empty dict on any I/O or parse error — never raises.  The empty
    dict triggers None defaults in frame() which is the correct degraded state.
    """
    path = _REGION_PATHS.get(region)
    if path is None:
        # Unknown region: degrade silently.  Caller gets an empty frame.
        return {}
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {}


def _flip_margin(raw: dict[str, Any]) -> float | None:
    """Extract flip_margin from the nested flip_condition block, or return None.

    The JSON schema stores flip info as:
        "flip_condition": {"margin": 0.05, ...}
    But we also accept a top-level "flip_margin" key for forward-compat.
    """
    # Top-level key takes priority if present.
    if "flip_margin" in raw:
        val = raw["flip_margin"]
        try:
            return float(val)
        except (TypeError, ValueError):
            return None
    # Fall through to the nested structure.
    fc = raw.get("flip_condition")
    if isinstance(fc, dict):
        val = fc.get("margin")
        try:
            return float(val)
        except (TypeError, ValueError):
            return None
    return None


def _flag_confidence_decay(raw: dict[str, Any]) -> bool | None:
    """Extract the confidence-decay flag from transition_flags or a top-level key.

    Returns None if absent so callers can distinguish "no data" from False.
    """
    # Top-level key (future schema or test override).
    if "flag_confidence_decay" in raw:
        return bool(raw["flag_confidence_decay"])
    # Nested in transition_flags (current production schema).
    tf = raw.get("transition_flags")
    if isinstance(tf, dict) and "flag_confidence_decay" in tf:
        return bool(tf["flag_confidence_decay"])
    return None


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def frame(region: str = "us") -> dict[str, Any]:
    """Return the full regime frame for *region*.

    Every key is present in the returned dict; missing raw fields map to None.
    This is the authoritative input for W2+ budget equations and LLM prompts
    that need more than the three lens_row fields.

    Returned keys
    -------------
    quad              str | None   — e.g. "Q1"
    quad_name         str | None   — e.g. "Goldilocks"
    liquidity_overlay str | None   — e.g. "expanding"
    confidence        float | None — aggregate regime confidence 0‥1
    transition_state  str | None   — "STABLE" / "WEAKENING" / "ROLLING" / …
    contradicting     list | None  — list of contradicting-leg strings
    flip_condition    dict | None  — raw nested flip_condition object
    flip_margin       float | None — margin to regime flip (extracted from flip_condition)
    flag_confidence_decay bool | None — True when the decay flag is set
    date              str | None   — YYYY-MM-DD the regime file was published
    """
    raw = _read_raw(region)
    result: dict[str, Any] = {}
    for key in _FRAME_FIELDS:
        result[key] = raw.get(key)  # None on missing — invariant: never raise
    # Synthesise the two derived fields (override the raw.get() above with real logic).
    result["flip_margin"] = _flip_margin(raw)
    result["flag_confidence_decay"] = _flag_confidence_decay(raw)
    return result


def lens_row(region: str = "us") -> dict[str, Any]:
    """Return the 3-field regime dict consumed by LLM prompts and brief builders.

    THE CONTRACT: this dict must be byte-identical (same keys, same values, same
    insertion order) to the output that today's five ``_regime_dict()`` copies
    return.  The golden-output test in tests/test_regime_frame.py asserts this.
    Never add, remove, or reorder keys without updating that test.

    ``{"quad": …, "quad_name": …, "liquidity_overlay": …}``

    Missing fields → None (same as raw.get on an empty dict).
    """
    raw = _read_raw(region)
    # Key order is load-bearing (LLM prompts embed the dict repr/JSON directly).
    return {
        "quad": raw.get("quad"),
        "quad_name": raw.get("quad_name"),
        "liquidity_overlay": raw.get("liquidity_overlay"),
    }


def freshness(region: str = "us") -> int | None:
    """Return the number of calendar days between the regime file's date and today.

    Returns None if the ``date`` field is absent, unparseable, or the file is
    missing.  Zero means published today; positive means N days old.  This is
    calendar-day arithmetic (cheap, no trading-day calendar dependency) — callers
    that need trading-day precision should convert externally.

    A return of None is NOT the same as 0 — it means "we do not know how old
    this is" and callers should treat it as potentially stale.
    """
    raw = _read_raw(region)
    date_str = raw.get("date")
    if not date_str:
        return None
    try:
        regime_date = date.fromisoformat(str(date_str))
        today = datetime.now(tz=timezone.utc).date()
        return (today - regime_date).days
    except (ValueError, TypeError):
        return None

"""control_plane.contracts — checked-in artifact contract registry (MW3 Lane A).

Baked contract for every external artifact Mastermind consumes from the vendored
macro analyzer checkout (vendor/macro/).  Source: config/contracts.yml, which
bakes the 16 synapse-declared artifacts (mastermind:anchor / mastermind:vendored)
and ~30 census-declared reads into a single checked-in registry.

Design laws
-----------
* Never-raises: missing file, bad YAML, or any key lookup always degrades to an
  empty / None result — the contracts loader must never abort a build.
* Cached: the YAML is parsed once per process.  `_reset()` is test-only.
* Thread-safe read: the cache assignment is a single Python attribute write
  (GIL-protected); no lock needed for the read-once pattern.

Public API
----------
  contract(path_or_key) -> dict | None
      Return the contract dict for the given artifact path (e.g.
      "site/factordata/us_standouts.json") or synapse key (e.g.
      "site-us-standouts"), or None when unknown.  Never raises.

  all_contracts() -> dict[str, dict]
      Return all artifact contracts keyed by their synapse key.

  freeze_class_keys() -> list[str]
      Keys of every FREEZE-class artifact (degradation_class == "FREEZE").
      Used by macro_refresh to build the anchor set from contracts.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_CONTRACTS_FILE = _ROOT / "config" / "contracts.yml"

# Module-level cache; None = not yet loaded
_cache: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    """Parse contracts.yml once and cache.  Never raises — returns {} on any error."""
    global _cache
    if _cache is not None:
        return _cache
    result: dict[str, Any] = {}
    try:
        import yaml  # optional — degrade gracefully if absent
        text = _CONTRACTS_FILE.read_text(encoding="utf-8")
        doc = yaml.safe_load(text) or {}
        arts = doc.get("artifacts") or {}
        if isinstance(arts, dict):
            result = {k: v for k, v in arts.items() if isinstance(v, dict)}
    except Exception:  # noqa: BLE001 — never abort a build over a missing contract file
        result = {}
    # Build a path→key reverse index for fast path lookups
    result["_path_index"] = {
        str(v.get("path", "") or ""): k
        for k, v in result.items()
        if isinstance(v, dict) and "path" in v
    }
    _cache = result
    return result


def _reset() -> None:
    """Test-only: clear the module cache so the next call reloads from disk."""
    global _cache
    _cache = None


def contract(path_or_key: str) -> dict | None:
    """Return the contract dict for `path_or_key` (artifact path or synapse key).

    Lookup order:
      1. Direct key match in the contracts registry (e.g. "site-us-standouts").
      2. Path match via the reverse index (e.g. "site/factordata/us_standouts.json").

    Returns None when the artifact is unknown.  Never raises.
    """
    if not path_or_key:
        return None
    try:
        data = _load()
        # 1. direct key
        entry = data.get(path_or_key)
        if isinstance(entry, dict) and "path" in entry:
            return entry
        # 2. path lookup
        idx = data.get("_path_index") or {}
        key = idx.get(str(path_or_key))
        if key:
            entry = data.get(key)
            if isinstance(entry, dict):
                return entry
        return None
    except Exception:  # noqa: BLE001
        return None


def all_contracts() -> dict[str, dict]:
    """Return all artifact contracts keyed by synapse key.  Never raises."""
    try:
        data = _load()
        return {k: v for k, v in data.items()
                if isinstance(v, dict) and "path" in v}
    except Exception:  # noqa: BLE001
        return {}


def freeze_class_keys() -> list[str]:
    """Keys of every FREEZE-class artifact (degradation_class == 'FREEZE').

    These are the artifacts whose staleness beyond their freshness_budget_sessions
    triggers freeze_to_prior in the run loop (R3).  Never raises."""
    try:
        return [k for k, v in all_contracts().items()
                if v.get("degradation_class") == "FREEZE"]
    except Exception:  # noqa: BLE001
        return []

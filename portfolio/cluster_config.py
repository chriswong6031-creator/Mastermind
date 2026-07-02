"""Shared cluster-config reader — the FIRM-WIDE single definition of correlation-based caps.

Reads ``config/clusters.yml`` and exposes it as normalised Python dicts. Lazy-loads on first
call; degrades to an empty spec (callers fall back to their own in-code defaults) when the file
is absent, malformed, or PyYAML is unavailable.  Never raises.

Public surface
--------------
``load()``        — raw spec dict (empty on any error; callers check key presence).
``clusters()``    — normalised list of cluster dicts (id, name, members, max_gross, kind).
``etf_clusters()``— the subset relevant to the ETF-book G5 guardrail (kind in {semis_ai, default}),
                     in the same shape as ``etf_strategy.yml``'s ``factor_clusters`` block so G5's
                     existing loop needs no structural change.

Design constraints (Architecture Stage-6 invariant)
-----------------------------------------------------
* Missing/stale file → callers get [] / empty dict → they use their own in-code defaults.
  The proven ETF brake NEVER weakens: if clusters.yml is absent, bot/etf.py falls back to
  the etf_strategy.yml values it has always used.
* A bad entry (wrong types, missing members/max_gross) is silently skipped — the good entries
  still apply.
* No side-effects, no global state beyond the lazy cache (_CACHE).  Thread-safe for CPython
  (dict assignment is atomic); an import-time race yields an extra file read, not corruption.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SPEC_PATH = _ROOT / "config" / "clusters.yml"

_CACHE: dict | None = None  # None = not yet loaded; {} = loaded but empty/invalid


def _clear_cache() -> None:  # test helper — call via monkeypatch if you need a fresh load
    global _CACHE
    _CACHE = None


def load() -> dict:
    """Raw spec dict from ``config/clusters.yml``.  {} on any error.  Cached after first call."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    result: dict = {}
    try:
        import yaml  # noqa: PLC0415 — optional dep; degrade gracefully when absent
        if _SPEC_PATH.exists():
            raw = yaml.safe_load(_SPEC_PATH.read_text())
            if isinstance(raw, dict):
                result = raw
    except Exception:  # noqa: BLE001 — corrupt file / missing yaml / any I/O error
        pass
    _CACHE = result
    return _CACHE


def clusters() -> list[dict]:
    """Normalised cluster list: [{id, name, members, max_gross, kind}].

    ``members`` are upper-cased strings.  Entries with missing/invalid members or max_gross are
    silently skipped (a bad entry never removes a good one).  Returns [] when the file is absent
    or has no valid clusters block."""
    raw = load().get("clusters")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "").strip()
        name = str(c.get("name") or cid)
        members = [str(m).upper().strip() for m in (c.get("members") or []) if str(m).strip()]
        mg = c.get("max_gross")
        if not members or not isinstance(mg, (int, float)):
            continue
        out.append({
            "id": cid,
            "name": name,
            "members": members,
            "max_gross": float(mg),
            "kind": str(c.get("kind") or "default"),
        })
    return out


def etf_clusters() -> list[dict]:
    """The cluster entries in ``etf_strategy.yml``'s ``factor_clusters`` shape (for G5 drop-in).

    Returns ``[{name, members, max_gross}]`` — the same shape ``_norm_clusters`` in
    ``portfolio/etf_universe.py`` already normalises, so G5's guardrail loop in ``bot/etf.py``
    needs no structural change to consume this.

    Returns [] when clusters.yml is absent/invalid (callers then fall back to etf_strategy.yml
    values — the proven brake level is preserved).
    """
    return [
        {"name": c["name"], "members": c["members"], "max_gross": c["max_gross"]}
        for c in clusters()
    ]

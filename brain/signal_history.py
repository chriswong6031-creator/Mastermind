"""Point-in-time signal history — the engine's MEMORY (the foundation the learning loop needs).

`vendor/macro/.../latest.json` (the regime + every per-name lens output the engine reasons over) is
OVERWRITTEN IN PLACE on every run. So the engine keeps NO faithful record of what it actually SAW on
any past day — which makes calibration and learning impossible: you cannot grade, or learn from, a
decision whose inputs you can no longer reconstruct. Today the only thing with real history is PRICE;
every lens/conviction/regime value is a same-day snapshot that vanishes at the next run.

This module fixes that at the source. On every daily build it records — KEEP-FIRST per (asof, ticker)
— the lens snapshot + the decision for each name the loop evaluated. Once realized outcomes accrue
(see brain/outcomes / brain/scorer; the first thesis cohort resolves ~2026-07-17), each historical
decision can be joined to its realized result, and the gate can finally LEARN which lenses actually
predicted, in which regime — instead of asserting a probability it has never verified.

KEEP-FIRST: an intra-day rebuild never overwrites the first, PIT-honest read of the day (mirrors the
macro engine's signal_archive discipline). Append-only JSONL, crash-safe: a corrupt/missing file
degrades to empty, never raises. This is irreversible-if-skipped — every day not recorded is signal
history that can never be reconstructed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_PATH = _ROOT / "data" / "signal_history" / "signals.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read() -> list[dict]:
    """All recorded rows; [] on a missing/corrupt file (skips any unparseable line)."""
    if not _PATH.exists():
        return []
    rows: list[dict] = []
    try:
        for line in _PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        return []
    return rows


def seen_keys(asof: str) -> set[str]:
    """The (ticker) set already recorded for `asof` — so KEEP-FIRST can skip same-day re-records."""
    return {r.get("ticker") for r in _read() if r.get("asof") == asof and r.get("ticker")}


def _flatten_lens_dirs(rows: list[dict]) -> dict[str, str]:
    """lens -> direction map from a decision-matrix `rows` list (the heart of the snapshot)."""
    out: dict[str, str] = {}
    for r in rows or []:
        lens = r.get("lens")
        if lens and r.get("direction") is not None:
            out[lens] = r.get("direction")
    return out


def make_record(asof: str, ticker: str, *, sleeve: str, decision: str, regime: dict | None = None,
                synthesis: dict | None = None, rows: list[dict] | None = None,
                verdict: str | None = None, weight: float | None = None,
                size_stage: str | None = None, price: float | None = None,
                time_stop_by: str | None = None, reason: str | None = None,
                extra: dict | None = None) -> dict:
    """Build one flat, queryable PIT record. `decision` in {sized, held, rejected, leadership}."""
    syn = synthesis or {}
    reg = regime or {}
    rec: dict[str, Any] = {
        "asof": asof, "ticker": (ticker or "").upper(), "sleeve": sleeve, "decision": decision,
        "verdict": verdict, "weight": weight, "size_stage": size_stage, "price": price,
        "time_stop_by": time_stop_by, "reason": reason,
        # the engine's read (the learnable substrate)
        "confluence": syn.get("confluence"), "bull": syn.get("bull"), "bear": syn.get("bear"),
        "size_authority": syn.get("size_authority"), "vetoes": syn.get("vetoes") or [],
        "weak_asymmetry": syn.get("weak_asymmetry"), "price_downtrend": syn.get("price_downtrend"),
        "price_falling_fast": syn.get("price_falling_fast"), "leadership_ok": syn.get("leadership_ok"),
        "divergences": [d.get("pattern") if isinstance(d, dict) else d
                        for d in (syn.get("divergences") or [])],
        "lens_dirs": _flatten_lens_dirs(rows or []),
        # the regime context the decision was conditioned on (for regime-conditional calibration)
        "quad": reg.get("quad"), "quad_name": reg.get("quad_name"),
        "liquidity_overlay": reg.get("liquidity_overlay"),
        "macro_risk": (reg.get("macro_risk") or {}).get("score") if isinstance(reg.get("macro_risk"), dict) else None,
        "archived_at": _now_iso(),
    }
    if extra:
        rec.update(extra)
    return rec


def archive(asof: str, records: list[dict]) -> int:
    """Append `records` (each a dict with a 'ticker') for `asof`, KEEP-FIRST per (asof, ticker) — an
    intra-day rebuild never overwrites the first PIT read. Returns the number appended. Never raises."""
    try:
        if not records:
            return 0
        already = seen_keys(asof)
        fresh = []
        for r in records:
            t = (r.get("ticker") or "").upper()
            if not t or t in already:
                continue
            already.add(t)
            r = {**r, "ticker": t}
            r.setdefault("asof", asof)
            r.setdefault("archived_at", _now_iso())
            fresh.append(r)
        if not fresh:
            return 0
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        with _PATH.open("a", encoding="utf-8") as fh:
            for r in fresh:
                fh.write(json.dumps(r, default=str, ensure_ascii=False) + "\n")
        return len(fresh)
    except Exception:
        return 0


def load(asof: str | None = None) -> list[dict]:
    """All recorded rows, optionally filtered to one `asof`."""
    rows = _read()
    return [r for r in rows if r.get("asof") == asof] if asof else rows


def load_df():
    """The history as a pandas DataFrame for analysis/calibration. None if pandas is unavailable."""
    try:
        import pandas as pd
        return pd.DataFrame(_read())
    except Exception:
        return None

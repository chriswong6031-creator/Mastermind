"""Mandate-compliance packets — MW5 Lane A (docket M3).

For every book run, ``build()`` assembles a compact compliance snapshot:
  - the book's one-line mandate,
  - three boolean checks (universe, sizing rails, currency),
  - n_positions, gross,
  - a list of breach strings (empty = clean).

The packet is ADVISORY ONLY this wave: it is written to disk and attached to
run output, but nothing gates on it. A future Fable ruling may promote it to a
gate (record that intention here so it is greppable):

    # GATE-HOOK PLACEHOLDER — promote from advisory to gate after Fable ruling
    # (research/MASTERMIND_CONTROL_PLANE_MASTERPLAN.md §2 MW6 + docket M3).

Deterministic, never raises. Degrades to ``{computed: False, reason: ...}``
when book state is absent or malformed.

Per-book mandate table
----------------------
Each entry is sourced from portfolio/registry.py comments and the relevant
bot/<book>.py docstrings.  The sizing rails are sourced from the bot enforcement
constants and cross-checked here (cite per-field).

  flagship      — Engine-gated, doctrine-disciplined US equity book; 3-sleeve
                  (leadership / conviction / cash); names gate through the
                  confluence scorecard; no direct Brain discretion.
                  Universe: any US equity admitted by the engine gate.
                  Sizing: no single-name cap declared in phase2 (sleeve-level
                  caps enforced via portfolio.sleeves); gross <= 1.0.

  heavyweight   — Concentrated best-ideas book; universe = firm union (flagship /
                  autonomous / etf published positions ∪ pending_orders, per R1
                  self_directed excluded); per-name 5%–50%; ≤8 names.
                  Sourced: bot/heavyweight.py MIN_W=0.05, MAX_W=0.50, MAX_NAMES=8.

  autonomous    — Free-form Opus Brain, US equities; no gate; paper cash is the
                  only hard constraint (no leverage).
                  Universe: all US equities (unrestricted by venue).
                  Sizing: no explicit per-name cap (Brain-submitted; firm clamp applies).

  etf           — Opus Brain, US-listed ETFs only; ETF allowlist enforced in trusted
                  layer (portfolio.etf_universe); single-ETF max weight from
                  config/etf_strategy.yml (default 0.40).
                  Sourced: bot/etf.py _guardrails() / portfolio.etf_universe.

  china         — Opus Brain, mainland A-shares (*.SS / *.SZ) only, marked CNY.
                  Universe: venue ["A-share"] per registry.
                  Sizing: Brain-submitted; firm clamp applies; no per-name cap declared.

  hk            — Opus Brain, Hong Kong listings (*.HK) only, marked HKD.
                  Universe: venue ["HK"] per registry.
                  Sizing: Brain-submitted; firm clamp applies; no per-name cap declared.

  self_directed — User's manual book; no compliance check enforced (yardstick, not
                  a managed book; constitutionally exempt per charter P6).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from portfolio import registry

_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Per-book mandate table  (one line; sourced from registry comments + bot docstrings)
# ---------------------------------------------------------------------------
_MANDATES: dict[str, str] = {
    "flagship": (
        "Engine-gated, doctrine-disciplined US equity book; "
        "3-sleeve (leadership / conviction / cash); "
        "confluence scorecard required per name."
    ),
    "heavyweight": (
        "Concentrated best-ideas book; universe = firm union (flagship/autonomous/etf) "
        "minus self_directed; per-name 5%–50%; ≤8 names. (bot/heavyweight.py R1)"
    ),
    "autonomous": (
        "Free-form Opus Brain; US equities; no gate; "
        "paper cash is the only hard constraint (no leverage)."
    ),
    "etf": (
        "Opus Brain; US-listed ETFs only (ETF allowlist enforced); "
        "single-ETF max ~40%; crisis-ladder doctrine (bot/etf.py)."
    ),
    "china": (
        "Opus Brain; mainland A-shares (*.SS / *.SZ) only; "
        "marked natively in CNY; no cross-FX conversion."
    ),
    "hk": (
        "Opus Brain; Hong Kong listings (*.HK) only; "
        "marked natively in HKD; no cross-FX conversion."
    ),
    "self_directed": (
        "User's manual book — yardstick, not managed; "
        "no compliance enforcement (charter P6)."
    ),
}

# ---------------------------------------------------------------------------
# Per-book sizing rails  (sourced from bot enforcement constants; comments cite the source)
# ---------------------------------------------------------------------------

def _sizing_rails(book_id: str) -> dict[str, Any]:
    """The sizing rails for each book, expressed as {min_w, max_w, max_names, note}.
    Sourced from bot/<book>.py constants (not re-derived at runtime to stay deterministic).
    """
    if book_id == "heavyweight":
        # bot/heavyweight.py: MIN_W=0.05, MAX_W=0.50, MAX_NAMES=8
        return {"min_w": 0.05, "max_w": 0.50, "max_names": 8,
                "note": "bot/heavyweight.py MIN_W/MAX_W/MAX_NAMES"}
    if book_id == "etf":
        # bot/etf.py: single-ETF max from config/etf_strategy.yml; default 0.40
        return {"min_w": None, "max_w": 0.40, "max_names": None,
                "note": "portfolio.etf_universe guardrails max_single_weight default 0.40"}
    # flagship / autonomous / china / hk / self_directed: no hard per-name cap declared
    return {"min_w": None, "max_w": 1.0, "max_names": None,
            "note": "gross <= 1.0 (no hard per-name cap beyond firm clamp)"}


# ---------------------------------------------------------------------------
# Universe helpers
# ---------------------------------------------------------------------------

def _firm_union_tickers() -> set[str]:
    """All tickers in the firm union books (flagship/autonomous/etf latest.json).
    Self-directed is constitutionally excluded (R1).  Never raises."""
    UNION_BOOKS = ("flagship", "autonomous", "etf")
    tickers: set[str] = set()
    for bid in UNION_BOOKS:
        try:
            p = registry.data_dir(bid) / "latest.json"
            if not p.exists():
                continue
            d = json.loads(p.read_text())
            for row in (d.get("positions") or []):
                t = (row.get("ticker") or "").upper().strip()
                if t:
                    tickers.add(t)
            for o in (d.get("pending_orders") or []):
                t = (o.get("ticker") or "").upper().strip()
                if t:
                    tickers.add(t)
        except Exception:  # noqa: BLE001
            pass
    return tickers


def _universe_check(book_id: str, positions: list[dict]) -> dict[str, Any]:
    """Check whether every held name is within the book's declared universe.

    Returns {ok: bool, detail: str, violating: list[str]}.
    Best-effort: missing data degrades to {ok: None, detail: reason}.
    """
    meta = registry.get(book_id)
    venues = set(meta.get("venues") or [])
    tickers = [
        (r.get("ticker") or "").upper().strip()
        for r in positions
        if (r.get("ticker") or "").strip()
    ]
    tickers = [t for t in tickers if t]

    # ── heavyweight: must be within firm union minus self_directed ──────────
    if book_id == "heavyweight":
        try:
            allowed = _firm_union_tickers()
            if not allowed:
                return {"ok": None, "detail": "firm union tickers unavailable (degraded)",
                        "violating": []}
            bad = [t for t in tickers if t not in allowed]
            return {
                "ok": len(bad) == 0,
                "detail": (f"all {len(tickers)} names within firm union"
                           if not bad
                           else f"{len(bad)} names outside firm union: {bad}"),
                "violating": bad,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": None, "detail": f"firm union check error: {exc!r}"[:200],
                    "violating": []}

    # ── etf: ETF-only check via portfolio.etf_universe ───────────────────────
    if book_id == "etf":
        try:
            from portfolio import etf_universe as eu
            allowed_set = set(eu.universe())
            if not allowed_set:
                return {"ok": None, "detail": "ETF universe unavailable (degraded)",
                        "violating": []}
            bad = [t for t in tickers if t not in allowed_set]
            return {
                "ok": len(bad) == 0,
                "detail": (f"all {len(tickers)} names within ETF allowlist"
                           if not bad
                           else f"{len(bad)} tickers not in ETF allowlist: {bad}"),
                "violating": bad,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": None, "detail": f"ETF universe check error: {exc!r}"[:200],
                    "violating": []}

    # ── china / hk: venue-listed check ─────────────────────────────────────
    if venues:
        bad: list[str] = []
        for t in tickers:
            if "A-share" in venues:
                # A-share: *.SS or *.SZ suffix
                if not (t.endswith(".SS") or t.endswith(".SZ")):
                    bad.append(t)
            elif "HK" in venues:
                if not t.endswith(".HK"):
                    bad.append(t)
        return {
            "ok": len(bad) == 0,
            "detail": (f"all {len(tickers)} names venue-correct ({venues})"
                       if not bad
                       else f"{len(bad)} names outside venue {venues}: {bad}"),
            "violating": bad,
        }

    # flagship / autonomous / self_directed: unrestricted
    return {"ok": True, "detail": f"unrestricted universe ({len(tickers)} names)", "violating": []}


def _sizing_check(book_id: str, positions: list[dict]) -> dict[str, Any]:
    """Check per-name sizing rails.  Returns {ok: bool, detail: str, violating: list[str]}."""
    rails = _sizing_rails(book_id)
    min_w = rails["min_w"]
    max_w = rails["max_w"]
    max_names = rails["max_names"]

    weights = [
        float(r.get("weight") or 0.0)
        for r in positions
        if (r.get("ticker") or "").strip()
    ]
    n = len(weights)
    violations: list[str] = []

    if max_names is not None and n > max_names:
        violations.append(f"n_positions {n} > max_names {max_names}")

    for r in positions:
        t = (r.get("ticker") or "").upper().strip()
        if not t:
            continue
        w = float(r.get("weight") or 0.0)
        if min_w is not None and w > 0 and w < min_w:
            violations.append(f"{t} weight {w:.3f} < min {min_w:.3f}")
        if max_w is not None and w > max_w:
            violations.append(f"{t} weight {w:.3f} > max {max_w:.3f}")

    if violations:
        return {"ok": False, "detail": "; ".join(violations), "violating": violations}
    return {"ok": True, "detail": f"all sizing rails met ({rails['note']})", "violating": []}


# ---------------------------------------------------------------------------
# Currency check
# ---------------------------------------------------------------------------

def _currency_check(book_id: str, book_state: dict) -> dict[str, Any]:
    """Verify the reported currency of the book state matches the registry declaration."""
    declared = registry.currency(book_id)
    reported = (book_state.get("currency") or "").upper().strip() or None
    if reported is None:
        # Currency not reported in state — degrade gracefully
        return {"ok": None, "detail": f"currency not reported in book state (declared {declared})"}
    ok = reported == declared
    return {
        "ok": ok,
        "detail": (f"currency ok: {reported} == {declared}"
                   if ok
                   else f"currency mismatch: state reports {reported}, registry declares {declared}"),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build(book_id: str, book_state: dict | None, config: dict | None = None) -> dict:
    """Build a mandate-compliance packet for one book run.

    Parameters
    ----------
    book_id    : The portfolio id (one of the PORTFOLIOS registry entries).
    book_state : The run output dict from the book's run function
                 (must carry at least: positions, gross, currency, asof).
                 None or empty → returns ``{computed: False, reason: "no book state"}``.
    config     : Optional override config (currently unused; reserved for doctrine.yml overrides).

    Returns
    -------
    dict  — see module docstring for full schema.  Never raises.  Always returns a dict.

    Degradation contract (charter P2)
    -----------------------------------
    Any failure in a sub-check sets the relevant bool to None (not False) and populates
    ``detail``.  The packet is still emitted; ``computed`` stays True.  A total failure
    (unhandled exception in build itself) returns ``{computed: False, reason: ...}``.
    """
    try:
        return _build(book_id, book_state or {}, config or {})
    except Exception as exc:  # noqa: BLE001
        return {
            "computed": False,
            "reason": f"mandate_packet.build crashed: {exc!r}"[:400],
            "book_id": book_id,
        }


def _build(book_id: str, book_state: dict, config: dict) -> dict:
    if not book_state:
        return {"computed": False, "reason": "no book state", "book_id": book_id}

    asof = (
        book_state.get("asof")
        or book_state.get("as_of")
        or date.today().isoformat()
    )

    # positions — normalise both list-of-dicts and dict[ticker->weight] shapes
    raw_pos = book_state.get("positions") or book_state.get("book") or []
    if isinstance(raw_pos, dict):
        positions = [{"ticker": t, "weight": w} for t, w in raw_pos.items()]
    else:
        positions = [r for r in raw_pos if isinstance(r, dict)]

    gross = book_state.get("gross")
    if gross is None:
        weights = [float(r.get("weight") or 0.0) for r in positions]
        gross = round(sum(weights), 4)

    n_positions = len([r for r in positions if (r.get("ticker") or "").strip()])

    # ── sub-checks ──────────────────────────────────────────────────────────
    universe_result = _universe_check(book_id, positions)
    sizing_result = _sizing_check(book_id, positions)
    currency_result = _currency_check(book_id, book_state)

    universe_ok = universe_result.get("ok")
    sizing_rails_ok = sizing_result.get("ok")
    currency_ok = currency_result.get("ok")

    # ── breach accumulation ─────────────────────────────────────────────────
    breaches: list[str] = []
    if universe_ok is False:
        breaches.append(f"universe: {universe_result.get('detail', 'violation')}")
    if sizing_rails_ok is False:
        breaches.append(f"sizing: {sizing_result.get('detail', 'violation')}")
    if currency_ok is False:
        breaches.append(f"currency: {currency_result.get('detail', 'mismatch')}")

    return {
        "computed": True,
        "book_id": book_id,
        "asof": asof,
        "mandate": _MANDATES.get(book_id, f"No mandate defined for {book_id}"),
        "universe_ok": universe_ok,
        "universe_detail": universe_result.get("detail", ""),
        "sizing_rails_ok": sizing_rails_ok,
        "sizing_detail": sizing_result.get("detail", ""),
        "currency_ok": currency_ok,
        "currency_detail": currency_result.get("detail", ""),
        "n_positions": n_positions,
        "gross": round(float(gross), 4) if gross is not None else None,
        "breaches": breaches,
    }


# ---------------------------------------------------------------------------
# Persistence helpers  (called by the bot wiring layer)
# ---------------------------------------------------------------------------

def write_packet(packet: dict, book_id: str | None = None) -> Path | None:
    """Persist the packet to data/portfolios/<id>/mandate_packet.json.  Never raises."""
    try:
        bid = book_id or packet.get("book_id")
        if not bid:
            return None
        out_dir = registry.data_dir(bid)
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / "mandate_packet.json"
        p.write_text(json.dumps(packet, indent=2))
        return p
    except Exception:  # noqa: BLE001
        return None


def emit_run_event(packet: dict, book_id: str, job: str = "") -> None:
    """Emit an ADVISORY_ONLY run_event when the packet contains breaches.  Never raises."""
    if not packet.get("computed"):
        return
    if not packet.get("breaches"):
        return
    try:
        from control_plane import run_events
        from control_plane.guardrail import Severity
        run_events.append({
            "kind": "mandate_breach",
            "job": job or f"{book_id}_daily",
            "book": book_id,
            "step": "mandate_packet",
            "status": "warn",
            "severity": Severity.ADVISORY_ONLY.name,
            "extra": {
                "breaches": packet.get("breaches", []),
                "n_positions": packet.get("n_positions"),
                "gross": packet.get("gross"),
                "asof": packet.get("asof"),
                "advisory_only": True,
                # GATE-HOOK PLACEHOLDER — promote from advisory to gate after Fable ruling
                # (research/MASTERMIND_CONTROL_PLANE_MASTERPLAN.md §2 MW6 + docket M3).
            },
        })
    except Exception:  # noqa: BLE001
        pass

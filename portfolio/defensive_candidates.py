"""portfolio/defensive_candidates.py — THE canonical defensive candidate generator (W4, task A1).

WHY THIS MODULE EXISTS
----------------------
Three consumers need a defensive-rotation pool: the additive PM's champion pool (W4), the
contingent DEF_SLEEVE rotation floor (task B2 / ``portfolio/rotation.py``), and the benchmark
ledger's regime-conditional basket (W5). Before this module each would have grown its own
private list, and they would silently DRIFT — the PM would see one set, the floor would buy a
second, the bogey would measure a third. That drift is exactly the failure the architecture
(Stage 3, "one canonical generator, three consumers") forbids. This is the ONE generator.

WHAT IT RETURNS
---------------
``candidates()`` = the deduped UNION of three sources:

  (a) ``defensive_playbook.favor`` — the driver-conditional defensive tilt (XLP/XLV/USMV/SGOV/
      TLT class). We REUSE ``defensive_playbook.defensive_tilt()`` — we do NOT fork its machinery.
      Archetype: ``quality_defensive`` for the quality/low-vol/staples/healthcare names, ``duration``
      for TLT/IEF, ``ballast_cash`` for SGOV/T-bills.

  (b) ``regime_frame.cycles()`` entry-side — sectors whose cycle read is on the fresh-entry /
      bottoming side (phaseLabel ∈ {Bottoming, Prime entry}, i.e. Trough/Recovery — the walk-forward
      -defensible entry tilt) map to THEIR sector ETFs (XLU/XLC/XLY today). Freshness is enforced
      BY cycles() itself: a stale/absent sector_cycles.json returns {} → this source contributes
      nothing. Archetype: ``sector_rotation``.

  (c) the ``us_standouts`` bottoming-alignment board — single-name buy[] rows labeled BUY-ZONE /
      BOTTOMING / NEARING-A-LOW. RESPECTS the board's own ``gate_go`` (W1 / P-NEW-2): when the board
      says NO-GO its single names are EXCLUDED. The sector-ETF sources (a)+(b) are UNAFFECTED by
      gate_go — the gate only ever gates the un-validated single-name board. Archetype:
      ``quality_defensive`` (bottoming single names entering on a defensive-rotation thesis).

WEIGHTS — EQUAL-WEIGHT PRIOR, EXPLICITLY FROZEN
----------------------------------------------
``weights()`` returns an equal weight for every deduped candidate with a ``note`` recording that the
weighting is frozen until the predictions ledger resolves ≥12 defensive theses. NO learned weighting
in this wave (architecture §Stage-3: "equal-weight prior, weights frozen until predictions-ledger
revival resolves ≥12 theses"). A learned weight here would be un-graded curve-fit.

THE INVARIANT (governs every path)
----------------------------------
Missing/stale/wrong data COARSENS or SHRINKS — it never un-caps, raises authority, or flips
direction. Concretely: every source degrades to [] independently; ALL sources absent → [] (a legal,
meaningful "no defensive candidates today" that consumers MUST handle as a no-op — never a fabricated
default basket); a stale cycle read contributes nothing; a NO-GO board drops its single names but
leaves the ETF sources intact. This module NEVER raises.

Pure / deterministic (given the artifacts) / degrade-never-raise.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# ── archetype taxonomy (the DEF_SLEEVE theme_id derives from this; task B2 consumes it) ──────────
# duration          — long Treasuries (TLT/IEF); a hedge ONLY in a growth-scare (see playbook caveats)
# quality_defensive — up-in-quality / low-vol / staples / healthcare + bottoming single names
# ballast_cash      — T-bills / near-cash (SGOV/BIL/SHV/SHY): dry powder with a coupon
# sector_rotation   — a whole SECTOR ETF entering on a fresh cycle read (XLU/XLC/XLY)
_ARCHETYPES: frozenset[str] = frozenset(
    {"duration", "quality_defensive", "ballast_cash", "sector_rotation"}
)

# Ticker → archetype for the driver-conditional playbook favor list (source a). Anything not mapped
# falls back to ``quality_defensive`` (the playbook's favor list is up-in-quality by construction, so
# an unmapped defensive name is a quality-defensive by default — never mis-tagged as duration/cash).
_DURATION_TICKERS: frozenset[str] = frozenset({"TLT", "IEF", "TLH", "VGLT", "EDV"})
_BALLAST_TICKERS: frozenset[str] = frozenset({"SGOV", "BIL", "SHV", "SHY", "USFR", "TFLO", "GBIL"})

# The fresh-entry / bottoming cycle labels that qualify a SECTOR for source (b). phaseLabel is the
# human label cycles() passes through: Trough→"Bottoming", Recovery→"Prime entry" are the walk-
# forward-defensible entry-side labels (the "Bottoming / FRESH BUY" the architecture names). Peak/
# Downturn/Expansion labels (Topping/Rolling over/Trending) do NOT qualify — they are held-leader or
# late-cycle states, not fresh defensive entries. Config-driven from doctrine.yml
# (defensive_candidates.cycle_entry_labels); this frozenset is the degrade-safe fallback.
_CYCLE_ENTRY_LABELS_FALLBACK: frozenset[str] = frozenset({"Bottoming", "Prime entry"})

# The single-name board labels that qualify a row for source (c). Substring-matched (case-folded) so
# "BOTTOMING (blocked)" and "NEARING A LOW (blocked)" still qualify on their base label — the
# board's own gate_go is the validation gate, not our label parsing.
_BOARD_BUY_LABEL_SUBSTRINGS: tuple[str, ...] = ("BUY ZONE", "BUY-ZONE", "BOTTOMING", "NEARING A LOW")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _u(t: Any) -> str:
    """Upper-cased, stripped ticker string ('' for None/non-str)."""
    return (str(t) if t is not None else "").upper().strip()


def _cycle_entry_labels() -> frozenset[str]:
    """The fresh-entry cycle labels for source (b), from doctrine.yml. Falls back to the frozen prior
    on any error / malformed config — a bad config edit can never break the union."""
    try:
        from bot.doctrine_config import load_doctrine
        block = load_doctrine().get("defensive_candidates") or {}
        labels = block.get("cycle_entry_labels")
        if isinstance(labels, list) and labels:
            got = frozenset(str(x) for x in labels if x)
            if got:
                return got
    except Exception:  # noqa: BLE001
        pass
    return _CYCLE_ENTRY_LABELS_FALLBACK


def _archetype_for(ticker: str) -> str:
    """Archetype for a playbook-favor (source-a) ticker. Unmapped → quality_defensive."""
    if ticker in _DURATION_TICKERS:
        return "duration"
    if ticker in _BALLAST_TICKERS:
        return "ballast_cash"
    return "quality_defensive"


# ---------------------------------------------------------------------------
# per-source generators (each independent + degrade-safe → [] on any miss)
# ---------------------------------------------------------------------------

def _from_playbook(risk_state: dict | None) -> list[dict[str, Any]]:
    """Source (a): the driver-conditional defensive_playbook.favor list. REUSES the playbook's own
    defensive_tilt() (no fork). Degrades to [] if the playbook import fails or favor is empty."""
    try:
        from portfolio import defensive_playbook as _dp
        tilt = _dp.defensive_tilt(risk_state)
    except Exception:  # noqa: BLE001
        return []
    favor = tilt.get("favor") if isinstance(tilt, dict) else None
    if not isinstance(favor, list):
        return []
    archetype = f"playbook:{tilt.get('archetype', 'broad_derisk')}"
    out: list[dict[str, Any]] = []
    for t in favor:
        tk = _u(t)
        if not tk:
            continue
        out.append({
            "ticker": tk,
            "source": "playbook",
            "archetype": _archetype_for(tk),
            "note": f"driver-conditional defensive favor [{archetype}]",
        })
    return out


def _from_cycles() -> list[dict[str, Any]]:
    """Source (b): sectors on the fresh-entry / bottoming side of the cycle → their sector ETFs.

    cycles() is the SOLE, freshness-gated reader of sector_cycles.json — a stale/absent file returns
    {} and this source contributes nothing (the freshness gate lives there, not here). The cycles()
    keys ARE the sector ETF tickers (XLK/XLV/XLU/…), so no sector-name mapping is needed for this
    source. Only phaseLabel ∈ {Bottoming, Prime entry} qualifies (entry-side / walk-forward-defensible)."""
    try:
        from brain import regime_frame as _rf
        rows = _rf.cycles()
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(rows, dict) or not rows:
        return []
    entry_labels = _cycle_entry_labels()
    out: list[dict[str, Any]] = []
    for etf, row in rows.items():
        tk = _u(etf)
        if not tk or not isinstance(row, dict):
            continue
        label = row.get("phaseLabel")
        if not (isinstance(label, str) and label in entry_labels):
            continue
        out.append({
            "ticker": tk,
            "source": "cycles",
            "archetype": "sector_rotation",
            "note": f"cycle {row.get('phase')} · {label} (fresh sector-rotation entry)",
        })
    return out


def _from_standouts() -> list[dict[str, Any]]:
    """Source (c): single names off the us_standouts bottoming-alignment board.

    RESPECTS gate_go (W1 / P-NEW-2): an explicit gate_go=False (with the doctrine toggle on) skips
    the WHOLE source — its single names are un-validated and must not seed a defensive buy. A missing
    gate_go (legacy artifact) or a truthy gate_go ingests. This gate touches ONLY this single-name
    source; sources (a)+(b) are sector ETFs and are unaffected by it. Reuses the same
    ``bot.doctrine_config`` toggle + the same board loader path the intake/conviction funnels use, so
    there is one definition of 'respect the standout gate' firm-wide."""
    d = _read_standouts() or {}
    rows = d.get("buy") or d.get("standouts") or []
    if not isinstance(rows, list):
        return []
    gate_go = d.get("gate_go")
    if gate_go is False and _respect_standout_gate():
        log.warning("us_standouts gate_go=False — excluding %d single-name defensive candidates "
                    "(sector-ETF sources unaffected)", len(rows))
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        tk = _u(r.get("ticker"))
        if not tk:
            continue
        label = (r.get("label") or r.get("state") or "").upper()
        if not any(sub in label for sub in _BOARD_BUY_LABEL_SUBSTRINGS):
            continue
        out.append({
            "ticker": tk,
            "source": "standouts",
            "archetype": "quality_defensive",
            "note": f"bottoming board: {r.get('label') or r.get('state') or 'standout'}",
        })
    return out


def _read_standouts() -> dict | None:
    """Read the us_standouts board from the vendored macro checkout. None on any miss. Uses the same
    site/factordata path the intake + conviction funnels read (one board, one path)."""
    try:
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        for base in ("site", "data"):
            p = root / "vendor" / "macro" / base / "factordata" / "us_standouts.json"
            if p.exists():
                import json
                return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        pass
    return None


def _respect_standout_gate() -> bool:
    """Doctrine toggle (P-NEW-2), shared with intake/conviction: honour the board's gate_go. Default
    TRUE. A missing/unreadable doctrine key degrades to respecting the gate — skipping only ever
    removes a source (never adds one), so defaulting to respect is invariant-safe."""
    try:
        from bot.doctrine_config import load_doctrine
        v = load_doctrine().get("us_standouts_respect_gate_go")
        return True if v is None else bool(v)
    except Exception:  # noqa: BLE001
        return True


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def candidates(risk_state: dict | None = None) -> list[dict[str, Any]]:
    """THE canonical defensive candidate pool: the deduped UNION of the three sources.

    Parameters
    ----------
    risk_state : dict | None
        The Macro Risk Officer's ``brain.macro_risk.risk_state`` dict, passed through to
        ``defensive_playbook.defensive_tilt`` for source (a). None → the playbook's broad_derisk
        default (still a legal defensive tilt); the playbook itself never raises on None.

    Returns
    -------
    list[{ticker, source, archetype, note}]
        One row per UNIQUE ticker (dedup by ticker; FIRST source to emit a ticker wins its
        source/archetype/note — sources are unioned in priority order a → b → c so a
        driver-conditional playbook tag out-ranks a coincidental board tag for the same name).
        EMPTY LIST is legal and meaningful: 'no defensive candidates today' — every consumer MUST
        treat [] as a no-op, never as a reason to fabricate a default basket.

    Degrade
    -------
    Every source degrades to [] independently; all three absent → []. NEVER raises.
    """
    unioned: list[dict[str, Any]] = []
    # Priority order a → b → c: the first source to claim a ticker owns its tag. Each generator is
    # independently degrade-safe, so one failing source never suppresses the others.
    for gen in (lambda: _from_playbook(risk_state), _from_cycles, _from_standouts):
        try:
            unioned.extend(gen() or [])
        except Exception:  # noqa: BLE001 — a broken source contributes nothing, never crashes the union
            continue

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for row in unioned:
        tk = _u(row.get("ticker"))
        if not tk or tk in seen:
            continue
        arch = row.get("archetype")
        if arch not in _ARCHETYPES:
            arch = "quality_defensive"          # never emit an unknown archetype (theme_id safety)
        seen.add(tk)
        deduped.append({
            "ticker": tk,
            "source": row.get("source"),
            "archetype": arch,
            "note": row.get("note") or "",
        })
    return deduped


def weights(cands: list[dict[str, Any]] | None = None,
            risk_state: dict | None = None) -> dict[str, Any]:
    """EQUAL-WEIGHT PRIOR, EXPLICITLY FROZEN. No learned weighting in this wave.

    Parameters
    ----------
    cands : list | None
        A candidate list (as returned by ``candidates()``). None → this calls ``candidates()``.
    risk_state : dict | None
        Passed through to ``candidates()`` only when *cands* is None.

    Returns
    -------
    {
      "weights": {ticker: float},   # equal 1/N across the deduped candidates ({} when N==0)
      "frozen": True,
      "method": "equal_weight",
      "note": "(frozen-equal-weight until predictions ledger resolves >=12 defensive theses)",
    }

    The frozen note is load-bearing: it records WHY the weights are equal (no graded evidence yet),
    so a future learned-weighting wave replaces a documented placeholder, not a silent default.
    An empty candidate set returns empty weights — a legal 'nothing to weight' no-op.
    """
    if cands is None:
        cands = candidates(risk_state)
    tickers: list[str] = []
    seen: set[str] = set()
    for row in cands or []:
        tk = _u(row.get("ticker")) if isinstance(row, dict) else _u(row)
        if tk and tk not in seen:
            seen.add(tk)
            tickers.append(tk)
    n = len(tickers)
    w = round(1.0 / n, 6) if n else 0.0
    return {
        "weights": {t: w for t in tickers},
        "frozen": True,
        "method": "equal_weight",
        "note": ("(frozen-equal-weight until predictions ledger resolves >=12 defensive theses)"),
    }

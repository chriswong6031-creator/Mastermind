"""brain/decision_provenance.py — the E2 replayable decision-provenance ledger (P0 observability).

WHY THIS MODULE EXISTS
----------------------
The build makes a chain of decisions per candidate — intake surfaced it, the conviction
gate scored it, the research desk confirmed/held it, the committee vetted it, the timing
gate withheld it, a final {action, weight} landed. Today that chain is scattered across
`_shadow_inputs`, `gate_info`, `research_held`, the runlog and the book; there is no single
row that says "for ticker T, on day D, under flag-config H, here is every stage verdict that
produced the final action". Without it a past decision cannot be REPLAYED or audited end to
end once `latest.json` overwrites in place.

This module writes exactly that: ONE self-contained provenance row per evaluated candidate,
per build, keyed to a deterministic fingerprint of the flag configuration the build ran under
(so two builds under different flags are distinguishable). It is ALWAYS-ON OBSERVABILITY —
purely ADDITIVE and FAIL-SOFT: it only READS decision data already computed and WRITES a jsonl
sidecar. It sizes nothing, gates nothing, changes no book/verdict. A write failure is logged
and swallowed — it can NEVER break a build.

THE INVARIANT (mirrors brain/neural_web_context.py + brain/divergence_clue.py)
------------------------------------------------------------------------------
Fail-soft EVERYWHERE. Absent / malformed input → an empty result, never a raise. Every public
function is safe to call bare; a failure degrades to a no-op (write) or [] (read), never an
exception into the build.

PUBLIC API
----------
* flags_hash()                     -> str   — 12-hex fingerprint of the KNOWN_FLAGS env config ("" fail-soft)
* row(ticker, *, sources, stage_verdicts, seats, sector_phase, nw, final) -> dict
                                            — assemble ONE replayable provenance record (stamps flags_hash())
* write(asof, rows)                -> None  — append rows to data/brain/decision_provenance/<asof>.jsonl
* read(asof)                       -> list[dict] — fail-soft reader ([] on absent/malformed)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
# module-level so tests can monkeypatch to a tmp dir (mirrors signal_history._PATH).
_DIR = _ROOT / "data" / "brain" / "decision_provenance"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# flags_hash — a deterministic fingerprint of the flag configuration
# --------------------------------------------------------------------------- #

def flags_hash() -> str:
    """Return a short (12-hex) sha256 fingerprint of the CURRENT KNOWN_FLAGS env configuration.

    The digest is taken over the SORTED ``(name, value)`` pairs of every flag in
    ``control_plane.flags.KNOWN_FLAGS``, reading each from ``os.environ`` (an UNSET flag
    contributes the empty string ""). This makes the hash a deterministic fingerprint of the
    exact flag configuration a build ran under — two builds under different flag configs get
    different hashes; the same config always gets the same hash.

    We read raw ``os.environ`` (NOT ``enumerate_flags()``) on purpose: enumerate_flags() masks
    secret values and only returns SET flags, whereas a stable fingerprint needs every known
    flag present (unset → "") so adding/removing a flag from the env changes the digest.

    Fail-soft: any error (KNOWN_FLAGS unimportable, hashlib failure) → "".
    """
    try:
        from control_plane.flags import KNOWN_FLAGS
        pairs = sorted((name, os.environ.get(name, "")) for name in KNOWN_FLAGS)
        blob = "\n".join(f"{k}={v}" for k, v in pairs)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
    except Exception as e:  # noqa: BLE001 — fingerprint is observability; never raise
        log.debug("decision_provenance: flags_hash failed (%s)", e)
        return ""


# --------------------------------------------------------------------------- #
# row — the replayable provenance record builder
# --------------------------------------------------------------------------- #

def row(
    ticker: str,
    *,
    sources: Any = None,
    stage_verdicts: Any = None,
    seats: Any = None,
    sector_phase: Any = None,
    nw: Any = None,
    final: Any = None,
) -> dict[str, Any]:
    """Assemble ONE replayable provenance record for ``ticker``.

    All fields are optional / nullable — a candidate for which a given stage did not run just
    carries None/[] for that field. The record stamps ``flags_hash()`` (the config the build
    ran under) and a UTC timestamp so a downstream replay can group by flag-config and order by
    time. Pure + deterministic given its inputs (aside from the timestamp + flags_hash env read);
    never raises — a bad input degrades that field, it does not fail the row.

    Parameters
    ----------
    ticker         : the evaluated candidate.
    sources        : intake provenance / vetoes / where the name came from (list|dict|None).
    stage_verdicts : the per-stage decision chain — e.g.
                     {forge_confirmed, gate, committee, timing, action} (dict|None).
    seats          : the seats/committee that ran (list|dict|None).
    sector_phase   : sector_phase_at_entry (str|None).
    nw             : the NW context block already gathered for the name (dict|None).
    final          : the final {action, weight} (dict|None).
    """
    try:
        tkr = (str(ticker) if ticker is not None else "").upper().strip()
    except Exception:  # noqa: BLE001
        tkr = ""
    return {
        "ticker": tkr,
        "sources": sources,
        "stage_verdicts": stage_verdicts,
        "seats": seats,
        "sector_phase_at_entry": sector_phase,
        "nw": nw,
        "final": final,
        "flags_hash": flags_hash(),
        "recorded_at": _now_iso(),
    }


# --------------------------------------------------------------------------- #
# write — append the provenance rows to the per-asof jsonl sidecar
# --------------------------------------------------------------------------- #

def write(asof: str, rows: list[dict]) -> None:
    """Append ``rows`` (one JSON line each) to data/brain/decision_provenance/<asof>.jsonl.

    Creates the directory if missing. Atomic-ish (a single buffered write per call) and
    FAIL-SOFT: any error — bad asof, unwritable dir, un-serializable row — is logged and
    SWALLOWED. This never raises, so a provenance-write failure can never break a build.
    An empty / falsy ``rows`` is a no-op.
    """
    try:
        if not rows:
            return
        safe_asof = str(asof or "unknown").strip() or "unknown"
        # guard the filename against a stray path separator in asof (defensive)
        safe_asof = safe_asof.replace("/", "_").replace("\\", "_")
        _DIR.mkdir(parents=True, exist_ok=True)
        path = _DIR / f"{safe_asof}.jsonl"
        lines = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            try:
                lines.append(json.dumps(r, default=str, ensure_ascii=False))
            except Exception:  # noqa: BLE001 — skip an un-serializable row, keep the rest
                continue
        if not lines:
            return
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception as e:  # noqa: BLE001 — observability-only; never raise into a build
        log.warning("decision_provenance: write failed (%s)", e)


# --------------------------------------------------------------------------- #
# read — the fail-soft reader for tests / replay
# --------------------------------------------------------------------------- #

def read(asof: str) -> list[dict]:
    """Return the provenance rows recorded for ``asof``; [] on an absent / malformed file.

    Fail-soft: a missing file → []; a corrupt line is skipped (never raises); any other error
    → []. This is the replay/test entry point that round-trips write().
    """
    try:
        safe_asof = str(asof or "unknown").strip() or "unknown"
        safe_asof = safe_asof.replace("/", "_").replace("\\", "_")
        path = _DIR / f"{safe_asof}.jsonl"
        if not path.exists():
            return []
        out: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:  # noqa: BLE001 — skip a corrupt line, keep the rest
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out
    except Exception as e:  # noqa: BLE001 — reader degrades to empty, never raises
        log.debug("decision_provenance: read failed (%s)", e)
        return []

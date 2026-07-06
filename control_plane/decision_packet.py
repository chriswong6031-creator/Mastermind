"""control_plane.decision_packet — DecisionPacket substrate (ruling R6; docket M4).

The packet binds at the SUBMISSION BOUNDARY (Charter P2/P3).  Brains stay free-form
inside; doctrine applies when the Brain hands the book to the deterministic layer.

Design law
----------
- MECHANICAL only.  No LLM judgment, no prose quality scoring beyond non-emptiness.
  The validator decides whether sentinels are acceptable per field.
- NEVER-RAISE.  record_rejection() catches all exceptions; validate() never raises.
  Callers must not need try/except around the public API.
- Stdlib only.  No third-party imports.
- Sentinel "<not provided>" signals an absent narrative field; the validator chooses
  which sentinels are OK (falsifiers: NOT OK; liquidity_notes: OK in v1).

Schema (packet_version=1)
--------------------------
  book                str            portfolio_id
  asof                str            trade date (ISO-8601 date string)
  mandate             str            one line restating the book's mandate as the Brain understood it
  holdings            list[dict]     [{ticker, weight}]  (weight in (0,1])
  evidence_planes     list[str]      planes/artifacts that informed the book
  source_provenance   list[str]      artifact paths/keys actually read
  falsifiers          list[str]      >=1, non-empty prose each — what proves this book wrong
  liquidity_notes     str            may be brief; "<not provided>" acceptable in v1
  portfolio_delta     dict           computed by compute_delta(); never Brain-supplied
  expected_failure_mode str          what the Brain thinks will kill this trade
  risk_direction      str            "increase"|"preserve"|"reduce" — FORCED-CORRECTED vs delta
  packet_version      int            1
  run_id              str            from the session/brain run
  submitted_at        str            UTC ISO-8601 isoformat(timespec="seconds")
  _risk_direction_overridden bool    True when Brain's claim was corrected (warning flag)
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PACKET_VERSION = 1
_SENTINEL = "<not provided>"

# ---------------------------------------------------------------------------
# RiskDirection enum (simple string-backed)
# ---------------------------------------------------------------------------

_VALID_RISK_DIRECTIONS = frozenset({"increase", "preserve", "reduce"})

# ---------------------------------------------------------------------------
# DecisionPacket dataclass
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class DecisionPacket:
    """One Brain submission boundary record (packet_version=1)."""

    book: str
    asof: str
    mandate: str
    holdings: list[dict]              # [{"ticker": str, "weight": float}]
    evidence_planes: list[str]
    source_provenance: list[str]
    falsifiers: list[str]             # >=1, non-empty prose
    liquidity_notes: str              # "<not provided>" OK in v1
    portfolio_delta: dict             # computed — never Brain-supplied
    expected_failure_mode: str
    risk_direction: str               # "increase"|"preserve"|"reduce"
    packet_version: int = PACKET_VERSION
    run_id: str = ""
    submitted_at: str = ""
    _risk_direction_overridden: bool = False

    # ------------------------------------------------------------------
    # serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict representation of the packet."""
        d = dataclasses.asdict(self)
        # dataclasses.asdict renames _risk_direction_overridden to
        # '_risk_direction_overridden' which is fine for JSON; leave as-is.
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DecisionPacket":
        """Reconstruct a DecisionPacket from a dict (e.g. loaded from JSON).

        Unknown keys are silently ignored to allow forward-compat reads of
        future schema versions.  Never raises; returns a best-effort packet.
        """
        known = {f.name for f in dataclasses.fields(cls)}
        kwargs: dict[str, Any] = {}
        for f in dataclasses.fields(cls):
            if f.name in d:
                kwargs[f.name] = d[f.name]
        return cls(**{k: v for k, v in kwargs.items() if k in known})


# ---------------------------------------------------------------------------
# compute_delta — shared so bot code and validator agree
# ---------------------------------------------------------------------------


def compute_delta(
    holdings: list[dict],
    prior_book: dict,
) -> dict:
    """Compute portfolio_delta between new holdings and the prior book.

    Parameters
    ----------
    holdings : list[{"ticker": str, "weight": float, ...}]
        The proposed holdings from the Brain submission.
    prior_book : dict
        The current book state.  Accepted shapes:
          - {"positions": {"TICKER": {...}, ...}}  (paper_account._load_account)
          - {"holdings": [{"ticker": str, "weight": float}, ...]}  (submission dict)
          - {"TICKER": weight, ...}  (plain weight map)

    Returns
    -------
    dict with keys:
        n_added       int   names in new but not in prior
        n_dropped     int   names in prior but not in new
        n_resized     int   names in both with |Δweight| > 0.001
        gross_before  float sum of prior weights (0.0 when prior empty)
        gross_after   float sum of proposed weights
        turnover      float abs Δweight summed over all names / 2
    """
    # ── normalise new holdings to {TICKER: weight} ──
    new: dict[str, float] = {}
    for h in (holdings or []):
        if not isinstance(h, dict):
            continue
        t = str(h.get("ticker") or "").upper().strip()
        try:
            w = float(h.get("weight") or 0.0)
        except (TypeError, ValueError):
            w = 0.0
        if t and w > 0:
            new[t] = w

    # ── normalise prior_book to {TICKER: weight} ──
    prior: dict[str, float] = {}
    if isinstance(prior_book, dict):
        # shape 1: {"positions": {"TICKER": {...}}}
        positions_raw = prior_book.get("positions")
        if isinstance(positions_raw, dict):
            for k, v in positions_raw.items():
                t = str(k or "").upper().strip()
                if not t:
                    continue
                # positions dict may have nested dicts or plain floats
                if isinstance(v, dict):
                    try:
                        w = float(v.get("weight") or 0.0)
                    except (TypeError, ValueError):
                        w = 0.0
                else:
                    try:
                        w = float(v or 0.0)
                    except (TypeError, ValueError):
                        w = 0.0
                if w > 0:
                    prior[t] = w
        # shape 2: {"holdings": [...]}
        elif isinstance(prior_book.get("holdings"), list):
            for h in prior_book["holdings"]:
                if not isinstance(h, dict):
                    continue
                t = str(h.get("ticker") or "").upper().strip()
                try:
                    w = float(h.get("weight") or 0.0)
                except (TypeError, ValueError):
                    w = 0.0
                if t and w > 0:
                    prior[t] = w
        # shape 3: plain weight map {"TICKER": weight}
        else:
            for k, v in prior_book.items():
                t = str(k or "").upper().strip()
                if not t or t in ("positions", "holdings", "cash"):
                    continue
                try:
                    w = float(v or 0.0)
                except (TypeError, ValueError):
                    continue
                if w > 0:
                    prior[t] = w

    all_tickers = set(new) | set(prior)
    n_added   = sum(1 for t in new   if t not in prior)
    n_dropped = sum(1 for t in prior if t not in new)
    n_resized = sum(
        1 for t in (set(new) & set(prior))
        if abs(new[t] - prior[t]) > 0.001
    )
    gross_before = round(sum(prior.values()), 6)
    gross_after  = round(sum(new.values()), 6)
    # turnover = half the sum of absolute weight changes (industry convention)
    turnover = round(
        sum(abs(new.get(t, 0.0) - prior.get(t, 0.0)) for t in all_tickers) / 2.0,
        6,
    )
    return {
        "n_added":      n_added,
        "n_dropped":    n_dropped,
        "n_resized":    n_resized,
        "gross_before": gross_before,
        "gross_after":  gross_after,
        "turnover":     turnover,
    }


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------

_CASH_TOLERANCE = 0.01  # holdings sum may exceed 1.0 by at most 1pp (rounding)


def validate(
    packet: "DecisionPacket | dict",
    prior_book: dict,
) -> tuple[bool, list[str]]:
    """Validate a DecisionPacket against its prior book.

    Mechanical checks only — no LLM judgment, no prose quality scoring beyond
    non-emptiness (P3: we bound the surface, not the mind).

    Parameters
    ----------
    packet    : DecisionPacket instance or dict (from_dict applied if dict)
    prior_book: current book state (same shapes as compute_delta accepts)

    Returns
    -------
    (ok, errors)
        ok     True when all checks pass
        errors list of human-readable error strings (empty when ok)
    """
    errors: list[str] = []

    # ── coerce to DecisionPacket ──
    if isinstance(packet, dict):
        try:
            p = DecisionPacket.from_dict(packet)
        except Exception as exc:  # noqa: BLE001
            return False, [f"from_dict failed: {exc!r}"[:300]]
    elif isinstance(packet, DecisionPacket):
        p = packet
    else:
        return False, ["packet must be a DecisionPacket or dict"]

    # ── required non-empty string fields ──
    def _req_str(val: Any, name: str, sentinel_ok: bool = False) -> None:
        if not isinstance(val, str) or not val.strip():
            errors.append(f"{name}: required non-empty string")
            return
        if not sentinel_ok and val.strip() == _SENTINEL:
            errors.append(f"{name}: sentinel '<not provided>' is not acceptable")

    _req_str(p.book,                  "book")
    _req_str(p.asof,                  "asof")
    _req_str(p.mandate,               "mandate")
    _req_str(p.expected_failure_mode, "expected_failure_mode")
    # liquidity_notes: sentinel acceptable in v1
    _req_str(p.liquidity_notes,       "liquidity_notes", sentinel_ok=True)

    # ── risk_direction ──
    if p.risk_direction not in _VALID_RISK_DIRECTIONS:
        errors.append(
            f"risk_direction: must be one of {sorted(_VALID_RISK_DIRECTIONS)}, "
            f"got {p.risk_direction!r}"
        )

    # ── packet_version ──
    if not isinstance(p.packet_version, int) or p.packet_version < 1:
        errors.append(f"packet_version: must be a positive int, got {p.packet_version!r}")

    # ── holdings ──
    if not isinstance(p.holdings, list):
        errors.append("holdings: must be a list")
    else:
        gross = 0.0
        for i, h in enumerate(p.holdings):
            if not isinstance(h, dict):
                errors.append(f"holdings[{i}]: must be a dict")
                continue
            t = h.get("ticker")
            w = h.get("weight")
            if not isinstance(t, str) or not t.strip():
                errors.append(f"holdings[{i}].ticker: required non-empty string")
            try:
                wf = float(w)
            except (TypeError, ValueError):
                errors.append(f"holdings[{i}].weight: must be a number, got {w!r}")
                continue
            if not (0.0 < wf <= 1.0):
                errors.append(
                    f"holdings[{i}] ({t}): weight {wf} not in (0, 1]"
                )
            gross += wf
        if gross > 1.0 + _CASH_TOLERANCE:
            errors.append(
                f"holdings weights sum to {gross:.4f} which exceeds 1.0 + cash tolerance "
                f"({1.0 + _CASH_TOLERANCE})"
            )

    # ── falsifiers ──
    if not isinstance(p.falsifiers, list) or len(p.falsifiers) == 0:
        errors.append("falsifiers: must be a non-empty list")
    else:
        for i, f in enumerate(p.falsifiers):
            if not isinstance(f, str) or not f.strip():
                errors.append(f"falsifiers[{i}]: must be non-empty prose")
            elif f.strip() == _SENTINEL:
                errors.append(
                    f"falsifiers[{i}]: sentinel '<not provided>' is not acceptable"
                )

    # ── evidence_planes / source_provenance — list types ──
    if not isinstance(p.evidence_planes, list):
        errors.append("evidence_planes: must be a list")
    if not isinstance(p.source_provenance, list):
        errors.append("source_provenance: must be a list")

    # ── portfolio_delta present ──
    if not isinstance(p.portfolio_delta, dict):
        errors.append("portfolio_delta: must be a dict (use compute_delta())")
    else:
        for key in ("n_added", "n_dropped", "n_resized", "gross_before", "gross_after", "turnover"):
            if key not in p.portfolio_delta:
                errors.append(f"portfolio_delta: missing key '{key}'")

    # ── risk_direction consistency check ──
    # Recompute delta from submitted holdings vs prior_book.
    # If gross_after > gross_before + 2pp → it IS increase, regardless of what the Brain claims.
    # This check is FORCED CORRECTION (not just rejection): validate() detects and reports it;
    # build_packet_from_submission() applies the correction before validation so the packet
    # that reaches the downstream layer always carries the correct risk_direction.
    # We report here as an error so callers know the Brain's claim was wrong.
    if p.risk_direction in _VALID_RISK_DIRECTIONS and isinstance(p.holdings, list):
        try:
            delta = compute_delta(p.holdings, prior_book)
            g_before = delta["gross_before"]
            g_after  = delta["gross_after"]
            if g_after > g_before + 0.02 and p.risk_direction != "increase":
                errors.append(
                    f"risk_direction: Brain claimed '{p.risk_direction}' but gross_after "
                    f"({g_after:.4f}) > gross_before ({g_before:.4f}) + 2pp — this IS an "
                    f"increase; packet must be corrected (use build_packet_from_submission)"
                )
            elif g_after < g_before - 0.02 and p.risk_direction == "increase":
                errors.append(
                    f"risk_direction: Brain claimed 'increase' but gross_after "
                    f"({g_after:.4f}) < gross_before ({g_before:.4f}) - 2pp — this is a "
                    f"reduction; packet must be corrected"
                )
        except Exception:  # noqa: BLE001 — validator must never raise
            pass

    ok = len(errors) == 0
    return ok, errors


# ---------------------------------------------------------------------------
# record_rejection — append-only ledger
# ---------------------------------------------------------------------------

def _rejections_path(root: str | Path | None = None) -> Path:
    if root is None:
        base = Path(__file__).resolve().parent.parent / "data"
    else:
        base = Path(root) / "data"
    p = base / "governance" / "packet_rejections.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def record_rejection(
    packet_dict: dict[str, Any],
    errors: list[str],
    *,
    root: str | Path | None = None,
) -> str | None:
    """Append a rejected packet to data/governance/packet_rejections.jsonl.

    Append-only; never raises.  Error strings are truncated to 300 chars each
    to keep line lengths sane.  The stored packet_dict is serialised with
    default=str so numpy/date objects survive.

    Returns the rejection_id (SHA-256[:16] of ts + book + asof) or None on any
    error.
    """
    try:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        book  = str(packet_dict.get("book")  or "")
        asof  = str(packet_dict.get("asof")  or "")
        run_id = str(packet_dict.get("run_id") or "")
        raw = f"{ts}|{book}|{asof}|{run_id}"
        rejection_id = hashlib.sha256(raw.encode()).hexdigest()[:16]

        # Truncate individual error strings to keep line lengths reasonable
        trimmed_errors = [str(e)[:300] for e in (errors or [])]

        # Sanitise the stored packet — keep all fields but cap long strings
        safe_packet: dict[str, Any] = {}
        for k, v in packet_dict.items():
            if isinstance(v, str) and len(v) > 1000:
                safe_packet[k] = v[:1000] + "...<truncated>"
            else:
                safe_packet[k] = v

        row: dict[str, Any] = {
            "ts":            ts,
            "rejection_id":  rejection_id,
            "book":          book,
            "asof":          asof,
            "run_id":        run_id,
            "errors":        trimmed_errors,
            "packet":        safe_packet,
        }
        p = _rejections_path(root)
        line = json.dumps(row, separators=(",", ":"), default=str)
        with p.open("a") as fh:
            fh.write(line + "\n")
        return rejection_id
    except Exception as exc:  # noqa: BLE001
        log.warning("control_plane.decision_packet.record_rejection failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# build_packet_from_submission — adapter from existing submit_book shapes
# ---------------------------------------------------------------------------

_RISK_DIRECTION_TOLERANCE = 0.02  # 2pp threshold for forced correction


def build_packet_from_submission(
    book: str,
    submission_dict: dict,
    prior_book: dict,
    extras: dict | None = None,
) -> "DecisionPacket":
    """Adapt an existing submit_book submission to a DecisionPacket.

    Maps the EXISTING submit_book shapes (brain/heavyweight_mcp.py submit_book args
    + what bot/autonomous.py receives) onto the packet schema.  Absent narrative
    fields default to the sentinel "<not provided>" — the validator decides which
    are acceptable.

    FORCED CORRECTION: risk_direction is computed from the portfolio_delta, not
    trusted from the Brain.  If gross_after > gross_before + 2pp → "increase";
    if gross_after < gross_before - 2pp → "reduce"; else "preserve".  The
    `_risk_direction_overridden` flag is set True when the Brain's claimed value
    is overridden.  This means no invalid packet ever reaches sizing because of a
    wrong risk_direction claim.

    Parameters
    ----------
    book            : portfolio_id, e.g. "heavyweight"
    submission_dict : the raw dict from read_submission() — keys: holdings,
                      summary, sold_note, gross; each holding has ticker/weight/rationale
    prior_book      : the book's last-known state (paper_account._load_account shape or
                      plain weight map — same shapes as compute_delta accepts)
    extras          : optional additional fields — any of: run_id, mandate, evidence_planes,
                      source_provenance, falsifiers, liquidity_notes, expected_failure_mode,
                      risk_direction (claim — will be validated/corrected)

    Returns
    -------
    DecisionPacket (never raises; malformed inputs produce a sentinel-filled packet)
    """
    try:
        extras = extras or {}

        # ── holdings: normalise to [{ticker, weight}] ──
        raw_holdings = submission_dict.get("holdings") or []
        holdings: list[dict] = []
        for h in raw_holdings:
            if not isinstance(h, dict):
                continue
            t = str(h.get("ticker") or "").upper().strip()
            try:
                w = float(h.get("weight") or 0.0)
            except (TypeError, ValueError):
                w = 0.0
            if t and w > 0:
                holdings.append({"ticker": t, "weight": w})

        # ── portfolio_delta ──
        delta = compute_delta(holdings, prior_book)

        # ── risk_direction: compute from delta, then force-correct ──
        brain_claimed = str(extras.get("risk_direction") or "").strip().lower()
        g_before = delta["gross_before"]
        g_after  = delta["gross_after"]
        if g_after > g_before + _RISK_DIRECTION_TOLERANCE:
            computed_direction = "increase"
        elif g_after < g_before - _RISK_DIRECTION_TOLERANCE:
            computed_direction = "reduce"
        else:
            computed_direction = "preserve"

        risk_direction_overridden = (
            brain_claimed in _VALID_RISK_DIRECTIONS
            and brain_claimed != computed_direction
        )

        # ── narrative fields — default to sentinel ──
        asof = str(extras.get("asof") or submission_dict.get("asof") or "").strip()
        if not asof:
            from datetime import date
            asof = date.today().isoformat()

        mandate = str(extras.get("mandate") or "").strip() or _SENTINEL
        evidence_planes = extras.get("evidence_planes") or []
        source_provenance = extras.get("source_provenance") or []
        falsifiers = extras.get("falsifiers") or []
        liquidity_notes = str(extras.get("liquidity_notes") or "").strip() or _SENTINEL
        expected_failure_mode = str(extras.get("expected_failure_mode") or "").strip() or _SENTINEL
        run_id = str(extras.get("run_id") or "").strip()

        submitted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        return DecisionPacket(
            book=book,
            asof=asof,
            mandate=mandate,
            holdings=holdings,
            evidence_planes=list(evidence_planes),
            source_provenance=list(source_provenance),
            falsifiers=list(falsifiers),
            liquidity_notes=liquidity_notes,
            portfolio_delta=delta,
            expected_failure_mode=expected_failure_mode,
            risk_direction=computed_direction,
            packet_version=PACKET_VERSION,
            run_id=run_id,
            submitted_at=submitted_at,
            _risk_direction_overridden=risk_direction_overridden,
        )

    except Exception as exc:  # noqa: BLE001 — adapter must never raise
        log.warning("build_packet_from_submission failed: %s", exc)
        # Return a minimally-identifiable sentinel packet so the validator can reject it
        return DecisionPacket(
            book=str(book or ""),
            asof="",
            mandate=_SENTINEL,
            holdings=[],
            evidence_planes=[],
            source_provenance=[],
            falsifiers=[],
            liquidity_notes=_SENTINEL,
            portfolio_delta={},
            expected_failure_mode=_SENTINEL,
            risk_direction="preserve",
            packet_version=PACKET_VERSION,
            run_id="",
            submitted_at="",
            _risk_direction_overridden=False,
        )

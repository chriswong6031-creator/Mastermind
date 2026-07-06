"""control_plane.guardrail — R3 severity taxonomy and GuardrailResult.

R3 (MASTERMIND_CONTROL_PLANE_MASTERPLAN.md) defines five severity levels
for Mastermind guardrail paths.  This module supplies the typed enum and a
reporting dataclass; ENFORCEMENT stays at the call site.

Design law (Charter P2)
-----------------------
Downgrade on ambiguity.  Missing/stale/wrong data may coarsen/freeze/shrink;
it may NEVER un-cap, raise authority, or flip direction.

The ``GuardrailResult`` REPORTS what happened.  The call site is responsible
for having taken the conservative action BEFORE constructing the result.

Severity hierarchy (weakest → strongest)
-----------------------------------------
TELEMETRY_ONLY  — logging/cost failures; never changes execution path.
ADVISORY_ONLY   — lens/journal/dashboard failures; execution continues.
SHRINK          — partial peer data, stale non-anchor sizing artifacts.
FREEZE          — firm-clamp/book-cap exception, all-expected-peers-missing,
                  stale anchor artifacts beyond freshness budget.
                  No new risk; holds and de-risking still allowed.
HARD_STOP       — layer unavailable/corrupt, account state unreadable/unwritable,
                  same-book run-lock conflict, settlement failure mid-flight,
                  auth disabled in production (startup refusal).

Logging
-------
``.log()`` appends to run_events.jsonl via ``control_plane.run_events.append``.
Only ADVISORY_ONLY, SHRINK, FREEZE, HARD_STOP are logged.
TELEMETRY_ONLY is intentionally silent (avoid log spam from cost/metrics paths).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class Severity(enum.Enum):
    """R3 severity taxonomy — ordered weakest to strongest."""
    TELEMETRY_ONLY = 0
    ADVISORY_ONLY  = 1
    SHRINK         = 2
    FREEZE         = 3
    HARD_STOP      = 4

    def __ge__(self, other: "Severity") -> bool:  # type: ignore[override]
        if isinstance(other, Severity):
            return self.value >= other.value
        return NotImplemented

    def __gt__(self, other: "Severity") -> bool:  # type: ignore[override]
        if isinstance(other, Severity):
            return self.value > other.value
        return NotImplemented

    def __le__(self, other: "Severity") -> bool:  # type: ignore[override]
        if isinstance(other, Severity):
            return self.value <= other.value
        return NotImplemented

    def __lt__(self, other: "Severity") -> bool:  # type: ignore[override]
        if isinstance(other, Severity):
            return self.value < other.value
        return NotImplemented


@dataclass
class GuardrailResult:
    """Typed record of a guardrail check outcome.

    Attributes
    ----------
    ok           : True if the check passed (no action required).
    severity     : The severity associated with this result.
    guard        : Stable name of the guard that produced this result, e.g.
                   "peer_freshness", "book_cap", "run_lock".
    detail       : Human-readable explanation.
    action_taken : What the call site did (must be set before constructing
                   this result).  On ``ok=True`` this is typically "none".
    """
    ok:           bool
    severity:     Severity
    guard:        str
    detail:       str
    action_taken: str
    extra:        dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # constructors
    # ------------------------------------------------------------------

    @classmethod
    def passed(cls, guard: str, *, detail: str = "", extra: dict | None = None) -> "GuardrailResult":
        """Construct a passing result (TELEMETRY_ONLY severity, ok=True)."""
        return cls(
            ok=True,
            severity=Severity.TELEMETRY_ONLY,
            guard=guard,
            detail=detail,
            action_taken="none",
            extra=extra or {},
        )

    @classmethod
    def failed(
        cls,
        guard: str,
        severity: Severity,
        detail: str,
        action_taken: str,
        *,
        extra: dict | None = None,
    ) -> "GuardrailResult":
        """Construct a failing result.

        The call site must have already taken the conservative action before
        calling this constructor (Charter P2: enforcement is at the call site).
        """
        return cls(
            ok=False,
            severity=severity,
            guard=guard,
            detail=detail,
            action_taken=action_taken,
            extra=extra or {},
        )

    # ------------------------------------------------------------------
    # logging
    # ------------------------------------------------------------------

    def log(self, *, job: str = "", book: str = "") -> str | None:
        """Append this result to the run-events ledger.

        Only ADVISORY_ONLY and above are logged.
        TELEMETRY_ONLY is intentionally silent.

        Returns the event_id, or None on error or when the result is not
        logged (TELEMETRY_ONLY).
        """
        if self.severity <= Severity.TELEMETRY_ONLY:
            return None
        try:
            from control_plane import run_events  # local import — avoids circular
            return run_events.append({
                "kind":     "guardrail",
                "job":      job,
                "book":     book,
                "step":     self.guard,
                "status":   "ok" if self.ok else "error",
                "severity": self.severity.name,
                "err":      None if self.ok else self.detail,
                "extra": {
                    "guard":        self.guard,
                    "detail":       self.detail,
                    "action_taken": self.action_taken,
                    **self.extra,
                },
            })
        except Exception:  # noqa: BLE001
            return None

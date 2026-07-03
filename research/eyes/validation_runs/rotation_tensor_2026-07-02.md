# Perception validation — `rotation_tensor` — FAIL

*Run 2026-07-03T01:24:28.438333+00:00 · harness `scripts/validate_perception.py` (W-E.1 task E1.4) · charter P3 (honest pass/fail)*

> This verdict DECIDES what E2/E3 may arm. The harness WIRES NOTHING — a human (Fable) reads this and makes the arming call.

## Verdict: FAIL

**Pre-registered gate.** AUC > 0.55 AND fires on < 10% of sessions.

**Window.** 2011-01-01 → latest (stride 1 sessions), 3888 evaluable sessions.

| metric | value | gate |
|---|---|---|
| AUC (signal vs SPY 5d fwd max-drawdown event) | 0.5426 | > 0.55 → FAIL |
| fire fraction | 0.0051 | < 0.1 → PASS |
| base event rate | 0.1793 | (reference) |
| conditional event rate on fire | 0.2 | (lift-over-base read) |
| defensive episodes seen | 467 | — |
| times fired | 20 | — |

**Arming decision.** rotation_tensor stays DISPLAY-ONLY (advisory plane; cannot size)

Auto-demotion falsifier (build-plan §3): the composite stays DISPLAY-ONLY — a negative shadow Brier vs coin-flip would put it on the do-not-size list.

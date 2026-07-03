# Perception validation — `label_vs_planes` — FAIL

*Run 2026-07-03T01:24:28.439542+00:00 · harness `scripts/validate_perception.py` (W-E.1 task E1.4) · charter P3 (honest pass/fail)*

> This verdict DECIDES what E2/E3 may arm. The harness WIRES NOTHING — a human (Fable) reads this and makes the arming call.

## Verdict: FAIL (cold-start, UNCOMPUTABLE with vendored history)

**Reason.** the walk-forward inputs do not exist over 2011-2026 with vendored data — the regime file is a single live snapshot, not a historical series

**What would make it computable.** a DAILY historical market_view series (label direction + validated-plane consensus) — which needs historical regime blocks + historical per-ticker mtf_signals. NONE are vendored. The live forward log of conflict-days (E1.3 wake trigger) must accrue months of resolved calls before an AUC gate has power.

**Precedent (P3).** the incident fixture PROVES the conflict fires on the 06-26..07-01 tape (5 sessions), but that is a replay assert, not a walk-forward gate. Ships ADVISORY.

**Arming decision.** label_vs_planes ships ADVISORY / cold_start=true — the notch/tilt seam stays DARK until a forward-graded historical series exists to gate it

Per the charter's degrade-never-fabricate rule, the harness does NOT substitute a price-only proxy and call it this alarm — a truthful non-result is the correct output.

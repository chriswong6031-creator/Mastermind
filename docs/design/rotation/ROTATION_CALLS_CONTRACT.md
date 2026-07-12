# `rotation_calls.v1` — the rotation-identification ↔ consumption contract

**Status:** authoritative conformance spec for BOTH sessions.
**Owners:** the *identification* session emits; the *consumption* session (Mastermind bot) reads.
**Reader:** `brain/rotation_intake.py` is the SOLE reader of this artifact firm-wide. Nothing else
opens the file. If you are adding a rotation consumer, import `rotation_intake` — never re-open the
JSON.

This document is the seam. It is intentionally short and testable: the schema, the state machine,
the invariants that make the seam safe, the two publication homes still open for a ruling, and the
freshness budget. The other session builds its emitter to this spec; the golden fixture
(`tests/fixtures/rotation_calls_v1.json`) is the executable copy.

---

## 1. Division of labor — "they IDENTIFY, we CONSUME"

| The identification session OWNS | The consumption session (bot) OWNS |
|---|---|
| The detection science: *which* sectors / themes / subsectors / names are turning, in *which* direction, with *what* per-call confidence, at *what* cadence. | The sole reader (`rotation_intake`), schema + freshness validation, the absence handshake. |
| Emitting **EARLY / TURNING** states, not only confirmed turns. | target → member-ticker **expansion** (via the existing basket machinery). |
| Per-call `evidence[]` and the `falsifier`. | The pre-ignition watchlist, every funnel consumer, **all sizing discipline** (starters until CONFIRMED). |
| The `state_history` for each call. | The `consumption_ledger.jsonl` (keyed by `call_id`) + grading of resolved outcomes. |

Neither side writes the other's files. The bot never re-derives the identification signal —
`evidence[]` is **opaque**: logged verbatim, never recomputed. The identification session never
reads the bot's ledger except as *its* calibration ground truth (see §5).

---

## 2. Schema

```jsonc
{
  "schema": "rotation_calls.v1",
  "as_of": "YYYY-MM-DD",          // the trading date this snapshot reflects — drives freshness
  "generated_at": "ISO-8601",      // wall-clock emit time (provenance only)
  "engine_version": "string",      // identification engine build id (provenance only)
  "calls": [
    {
      "call_id": "string",              // IMMUTABLE JOIN KEY — see §4
      "target_kind": "sector|theme|subsector|ticker",
      "target": "string",               // sector/theme/subsector id (matches a basket id/slug) OR a ticker
      "members": ["TICK", ...] | null,   // explicit member set; null → the reader expands via baskets
      "state": "EARLY|TURNING|CONFIRMED|FAILED|EXPIRED",
      "direction": "rotation_in|rotation_out",
      "confidence": 0.0,                 // 0–1
      "horizon_bdays": 20,               // expected horizon in business days
      "evidence": [                      // OPAQUE to the reader — logged verbatim, never recomputed
        {"source": "string", "value": <any>, "note": "string"}
      ],
      "falsifier": {                     // the condition that proves the call wrong
        "text": "human-readable falsifier",
        "check": {
          "kind": "rel_return",          // machine-checkable check kind
          "subject": "TICK|basket",
          "benchmark": "SPY|XLK|...",
          "horizon_bdays": 20,
          "op": "<|<=|>|>=",
          "value": 0.0
        },
        "check_by": "YYYY-MM-DD"
      },
      "first_seen": "YYYY-MM-DD",
      "state_history": [ {"date": "YYYY-MM-DD", "state": "EARLY"}, ... ]
    }
  ]
}
```

### Reader validation (what `rotation_intake` actually enforces)

Two tiers, deliberately minimal so the seam can evolve additively (§4):

1. **Envelope gate** (all-or-nothing — failure ⇒ `[]`, the absence handshake):
   - `schema == "rotation_calls.v1"`
   - `as_of` present and parseable
   - `as_of` fresh: `age_days ≤ 2` (see §6)
2. **Per-call gate** (a bad call is SKIPPED, never fatal to the read): a call is kept iff it is a
   dict with a non-empty string `call_id`, a `state` in the state enum, and a `target_kind` in the
   target-kind enum. Everything else (`evidence`, `falsifier`, `members`, `confidence`, …) is passed
   through opaque — a malformed or missing optional field never drops an otherwise-valid call.

The reader validates the **join key, the state machine, and the target kind** — and nothing else —
precisely so the emitter can add fields under v1 without a reader change.

---

## 3. State machine

```
        ┌─────────┐      ┌──────────┐      ┌────────────┐
        │  EARLY  │ ───► │ TURNING  │ ───► │ CONFIRMED  │
        └─────────┘      └──────────┘      └────────────┘
             │                │                  │
             └────────────────┴──────────────────┘
                              ▼
                     ┌──────────────────┐
                     │  FAILED / EXPIRED │   (terminal)
                     └──────────────────┘
```

- **EARLY → TURNING → CONFIRMED** is the forward progression: a turn detected, then strengthening,
  then confirmed. The identification session **must emit EARLY and TURNING**, not only confirmed
  turns — unconfirmed states get *eyes* immediately; only *size* waits (this is the operator's
  stated goal, roadmap §2).
- **FAILED** / **EXPIRED** are the terminal states. FAILED = the falsifier tripped or the turn
  reversed; EXPIRED = the horizon elapsed without confirmation.
- **State regression is read as FAILED.** If a call's state moves *backward* on the ladder
  (e.g. CONFIRMED → TURNING, or TURNING → EARLY), the consumption side reads that as a de facto
  FAILED — a turn that un-confirms is a turn that failed. (The emitter should prefer to emit an
  explicit FAILED; but a regression is treated as one regardless.)
- **Terminal calls are still published** (state_history is the ground truth) but are **not
  "active"** for candidacy: `active_call_for()` skips FAILED / EXPIRED. `calls()` still returns them
  so the ledger and grader see the full lifecycle.

---

## 4. Seam invariants

These are the properties that make the seam safe. Both sessions conform to them.

1. **`call_id` is the immutable join key.** Once emitted for a call it never changes, across state
   transitions, across days, across artifact rewrites. It is how the consumption ledger, the
   pre-ignition watchlist, and the grader all join back to the identification call. Re-labelling the
   same underlying turn with a new `call_id` breaks the join and orphans its history — don't.
2. **Absence ≠ all-clear.** No file, a stale file (as_of > 2 sessions), or a malformed/wrong-schema
   file all mean **"no calls today"**, never "all clear". The reader returns `[]` in every degraded
   state, and every downstream lane is provably inert on `[]`. Missing data may coarsen or freeze;
   it may never un-cap, raise authority, or flip direction.
3. **Additive-only evolution under v1.** The emitter may ADD fields to a call (or to the envelope)
   without a schema bump; the reader ignores unknown fields. It may NOT rename or remove a field, or
   change a field's meaning, or change the state / target-kind vocabularies, under `v1` — that is a
   `v2`. The reader validates only the join key + state + target_kind, so additive growth is free.
4. **They emit EARLY / TURNING, not only confirmed.** (See §3.) The whole point of the seam is early
   eyes; a source that only emits CONFIRMED defeats it.
5. **They never write our files; we never re-derive their signal.** The identification session writes
   only the `rotation_calls.json` artifact. The consumption session writes only its own ledgers /
   watchlist. `evidence[]` is opaque to us.
6. **The consumption ledger IS their calibration ground truth.** Our `consumption_ledger.jsonl`
   (keyed by `call_id`) + the resolved outcomes are what the identification session calibrates its
   confidence against. Reciprocally, our divergence-clue density is an input feature to their
   sector-turn identification. The `call_id` join makes both directions possible.

---

## 5. `evidence[]` and the falsifier — opaqueness

`evidence[]` entries (`{source, value, note}`) are **logged verbatim and never recomputed** by the
reader. The bot does not second-guess *why* the identification engine called a turn; it consumes the
call, applies its own sizing discipline, and grades the outcome. This is the "we never re-derive
their signal" invariant in practice: re-deriving would (a) couple the two engines' internals and (b)
double-count the same evidence in the bot's own sizing.

The `falsifier` is the identification session's pre-registered kill condition for the call. The bot
grades against it (did the `check` trip by `check_by`?) and feeds the verdict back through the
`call_id`-keyed ledger. A call with no falsifier is still valid to read but is un-gradable
machine-side — the emitter should always ship one.

---

## 6. Freshness budget — 2 sessions

The budget is **2 trading sessions**. The reader implements it with **calendar days as a
deliberately-tighter proxy**: `age_days = today − as_of` in calendar days, stale iff `age_days > 2`.

Calendar-day arithmetic is strictly tighter than trading-day arithmetic (a weekend counts, so a
Friday call is stale by Monday under this proxy). That is the conservative direction — the reader
would rather drop a marginally-old call ("no calls today") than trust a stale one. If the seam later
needs the exact trading-day budget, the reader swaps its `_age_days` for a trading-day counter (cf.
`brain/regime_frame._trading_days_since`); the gate logic is otherwise unchanged. The 2-session
budget is documented here so both sessions agree on when a call goes dark.

An `as_of` that is absent or unparseable is treated as **stale** (fail-closed), never as fresh.

---

## 7. Publication home — OPEN (operator ruling pending, roadmap §4.2)

Two options, not yet decided. The reader supports both today via a primary+fallback path so the
ruling is a one-line change:

- **Option A — bot-side data plane:** `data/rotation/rotation_calls.json` under the repo root.
  Git-tracked with the bot; simplest join to the bot's ledgers; the reader's PRIMARY path.
- **Option B — macro-side site plane:** `vendor/macro/site/rotationdata/rotation_calls.json`.
  Published alongside the other `site/*.json` signal contracts (and eligible for the same R2 data
  plane); the reader's FALLBACK path, tried only if the primary is absent. A variant of B publishes
  the calls *inside* the NW artifact instead of standalone — that would need a reader change and is
  explicitly out of scope for v1.

Also pending a joint ruling: the **state vocabulary must match across both programs** so the
calibration buckets align (the rotation state ladder here and the NW / cycle programs' state tags).

---

## 8. The fallback-synth bridge (until the emitter ships)

Until the identification engine ships a real artifact, `rotation_intake.synthesize_fallback()`
fills the lane with a deterministic **identification-LITE** read composed only from in-repo signals
(`regime_frame.cycles()` entry-favored sectors + `rotation_tensor` top-pair leads). Every synthesized
call is tagged `provenance="fallback_synth"` and its `confidence` is capped at **0.5** so it can
never impersonate a real EARLY→CONFIRMED call, and its `call_id` is namespaced `synth:…` so it never
collides with a real join key. The fallback doubles as the **executable reference semantics** for the
emitter, and is retired after 5 consecutive present + fresh real-artifact builds (or kept permanently
as a disagreement check — operator ruling pending, roadmap §4).

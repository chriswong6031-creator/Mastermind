# Macro → Bot Publication Handoff — the 3 contracts that activate the rework

**As of 2026-07-12.** The Flagship superintelligence rework is built, deployed, and running live (Mac +
box). Its candidacy readers are **fail-soft and DARK — inert until the macro dashboard publishes three
artifacts.** This doc is the authoritative spec for the macro (`macro-x` / `vendor/macro`) side: publish
these three JSONs on the stated cadence and the bot-side readers light up. Nothing here needs bot code —
the readers already exist and validate against exactly these schemas.

**Activation is a two-step gate per contract:** (1) macro publishes the artifact → the bot reader's
`audit_row()` flips to `status:"present"`; (2) after N fresh present-builds, the operator arms the bot-side
flag (see each section). Publishing alone changes **nothing** in the book until the flag is armed — so it's
safe to ship the publishers first and validate in shadow.

---

## Contract 1 — Neural Web context  `neural_web_mastermind_context.v1`

**What it unblocks:** NW-driven candidacy, subtract-only entry-shrink, the "clean-in-conflicted" safe-haven
tell, and the whole-universe NW passthrough. **Highest breadth impact.**

- **Bot reader:** `brain/neural_web_context.py` (sole reader; `context()`, `candidate()`, `market_plane()`,
  `decision_signals()`, `audit_row()`). Never imports the macro engine.
- **Path the bot reads:** `vendor/macro/site/neuralwebdata/mastermind_context.json`
  (git-tracked under `site/`, or R2 — if R2, add `neuralwebdata` to `macro_refresh._R2_DIRS`).
- **Freshness budget:** `as_of` age > **4 calendar days** → treated as absent-stale.
- **Schema (validated fields the reader checks):**
  ```json
  {
    "schema": "neural_web_mastermind_context.v1",
    "is_context_only": true,                 // MUST be true — the bot rejects anything else
    "as_of": "YYYY-MM-DD",
    "lobes": {
      "market": {
        "verdict":  {"label_en": "...", "verdict": "..."},
        "regime":   {"quad": 1, "quad_name": "...", "confidence": 0.0, "cycle_tag": "...",
                     "transition_state": "...", "flip_margin": 0.0, "liquidity_overlay": "..."},
        "vol":      {"label_en": "..."},
        "breadth":  {"label_en": "..."}
      },
      "contradictions": {"summary": {...}, "records": [ ... ]},   // len(records) → contradiction_count
      "bottom_sensors": { ... }                                   // optional; feeds defensive/rotation reads
    },
    "candidate_context": {
      "AAPL": {
        "bottom":  {"bottom_state": "WATCH|BOTTOMING|CONFIRMED|neutral"},
        "options": {"gate_status": "..."},                        // enriches reason text only
        "graph_conflicts": [ ... ],                               // len ≥2 → subtract-only entry shrink
        "kernel":  {"fdr_cleared": true}                          // false → per-name INERT (display-armed only)
      }
      // ... one row per ticker the NW has an opinion on
    },
    "gap_notes": [ ... ]
  }
  ```
- **STRUCTURAL RULE (already enforced by the reader):** cortex/memo prose is NEVER read. Only the structured
  fields above. `is_context_only:true` is mandatory. `kernel.fdr_cleared:false` makes a name display-only.
- **Cadence:** ≥1× per build day (daily). Stamp `as_of` to the data date.
- **Bot flag to arm after publish:** `MASTERMIND_NW_DECISION` ladder `off→shadow→candidacy→shrink→vote`
  (also `MASTERMIND_NW_CONTEXT=1` for text injection). Arm to `candidacy` after 5 consecutive present builds
  in `data/brain/nw_context_audit.jsonl`.

---

## Contract 2 — Rotation calls  `rotation_calls.v1`

**What it unblocks:** rotation-in candidacy (buy the turn, not just the confirmation) + the pre-ignition
watchlist that holds names through UNCONFIRMED turns. **This is the "another session owns identification"
seam** — *they IDENTIFY, we CONSUME.* Full spec: [ROTATION_CALLS_CONTRACT.md](ROTATION_CALLS_CONTRACT.md).

- **Bot reader:** `brain/rotation_intake.py` (`calls()`, `active_calls()`, `expand()`, `audit_row()`).
  Until this artifact ships, the bot runs `synthesize_fallback()` (identification-lite, confidence ≤0.5,
  `provenance:"fallback_synth"`) so the lane isn't empty — retire that once 5 present builds land.
- **Path the bot reads (primary → fallback):** `data/rotation/rotation_calls.json`  → 
  `vendor/macro/site/rotationdata/rotation_calls.json`. **OPEN DECISION for the macro side:** publish
  standalone at the site path (needs an `_R2_DIRS`/`_SPARSE_PATHS` + anchor entry) OR embed inside the NW
  artifact (`candidate_context[T].bottom.bottom_state` + `lobes.bottom_sensors`) → zero extra receivers.
- **Freshness budget:** **2 sessions** (calendar-day proxy). Absent/stale = "no calls today", never "all clear".
- **Schema (superset):**
  ```json
  {"schema":"rotation_calls.v1", "as_of":"YYYY-MM-DD", "generated_at":"...", "engine_version":"...",
   "calls":[
     {"call_id":"rot_2026-07-11_XLV_a1b2",          // IMMUTABLE join key
      "target_kind":"sector|theme|subsector|ticker", "target":"XLV", "members":["...",null],
      "state":"EARLY|TURNING|CONFIRMED | FAILED|EXPIRED",
      "direction":"rotation_in|rotation_out", "confidence":0.0, "horizon_bdays":21,
      "evidence":[{"source":"...","value":0,"note":"..."}],    // opaque — logged verbatim, never recomputed
      "falsifier":{"text":"...","check":{"kind":"rel_return","subject":"XLV","benchmark":"SPY",
                   "horizon_bdays":21,"op":">","value":0}, "check_by":"YYYY-MM-DD"},
      "first_seen":"YYYY-MM-DD", "state_history":[{"date":"...","state":"EARLY"}]}
   ]}
  ```
- **Seam invariants (binding):** `call_id` immutable across transitions; **emit EARLY/TURNING, not only
  CONFIRMED** (the operator's whole goal — unconfirmed states get eyes immediately, only *size* waits);
  state regression is read as FAILED; additive-only under v1. Our `data/rotation/consumption_ledger.jsonl`
  (keyed by call_id) is YOUR calibration ground truth (shared scoreboard, no self-grading).
- **Bot flag to arm:** `MASTERMIND_ROTATION_IN` = `off|watch|starter`. `watch` = park + candidacy; `starter`
  = 0.5× pre-ignition buys (evidence-gated, ≥12 resolved outcomes).

---

## Contract 3 — Buy-board track record  `us_board_track_record.v1`

**What it unblocks:** the board-outcome LEARNING LOOP (the bot learns which board setups actually have edge
and shrinks its trust accordingly) + the single-stock divergence detector's point-in-time grounding (the
AAPL-Jul-1 case). Full spec: [BOARD_TRACK_RECORD_CONTRACT.md](BOARD_TRACK_RECORD_CONTRACT.md).

- **Bot readers:** `brain/board_track_record.py` (`records()`, `surfaced_on()`, `board_stats()`,
  `forward_grade()`) + `brain/board_learning.py` (edge verdict → shrink-only trust multiplier).
- **Source of truth (macro side):** `data/us_board_ledger/retro_grades.parquet` (append-only, keep-FIRST
  per (date,ticker)) — the same ledger `vendor/macro/site/us_stocks.html` "names that left the buy board"
  renders from. **Publish a JSON projection of it** at:
  `vendor/macro/site/factordata/us_board_track_record.json` (fallback `data/us_board_ledger/track_record.json`).
- **Freshness budget:** 5 calendar days.
- **Schema (JSON projection):**
  ```json
  {"schema":"us_board_track_record.v1", "as_of":"YYYY-MM-DD", "window_sessions":21,
   "rows":[
     {"ticker":"AAPL", "sector":"Technology", "surfaced":"2026-07-01",   // first-on-board date (immutable)
      "return_pct":7.4, "status":"running|stopped|flat",
      "board_pos":1, "fwd_mfe_pct":9.1, "on_board":true}
   ]}
  ```
  (`surfaced` = keep-FIRST date the name entered the board; `status` maps the ledger's terminal-state /
  cushion logic to running/stopped/flat; `on_board` = currently on today's board.)
- **Why this matters:** today the bot reads ONLY the volatile `us_standouts.json` daily snapshot (overwritten
  each build) and skips it entirely when `gate_go=false`. The persistent track record — the empirical proof
  the board has edge (e.g. AAPL surfaced 2026-07-01 → +7.4% running) — is currently invisible to the bot's
  decision + learning loop. This projection fixes that.
- **Bot flag to arm:** `MASTERMIND_BOARD_LEARNING=1` (shrink-only trust; safe to arm once present, MIN_N=12
  guards it). Divergence-clue grounding is automatic once the ledger is readable.

---

## Activation checklist (per contract)

1. **Publish** the artifact at the stated path on the stated cadence.
2. **Verify the bot sees it:** the reader's `audit_row()` (surfaced in the perception runlog / CIO
   `perception_health`) flips `absent → present`, `age_days` small, `n_*` non-zero.
3. **Watch N fresh present-builds** (NW: 5; rotation: 5 to retire fallback; board: 1).
4. **Arm the bot flag** at the next restart (add to the inline flag set) — starts in the safest mode
   (`candidacy`/`watch`/on), never `enforce`/`starter` until the graded record clears the bar.
5. **Grade:** the bot's consumption ledgers + resolved outcomes (keyed by call_id / ticker+date) feed back
   as YOUR calibration ground truth. No self-grading on either side.

## Open decisions for the operator / cross-session
- **Rotation artifact home:** standalone `site/rotationdata/` (needs anchor + R2/sparse wiring) vs embedded
  in the NW artifact (zero extra receivers). Pick one; the reader already supports both paths.
- **State vocabulary alignment:** rotation `EARLY/TURNING/CONFIRMED` vs NW `bottom_state
  WATCH/BOTTOMING/CONFIRMED` — record the authoritative enum mapping so calibration buckets match across both
  programs.
- **R2 vs git:** if any artifact goes to R2, add its dir to `data_layer/macro_refresh._R2_DIRS` +
  `_SPARSE_PATHS` and a freshness anchor in `config/contracts.yml`.

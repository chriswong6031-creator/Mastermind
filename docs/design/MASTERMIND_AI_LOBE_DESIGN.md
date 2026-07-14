# W-AI — The Mastermind AI lobe + the Orchestrator dialogue (design)

**Owner:** Fable · **Date:** 2026-07-13 · **Charter law:** P2 (degrade-never-raise), P3 (signals earn
authority), P6 (mistakes become machinery), P8 (autonomy earned in shadow) · **Extends:** W-L
(`research/MASTERMIND_LEARNING_DESIGN.md` — journal/agenda/self_tune, SHIPPED 2026-07-03) and the
three macro→bot contracts (`docs/design/rotation/MACRO_PUBLICATION_HANDOFF.md`).

**The user's mandate this answers:** a self-improving Mastermind AI that learns from wrong trades
(execution, selection, timing), learns to use Neural Web data better, and NUDGES the Neural Web's
master brain when context is inaccurate or missing — so the two systems upgrade each other. Plus:
operator settings/logs/improvements in the admin panel ("Mastermind AI" section), the master
orchestrator surfaced + highlighted at the top of Core in the Observatory with settings, a per-run
log, an every-5-runs progress review, and a chat box to wake it and direct it.

## 0. Identity map (the naming the user asked about)

- **The "master orchestrator"** is not a module today — it is the macro repo's nightly pipeline
  (`.github/workflows/daily.yml` engine job) over the `config/synapse.yml` signal-bus registry.
  This wave gives it a *face*: an **Orchestrator** hero card in the Observatory, a per-run log, a
  5-run review, settings, and a chat.
- **Cortex is NOT the orchestrator.** `engine/neuralweb/cortex.py` is the single LLM deliberation
  lobe (Opus, shadow probation, three write tools). The orchestrator card links to it but they are
  distinct.
- **The "Mastermind AI lobe"** is the bot's reflection engine (this wave, bot side), and it appears
  *inside* the Neural Web as `lobes.mastermind_ai` in `mastermind_context.v1` — the web sees the
  trader as one of its own lobes, closing the dialogue loop.

## 1. What already exists (extend, never duplicate)

- Mistake machinery: `brain/journal.py` (per-seat drafts/lessons/pins with falsifiers),
  `brain/improvement_agenda.py` (weekly ranked self-critique), `brain/self_tune.py`
  (bounded self-repair through the immutable `loop/harness.py` judge, `MASTERMIND_SELF_TUNE` OFF),
  `brain/experiment_registry.py`, `brain/calibration.py`, `brain/self_mirror.py` (armed),
  `brain/outcome_ledger.py` (graded outcomes; first cohort ~2026-07-17).
- Dialogue transport: bot→macro `bridge/nw_feedback.py` (`mastermind_nw_feedback.v2`, counts-only,
  public, pushed by `publish_macro_snapshot` twice daily — NEVER add a scheduler job for this);
  macro→bot `site/neuralwebdata/mastermind_context.json` read solely by
  `brain/neural_web_context.py`. Macro-side reader: `engine/neuralweb/mastermind_feedback.py`
  (whitelist, 4-day staleness, → `data/governance/mastermind_feedback_summary.json`).
- Loop cadence: `app/scheduler.py` `loop_maintenance` (23:45 UTC) — the bot's nightly learning tick.

## 2. Bot side (Mastermind repo)

### 2.1 `brain/nw_reflection.py` — the reflection engine (deterministic, no LLM, no authority)
`build(asof)` → `nw_reflection.v1` report; `persist(asof)` → `data/nw_reflection/latest.json`
+ append-only `history.jsonl` (keep-first per asof). Blocks:
- **contract_drift** — fields the bot's decision policy consumes vs what the live artifact actually
  carries: `graph_conflicts` presence rate, `bottom.bottom_state` vocabulary overlap with
  `NW_CANDIDACY_SCORES` keys, market-plane `contradiction_count`/liquidity availability. (Found in
  recon: live artifact has ZERO `graph_conflicts` and no BOTTOMING/CONFIRMED — the shrink path and
  0.50 candidacy priors are dead against live data. This detector's first real catch.)
- **coverage** — open theses + recently resolved subjects vs `candidate_context` rows (counts only).
- **attribution** — NW-signal usefulness vs graded outcomes. COLD-START HONEST: emits
  `{state:'building', n_resolved, joinable_n}` until decision-time NW stamps accrue; never
  fabricates a grade (P2/P3).
- **context_quality** — rolling present/stale/absent + gap_notes trend from
  `data/brain/nw_context_audit.jsonl`.
- **nudges** — ≤10 structured, stable-coded asks to the orchestrator:
  `{code, kind: contract_drift|coverage_gap|staleness|lobe_request, severity, detail, first_seen,
  builds_seen}`. Codes are sanitized tokens (public-surface safe), no tickers, no prose leakage.

### 2.2 `brain/mastermind_ai.py` — the loop coordinator, log, and settings
- `run_cycle(asof, trigger)` — one self-improvement tick, wired as a new best-effort step at the end
  of `_run_loop_maintenance_steps` (established extension point): journal `draft_all` +
  `recompute_pins` per seat (idempotent), `nw_reflection` build+persist, snapshot self_tune state +
  agenda top items; append one row to `data/mastermind_ai/loop_log.jsonl`
  `{ts, asof, run_id, trigger, loop_n, steps{...counts}, nudges_open, summary}`.
- **Every N loops (default 5, operator-settable):** a review row → `data/mastermind_ai/reviews.jsonl`
  — roll-up of the window (drafts added, lessons completed, pins changed, nudges raised/resolved,
  self_tune events) + a deterministic progress assessment against the W-L pre-registered falsifiers.
  Optional LLM prose assessment behind `MASTERMIND_AI_REVIEW_LLM` (default OFF, analyst tier).
- **Settings**: doctrine `mastermind_ai:` block defaults, merged with operator overrides in
  `data/mastermind_ai/settings.json` (bounded known keys only, written by the API — hot-adjustable
  without restart). Master env switch `MASTERMIND_AI_LOOP` (default ON — the cycle is purely
  observational: it writes files, never touches a book, a flag, or a prompt).
- **Directives**: `add_directive(text)` → scrubbed (secret patterns, $-amounts, 280-char cap) →
  `data/mastermind_ai/directives.jsonl` `{id, ts, text, status: queued|published|acknowledged}`.
  Statuses advance when the publisher ships them and when the macro ack (2.5) names them.

### 2.3 `bridge/nw_feedback.py` → v3
Schema bump to `mastermind_nw_feedback.v3` (macro reader accepts v1/v2/v3). New whitelisted,
counts-only blocks: `reflection` (drift codes + coverage counts + attribution state + quality
counts), `nudges` (≤10 coded rows), `operator_directives` (≤10, scrubbed text — the operator's own
deliberate public instructions). Same `_redact_secrets` backstop; same test bar
(`tests/test_nw_feedback.py`).

### 2.4 Agenda + flags + API
- `brain/improvement_agenda.py`: new source `_from_nw_reflection` (class `nw-context-drift`,
  weight 72) — high-severity nudges become evidence-cited agenda items (owner `opus-session`).
- `control_plane/flags.py` KNOWN_FLAGS += `MASTERMIND_AI_LOOP`, `MASTERMIND_AI_REVIEW_LLM`.
- API (bot): GET `/api/mastermind_ai` (status), `/loop_log`, `/improvements`, `/reflection`;
  POST `/api/mastermind_ai/settings`, `/directive`, `/run` — POSTs registered in
  `app/auth.py::_NON_LLM_OPERATOR_PATHS` (blocked on the serve-only mirror);
  `/api/mastermind_ai` added to `app/response_cache._DENY_PREFIXES`.

## 3. Macro side (macro repo, PR off origin/main)

### 3.1 `engine/neuralweb/orchestrator_log.py` — the run log + 5-run review
- `record_run(workflow)` — appends `data/neuralweb/orchestrator_runlog.jsonl`
  `{run_date, produced_at, workflow, lobes_total, lobes_stale, what_changed_n, contradictions_n,
  gaps_n, feedback_state, nudges_n, directives_n, cortex_status, summary}` (composed from
  health.json + daily_brief.json + mastermind_feedback_summary.json) and publishes
  `site/neuralwebdata/orchestrator_runlog.json` (array-rooted, last 60, sibling `.envelope.json` —
  the `health_history.json` precedent).
- Every `orchestrator.review_every_n_runs` (default 5) runs: a review entry → deterministic window
  roll-up + progress assessment (stale/gap/nudge trends, what was completed, cortex probation
  track) → `orchestrator_reviews.jsonl` + site copy (last 12).
- CLI `scripts/build_orchestrator_log.py`; runs in the daily.yml **cortex job** right after
  `build_neuralweb_brief --finalize` (sees final state; committed in the cortex lane).
- Registered in `config/synapse.yml` (runlog + reviews + site mirrors, owner_program neural-web).

### 3.2 Reverse-bridge v3 ingestion + ack
- `engine/neuralweb/mastermind_feedback.py`: accept v3; whitelist-extract `reflection`, `nudges`
  (codes/severities re-sanitized), `operator_directives` (re-scrubbed, ≤10) into the summary.
- `engine/neuralweb/mastermind_context.py`: new `mastermind_ai` entry in `LOBE_SUMMARIZERS` reading
  the feedback summary → `{state, nudges{n, by_severity, top_codes}, directives{n, ids},
  ack{nudge_codes_seen, directive_ids_seen}}` — the macro→bot ACK channel; the bot's reflection
  engine flips directive statuses on it.
- `engine/neuralweb/daily_brief.py`: `operator_attention` item when new nudges/directives are
  pending ("N Mastermind nudges — see admin Observatory").

### 3.3 Observatory + Admin panel (macro admin console)
- `admin/neural_web.py::lobes_panel()` returns a synthesized **`orchestrator` hero block** rendered
  PINNED + highlighted above the Core group in `RENDER.neural_web`; detail page `#/orchestrator`.
- New admin nav items under Neural Web: **Master Brain** (`orchestrator`: status hero, settings
  panel via the `admin/flags.py` + `config_store.py` pattern over a new `config.yml orchestrator:`
  block, run-log table, 5-run reviews, chat box, Wake button) and **Mastermind AI**
  (`mastermind_ai`: bot loop settings + loop log + improvements + nudge/directive dialogue,
  proxied by the admin server to the bot at `MASTERMIND_BOT_BASE`, default `http://127.0.0.1:8000`).
- **Chat**: POST `/api/orchestrator/chat` — reuses the `engine/neuralweb/ask_brain.py` pattern
  (read-tools-only Opus loop) with an orchestrator persona + read tools over the runlog, reviews,
  health, brief, and feedback summary. "Tell it to do things" = the directive composer (routed via
  the bot lane → next nightly build ingests). **Wake** = POST `/api/orchestrator/wake` → best-effort
  `gh workflow run daily.yml` (degrades to instructions when gh is unavailable).

## 4. The dialogue loop, end to end

1. Nightly bot `loop_maintenance` → `mastermind_ai.run_cycle` → reflection + nudges + loop log.
2. Twice-daily `publish_macro_snapshot` → `nw_feedback.v3` (reflection/nudges/directives) pushed to
   macro `site/mastermind/`.
3. Macro nightly build ingests → feedback summary → `lobes.mastermind_ai` ack in the context
   artifact + daily_brief operator_attention + cortex can read the summary (governance dir).
4. Bot's next vendor refresh reads the ack → directive/nudge statuses advance; new/better context
   flows through the existing contract; the agenda + operator (via the admin section) see the
   whole exchange. New lobes born from nudges arrive as new synapse registrations, auto-visible in
   the Observatory and the context lobe_manifest.

## 5. Authority + safety invariants (unchanged law)

All five authority booleans stay FALSE both directions; everything here is context/telemetry.
Byte-identical-off for any prompt-path change (none in this wave — reflection never injects into
seats; only the existing agenda/journal seams do). Public-surface counts-only + redaction on every
published byte (bot `data/` rsyncs to the public mirror in 15 min — loop logs and reflection
artifacts carry codes and counts, never prompts/keys/sizes; directives are operator-authored and
scrubbed). No new scheduler jobs — piggyback `loop_maintenance` and `publish_macro_snapshot`.
The frozen judge (`loop/harness.py`) and the self_tune denylist are untouched.

## 6. Falsifiers (P3, pre-registered)

- The reflection lobe earns its lane if ≥1 of its first 5 nudges, when actioned macro-side, flips a
  dead decision-policy field live (e.g. `graph_conflicts` appearing with real rows) within 4 weeks.
- The 5-run orchestrator review earns its cadence if the operator acts on ≥1 item per month sourced
  from it (else fold it into the daily brief).
- Attribution stays `building` until ≥12 joinable graded outcomes; if after the 07-17 cohort + 4
  weeks the join rate is <50%, stamp NW candidacy into decision provenance (follow-up item).

## 7. W-AI.1 hardening (2026-07-13)

- **Nudge lifecycle registry** — `data/nw_reflection/nudge_state.json` (`nw_nudge_state.v1`):
  per-code `first_seen/last_seen/builds_seen/status/resolved_on`. Counters survive a skipped
  build (kills the fragile prior-latest.json carry, which legacy-seeds it once). Resolution is a
  dated tombstone judged on the PRE-CAP candidate set — a nudge cut by `nudges_max` is dropped,
  never "resolved" — and only when the code's detector actually ran (an absent context resolves
  nothing: vanished ≠ fixed). A resolved code that reappears reopens with its original first_seen.
- **Coverage hysteresis** — `coverage_below_half` fires at rate <0.5 and, once open, clears only
  at ≥0.55 (the live rate sat at exactly 0.5 and flapped the nudge). The band is stated in the
  detail string.
- **Latest-wins history** — `history.jsonl` re-persists for the same asof replace the row in
  place, so a morning run's worse numbers never freeze for the day. Report gains
  `nudges_dropped_n` + `nudges_resolved_recent` (≤5, codes+dates only); the v3 bridge ships
  `nudges_dropped_n` when present.
- **Dialogue health + last_ack** — `reconcile_ack` persists `data/mastermind_ai/last_ack.json`
  (validated codes + id count) on every ack sighting; `dialogue_health()` (in the status payload
  as `dialogue`) reports counterparty live/absent, loops since ack, queued/published/expired
  counts, oldest published age. The cycle summary and the N-loop review say so out loud when the
  macro side has been silent ≥3 loops.
- **Directive expiry** — new bounded setting `directive_expiry_days` (3..60, default 14):
  published rows never acknowledged in the window get an `expired` delta (new status), leave the
  publish wire, and free the open cap. Swept in `run_cycle` right after `reconcile_ack`.
- **act_on_nudges** — POST `/api/mastermind_ai/act_on_nudges` `{codes?}` →
  `draft_directives_from_nudges`: operator ONE-CLICK bulk authoring of directives from open
  nudges (templated text per known code, truncated generic fallback). The authority boundary is
  unchanged — the loop still never acts on its own; a human click authors the directives, and
  every auto-drafted text passes the same intake scrub. Rows carry `source: nudge:<code>`
  (dedup key; never published). Registered in `_NON_LLM_OPERATOR_PATHS`.
- **Deny-category errors** — intake rejections name the pattern category (env-flag name / dollar
  amount / key-shaped token / credential assignment), never echo the matched text.
- **loop_n continuity** — derived from the last row's counter (fallback row-count+1), immune to
  log truncation/rotation.

## 8. Auto-act on findings (2026-07-14)

- **Setting**: `auto_act_on_findings` (bool, default `False`). Bounded in `_BOUNDS`; hot-adjustable
  via the settings API without restart.
- **Doctrine amendment**: the W-AI.1 law "the click IS the authority — the loop never calls this on
  its own" is amended to "authority is operator-granted: per-click via the API, or as a STANDING
  grant via the `auto_act_on_findings` setting." The loop still never originates directives without
  an operator grant; the authority boundary is otherwise unchanged.
- **Placement in run_cycle**: the auto-draft step runs AFTER ack reconciliation and directive expiry
  (step 3), so any slots freed in the same tick are immediately available to the new drafts. Step 2
  has already persisted the fresh reflection, so `draft_directives_from_nudges()` reads tonight's
  nudges — step ordering must not change.
- **Unchanged machinery**: dedup on `source nudge:<code>` (an open directive for a code is skipped),
  the `directives_max_open` slot cap, the full `_DIRECTIVE_DENY` + 280-char intake scrub, and
  directive expiry are all identical to the operator-click path — no scrub exemption for auto-draft.
- **Loop-log row**: `steps["auto_draft"]` carries counts only (`queued_n`, `skipped_n`; or
  `{"error": "<ExcName>"}` on failure). Directive texts are never logged here; loop_log.jsonl
  rsyncs to the public mirror.

# W-L — THE CONSCIOUS LEARNING WAVE (design)

**Owner:** Fable · **Slot:** after W-E.0/1 merges (subsumes and extends the planned W5) · **Charter law:** P3 (signals earn authority), P6 (mistakes become machinery), P8 (autonomy earned in shadow)
**The user's mandate this answers:** journaled mistakes *and* successes · the Opus seats knowing what to fix · suggesting fixes or fixing them autonomously · per-seat memory files · a system that improves without Fable supervision · "we need to know what to tell you to improve."

**Design constraint that shapes everything:** future maintenance may be Opus-only (cost), or human-relayed. So the system must (a) improve itself within safe bounds with zero LLM supervision, (b) write its own briefing so any future session — Opus or Fable — starts with a ranked, evidence-backed agenda instead of a cold audit, and (c) keep every self-modification inside the validated-gate culture that just proved itself (the nowcast failing its own gate and shipping powerless).

---

## 1. The Journal — per-seat memory files (`brain/journal.py`, `data/journal/<seat>/`)

The missing conscience. Two layers:

**Auto-drafted entries (deterministic, free):** when any graded object resolves — a 21d falsifier, a thesis close, a prediction grade, a three-questions call — a journal entry is drafted mechanically: `{call, thesis_at_entry, planes_at_entry (from market_view), outcome, grade, regime_context, close_reason}`. No LLM cost; the ledgers already carry every field.

**Conscious entries (the Opus seat's duty):** on each build, the seat receives its last-N resolved drafts and MUST complete the ones that graded badly — same enforcement pattern as the three-questions duty:
```
lesson: {what_i_believed, what_actually_happened,
         why_wrong: bad-signal | bad-timing | bad-sizing | ignored-plane |
                    crowd-follow | label-trust | thesis-drift | luck-bad,
         rule_i_adopt (one sentence, falsifiable),
         confidence_in_rule}
```
Successes get the mirror treatment with the humility check: `{what_worked, skill_or_luck (was the regime doing the work? — checked against the regime-conditional grade), rule_i_keep}`. This fixes the verified self-mirror gap (misses-only, stats-only): the seat now narrates both directions.

**Memory mechanics:** journals are curated files, not append-only logs — capped size, lessons clustered by taxonomy, the most load-bearing rules PINNED and injected into the seat's future prompts (extends `self_mirror`'s injection seam; one contract, P7). A pinned rule that stops predicting (its adopted-rule falsifier fails) gets unpinned automatically — even lessons earn their authority (P3). Journals are versioned in `data/journal/` and browsable on the dashboard.

## 2. The Improvement Agenda — the system critiques itself (`brain/improvement_agenda.py`)

The weekly self-audit that answers "what should we tell the AI to fix." Extends the existing CIO job (which already reads calibration + KPIs + NAV + shadow leaderboard but is display-only and covers 2 books) into THE fusion engine over every accountability artifact:

- calibration deltas per seat/regime · journal lesson clusters (3 seats independently logging `ignored-plane` = a systemic item) · shadow-vs-live gaps (incl. the W5 do-nothing and defensive arms) · benchmark-ledger gaps vs SPY *and the user's defensive basket* · validation-run verdicts (E1.4) · replay-battery status · experiment registry maturities · cost_guard · armory/deploy-lag · student/distill accuracy drift.

Output, weekly: `data/agenda/<date>.json` + a human `AGENDA.md` — a **ranked list of concrete items**, each `{evidence, suggested_fix, fix_type: config-tune | prompt-edit | code-change | experiment, expected_impact, owner: self-tunable | opus-session | fable-review}`. This artifact IS the answer to "how do we know what to tell you": the user (or a scheduled Opus session) opens AGENDA.md and the top items are pre-argued with evidence. The incident post-mortem process, made weekly and automatic.

## 3. Bounded self-repair — what the bot may fix ALONE (`brain/self_tune.py`)

The critical boundary, drawn by charter P3/P8:

**Autonomously tunable (no LLM, no human):** ONLY constants in `doctrine.yml` tagged `(unverified-prior)` — thresholds, caps, band edges, weights the waves deliberately parked as priors. Mechanics: one parameter family per week, proposed by the agenda's evidence, evaluated through the **existing immutable Lab harness** (`loop/harness.py` frozen judge + PBO + holdout — the machinery with real statistical hygiene that today is write-only), bounded step sizes, shadow-run before live, **auto-revert** if the live delta underperforms the shadow's projection over its window. Every tune is journaled with its evidence and its falsifier. This finally gives `loop/` its missing consumer — the Lab's discoveries flow to parameter proposals, closing the verified write-only gap (or, where a discovery needs code, it lands in the agenda as an `opus-session` item instead).

**Proposal-only (needs an LLM session):** prompt edits, code changes, new planes, new books. The agenda carries these with `fix_type` and pre-written specs. A future Opus session doesn't design from scratch — it executes reviewed items, exactly as this program's waves did.

**Never self-modifiable:** the charter, the invariant, the validation gates themselves, the replay batteries, cap ceilings' hard bounds, anything touching real-money paths. The system may propose changes to its own gates; it may never apply them.

## 4. The measurement floor (W5 proper — everything above stands on it)

Unchanged from the masterplan, now scoped inside W-L: `portfolio/marks.py` (one price source, all 7 books, never avg_cost) · `brain/benchmark_ledger.py` (common-inception renorm; bogeys = SPY + the user's defensive basket + carry/do-nothing shadows + regime-conditional `max(SPY, defensive)`) · regime-conditional calibration (a defensive rotation into a down-tape finally scores WELL) · allocation+cash attribution terms · `posture_governor` (default OFF, statistical guards) · the missing shadow arms · desk_levers honest effective_n.

## 5. Long-term conditions — the experiment registry (`data/experiments/registry.json`)

Every accruing experiment tracked with **come-back dates** (the dashboard's admin experiments-tracker pattern, ported): shadow trim ladder (needs ≥40 graded trims) · judgment-book promotion (2–4wk rule) · posture-decider arming gates · CRASH-RISK AUC gate · rotation-tensor gate · governor arming (effective_n ≥ 8) · every pinned journal rule's falsifier. The weekly agenda surfaces matured-but-unjudged experiments as its top item — nothing silently rots. Long-term memory that outlives any session.

## 6. `MAINTENANCE.md` — the runbook for a future non-Fable world

The entry point for any maintenance session (Opus or human-relayed): read AGENDA.md → check the experiment registry for matured items → check armory + deploy-lag + replay batteries → execute the top `opus-session` items per their specs → close the loop by updating the masterplan status log and the self-interrogation. Written so an Opus session with zero context can operate the whole system safely. The charter's standing self-interrogation becomes its checklist.

---

## Build order (one wave, after W-E.0/1 merges — collides with its prompt/cio files otherwise)

| # | Task | Tier |
|---|---|---|
| L1 | marks.py + benchmark_ledger + shadow arms (do-nothing, defensive) — the floor | Opus |
| L2 | journal.py: auto-drafts + conscious-duty enforcement + curation/pinning + self_mirror injection merge | Opus (schema+injection), Sonnet (plumbing) |
| L3 | improvement_agenda.py: the fusion + AGENDA.md writer; CIO job extension to all 7 books | Opus |
| L4 | self_tune.py: the bounded loop through the Lab harness + auto-revert + journal trail; wire loop/promote | Opus design + Fable sign-off (it's the self-modification boundary) |
| L5 | regime-conditional calibration + allocation attribution + governor (W5 items) | Opus |
| L6 | experiment registry + come-back surfacing; MAINTENANCE.md; agenda→dashboard page | Sonnet |
| L7 | Replay + charter audit: journals fill from the incident window retroactively (the 07-02 lessons become the first entries); agenda's first run must rank the known open items correctly (sanity: it should tell us to arm the posture decider and grade the trim ladder) | Integrator |

**Falsifiers (P3, pre-registered):** the journal earns its prompt space only if pinned-rule seats outperform their own pre-journal calibration on a 60-session window; the agenda earns its cadence if ≥half its top-5 items, when executed, move their cited metric; self_tune earns continued autonomy per-parameter-family by beating its own shadow projection — a family that reverts twice goes proposal-only forever.

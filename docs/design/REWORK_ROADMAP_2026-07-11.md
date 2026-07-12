# Mastermind Flagship Superintelligence Rework — Roadmap (2026-07-11)

Synthesized from a full-sweep multi-agent re-audit (85 agents) + 5 Opus/Fable design lanes.
This is the follow-through to `research/MASTERMIND_PROBLEM_AUDIT_FOR_FABLE.md` (2026-07-01) and
`docs/design/desk/*.md`. It is a **staged, flag-gated, shadow-A/B'd** plan — nothing changes live on noise.

---

## 0. The reframed diagnosis (what changed since 2026-07-01)

The 07-01 audit's headline "critical flags off" items are **now false** — the 07-02 prod-deploy armed
them (`.env`: `MASTERMIND_FLAGSHIP_JUDGMENT=1`, `MASTERMIND_MACRO_RISK=1`). A great deal was fixed:
missing-stockdata fail-**open** (C1) is now fail-**closed**; the theme-cap-defeated-by-per-name-id bug is
fixed (cluster-keyed caps); the fragility scorer has a dwell state machine (no more crash-day
CAUTION→RISK_ON flip); `regime_frame.budget()`/`cycles()`/`rotation_evidence()` are wired; `market_view`
is a 21-plane perception organ; the NW reader, outcome_ledger, predictions, attribution, benchmark_ledger,
experiment_registry, improvement_agenda all now exist.

**The disease today is different and more subtle:**

1. **Dark-shipped infra that isn't wired into decisions.** The Neural Web bridge influences *zero*
   decisions (`MASTERMIND_NW_CONTEXT` default OFF, and even ON it's prompt-text only, not candidacy/
   sizing/veto). `rotation_tensor` and `anticipation` organs are **built but never invoked** by the daily
   pipeline. `attribution.persist()` is **never scheduled** (reputation permanently cold). `sector_cycles`
   reaches `regime_frame.cycles()` but SENTINEL/Strategist inputs don't carry it.
2. **Candidacy is still ~90% momentum-sourced.** Flagship reviews ~200-340 names fed by two momentum
   boards; there is **no whole-universe pass**, no rotation-in lane, no single-stock-divergence lane. The
   leadership sleeve (40-60% of NAV) is still selected by raw RS rank with cycle phase only a post-selection
   weight brake, never an inclusion gate.
3. **No additive mind at candidacy.** Every seat is subtract-only; nothing *originates* a defensive,
   rotation-in, or contrarian idea the momentum funnel didn't surface.

**Grinold frame (all 5 designs agree):** IR ≈ IC·√BR. Raise **BR** by widening candidacy to the whole
universe (NW passthrough + rotation-in + cycle-bottoming + single-name divergence). Protect **IC** by
keeping every new lane inside the constitution: **additive is legal only at candidacy** ("decides WHAT to
look at, never sizes"); sizing/authority stays subtract-only ½-Kelly; missing/stale data degrades to a
**byte-identical no-op** (fail-closed); every signal earns authority only through a **graded record**;
held names are never touched by new vetoes (the walk-forward-refuted cycle-veto lesson).

---

## 1. Consolidated components (deduped from ~45 → 24)

### Group A — Data plane & perception organs (observability, mostly EXTEND)
- **A1 NW artifact contract + sync + TTL cache** — `macro_refresh._ANCHOR_DEFS` 5th anchor + sparse/R2;
  `contracts.yml` `site-neural-web` (ADVISORY); `neural_web_context._CACHE` gets a 3h TTL + per-build reset.
- **A2 Organ triggers** — call `rotation_tensor.write_artifact(assemble())` + `anticipation.write_battery()`
  nightly in `bot/daily.py` (fixes the never-invoked defect). Lands **once**, in P0.
- **A3 `brain/rotation_intake.py`** (NEW, sole reader) — reads `rotation_calls.v1`; `synthesize_fallback()`
  (deterministic identification-lite, confidence≤0.5) fills the lane until the other session's engine ships;
  `expand(call)` maps sector/theme → member names.
- **A4 `brain/universe_triage.py`** (NEW organ) — per-sector `{phase, osc_slope, entry_favored, late_cycle,
  tensor level/accel, nw_stance, rotation_in, action: favor|neutral|reduce, why[]}`. Consumers: intake
  weighting (±0.05), conviction lane filters, leadership suppression, strategist/SENTINEL/PM payloads.
- **A5 `nw_decision_mode()` + `decision_signals()`** — ONE typed policy chokepoint in `neural_web_context.py`;
  no consumer reads raw NW fields ad hoc. Five-mode ladder `off|shadow|candidacy|shrink|vote` on new flag
  `MASTERMIND_NW_DECISION` (subsumes the proposed `MASTERMIND_NW_UNIVERSE`; `MASTERMIND_NW_CONTEXT` keeps
  owning text-only prompt injection).

### Group B — Candidacy lanes (ADDITIVE — widen WHAT is looked at; the gate still filters)
- **B1 `intake._from_neural_web()`** — NW bottom_state/options/conflicts → candidacy (score 0.35-0.5).
- **B2 `conviction.nw_universe_scan()`** — whole-universe NW passthrough, cap **25**, hygiene filters only
  (fdr/conflicts/freshness/caps) — never re-derives bottoming logic (that's the other session's signal).
- **B3 `intake._from_rotation_in()`** — thin wrapper over `rotation_intake`; score by state EARLY 0.35 /
  TURNING 0.50 / CONFIRMED 0.65.
- **B4 Cycle-bottoming promotions** — `_from_cycles_bottoming()` (entry_favored ∧ osc_slope>0);
  `regime_seed()` ranks baskets by osc_slope; `defensive_candidates._from_cycles()` gains a velocity filter.
- **B5 Divergence-clue lane (the AAPL-Jul-1 pattern)** — NEW `brain/divergence_clue.py`. Fires only on a
  conjunction: (standout buy-board OR radar POSITIVE_DIVERGENCE) AND ≥2 of {down-day alpha ≥+50bps/day;
  RS-velocity gap ≥+3bps/day vs sector; flow rotation} AND ≥1 sector-stress {cycles Peak/Downturn; tensor
  distribution; macro_risk≠risk_on or sector radar flare}. Guards: not parabolic, 10-session cooldown, ≤5/
  build. **Acceptance test: `scan(asof='2026-07-01')` must surface AAPL.** Shared math legs:
  `distribution_tells.down_day_alpha()` + `rotation_tensor.single_rs_velocity()`.
- **B6 Defensive/rotation-in pool receivers** — `defensive_candidates` gains NW + rotation-in generators.
- **B7 Pre-ignition watchlist** — EXTEND `portfolio/watchlist.py` with rotation-origin rows that **hold
  names through unconfirmed turns** (30td WATCH / 15td ARMED; separate namespace, never evict timing rows).
  The fast-turn catch: a call jumping straight to CONFIRMED promotes **same build**. Divergence clues park
  early at starter size; their sector's later confirmation arms the full-size path (cross-session trigger).

### Group C — Sizing, posture, desk seats (SUBTRACT-ONLY)
- **C1** graph_conflicts entry shrink (×0.7, new names only, absent data → 1.0).
- **C2** `_nw_caution_source()` 5th tri-state in `rotation_evidence` + bear-only macro-bloc row (damps
  leadership budget toward its 0.40 floor only; can never lift).
- **C3** Pre-ignition starter discipline — rotation-tagged non-CONFIRMED name forced to starter × 0.5.
- **C4** Leadership reduce-sector suppression (skip NEW legs in `reduce`/late-cycle sectors; held exempt).
- **C5** `brain/technician.py` (NEW seat) — entry-timing `now|staged_starter|wait`; Sonnet tier; offline
  default = `wait` (fail-conservative, inverse of the old SENTINEL default-CONFIRM hole).
- **C6** NEXUS quorum extension + `data/pipeline/ledger.jsonl`; enforces MAX_NAMES=12 / MAX_NEW_ADDS=3
  (currently enforced nowhere); never-blow-to-cash exit floor.

### Group D — Judgment surfaces (additive mind, text-only, zero sizing effect)
- **D1** PM enrichment (`nw_universe_watch` cap 40, `rotation_opportunities`, divergence-clue paragraph).
- **D2** Strategist/SENTINEL context enrichment (sector_cycles, universe_triage, anticipation alarms).
- **D3** Lenses `divergence_clue` context row (neutral until the promotion gate).

### Group E — Learning spine & governance (observability)
- **E1** Close-reason taxonomy · **E2** decision-provenance ledger + `flags_hash` · **E3** learning fields
  at decision time (`sector_phase_at_entry`, `divergence_from_sector`, NW fields — via `make_record(extra=)`,
  no signature changes) · **E4** lane ledgers + graders in `loop_maintenance` · **E5** loop unfreeze
  (`attribution.persist` scheduled; evaluators for the two 07-17-maturing experiments) · **E6** `desk_ab`
  shadow policies that BUY what prod ignores · **E7** sector-phase calibration bucket · **E8** arming ledger
  + two-lane flag governance (Lane S = subtract/advisory/judgment arms on parity; Lane A = additive buy
  sources arm only on the statistical bar: effective-n≥8 clusters, HAC p<0.05, CI>0 vs prod) · **E9**
  benchmark visibility + beaten-by-defensive trigger · **E10** CIO `perception_health()`.

### Group F — Cleanups (own PRs, before the flag wave)
- **F1 Quiver competitor-read removal** — ✅ DONE 2026-07-11 (worktree). Alt-data feed preserved.
- **F2 Login removal** — pending operator scope ruling (see §4).

### Group G — Dark-ship arming (config only, the 07-17/19 restart)
- Arm the built-and-tested Lane-S layer: `NW_CONTEXT=1` (iff the 5-present-build streak verifies),
  `RISK_OFFICER=1`, `RISK_GOVERNOR=1`, `POSTURE_DECIDER` (iff shadow delta passes); `def_sleeve.max 0→0.10`;
  heavyweight into `derisk.sweep_us()`. **Zero new code** — activates existing paths.

---

## 2. The coordination seam (the other, rotation-identification, session)

**They IDENTIFY, we CONSUME.** They own the detection science (labeling sectors/themes/subsectors/names as
turning, per-call confidence, publication cadence). We own the sole reader (`brain/rotation_intake.py` —
nothing else opens their file), schema/freshness validation, target→name expansion, the pre-ignition
watchlist, every funnel consumer, **all sizing discipline** (starters until CONFIRMED), the consumption
ledger, and grading.

**Contract `rotation_calls.v1`:**
```
{schema:"rotation_calls.v1", as_of, generated_at, engine_version,
 calls:[{call_id,                       # IMMUTABLE join key
         target_kind: sector|theme|subsector|ticker, target, members[]|null,
         state: EARLY|TURNING|CONFIRMED | FAILED|EXPIRED,
         direction: rotation_in|rotation_out, confidence:0-1, horizon_bdays,
         evidence:[{source,value,note}],           # opaque — logged verbatim, never recomputed
         falsifier:{text, check:{kind:'rel_return', subject, benchmark, horizon_bdays, op, value}, check_by},
         first_seen, state_history:[{date,state}]}]}
```
**Seam invariants:** call_id immutable; state regression read as FAILED; **absence handshake — no/stale file
= "no calls today", never "all clear"**, every lane provably inert; additive-only schema evolution under v1;
they never write our files, we never re-derive their signal; **they must emit EARLY/TURNING, not only
confirmed turns** (the operator's stated goal — unconfirmed states get *eyes* immediately; only *size*
waits); feedback loop — our `consumption_ledger.jsonl` + resolved outcomes keyed by call_id are THEIR
calibration ground truth, and our divergence-clue density is an input feature to their sector-turn ID.

**Until their engine ships**, `synthesize_fallback()` fills the lane and doubles as the executable
reference semantics; retired after 5 consecutive present+fresh real-artifact builds.

---

## 3. Staged roadmap

- **P0 — Live fixes + observability substrate (wk 1, flagless):** loop unfreeze (`attribution.persist` +
  scorer track_record scheduled; evaluators for the 2 experiments maturing **07-17**); organ triggers
  (A2); NW cache TTL/reset (A1 partial); close-reason taxonomy (E1) + provenance ledger (E2) + learning
  fields (E3) — ship *first* so everything later is measurable; verify live VPS flag env; cleanups F1 ✅/F2.
- **P1 — Foundations + the Lane-S flag wave (wk 1-3):** NW data plane (A1); rotation seam (contract + golden
  fixture + `rotation_intake.py` + `universe_triage.py`, write-only); NW typed policy (A5) at `shadow`;
  arming ledger + governance (E8); benchmark visibility (E9) + CIO perception_health (E10); **G1 arming**
  at the 07-17/19 restart.
- **P2 — NW + rotation candidacy infra (wk 3-5, ship dark → arm on parity):** all Group-B lanes + Group-D
  surfaces + grading substrate; the AAPL replay acceptance test; ≥5-10 shadow sessions before arming each
  lane. Flags: `NW_DECISION=candidacy`, `ROTATION_IN=watch`, `DIVERGENCE_CLUE=1`, `UNIVERSE_TRIAGE=1`.
- **P3 — Desk seats + subtract-side enforcement (wk 5-8):** Technician seat (C5); NEXUS quorum (C6) shadow
  → enforce subtract-side; NW subtract-only sizing (C1/C2) at `NW_DECISION=shrink`; leadership suppression
  (C4). Arming order: **subtract-side first** (worst case = a smaller book).
- **P4 — Learning-gated additive authority (Q3→Q4, evidence-gated not calendar-gated):** sector-phase
  calibration (E7); each additive lane climbs its pre-registered ladder (advisory → corroboration bonus at
  starter → standalone candidacy) only on its statistical bar. `ROTATION_IN=starter` after ≥12 resolved
  outcomes + Lane-A bar; divergence-clue vote after ≥25 resolved clues >55% hit; `NW_DECISION=vote` at
  AUC>0.55 over ≥60 graded sessions. One flag reverts any of it.

---

## 4. Open questions for the operator

**Blocking:**
1. **NW artifact publication** — who lands `site/neuralwebdata/mastermind_context.json` on origin/main,
   git-tracked or R2? Its shape is inferred from the reader (unverified); the artifact doesn't exist yet, so
   the NW_CONTEXT 5-build arming streak can't be verified until it does.
2. **Rotation artifact home + form** — bot-side `data/rotation/` vs macro-side `site/rotationdata/`;
   standalone `rotation_calls.json` vs published inside the NW artifact. Joint ruling on state vocabulary so
   calibration buckets match across both programs.
3. **Live VPS flag env** — confirm `MASTERMIND_FLAGSHIP_JUDGMENT` is truly armed on the running PID; confirm
   07-17 vs 07-19 as arming date.
4. **Login-removal scope** — page-only (keep serve-only + bearer; recommended) vs full auth deletion.
5. **Posture-decider collision** — if it arms 07-17 and NW_CONTEXT 07-19, `nw_caution` must ship wired into
   the decider's defense-pressure from day one; pre-register the shadow-vs-live threshold before 07-17.

**Design rulings:** NW independence (does it share substrate with radar → earns the +0.08 corroboration
bonus?); may CONFIRMED-rotation members bypass the `gate_go=False` momentum skip; defensive rotation-ins →
conviction sleeve vs DEF_SLEEVE; pre-ignition starters count against MAX_NAMES with a separate
MAX_PREIGNITION=2/build?; are 0.5× starters acceptable additive authority at all, or watchlist-only until
CONFIRMED; threshold priors (all unverified — run a 2025-26 historical replay sweep before arming?);
fallback-synth retirement (retire at 5 builds vs keep permanently as a disagreement check).

---

*Full audit register, per-layer defects, adversarial verdicts, and the 5 raw design lanes are in the
workflow output (session transcript, run `wf_52099c3f-0a8`).*

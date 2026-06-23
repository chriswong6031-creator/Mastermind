# Chapter 07 — Data Contracts, Module Mapping & Phased Build Plan

This chapter is the build spec. It converts the target operating model (Chapter 02 — *The Desk: Organizational Structure & Decision Rights* and Chapter 03 — *The Buy Pipeline & Watchlist Subsystem*) and the accountability design (Chapter 05 — *The Accountability & Learning Loop*) into concrete, on-disk artifacts, module-level work items, an order-of-operations for `app/scheduler.py`, and a phased plan with acceptance criteria. Every rule in the Canonical Brief maps here to a file and a function. Nothing is left to interpretation: where a tie-break or edge case exists, it is named and resolved.

The non-negotiable invariants this chapter must preserve, end to end (restated from the Brief, §C/§D and the Core Invariants list):

1. **Separation of powers** — distinct seats, distinct mandates, no seat both pumps size and approves it.
2. **Subtract-only on both sides** — LLM judgment may only de-risk (veto / trim / exit / withhold / downsize / park). The deterministic engine (NEXUS) owns the additive sizing floor and the caps.
3. **Quorum** — an add requires a quorum of positive sign-offs **AND** no Gate-Officer veto.
4. **No broad-ETF in the alpha book; never blow a book to cash; never force-buy.**
5. **Everything is a falsifiable, gradable thesis**, graded leakage-free vs SPY, fed back per-role.

These five are enforced in code by NEXUS (`brain/committee.py`) and asserted by tests; they are not prompt instructions.

---

## 7.1 Storage Conventions

All new state lives under the existing `data/` tree, resolved via absolute paths the way the current modules do (`Path(__file__).resolve().parent.parent / "data" / ...`). We extend, never relocate, the established roots:

| Root | Owner today | New use |
|---|---|---|
| `data/committee/<asof>/<TICKER>/` | `brain/committee.py` | add `strategist.json`, `technician.json`, `gate.json`, `pm_conviction.json` per name; `debate.md` extended |
| `data/brain/` | `calibration.json`, chat history | add `reputation.json`, `kpi/`, `attribution/`, `cio/`, `self_mirror/` |
| `data/shadow/` | `shadow_books.py` | add `funnel` policy book (full desk) for counterfactual credit |
| `data/portfolios/<book>/` | `registry.data_dir()` | add `watchlist.jsonl`, `exit_theses.jsonl`, `grades.jsonl` per book |

**Format rules (match existing code):** append-only event logs are `*.jsonl` (one JSON object per line, like `nav_history.jsonl`, `theses.jsonl`); current-state snapshots that are recomputed each build are pretty-printed `*.json` (like `calibration.json`, `leaderboard.json`). Per-name committee artifacts are one directory per `(asof, TICKER)` (existing convention). All timestamps are ISO-8601 `YYYY-MM-DD` for trading dates; full ISO for event times. Every record carries `schema_version` (start at `1`) so migrations are cheap.

---

## 7.2 Artifact Schemas

All schemas below are JSON-ish (comments are `//`, not literal). Required fields are marked `*`. Where a field mirrors an existing one (e.g. `falsifier`, `combined`), the existing shape is authoritative.

### 7.2.1 Per-stage decision record — `data/committee/<asof>/<TICKER>/<seat>.json`

One file per seat that ran for that name on that date. `<seat> ∈ {forge, strategist, technician, sentinel, pm_conviction, gate, nexus}`. This is the atomic unit the grader resolves. The pre-existing `sentinel.json` / `forge.json` / `nexus.json` shapes are preserved; the new seats follow the same envelope.

```jsonc
{
  "schema_version": 1,
  "agent": "strategist",          // *  one of the canonical seat ids (lowercase)
  "ticker": "ASML",               // *
  "asof": "2026-06-22",           // *  decision date (trading date)
  "stage": "macro",               // * sourcing|macro|fundamental|technical|adversary|champion|gate|synthesis
  "stance": "SUPPORT",            // * seat-specific verdict vocabulary (see 7.3)
  "verdict": "enter_now",         //   technician-only: enter_now|staged_starter|wait ; others null
  "confidence": 0.72,             // * post-calibration (shrunk) confidence in [0,1]
  "raw_confidence": 0.80,         // * pre-calibration — graded against this (fixed-point, see calibration.py)
  "rationale": "…",               // * <=600 chars, blunt
  "signals_used": {               //   the dashboard surface this seat consumed (provenance, for audit)
    "regime": {"quad": 2, "liquidity_overlay": "neutral", "sector_rs_top": ["XLK","XLI"]},
    "tech":   {"pct_vs_50dma": 4.1, "rsi14": 61, "off_52w_high_pct": -3.2}
  },
  "falsifier": {                  // * makes this decision gradable (same shape as thesis falsifier)
    "check": {"kind": "rel_return", "op": "<", "threshold": 0.0, "horizon_bdays": 21},
    "check_by": "2026-07-22",
    "condition": "If ASML lags SPY by >0 over 21 bdays this macro-timing call was wrong."
  },
  "blind": true,                  //   adversary/technician blind to bull score (see 7.3)
  "cost_usd": 0.31,               //   per-seat LLM spend (Opus accounting; null if deterministic)
  "model_tier": "opus"            //   opus|sonnet|haiku|deterministic
}
```

**Edge cases:** if a seat did not run (no LLM, disabled, or pruned by funnel), **no file is written** — absence means "not run", graded as neutral, never as a verdict. A seat that errored writes nothing (best-effort, like today's `_write_artifacts`).

### 7.2.2 Watchlist entry — `data/portfolios/<book>/watchlist.jsonl` (append) + `watchlist_state.json` (snapshot)

Watchlist is a **first-class state**, re-reviewed daily. The append log records every transition; the snapshot is the current set.

```jsonc
// watchlist.jsonl event
{
  "schema_version": 1,
  "ticker": "VRT", "asof": "2026-06-22",
  "event": "park",                // park|re_review|promote|expire|drop
  "reason": "good_thesis_bad_setup", // canonical reason code (7.6)
  "source_seat": "technician",    // who sent it here (technician WAIT / gate WITHHOLD / strategist hostile backdrop)
  "combined": 71,                 // Conviction Index at park time (carried, not re-bought)
  "trigger": {                    // * the condition that would PROMOTE it to a buy
    "kind": "technical_setup",    // technical_setup|catalyst_date|regime_flip|rel_return
    "detail": "reclaim 50dma on >1.2x volume",
    "watch_until": "2026-07-31"   // re-review at least until here; never auto-dropped before
  },
  "ttl_watch_td": 20,             // TTL in WATCH without reaching ARMED (Ch03 §3.6.2)
  "ttl_armed_td": 10,             // TTL in ARMED without firing (Ch03 §3.6.2)
  "falsifier": { /* same shape — a park is also a gradable decision: did parking beat buying? */ }
}
```

`watchlist_state.json` is `{ "as_of": "...", "names": { "VRT": {<latest event + days_parked + n_reviews> } } }`. **Never auto-dropped** before `watch_until`; expiry past `watch_until` requires the trigger to have gone stale (e.g. catalyst passed) — logged as `expire`, graded.

### 7.2.3 Exit-thesis record — `data/portfolios/<book>/exit_theses.jsonl`

Owned by RISK OFFICER / EXIT MANAGER. Subtract-only: an exit thesis can only justify trim/exit, never an add.

```jsonc
{
  "schema_version": 1,
  "ticker": "SMCI", "asof": "2026-06-22",
  "action": "trim",               // * hold|trim|exit  (NEVER add)
  "scale": 0.5,                   // * surviving fraction (1.0 hold, 0.0 exit) — multiplies current size
  "trigger": "thesis_invalidation", // thesis_invalidation|falsifier_hit|risk_event|mechanical_overlay
  "entry_thesis_ref": "data/portfolios/flagship/theses.jsonl#<id>",
  "rationale": "Original catalyst (DC win-rate) reversed; falsifier rel_return<-0 confirmed.",
  "confidence": 0.7, "raw_confidence": 0.7,
  "layered_on": ["D5_time_stop", "hard_veto"], // which mechanical detectors also fired (atop, not instead)
  "falsifier": { /* the exit's OWN falsifier: was exiting right? rel_return of the name AFTER exit */ },
  "deferred_exits": []            // exits throttled by the never-to-cash floor (Ch04 §4.5 / nexus rule 7);
                                  // each: {ticker, defer_reason, retry_asof}
}
```

**Tie-break with mechanical detectors:** the Risk Officer judgment is *additive de-risking on top of* the existing mechanical detectors (D5 time-stop, hard vetoes, caps, hysteresis). If a mechanical detector says exit and the Risk Officer says hold, **the mechanical detector wins** (subtract-only floor — the stricter de-risk applies). If the Risk Officer says trim and no mechanical detector fired, the trim applies. The two never *add* exposure.

### 7.2.4 Grade record — `data/portfolios/<book>/grades.jsonl` (per book) + roll-up in `data/brain/grades/`

The grader (an engine, not a seat) resolves every decision record once its `check_by` is due and the outcome is leakage-free.

```jsonc
{
  "schema_version": 1,
  "decision_ref": "data/committee/2026-06-22/ASML/technician.json", // * what is being graded
  "agent": "technician", "ticker": "ASML", "decision_asof": "2026-06-22",
  "decision_type": "timing",      // * add|veto|withhold|size|trim|exit|watchlist|timing|macro
  "resolved_asof": "2026-07-22",  // * = check_by, when label became available
  "rel_return": -0.014,           // * subject − SPY over horizon (brain/outcomes.label_thesis)
  "stated_conf": 0.80,            // * raw_confidence at decision time
  "correct": false,               // * directional correctness for this decision_type (7.7)
  "counterfactual": {             //   what the *alternative* would have earned (credit assignment)
    "alt": "buy_now_instead_of_wait", "alt_rel_return": -0.031, "delta": 0.017 // wait was right by +1.7%
  },
  "horizon_bdays": 21, "leakage_safe": true
}
```

Grades are **never** computed before `check_by` and always anchor both subject and SPY to the last close ≤ entry (reusing `brain/outcomes.py` leakage discipline verbatim). `req_end = min(final_end, asof)` is preserved.

### 7.2.5 Per-role calibration / reputation state — `data/brain/calibration.json` (extend) + `data/brain/reputation.json`

`calibration.json` already exists with `agents.{forge,sentinel}`. We extend the same block to every seat and split by `decision_type` so a seat's *timing* skill and its *selection* skill calibrate independently.

```jsonc
// calibration.json (extended)
{
  "as_of": "2026-06-22", "min_n": 12, "floor": 0.5,
  "agents": {
    "strategist": { "by_type": {
        "macro":  {"n": 14, "reliability": 0.61, "mean_confidence": 0.70, "multiplier": 0.871, "status": "scoring"} }},
    "technician": { "by_type": {
        "timing": {"n": 9, "reliability": null, "mean_confidence": 0.66, "multiplier": 1.0, "status": "building"} }},
    "gate":       { "by_type": { "veto": {...}, "withhold": {...} } },
    "risk_officer": { "by_type": { "exit": {...}, "trim": {...} } },
    "forge": {...}, "sentinel": {...}   // unchanged shape, now optionally typed
  }
}
```

`multiplier = max(FLOOR, min(1.0, reliability/mean_conf))`, inert until `n ≥ MIN_N=12`, **never inflates** — identical math to the current `_mult`. `multiplier(agent, decision_type=None)` resolves `by_type[decision_type]` then falls back to a pooled aggregate then `1.0`.

`reputation.json` is the CIO-tunable influence layer (Phase 5), **separate** from calibration so the two cannot be conflated:

```jsonc
{
  "as_of": "2026-06-22",
  "roles": {
    "technician": { "authority_weight": 1.0, "quorum_vote": true, "veto_power": false,
                    "status": "probation",      // probation|trusted|demoted
                    "effective_n": 9, "significant": false, // gates any change (Newey-West / cluster)
                    "last_changed_by": "cio_2026-06-15", "note": "below MIN_N; weight pinned at 1.0" }
  }
}
```

**Invariant:** reputation may change a role's *influence* (vote weight, whether its WITHHOLD counts toward quorum-block) but can **never** grant additive power — a demoted Technician still cannot force a buy. CIO changes are gated on `significant=true` (effective-n past threshold, `portfolio/predictions.py` HAC machinery reused).

### 7.2.6 KPI scorecard — `data/brain/kpi/<book>.json` (recomputed each build)

```jsonc
{
  "schema_version": 1, "book": "flagship", "as_of": "2026-06-22",
  "roles": {
    "scout":     {"ideas": 41, "promoted_to_research": 12, "hit_rate_promoted": null, "n": 41},
    "strategist":{"macro_calls": 14, "hit_rate": 0.61, "brier": 0.21, "effective_n": 9},
    "technician":{"timing_calls": 18, "enter_now_hit": 0.55, "wait_avoided_loss": 0.017, "effective_n": 7},
    "forge":     {"theses": 22, "brier": 0.19, "hit_rate": 0.50, "effective_n": 11},
    "sentinel":  {"opposes": 6, "oppose_correct": 0.50, "effective_n": 4},
    "gate":      {"vetoes": 3, "veto_saved_loss": 0.04, "withholds": 5, "withhold_regret": 0.01},
    "risk_off":  {"exits": 4, "exit_saved_loss": 0.03, "trim_hit": 0.5}
  },
  "book_level": {"nav": 1041200, "active_vs_spy_21d": 0.012, "n_names": 9, "cash_pct": 0.06}
}
```

KPIs are **multi-metric by design** (Goodhart guard): no single number can be gamed. Idea generation is credited separately from sizing/timing (Brief §D, Attribution).

### 7.2.7 Attribution record — `data/brain/attribution/<asof>.json`

Brinson-style split of active return into allocation (sector call → MACRO STRATEGIST), selection (name pick → ANALYST/FORGE + SCOUT), interaction, and a timing residual (TECHNICIAN).

```jsonc
{
  "schema_version": 1, "as_of": "2026-06-22", "book": "flagship", "window_bdays": 21,
  "active_return": 0.012,
  "brinson": { "allocation": 0.004, "selection": 0.006, "interaction": -0.001 },
  "timing_residual": 0.003,           // entry-timing alpha vs a naive next-open fill (Technician credit)
  "by_role": { "strategist": 0.004, "forge": 0.005, "scout": 0.001, "technician": 0.003, "gate": -0.001 },
  "by_name": { "ASML": 0.006, "VRT": -0.002 },
  "effective_n": 9, "significant": false   // never drives a behavior change until significant
}
```

### 7.2.8 CIO note — `data/brain/cio/<week>.md` + `data/brain/cio/<week>.json`

Weekly. The `.md` is the human-readable "what's working / who's miscalibrated" memo; the `.json` is the machine-actionable tuning proposal (applied to `reputation.json` only if significant).

```jsonc
{
  "schema_version": 1, "week": "2026-W26", "as_of": "2026-06-28",
  "summary": "Technician timing not yet significant (eff_n=7<12). Gate vetoes saving ~4%/name; keep.",
  "proposed_changes": [
    {"role": "gate", "field": "authority_weight", "from": 1.0, "to": 1.0, "applied": false,
     "reason": "positive but eff_n=3 < MIN_N — hold"} ],
  "books_reviewed": ["flagship","heavyweight","autonomous","china","hk","etf"],
  "significance_gate": {"min_effective_n": 12, "method": "newey_west_cluster"}
}
```

---

## 7.3 Module Mapping — role → concrete code

Each seat maps to a module that exposes a `_<seat>_input(...)` context builder and a `<seat>_assess(...)` runner returning a normalized verdict dict, **exactly mirroring** the existing `_sentinel_input` / `sentinel_assess` contract in `brain/committee.py`. NEXUS (`nexus()` in `committee.py`) is extended to fold the new verdicts subtract-only.

| Role | Module (new/extend) | Verdict vocabulary | Touchpoints |
|---|---|---|---|
| **SCOUT** | new `brain/scout.py` (Haiku/Sonnet) | `idea{ticker, one_line, why_now}` | feeds `bot/phase2.py` candidate set; reads `brain/bot_mcp.py` surface + web |
| **MACRO STRATEGIST** | new `brain/strategist.py` (Opus/Sonnet) | `SUPPORT/NEUTRAL/HOSTILE` + `backdrop_ok: bool` | called in `bot/phase2.py` after research gate; reads `get_regime/get_standouts/get_themes/get_divergences` |
| **ANALYST / FORGE** | existing `brain/research_paper.py` | unchanged (`confirmed`, `combined`, `viability`) | `bot/phase2.py:220` |
| **TECHNICIAN** | new `brain/technician.py` (Sonnet/Opus) | `enter_now / staged_starter / wait` | reads `get_fundamentals` tech block, `get_anticipation`, `get_options` GEX; **blind to `combined`** |
| **ADVERSARY / SENTINEL** | extend `brain/committee.py` (broaden input) | `SUPPORT/CONDITIONAL/OPPOSE` (unchanged) | broaden `_sentinel_input` to add chart + thesis-break lens; stay blind to score |
| **PM – CONVICTION** | new `brain/pm_conviction.py` (Opus) | `champion{target_size_intent, conviction_rank}` | proposes after quorum inputs gathered; **cannot self-approve** |
| **PM – GATE OFFICER** | new `brain/gate_officer.py` (Opus) | `APPROVE / VETO / WITHHOLD` + `max_size_cap` | final stage in `committee.assess`; subtract-only; owns no-ETF/max-names/min-conviction |
| **RISK OFFICER / EXIT MGR** | new `brain/risk_officer.py` (Opus) | `hold / trim / exit` + `scale` | new exit pass in `bot/phase2.py` over held names; layered on `brain/gate.py` hard-exit sweep |
| **CIO / META-PM** | new `brain/cio.py` (Opus, weekly) | tuning proposal (writes `reputation.json` if significant) | new weekly job in `app/scheduler.py` |
| **ATTRIBUTION** | new `brain/attribution.py` (deterministic, **Phase 5**) | Brinson split `{allocation, selection, interaction, timing, sizing, exit, veto}` | reads `data/committee/<asof>/<TICKER>/` + breadth panel via `predictions._load_panel`; accepts a resolved grade record, returns the Brinson component split; writes `data/brain/attribution/<asof>.json` (§7.2.7) |
| **NEXUS + grader** | extend `brain/committee.py:nexus()` + extend `brain/calibration.py` + new `brain/grader.py` | deterministic | enforces quorum/caps/subtract-only; grader reuses `brain/outcomes.py` |
| **WATCHLIST** | new `portfolio/watchlist.py` | state machine (7.6) | written by `phase2.py`; read each build before research to re-review |
| **Self-mirror** | new `brain/self_mirror.py` | injects graded track record into prompts | hooks into `bot/autonomous.py`, `china.py`, `hk.py`, `heavyweight.py`, and the Flagship seats |

**NEXUS quorum/subtract-only extension (the heart of the rebuild).** `nexus()` becomes the quorum + veto arbiter while keeping its pure-function, fully-testable nature. New signature folds the new seats:

```
nexus(breakdown, sentinel, strategist=None, technician=None,
      pm_conviction=None, gate=None) -> {action, scale, lean, rationale, quorum, vetoed_by}
```

Rules (deterministic, subtract-only, in order):
1. `forge_confirmed == False → drop` (unchanged invariant, `committee.py:9`).
2. `gate.verdict == VETO → drop`; `gate.verdict == WITHHOLD → park` (to watchlist, not bought).
3. `strategist.backdrop_ok == False → park` (good name, hostile macro → watchlist).
4. `technician.verdict == wait → park`; `staged_starter → scale = min(scale, 0.7)` (matching Ch03 §3.5 `_INITIAL_SIZE_FRACTION=0.7` and the `conviction.py:168-173` catalyst-gate scalar).
5. **Quorum check (conjunctive — every named arm is individually necessary, not a count).** An add requires **all five** positive sign-offs: `strategist.backdrop_ok` (non-hostile) **AND** `technician.verdict ∈ {enter_now, staged_starter}` **AND** `sentinel` not a high-confidence OPPOSE **AND** `pm_conviction` champion present **AND** `gate.verdict == APPROVE`. Any missing/negative arm → `park`. There is no counting threshold; matches Ch02 §2.4 and Ch03 §3.7.1.
6. Surviving size = `min` of all caps (the gate's `max_size_cap`, `name_cap=0.08`, `SECTOR_MAX_FRACTION=0.50`) × the *additive* engine/research floor. **No seat raises this.**
7. **Never-blow-to-cash floor (exit side, Ch04 §4.5).** On the held-book exit pass, NEXUS caps same-session de-risking: at most `max_exits_per_session = max(1, ceil(0.34 × n_holdings))` full exits, and a hard `min_invested_fraction = 0.20` of book NAV. Both live in `config/doctrine.yml` (alongside `default_window_td`). Exits beyond the floor are throttled and recorded in `exit_theses.jsonl#deferred_exits` (with `defer_reason` + `retry_asof`) for re-evaluation next session. Mechanical hard vetoes (parabolic/Altman) are **exempt** and always execute.

`scale` only ever ratchets down across rules 1–6. A name that survives all six is `action: "add"`; any park routes to `portfolio/watchlist.py`; any drop is logged with `vetoed_by`.

---

## 7.4 Daily & Weekly Order of Operations (`app/scheduler.py`)

The current Flagship job (`_job` → `bot.daily.run_daily` → `bot/phase2.py`) fires at 22:40 UTC after the US close; the morning settle job at 15:00 UTC fills queued orders. The desk funnel slots **inside** `phase2.py` so the cron cadence and `brain/gate.py:should_run` regime-trigger are unchanged. The intra-build order:

```
NIGHTLY (Flagship, 22:40 UTC) — inside bot/phase2.py, gated by gate.should_run:
  0. gate.should_run(state_sig)        ── unchanged: full rebuild only on regime/interval trigger
  1. watchlist.re_review(book)         ── re-score parked names FIRST (promote-eligible feed the funnel)
  2. SCOUT.source()                    ── Haiku: candidate ideas (dashboard + web) → candidate set
  3. for each candidate: FORGE research gate (existing conviction.build → score_breakdown)
  4. STRATEGIST.assess(regime/rotation) ── backdrop_ok? (one call per surviving candidate OR one batched macro read + per-name fit)
  5. TECHNICIAN.assess(chart/RS/GEX)    ── enter_now / staged_starter / wait
  6. SENTINEL.assess (broadened)        ── blind bear
  7. PM-CONVICTION.champion             ── target size intent, ranking
  8. GATE-OFFICER.assess → NEXUS(...)   ── APPROVE/VETO/WITHHOLD + quorum + caps (subtract-only)
  9. add → position_log.update → paper_account.rebalance (buy / queue_orders)
     park → watchlist.park ;  drop → logged
 10. RISK-OFFICER exit pass over HELD names (trim/exit only, atop mechanical detectors)
 11. grader.resolve_due() ── grade any decision whose check_by ≤ asof, leakage-free
 12. calibration.persist() ; kpi/attribution recompute ; publish latest.json
```

**Cost-control fan-out.** Stages 4–8 are Opus-heavy. The funnel is a **decreasing cone**: SCOUT (Haiku, cheap, many) → FORGE confirmed only → STRATEGIST/TECHNICIAN only on FORGE-confirmed → PM/GATE only on quorum-eligible. This caps Opus calls at roughly `n_confirmed × 4`, not `n_candidates × 4`. A hard `MAX_OPUS_SEATS_PER_NIGHT` budget (config) prunes lowest-`combined` candidates first if exceeded.

**Weekly CIO job** — new `_cio_job` in `app/scheduler.py`, `CronTrigger(day_of_week="sat", hour=12)` (after the week's books resolve, before Monday). Reads all `kpi/`, `attribution/`, `grades.jsonl`; writes `cio/<week>.{md,json}`; applies `reputation.json` changes **only** where `significant=true`.

**Other books (`autonomous/china/hk/heavyweight/etf`).** They keep their own cron times (08:00–23:25 UTC, disjoint data dirs — no state race per the existing comments). They receive **self-mirror only** in Phase 1 (cheapest), and the full funnel is Flagship-first; rolling the funnel to the Brain books is a Phase 3+ option (Open Question 7.6 E).

---

## 7.5 Phased Build Plan

Sequenced by ROI and anchored to the **2026-07-17 first-resolution window** (the earliest date 21-bday horizons from the first armed decisions become gradable; see Chapter 05 *Accountability* and the calibration deadline in MEMORY). Phases 1–2 must ship **before** 07-17 so the loop has graded data the moment outcomes resolve; Phases 3–6 consume that data.

| Phase | Deliverables | Acceptance test | Proving KPI |
|---|---|---|---|
| **P1 — Exit discipline + self-mirror** (cheapest, highest ROI; no new buy-side seats) | `brain/risk_officer.py` (judgment exits, subtract-only); `brain/self_mirror.py` injecting graded track records into existing brain prompts (`autonomous/china/hk/heavyweight`) + FORGE/SENTINEL; `exit_theses.jsonl` writer | held name with invalidated thesis is trimmed/exited *before* a mechanical detector fires, in a unit test; self-mirror string appears in the prompt and is contradiction-checked; **subtract-only asserted** (no exit thesis can add) | exit_saved_loss (KPI 7.2.6 `risk_off.exit_saved_loss` > 0 once resolved) |
| **P2 — Exit / veto / timing / watchlist GRADING** (closes the loop) | `brain/grader.py` (extends `outcomes.label_thesis` to every `decision_type`); `grades.jsonl`; `calibration.py` extended to `by_type` + per-role; `readiness.py` thresholds for new roles | every decision record with a due `check_by` produces exactly one leakage-free grade vs SPY; calibration multiplier stays `1.0` until `n≥12`; replaying a fixed fixture is deterministic | per-role `effective_n` climbing; calibration `status` flips `building→scoring` at n=12 |
| **P3 — Buy funnel + watchlist + Strategist/Technician/Gate** | `brain/strategist.py`, `brain/technician.py`, `brain/gate_officer.py`, `brain/pm_conviction.py`; `portfolio/watchlist.py`; `nexus()` quorum extension; `phase2.py` wiring (stages 1–8) | a FORGE-confirmed name with `technician=wait` is **parked, not bought** (test); a name with `gate=VETO` is dropped; quorum<MIN parks; a good-name/hostile-macro parks; **no path force-adds** | n_names discipline ↓ (concentration), watchlist promotion hit-rate, funnel shadow book vs prod |
| **P4 — Risk-Officer judgment exits at full authority** | promote P1 exits from advisory to acting; daily falsifier check on every held thesis; layered-on-detector tie-breaks (7.2.3) | when mechanical + judgment disagree, the stricter de-risk wins (test); held thesis whose falsifier hits is exited next build | active_vs_spy on exits; trim_hit rate |
| **P5 — CIO + reputation/attribution** | `brain/cio.py`; weekly job; `reputation.json`; `attribution/<asof>.json` (Brinson + timing residual) | CIO proposes a weight change but it is **not applied** while `significant=false`; attribution sums to active return ±ε | role authority changes only past significance; allocation vs selection split is stable |
| **P6 — Reward / risk-budget** | per-role reward shaping into `reputation.authority_weight`; risk-budget allocation across seats; optional funnel roll-out to Brain books | reward never grants additive power (demoted role still can't force a buy); risk budget respects caps | cross-book Sharpe / drawdown (`risk_tilt` shadow lever) |

**Dependencies:** P2 depends on P1 (needs decision records to grade). P3 depends on P2 (a buy funnel with no grading is a regression — we'd be adding seats with no accountability). P5 depends on P2+P3 (needs per-role KPIs). P6 depends on P5 (reputation is the substrate for reward). P1 and the self-mirror half of P1 are independent and can land in parallel.

---

## 7.6 Watchlist State Machine

```
                ┌─────────── re_review (daily, FREE) ───────────┐
                ▼                                                │
  [CANDIDATE] ──park──▶ [WATCHLIST] ──trigger met──▶ [PROMOTE] ──▶ re-enter funnel @ stage 4 (STRATEGIST)
       │                    │  ▲                                         (skips SCOUT+FORGE; `combined` re-scored daily
       │                    │  │                                          and dropped if <60; never auto-buys)
   gate VETO            watch_until                                  promotion still needs quorum + no veto
       ▼                 passed & trigger stale
   [DROPPED]                 ▼
   (logged, graded)      [EXPIRE] (logged, graded: did parking beat buying / dropping?)
```

**Reason codes** (closed vocabulary, used in `watchlist.jsonl.reason`): `good_thesis_bad_setup` (technician wait), `hostile_macro` (strategist), `gate_withhold`, `awaiting_catalyst`, `crowded_now`. **Never-dropped-before-`watch_until` invariant**: a parked name is re-reviewed every build for free (no LLM unless its trigger condition shows movement) and cannot be silently lost. Promotion is **not** an auto-buy — it re-enters the funnel and must clear quorum again (closes the "promote spam" hole).

---

## 7.7 Decision-Type Correctness Definitions (grader)

Each `decision_type` defines `correct` differently — this is the table `brain/grader.py` implements so calibration grades like-for-like:

| decision_type | `correct` iff | Graded against |
|---|---|---|
| add / champion | `rel_return > 0` over horizon | the name vs SPY |
| veto / withhold | `rel_return ≤ 0` (the name we declined underperformed) | the declined name vs SPY |
| timing `enter_now` | name `rel_return > 0` from entry date | name vs SPY |
| timing `wait` | name `rel_return ≤ 0` over the wait window (waiting avoided loss) | name vs SPY |
| macro SUPPORT/HOSTILE | sector/name aligned with stated direction | sector ETF or name vs SPY |
| trim / exit | name `rel_return ≤ 0` *after* the exit (exiting avoided loss) | name vs SPY, post-exit anchor |
| watchlist park | `delta = (would-have-bought rel_return) − 0` ≤ 0 (parking beat buying) | counterfactual |

`CONDITIONAL` and `NEUTRAL` stances remain **non-directional and excluded** from grading (preserving the existing SENTINEL exclusion). All use `brain/outcomes.label_thesis` with `falsifier.check.kind=="rel_return"`, anchored leakage-free.

---

## 7.8 Open Questions / Decisions for the User

These require a human call before or during the build; each has a concrete default we will adopt if no answer is given.

- **(A) Opus cost budget per night.** With N seats × confirmed names, Opus spend scales. *Default:* `MAX_OPUS_SEATS_PER_NIGHT` cap + SCOUT on Haiku, STRATEGIST/TECHNICIAN on Sonnet, only PM-CONVICTION/GATE-OFFICER/SENTINEL on Opus. **Decision needed:** the nightly dollar ceiling and whether STRATEGIST/TECHNICIAN may be promoted to Opus.
- **(B) How hard is the no-broad-ETF rule?** The Leadership sleeve currently *does* hold sector ETFs (`phase2.py:193-209`). *Default:* keep the no-ETF rule scoped to the **conviction/alpha sleeve only**, Leadership sleeve unchanged. **Decision needed:** does the Gate Officer's no-ETF mandate ever touch the Leadership sleeve, or is it a hard wall between sleeves?
- **(C) Concentration target.** "Too many random things" → what is the max-names floor/ceiling? *Default:* `MAX_NAMES=12`, `MIN_CONVICTION=60`. (The quorum is **conjunctive** — every named arm is individually necessary, §7.3 rule 5 — so there is no `QUORUM_MIN` count to tune.) **Decision needed:** confirm `MAX_NAMES=12` is the binding cap.
- **(D) Tie-break when quorum met but PM-Conviction is lukewarm.** *Default:* PM-CONVICTION is a required positive sign-off (no champion → no add, even at quorum). **Decision needed:** confirm PM-CONVICTION is mandatory, not just one vote of N.
- **(E) Do the Brain books (`autonomous/china/hk/heavyweight/etf`) get a Gate-style guardrail?** They are free-form Opus books today with no quorum. *Default:* Phase 1 self-mirror + Phase 4 Risk-Officer exits only; full funnel stays Flagship-only. **Decision needed:** whether any Brain book should adopt the Gate-Officer veto + watchlist (loses some free-form character, gains discipline).
- **(F) Significance method for CIO authority changes.** *Default:* reuse `portfolio/predictions.py` Newey-West HAC + date-clustering, `MIN_N=12`, no change below threshold. **Decision needed:** confirm the effective-n threshold for promoting/demoting a role's authority (12 is the calibration floor; the user may want a stricter bar for *authority* changes than for *confidence* shrink).
- **(H) Self-mirror injection — Phase-1 seat coverage. DECIDED (default):** the digest is injected as a **system-prompt prefix block** per seat, wrapped in a sentinel header (`<!--self-mirror-->…<!--/self-mirror-->`) so it can be stripped for `mirror_off` ablation. In **Phase 1** inject only into the two graded seats today — **FORGE** and **SENTINEL** — plus all Brain Opus prompts (`autonomous/china/hk/heavyweight`); **Phase 3+** extends to STRATEGIST, TECHNICIAN, PM-CONVICTION. Contradiction-check (`brain/self_mirror.py:_contradict_check()`): a new pattern supersedes an existing one only if it has `≥ MIN_N` support **and** the existing pattern has fewer supporting observations. **Decision needed:** confirm the Phase-1 seat set (FORGE/SENTINEL + Brain Opus) vs. injecting all Flagship seats from day one.
- **(G) Watchlist size cap. DECIDED:** `MAX_WATCH=40` (Ch03 §3.6.2). TTLs are `ttl_watch_td=20` / `ttl_armed_td=10`. At cap, the **lowest-scored** WATCH name (by `0.5·combined + 0.25·E_macro + 0.25·E_tech`) is evicted/EXPIRED to make room (Ch03 §3.6.2). Re-review is free unless a trigger moves, so the cost concern is bounded by construction.

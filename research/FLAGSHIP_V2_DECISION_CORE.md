# FLAGSHIP V2 — THE ENTRY ERA (decision-core overhaul)

**Date:** 2026-07-19 · **Owner:** Fable (main loop) · **Status:** BUILD (operator-ordered)
**Operator order (2026-07-19, verbatim intent):** Flagship is the worst-performing book because its
only buy process is a research/quality score — it buys AAPL +21% off a 3-week low, ANET extended and
topping, SMH into the Korea semis shock, MTUM right after a momentum unwind, QUAL/RF/SCHW/V with 3D
StochRSI ~90 rolling over, XLK while contagion spreads from semis to tech — and the one good buy
(XLE, bottoming) it almost exited. Feed it US Prophet's picks, connect the research process to the
full Neural Web context, and make ENTRY a first-class, binding concept.
**Relation to prior programs:** adopts the findings of `research/ENTRY_DISCIPLINE_PLAN.md`
(2026-07-03, unmerged branch `fable/entry-discipline` — its W0 annotator is absorbed here) and
**supersedes its advisory-first W1–W3 clock**: the operator order IS the P3 sign-off to bind at
birth. The instrumentation + pre-committed kill criteria from that plan §5 are retained so every
new gate stays refutable. The masterplan (`MASTERMIND_FIX_MASTERPLAN.md`) status log gains a W8
entry on merge.

---

## 1. Diagnosis (evidence-cited)

| # | Defect | Evidence |
|---|--------|----------|
| D1 | **Near-52w-high is encoded as buy confirmation.** `uptrend` requires `off_52w_high_pct > -12` — the gate's best entry IS the top. | `portfolio/lenses.py:377` |
| D2 | **All brakes are slow (200dma-scale).** No range position, no off-low run-up, no 20dma stretch, no oscillator/tier state, no rollover read. AAPL +21%/3wk cleared everything. | `lenses.py:584`, `conviction.py:141-179`, `watchlist.py:58` |
| D3 | **Thin-vote confluence inflation.** n_scored floor is 2; RF hit confluence **1.00 on 3 votes** (3 bull / 0 bear) → engine_score 100. | `lenses.py:1063`, thesis `2026-07-16-RF-conv` |
| D4 | **Context blindness.** sector_rs (rank + 200d) and narrative (rank + 5d rel) are the only rotation reads. No sector_pulse heat, no theme_context leadership state, no contagion, no momentum-unwind, no risk-tightened entry standards. `mastermind_context.json` — built FOR this bot — is dark (`MASTERMIND_NW_CONTEXT` off). | `lenses.py:431-492,702-730`; W-NW.1 status-log entry |
| D5 | **Leadership sleeve is entry-blind by construction.** Top-RS ETFs above 200d bought mechanically → SMH/XLK/MTUM/QUAL at the top. The universe-triage brake exists but is dark. | `bot/phase2.py:1018`, `_universe_triage_enabled` (default OFF) |
| D6 | **Research gate measures quality, then quality is averaged with a strength-biased engine score.** Nobody owns "is NOW a good time"; a 76 combined bought AAPL at the range top. | `brain/research_paper.py` REDIGEST; combined = .5·engine + .5·research |
| D7 | **Bottoming names look weak to a strength-rewarding gate.** XLE fell to confluence 0 at the low and survived only on hysteresis. There is no positive path for base/turn entries. Rotation-in park lane exists, dormant. | thesis `2026-07-08-XLE`, `watchlist.py` rotation lane ("no live call site") |
| D8 | **Prophet, signal_gate tiers, and stage analysis are unused** despite being on disk: `site/prophet/index.json` (entry/trigger/invalidation plans, tier-gated upstream), `site/factordata/signal_gate.json` (validated MACD-2D × StochRSI-3D tier cascade), stage roster. | vendor/macro on-disk; no consumer greps in repo |

Root cause in one line: **the pipeline answers "is this a real leader?" and "is it broken?" but
never "is NOW a good price?" nor "is the market weather FOR this cohort right now?"** — and its
definition of a confirmed entry is literally the top of the range.

## 2. Architecture — the buy triad

Every would-be NEW position (conviction name, leadership ETF, prophet pick) must pass **three
orthogonal assessors. Multiplicative, never compensatory** — a 100 quality score cannot buy through
a failed entry. Failing Entry/Context does not discard the name: it **parks** it on the existing
watchlist with explicit *promotion triggers* (patience, not forfeit).

```
                    ┌────────────────────────────────────────────┐
 candidates ─────►  │ QUALITY  — confluence matrix + research    │  is it worth owning?
  us_standouts      │   paper (role narrowed to quality/thesis)  │
  baskets ∪ theses  ├────────────────────────────────────────────┤
  regime_seed       │ ENTRY    — portfolio/entry_engine.py (NEW) │  is NOW a good price/technical
  intake ∪ NW scan  │   range/stretch/off-low/rollover/tier/     │  moment to START?
  PROPHET feed(NEW) │   stage/prophet-geometry                   │
  watchlist promos  ├────────────────────────────────────────────┤
                    │ CONTEXT  — portfolio/context_gate.py (NEW) │  is the market weather FOR
                    │   sector heat/theme leadership/contagion/  │  this name's cohort?
                    │   momentum-unwind/risk-regime tightening   │
                    └────────────────────────────────────────────┘
        buy = quality PASS ∧ entry BUYABLE ∧ context ¬BLOCKED
        fail entry/context → PARK with promotion triggers (watchlist; rotation lane for base-turns)
        held names: NEVER exited by entry/context (entry is an entry brake — standing invariant)
```

### 2.1 `portfolio/entry_engine.py` (NEW — absorbs `entry_quality.py` from the stalled branch)

Pure, injectable, fail-open (charter P2: a missing signal withholds nothing; the existing W0
fail-closed stockdata floor still owns true outages).

API: `assess(ticker, *, series=None, stockdata=None, signal_gate_row=None, stage_row=None,
pulse=None, theme_id=None, prophet_plan=None, as_of=None) -> dict` returning
`{ticker, verdict, buyable, entry_score (0-100), metrics, notes, park_triggers, sources, as_of}`.

Verdict vocabulary (blocking → parked unless noted):
- `knife` — ret_5d ≤ −9% or ret_10d ≤ −14% (existing thresholds, reused).
- `rollover` — was at range top (≥90th pctile of 60d range within last 10 sessions) AND now
  (range ≤ 80th OR ret_5d ≤ −3%) AND not at range low; strengthened by MACD− or tier-ineligible.
  *The ANET / QUAL / V "stoch 90 rolling over" class.*
- `chase` — range_pctile_60d ≥ 90 AND (ret_10d ≥ +18% OR pct_vs_20dma ≥ +12%). *(W0 thresholds.)*
- `late_leg` — ret_from_63d_low ≥ +20% AND range_pctile ≥ 85 AND days_since_63d_low ≤ 45.
  *The AAPL +21%-off-the-bottom-3-weeks-ago class.*
- `extended` — pct_vs_20dma ≥ 12 OR pct_vs_200dma ≥ 30 OR within 6% of 52w high (no fresh rip).
- Buyable: `base_turn` — stage 1→2 fresh (entered_stage2 / weeks_in_stage ≤ 8) AND tier ∈ {T1,T2}
  (or signal_gate eligible) AND range ≤ 60. *The XLE class — the positive path D7 lacked.*
- Buyable: `pullback_in_trend` — above 200dma, range_pctile ≤ 40, not knife. *(The AVGO dip class.)*
- Buyable: `clean` — none of the above fired.
- `unknown` — no usable sources → **fail-open: no withhold** (upstream data_degraded floor owns outages).

Prophet geometry (when `prophet_plan` present): R = |entry − invalidation|;
price > T1 → `missed_move` (blocking); price > entry + 0.5R → `extended_vs_plan` (blocking);
within [invalidation, entry + 0.5R] → within-zone (+10 entry_score, marks `plan_zone_ok`).

`park_triggers` (consumed by the watchlist predicate): e.g. `{range_pctile_lte: 60,
tier_in: ["T1","T2"], not_verdicts: ["rollover","chase","late_leg","extended"]}`.

Also exports `still_withheld_reason(ticker) -> str|None` for the watchlist review predicate
(composes with the existing `timing_withhold`).

Data sources (all vendored, all optional): price series via `lenses._closes`; stockdata tech block;
`site/factordata/signal_gate.json` (per-ticker `{eligible, tier_cascade, state, above200}`);
stage roster `data/stage_analysis/context/latest.json` (`roster{ticker: {stage_flag,
weeks_in_stage, ...}}`, requires sparse-checkout addition); `site/basketdata/sector_pulse.json`.

### 2.2 `portfolio/context_gate.py` (NEW)

API: `assess(ticker, *, sector=None, theme_id=None, entry_verdict=None, is_etf=False, …injectables)
-> {verdict ∈ favorable|neutral|against|blocked, context_score, reasons, park_triggers, sources}`.

Rules (each fail-open; missing artifact contributes nothing):
1. **Sector heat** (`sector_pulse` + `sector_central.json`): theme heat `broken` → BLOCKED for new
   entries; `cooling` + `fading` + high crowding → AGAINST. `early`/`heating` + clean_entry →
   FAVORABLE credit.
2. **Theme leadership** (`theme_context.json`): candidate's basket in `leadership.breaking[]` →
   BLOCKED; leadership.state ∈ {strain, rotating} and candidate is in the *trailing leader's*
   cohort → AGAINST (don't chase the old leader mid-rotation); in `strength[]`/challenger →
   FAVORABLE credit.
3. **Contagion** (`mastermind_context.lobes.contagion`, introspected defensively): active elevated
   pressure whose source/downstream channels touch the candidate's sector/theme → BLOCKED for new
   entries in that cohort. *The SMH/XLK "semis → tech spread" class.*
4. **Momentum unwind** (`site/factordata/momentum_display.json`): state=unwind → BLOCKED for
   momentum-vehicle ETFs (MTUM/SPMO); −15 context_score + reason for chase-class equity entries.
   *The MTUM class.*
5. **Risk regime** (`data/regime/latest.json` risk_radar + `site/live/risk_state.json`): caution →
   only `base_turn`/`pullback_in_trend` entry classes allowed (breakout chases parked); elevated →
   additionally require tier ∈ {T1,T2}; risk_off → BLOCKED for new non-defensive entries.
6. **Factor seasonality** (`factor_seasonality.json`): headwind month for the name's dominant
   factor → score tilt only (never veto). *(v1.1 backlog — not in the shipped v1, alongside
   `sector_central.json` fading/crowding: the shipped v1 covers rules 1-5 via sector_pulse +
   theme_context + the contagion lobe + momentum_display + risk_radar.)*

Verdict → behavior: BLOCKED → reject + park (context lane, triggers = what must clear);
AGAINST → entry-size multiplier 0.6 (composed via the existing terminal-subtract pattern so
`risk_sizing.apply` renorm can't erase it — same trick as `_apply_extension_brake`);
FAVORABLE/NEUTRAL → no brake.

### 2.3 Prophet feed — `portfolio/prophet_feed.py` (NEW, opus-built)

Reads `vendor/macro/site/prophet/index.json` (`prophet.index/v1`; mirrored to public R2). Exposes
`plans()`, `candidate_tickers()` (additive source into `conviction.candidates()`, capped 8),
`plan_for(ticker)` (freshest, highest `_conviction_score`), and `entry_discipline(ticker, price)`
(the plan-geometry chase guard of §2.1). Staleness > 4 days → inert. Prophet-sourced names carry
`source="prophet"` + plan id into thesis evidence. Flag `MASTERMIND_PROPHET_FEED` default ON.

### 2.4 Synthesis repairs (unconditional bug fixes, deliberately not flag-gated)

1. **D1 fix:** `_trend_row` bull no longer requires `offhi > -12` (proximity-to-high is entry
   evidence, not trend confirmation; the entry engine owns it now). Bear semantics unchanged.
2. **D3 fix:** `size_authority='up'` additionally requires `n_scored ≥ 5`. Below the floor →
   `hold` (held names unaffected — hysteresis path keys on confluence, not `sa`). RF-class
   3-vote confluence=1.0 can no longer enter.

### 2.5 Conviction wiring (`conviction.build`, additive param `asof=None`)

After the existing gate passes a NEW entry (`entry_ok`): compute `entry_engine.assess(t,
prophet_plan=prophet_feed.plan_for(t))` + `context_gate.assess(t, entry_verdict=…)`.
- entry not buyable OR context BLOCKED → move to `rejected` with the verdict + a `park` record
  (phase2 enrolls it via `watchlist.append` — same seam as today's timing withholds).
- context AGAINST → `ctx_mult=0.6` composed with `ext_mult` in the terminal subtract.
- reports attached to the position dict (`entry_report`, `context_report`) → flow into theses,
  provenance rows, and the research prompt. Thesis evidence now says *why now*, not just
  "confluence +0.60".
- `base_turn` names that fail ONLY the confluence bar (< 0.30) are enrolled into the watchlist
  **rotation lane** (`append_rotation`, `MASTERMIND_ROTATION_IN` default flips to `watch`) — the
  D7 positive path: tracked WATCH→ARMED as tier/stage confirmation builds, promoted into
  `candidates()` on confirmation. Additive candidate sourcing only; the gate still decides.

### 2.6 Leadership sleeve second pass (phase2, after `_leaders_pre`/triage)

Each NEW (non-held) leadership ETF must pass `entry_engine.assess(etf)` (buyable) and
`context_gate.assess(etf, is_etf=True)` (not BLOCKED). A failed leg is DROPPED — freed budget
stays in cash (`lw` still computed on `len(_leaders_pre)`, the same invariant as the triage
brake), the ETF is parked with triggers, and a `leadership_withheld` runlog row is written.
*Replay: SMH (contagion source), XLK (contagion downstream), MTUM (unwind), QUAL (rollover) all
fail; their slots ride in cash rather than being force-bought at the top.* Held legs are exempt.

### 2.7 Research gate overhaul (`brain/research_paper.py`, opus-built)

- **Role narrowed:** the paper validates the BUSINESS (quality, valuation, catalysts, falsifiers).
  Timing authority moves to the deterministic entry/context engines.
- **Context injection:** the prompt receives the NW seat block (`neural_web_context.
  seat_prompt_block`), the market-weather line, the candidate's `entry_report` +
  `context_report`, and the Prophet plan when present. Two new required sections: *Entry & timing
  read* and *Market context fit* — the analyst must engage with the injected evidence.
- **Redigest additions:** `entry_agreement ∈ agree|caution|disagree` + `entry_note`. **The LLM may
  only DE-ESCALATE** (house law): `disagree` on a deterministically-buyable entry downgrades it to
  `extended` (park); agreement can NEVER upgrade a blocked entry. `combined` formula and the
  ≥60 bar are unchanged — score conflation was the disease; entry/context stay gates, not blends.
- `MASTERMIND_NW_CONTEXT` default flips ON (arming condition + operator order; prompt-text only).
  `MASTERMIND_NW_DECISION` default → `shrink` (candidacy sourcing + subtract-only shrink; `vote`
  stays off).

### 2.8 Watchlist promotion upgrade (plan §4.5, now live)

`still_withheld` predicate for timing-lane rows becomes: existing `timing_withhold(tech)` OR
`entry_engine.still_withheld_reason(t)` OR context still BLOCKED. A parked name promotes the
build after its triggers clear — tier flips T1/T2-fresh, range pctile retreats ≤ 60, rollover
state clears, sector heat flips heating, contagion clears, unwind ends. The queue stops being
"wait N days and retry the same coarse check" and becomes "wait for the validated entry trigger".

## 3. Invariants (unchanged and honored)

- **Subtract-only (charter P2):** every new mechanic can only withhold, park, shrink, or leave in
  cash. Nothing new sizes up, un-caps, or flips direction. Missing/stale NEW inputs contribute
  nothing (fail-open); the existing fail-closed stockdata floor + data-health breaker still own
  true outages. Held names are never exited by entry/context reads.
- **The refuted cycle veto stays dead:** all new gates act on NEW entries only.
- **Flags:** `MASTERMIND_ENTRY_GATE` (entry+context binding, default ON), `MASTERMIND_PROPHET_FEED`
  (default ON), `MASTERMIND_ROTATION_IN` (default `watch`), `MASTERMIND_NW_CONTEXT` (default ON),
  `MASTERMIND_NW_DECISION` (default `shrink`). Each opt-out restores the prior path. §2.4's two
  repairs are unflagged bug fixes.
- **Instrumentation:** every entry/context verdict is stamped on decision/provenance rows for
  forward grading. Pre-committed kills (plan §5) stand: if `chase`-verdict buys do NOT
  underperform `clean` buys on forward entry-drawdown over ≥40 graded, the chase-guard demotes to
  display-only; same discipline per gate.

## 4. Acceptance (ships only if all green)

1. **Replay battery** (`tests/test_flagship_v2_replay.py`, fixtures snapshotted from the real
   2026-07-18 vendor artifacts): AAPL→`late_leg`, ANET→`rollover|extended`, SMH→context BLOCKED
   (contagion), MTUM→context BLOCKED (unwind), QUAL→`rollover|extended`, SCHW/V→`extended|rollover|
   chase`, XLK→context BLOCKED (contagion downstream), RF→blocked by the n_scored floor (D3) or
   entry — **and XLE passes** (buyable entry, context not blocked).
2. Flags-off inertness: with the five flags off, entry/context/prophet/NW/rotation additions are
   inert (no new rejects, no parks, no new candidates).
3. Full pytest in the worktree: zero NEW failures (catalogued pre-existing only);
   `tests/incident_replays/ -q` green EXCEPT the catalogued pre-existing
   `test_composed_stack_flag_on_disagreeing_tape` red (posture_class BALANCED vs
   ROTATE_DEFENSIVE — predates W8, fails byte-identically on master with all W8 flags off;
   owned by the posture-decider lane, not this program).
4. Masterplan status log gains the W8 entry; this doc + the absorbed `ENTRY_DISCIPLINE_PLAN.md`
   land in `research/`.

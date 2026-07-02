Now I have the full grounding — charter (ten principles + §standing questions), masterplan (W0–W4 shipped, W-I open, W5–W7 pending), architecture §6 wishlist, module inventory, and the four synthesis inputs. Composing the build plan.

# W-E (EYES) BUILD PLAN — the Perception Era wave

**Owner:** Fable · **Slot:** runs after W-I merges, before/with W5 · **Flag:** `MASTERMIND_POSTURE_DECIDER=0` throughout until §3 gates green · **Governing law:** P1/P2/P3/P5/P7 + the W2 anti-compounding rule (every signal consumed exactly once) + the masterplan §4 guardrail (bot never blocks on the dashboard).

---

## 1. Target perception architecture (one page)

```
 PLANES (deterministic, freshness+confidence stamped, zero LLM)
 ├─ Tier-A adapters over the ALREADY-LOADED vendor/macro/data/regime/latest.json
 │    risk_radar(pri97) · mtf_signals(94) · froth_fragility(88) · risk_state.gross_factor(85)
 │    turning_point(66) · vol_shock(61) · cross_asset · market_drivers · dislocation · macro_risk
 ├─ regime_frame.cycles() sector_rs/phase (existing one-reader; UNTOUCHED as the reader)
 ├─ NEW brain/rotation_tensor.py  → data/market_view/rotation_tensor.json
 │    (12×12 R/dR bps/day pairwise RS-velocity, breadth-migration, churn — from data/yahoo parquets)
 ├─ NEW brain/anticipation.py     → data/anticipation/<asof>.json
 │    (SECTOR-TOP / BUBBLE-FORMATION / CRASH-RISK alarms; vendored legs only in v1)
 ├─ W-I planes: distribution_tells · liquidity_quality · regime_nowcast  (whatever authority W-I earned)
 └─ null-advisory stubs: subsector RRG, group_flow, event calendar, intl spillover (fill via handoff H4)
        │
        ▼
 NEW brain/market_view.py — THE one view (P7), data/market_view/latest.json, schema market_view.v1
   PlaneRecord{reading, direction, magnitude, freshness{asof,age_sessions,stale}, confidence,
   status: validated|advisory, source_contract, raw} · net_posture_tilt from VALIDATED planes only ·
   disagreements[] + TOP-LEVEL label_vs_planes{conflict, magnitude} · coherence · budget_ref (read-only
   audit embed) · deterministic brief{what_changed, whats_rotating, wheres_the_risk, posture_implication}
   Absent/stale planes = weight 0, can never lower disagreement; coverage<floor ⇒ view may only refuse
   to loosen (P2). Optional Sonnet brief polish: env-gated OFF, display-only, never re-read.
        │
        ▼
 NEW brain/posture_decider.py — the ONE sizing consumer of the view (P5)
   OFFENSE: W2 equation clamp(0.40+0.20·conf·T·F, .40, .60) moves VERBATIM inside decide()
   DEFENSE: defense_pressure D = availability-renormalizing EQUAL-WEIGHT mean over shrink-biased planes
     (regime fragility, transition tilt, flip fragility, W1 dwell, cycles entry/late, distribution_tells,
      liquidity_quality, regime_nowcast, rotation_tensor composite, anticipation floor, radar-when-present)
   → class bands OFFENSE<0.25 / BALANCED / ROTATE-DEFENSIVE≥0.50 / PRESERVE≥0.75
   → offense_budget · defense_floor=DEF_SLEEVE_MAX·D · cash_floor · conviction_appetite · posture_notch_cap
   Hysteresis: escalate same-session; de-escalate = 3 consecutive lower-band builds AND no sev≥2 in 2
   sessions; max-dwell auto-release 15; state data/posture/state.json, degrade-to-stateless.
        │
        ▼
 BOOKS (P7 — one view, all books)
   systematic: phase2 lead_budget ← posture.offense_budget (:337-343) · build_def_sleeve(target=
   posture.defense_floor) (:820) · conviction NEW × appetite · eff_cap = min(state_cap, severity_cap,
   posture_notch_cap) in bot/derisk.py (min-composed — idempotent ceilings can't double-cut)
   LLM books: PRIORITY DIRECTIVE seam (autonomous.py:248, judgment_book.build(directive=)) — binding
   guidance bracketed by engine caps; brain/posture_compliance.py grades realized-vs-target gross per
   book → data/posture/<asof>/deviations.json → three-questions Brier duty → W6 registry probation.
```

### Subsumption ledger (P7 — exact, each with its byte-identity migration test)

| Signal/home today | Disposition under W-E | Migration test (ship-blocker) |
|---|---|---|
| `regime_frame.budget()` (the W2 ONE equation) | Becomes a thin shim returning `posture.offense_budget`; the equation text moves verbatim into `decide()` | Flag OFF ⇒ `budget()` byte-identical to pre-W-E master on W2 golden vectors + calm-tape invariance + China/HK callers green |
| **W-I evidence-damped budget** | Damp multiplier REMOVED from the offense equation at arming; its evidence planes (distribution_tells / liquidity_quality / regime_nowcast) become planes of D — shrink applied once, on the defense side | Flag OFF ⇒ damped budget byte-identical to W-I master. Flag ON ⇒ grep-gate + runtime consumption-registry assert that no plane feeds both the offense multiplier and D; disagreeing-tape fixture asserts posture-on total gross ≤ posture-off (shrink-only, no compounding) |
| `portfolio/rotation.py fragility_signal()` | DIES as a public sizer (its legs — dwell, conf, WEAKENING — are planes of D); survives only as the flag-off fallback inside `build_def_sleeve` | Flag OFF ⇒ def-sleeve output byte-identical; grep-gate CI: `fragility_signal`, frame confidence, transition_state read NOWHERE downstream of `decide()` when flag ON |
| **W-I item 4 (rotation-evidence budget term)** | NEVER ships as a separate budget term (coordinate with the W-I session now); if already landed, the E2.2 subsumption PR deletes it — it IS the rotation_tensor + nowcast planes of D | Ablation: fixture day with rotation firing shows exactly one gross-reduction pathway in the runlog provenance |
| `bot/derisk.py` severity ladder | Unchanged off-schedule reflex; gains `posture_notch_cap` via `eff_cap=min(state_cap, severity_cap, posture_notch_cap)`; anticipation-CRITICAL notch and W-I distribution-escalator notch write through ONE seam with `dedup_key`/`source` and `max()` not sum. GEX exactly-once: derisk owns gex for severity; anticipation reads gex only to classify, its D contribution computable from non-gex legs | Notch-dedup unit test (both notches firing = one notch); posture_notch_cap absent ⇒ eff_cap identical to W1 behavior |
| market_view Phase-2 binding consumers (DEF_SLEEVE `view_conflict` bump; derisk disagreement hold) | SHADOW-ONLY stepping stones; subsumed by D at arming. Pre-committed fallback: if posture slips >2 weeks past gate-green, arm the bounded view_conflict sleeve term ALONE (and remove it the day posture arms) | Never-both-live assert in config validation |
| anticipation `w_antic` in fragility_signal | Shadow-only; dies with fragility_signal — the battery enters D as an alarm floor (raises D, never lowers) | Covered by fragility retirement test |
| `brain/posture.py` (existing display-only) | Stays display-only; re-pointed to read `data/posture/latest.json` (name collision is why the decider is `posture_decider.py`) | — |
| ad-hoc 7-key regime slices in pm_conviction / strategist / bot_mcp `get_regime` | Replaced by brief + PlaneRecords + label_vs_planes (this completes W4's prompt edits from one contract) | Prompt-payload golden test; key-order golden on the artifact |
| `regime_frame.py` frame/cycles readers, `defensive_candidates.py`, W1 dwell/tripwire, W2/W3 caps | UNTOUCHED — they are planes/name-sources/brakes the posture layer composes with | Existing golden suites stay green |

---

## 2. Build order, dependencies, model routing

**Hard dependency facts:** market_view and anticipation need NOTHING new — ~90% assembles from blocks already embedded in the vendored regime file plus data/yahoo parquets. Posture decider can BUILD before W-I merges (missing planes = weight 0) but may only ARM after W-I lands (D needs its incident-catching planes) and after the E2.2 subsumption PR. Nothing waits on the dashboard (all handoffs fill null-advisory planes later).

### W-E.0 — Perception organs, zero behavior change (days 1–3, bot-side only)
| # | Task | Tier |
|---|---|---|
| E0.1 | `brain/rotation_tensor.py` per the tensor spec (blocks a–e, causal-only math, `top_pairs` extract, breadth degrade-to-`unavailable` never fabricate) → `data/market_view/rotation_tensor.json` | Sonnet |
| E0.2 | `brain/anticipation.py` battery (sector_top/bubble_formation/crash_risk from sector_cycles baskets + mtf_monitor + gex; authority tiers AS CODE — notch-eligible set hard-restricted to forward-graded legs; `rs_diff` magnitude on SECTOR-TOP) → `data/anticipation/<asof>.json`, flag OFF | Sonnet build, Opus review of alarm logic |
| E0.3 | `brain/market_view.py`: PlaneRecord adapters over the embedded Tier-A keys + cycles() + W-I planes + null stubs; disagreement layer (validated-only tilt, label_vs_planes top-level); deterministic brief; atomic tmp+`os.replace`; per-plane freshness from each block's OWN asof | Opus (disagreement layer), Sonnet (adapters), Haiku (schema stubs, golden key-order test) |
| E0.4 | Incident replay fixture (frozen 06-20..07-02 inputs) + calm-tape fixture + missing-file no-op + freshness fail-closed tests | Sonnet (asserts), Haiku (input freezing) |
| E0.5 | Runlog perception step at the TOP of phase2 (P5: perception logged before position) | Sonnet |

### W-E.1 — Read-only fan-out + validation harness (week 1–2)
| # | Task | Tier |
|---|---|---|
| E1.1 | pm_conviction/strategist/bot_mcp `get_regime` → brief + PlaneRecords + label_vs_planes ("3 validated planes dissent, no semis seed") | Sonnet, Opus prompt review |
| E1.2 | `site/market_view.html` mirror off the artifact | Haiku |
| E1.3 | Wake trigger: new/flipped VALIDATED disagreement joins `gate.state_signature`; overnight re-assembly with `seq++` | Sonnet |
| E1.4 | Walk-forward jobs: CRASH-RISK AUC 2011–2026, rotation-tensor composite, disagreement forward log (gates in §3) | Opus (gate design), Sonnet (harness) |

### W-E.2 — Posture decider + the subsumption PR (weeks 2–3; shadow by default)
| # | Task | Tier |
|---|---|---|
| E2.1 | `brain/posture_decider.py`: D equal-weight PINNED (adaptive weights amputated from v1 — posture_governor stays the only learner), class bands, hysteresis state machine, offense equation verbatim; publishes `data/posture/latest.json` SHADOW every build with full evidence trail | Opus, Fable sign-off (sizing spine) |
| E2.2 | The subsumption in ONE PR: budget shim, fragility retirement, `build_def_sleeve(target=)`, min-composed eff_cap + notch-dedup seam, evidence-damp removal-at-arm wiring, grep-gate as CI ship-blocker — with every §1 byte-identity test written FIRST | Sonnet build, Fable sign-off |
| E2.3 | `brain/posture_compliance.py` + deviations artifact → Brier duty | Sonnet |
| E2.4 | `doctrine.yml posture:` block (bands + class maps, tagged `unverified-prior`; W5 E-regression sizes them later) | Haiku |
| E2.5 | Directive rendering to the 5 LLM/free-form books via the existing autonomous.py:248 seam | Sonnet |

### W-E.3 — Shadow → arm (weeks 3–4+)
| # | Task | Tier |
|---|---|---|
| E3.1 | 2+ weeks shadow posture logging would-have-done deltas per book | (runs itself) |
| E3.2 | Dashboard handoffs land as available; null adapters flip live (one small PR each) | Sonnet per item |
| E3.3 | ARM `MASTERMIND_POSTURE_DECIDER=1` when: replay battery green + calm-tape zero-drift + shadow shows no compounding on disagreeing tape + CRASH-RISK AUC gate passed + W-I merged | Fable decision |
| E3.4 | Pre-committed kills executed per §3; fallback path per §1 ledger if posture slips | Fable |

---

## 3. Validation battery (P3 — every object, pre-registered gate, honest cold-start label)

| Object | Cold-start label | Pre-registered gate (to touch size) | Auto-demotion falsifier |
|---|---|---|---|
| risk_radar plane | **validated** (forward_log.jsonl, 2006–2026 calibration exists) | already earned; enters D and tilt day one | rolling 60-session rank-IC vs SPY 5d fwd drawdown ≤0 ⇒ demote to advisory |
| mtf_signals plane | **validated** (MTF walk-forward on record) | already earned | same 60-session IC rule |
| froth_fragility plane | advisory (`provisional` flag present) | dashboard graded log shows AUC>0.55 on alert days | stays advisory otherwise |
| cycles entry/late plane | **validated** (entry-tilt survives; veto refuted — tilt only, never exit) | already earned | W7 ablation: entry-tilt spread ≤0 ⇒ display-only |
| rotation_tensor composite | advisory | walk-forward 2011–2026: defensive-rotation episodes (percentile<5%, dR confirming) vs SPY 5d fwd max-drawdown AUC>0.55 AND fires <10% of days | 21d falsifier per episode; negative shadow Brier vs coin-flip ⇒ display permanently |
| CRASH-RISK alarm | advisory, `cold_start=true` | same AUC>0.55 + <10%-of-days gate ⇒ arms w_antic-equivalent D floor + the severity notch (with dedup seam shipped together) | negative shadow Brier ⇒ do-not-build list |
| SECTOR-TOP alarm | advisory forever-until-gated (barred from severity) | 21d fwd sector-RS-vs-SPY after ELEVATED: rank-IC>0 on ≥20 episodes | pre-registered in calibration/shadow_books |
| BUBBLE-FORMATION | advisory until a forward-graded crowding artifact exists (needs handoff H4 froth/radar legs) | explicit: no gate available yet — honestly labeled | — |
| W-I planes (distribution/liquidity/nowcast) | whatever authority W-I earned — sub-validated planes may RAISE D, never lower it | their own W-I gates | inherit W-I falsifiers |
| label_vs_planes disagreement | advisory | forward log: conflict-days vs 5d fwd drawdown AUC>0.55 ⇒ "validated disagreement" may hold severity-cap release | 60-session IC rule |
| posture class bands | shadow 4 weeks | (a) ROTATE-DEFENSIVE-day fwd 5d SPY max-drawdown distribution stochastically worse than OFFENSE-day (pre-registered KS test); (b) ablation: brake-week saving ≥ ⅓ of trend-week give-up, else bands revert | kill = revert to flag-off (today's book, byte-identical) |
| deferred by design | — | adaptive plane weights, skill_mult ledger (cannot reach effective_n≥8 in-gate), LLM brief as decider input: all explicitly NOT in v1 | — |

Every artifact stamps `status`, `cold_start`, `effective_n`. Nothing advisory signs `net_posture_tilt` or releases a cap — advisory can only annotate and shrink.

## 4. Incident-replay extension (P6 — permanent CI fixtures, frozen 06-20..07-02 inputs)

The battery the stack must pass forever, extending the existing W1/W2 replay asserts:
1. **anticipation:** SECTOR-TOP(tech/semis) ≥ELEVATED **by 06-25** (design evidence says ~06-19, so the assert has margin; CRITICAL 06-22..25); CRASH-RISK ≥ELEVATED by 06-26.
2. **rotation_tensor:** `R[XLV][SMH]` positive multi-session episode in `top_pairs` by 06-24, `dR` same-signed, episode percentile <1%.
3. **market_view:** `label_vs_planes.conflict=True` EVERY session 06-26..07-01 (soft/allowed-either on 06-24); 07-01 assert: label risk_on @ conf 0.327 vs cycles/rotation/radar risk_off, coherence≈0.38, `posture_floor_defense=True`.
4. **posture:** D≈0.74 on 06-26 ⇒ **ROTATE-DEFENSIVE by 06-26** (offense 0.40–0.45, defense_floor 0.22–0.27, notch cap 0.70, appetite ~0.5); 07-01's regressed STABLE print drops raw D but de-escalation is **dwell-blocked** — class HELD; **07-02 SMH rebuy suppressed**.
5. **Rosy-plane stress variant:** regime planes forced to 0 ⇒ D≈0.50, still ROTATE-DEFENSIVE (band edge pinned by fixture, not hand-tuned).
6. **Calm-tape control:** high-confidence agreeing window ⇒ conflict=False, class OFFENSE, budgets byte-identical to today — zero drift.
7. **Deploy-lag:** the W-I tripwire covers W-E artifacts too (a perception organ built but not running protects nothing — P10).

## 5. Dashboard handoff list vs build-direct list

**Handoff to dashboard sessions** (spec already written in the contracts doc; each fills a null-advisory plane, none blocks):
- H1: transition-state RATCHET + `flag_rotation_persistence` (engine/transition.py; additive fields `transition_state_raw/ratcheted/dwell_remaining`).
- H2: continuous P(Quad) vector under a NEW name (`next_quad_probs` is taken — it's the historical Markov object).
- H3: `schema_version` + top-level `flip_margin` + liquidity-quality field in latest.json.
- H4: sparse vendoring-manifest extension — risk_radar snapshot (eb9fd0b lineage), froth log.jsonl, dislocation state_log, group_flow DSR meta, auction parquet, `subsector_rotation.json` (RRG), event calendar, intl spillover. Unlocks: BUBBLE-FORMATION's crowding leg, the RRG plane, radar-floor upgrade.
- H5: per-sector breadth (`pct_above_50` per GICS sector) — closes the tensor's named breadth_migration gap.
- H6: one consistent `asOf` across artifacts (wishlist #5).

**Build-direct (bot-side, no dashboard work):** all Tier-A embedded-key adapters (risk_radar/mtf_signals/froth/gross_factor/turning_point/vol_shock — the coverage audit's structural finding: they're in the file the bot already reads); rotation_tensor (data/yahoo parquets); anticipation v1 (sector_cycles baskets + mtf_monitor + gex, all vendored); market_view; posture_decider; posture_compliance; all fixtures.

## 6. Standing self-interrogation — as it reads after W-E ships

1. **Autonomous enough to run with no human catching mistakes?** Materially closer, not yet. Posture is now decided mechanically before names from a multi-plane view (P5 structural), wrong labels are outvoted rather than obeyed, and compliance deviations are graded. But the learning loop is still open: W5's marks/benchmark-ledger/attribution don't exist, so the bot still cannot *measure* whether its decisions were correct against the bogey — autonomy without self-grading is confidence, not competence. Judgment-seat promotion is still mid-shadow.
2. **Would it make the 07-02 class of mistake again?** No — proven by the §4 replay battery in CI, not opinion: SECTOR-TOP by 06-25, ROTATE-DEFENSIVE by 06-26, dwell-blocked de-escalation on 07-01, SMH rebuy suppressed 07-02. Honest scope limit: the fixture proves the *label-lies-while-price-rotates* class. A credit-led or overnight-gap class has planes (liquidity_quality, gex) but no fixture yet — next incident of that shape must become one (P6).
3. **Enough visibility, and does it reach sizing?** Visibility: from ~10 consumed fields (~20% of the audited queue) to all Tier-A embedded sub-planes plus the tensor — roughly 70–80% of contracts, ~95% priority-weighted; remaining blind spots are exactly the H4/H5 handoffs (RRG, group_flow, event calendar, intl spillover, per-sector breadth), shipped as honest null planes. Reach: yes, via exactly ONE path — planes → view → D → offense_budget/defense_floor/notch_cap — with grep-gated single consumption; advisory planes can annotate and shrink but never size.
4. **Single highest-leverage enrichment remaining?** W5 (marks + benchmark ledger + grading fixes + shadow arms) — the §0 goal metric is still uncomputable, and until "held defensives and did nothing" is creditable, the posture decider is steering by a compass no ledger checks. Schedule W5 immediately behind W-E arming; W6 (book portfolio/registry lifecycle) follows. Within perception itself: the H4 manifest extension is the cheapest large visibility gain left.

---
**Plan file references:** charter `/Users/chriswong/Documents/Cluade/Mastermind/research/MASTERMIND_CHARTER_V2.md` · masterplan `/Users/chriswong/Documents/Cluade/Mastermind/research/MASTERMIND_FIX_MASTERPLAN.md` (add W-E to §3 + Status log) · architecture §6 wishlist `/Users/chriswong/Documents/Cluade/Mastermind/research/MASTERMIND_V2_ARCHITECTURE.md` · incident fixtures root `/Users/chriswong/Documents/Cluade/Mastermind/research/incidents/2026-07-02-semis-breakdown/` · new modules: `brain/market_view.py`, `brain/rotation_tensor.py`, `brain/anticipation.py`, `brain/posture_decider.py`, `brain/posture_compliance.py` · touched: `brain/regime_frame.py` (budget shim), `portfolio/rotation.py` (fragility retirement), `bot/derisk.py` (min-composed cap + dedup seam), `bot/phase2.py` (:337-343, :820, perception runlog step), `bot/autonomous.py` (:248 directive seam), `doctrine.yml`.
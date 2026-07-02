# DASHBOARD CONTRACT UPGRADES — handoff-ready spec (Perception Era)

Grounded in the three engine files (`engine/regime.py`, `transition.py`, `axes.py`), the 07-02 incident autopsy (`signals.md §4`, `synthesis.md §4`), the published `latest.json` assembly (`engine/run.py:96-119`), and the existing `playbook.next_quad_probs` contract. Governing charter principles: **P1** (label is an input to perception, never perception), **P2** (shrink-only / wrong data degrades), **P3** (advisory unless validated), **P7** (one source of truth per concept). The bot side degrades to today's behavior when any item never ships (masterplan §4 guardrail 11).

Key primary-source facts that shape every item below:
- `latest.json` today has **no `schema_version`**, no top-level `flip_margin`, no liquidity-quality field. `flip_condition` (a dict with nested `margin`) is published at `run.py:112`; `confidence` = mean(growth_conf, inflation_conf) at `run.py:105`.
- The ±1 clip is exactly `engine/indicators.py:39-45` (`score_from_z`), consumed by `axes.py:_component_scores` via `trend()`.
- `transition.state_machine` (`transition.py:85-112`) is the **memoryless flag counter** — no ratchet, no dwell.
- **`next_quad_probs` already exists** (`playbook.py:844`, published in `latest.json.playbook`) but it is a **historical Markov transition matrix keyed on the hard label** (`transition_stats`, `playbook.py:170-185`) — "given Q1, what came next historically." It is **NOT** a continuous current-state P(Quad). So the bot's wanted P(Quad) vector is a genuinely new object; the name `next_quad_probs` is taken and must not be overloaded.

---

## (1) Transition-state RATCHET — `engine/transition.py:85-112`

**Problem (incident root-cause #2).** `state_machine` is a memoryless boolean-flag counter (`n>=3→TRANSITIONING`, `n>=2→WEAKENING`, else STABLE). Flags are momentary rolling windows; when `flag_gex`/`flag_confidence_decay` windows rolled off, WEAKENING→STABLE reset "all clear" on the crash day. Verified oscillation 06-15..07-01 (5× TRANSITIONING↔STABLE↔WEAKENING).

**Design — asymmetric de-escalation dwell (escalate instantly, clear slowly), mirroring the bot's own `macro_risk` dwell machine.**

Statefulness rule:
- **Escalation is instant** (unchanged): any session may go STABLE→WEAKENING→TRANSITIONING→NEW_REGIME the moment flags/pending justify it.
- **De-escalation is ratcheted:** once in `WEAKENING` (or hotter), the state may only step *down* one level after **`clear_dwell_days` (default 5)** consecutive sessions with `n_flags < weakening_flags` **AND** the slow rotation persistence flag (below) is off. A single flag rolling off cannot restore STABLE.
- **Re-arm hard clamp:** if any transition flag *re-fires* during the dwell countdown, the countdown **resets to 0** (this is the "flags re-armed < N sessions" guard). This is what makes the 06-25 WEAKENING → 07-01 STABLE reset structurally impossible: the cyc/def rotation was still live, so the counter never completes.
- **New slow flag `flag_rotation_persistence`** (7th flag, added to `compute_flags`): the cyclical/defensive ratio (`xly_xlp`, `sphb_splv`) slope has been negative-vs-quad-stance for `>= rotation_persist_days` (default 10) — a *level* condition, not a 5-day inflection that resets. This is the flag whose absence let the memoryless counter forget the rotation. It counts toward `n_flags` AND gates de-escalation.
- **Max-dwell auto-release** (default 15 sessions) so a permanently-stuck WEAKENING self-heals — degrade-safe, never a hard lock.

**Backward-compatible field additions** (state carried in the parquet row + surfaced in `latest.json`):
```
transition_state            (unchanged enum; STABLE/WEAKENING/TRANSITIONING/NEW_REGIME)
transition_state_raw        NEW  — the memoryless read (pre-ratchet), for audit + A/B
transition_ratcheted        NEW bool — true when raw would be STABLE but dwell holds WEAKENING
transition_dwell_remaining  NEW int  — sessions of clean tape still required to de-escalate
transition_flags.flag_rotation_persistence  NEW bool
n_flags                     (now includes the 7th flag)
```
Statefulness implementation: `state_machine` currently reconstructs the whole series from flags each build, so the ratchet is deterministic from history — **no external state file needed** (walk the confirmed series forward with the dwell/re-arm logic, exactly as `apply_hysteresis` already walks quads at `regime.py:41-76`). This means it degrades-to-stateless for free: a corrupt/short history just yields the raw read.

**Schema diff (`latest.json`):** 3 new scalar fields + 1 new boolean inside the existing `transition_flags` object. Purely additive. Config additions under `engine.transition`: `clear_dwell_days: 5`, `rotation_persist_days: 10`, `max_dwell_days: 15`, tagged `(unverified-prior)`.

**Migration safety.** `transition_state` enum is unchanged, so existing consumers (incl. the bot's `regime_frame`) keep working. `transition_state_raw` preserves the old value for the golden-output diff. Ship behind `engine.transition.ratchet_enabled` (default ON after one build-diff review) so a single flag reverts to today's behavior. The parquet gains columns — `store_df.to_parquet` at `run.py:80` handles new columns transparently; downstream parquet readers must not assume a fixed column set (they don't).

**WHO builds it.** **This program (perception/dashboard side), directly.** Rationale: it is a self-contained state-machine fix with a pre-existing bot-side replay falsifier (the W1 dwell mirror). It does NOT depend on the hedgeye P(Quad) work — the ratchet operates on flags, which exist today. It is the single highest-leverage dashboard fix for the incident and should not wait on the continuous-quad program.

---

## (2) Magnitude-weighted axis votes → SUPERSEDED by the P(Quad) VECTOR CONTRACT

**Decision: do NOT patch `score_from_z` in place; spec the P(Quad) vector the hedgeye program will publish, and have this program add only the minimal *contradiction-surfacing* fix.** Rationale below.

**Why not magnitude-weight the votes directly.** The naive fix (replace `score_from_z`'s ±1/0 clip at `indicators.py:39-45` with a continuous `tanh(z/threshold)` or clipped-linear) is the right *idea* but the wrong *owner*: the hedgeye session is already building continuous P(Quad), and a magnitude-weighted axis mean IS the raw material of a soft-quad score. Doing both = two overlapping continuous-growth engines = P7 violation (two sources of truth for "how bullish is growth"). The magnitude fix should land **inside the P(Quad) engine**, not as a competing patch to `axes.py`.

**But two guards the hedgeye program MUST honor** (else magnitude-weighting reintroduces the incident):
- **Single-leg domination guard.** A continuous axis mean is dominated by one deeply-trending leg (e.g. `copper_gold` at z=3 outvoting five decelerating legs). Cap each leg's contribution at `|w_i · clip(z_i/zt, -M, +M)|` with **M=1.5** (a leg can carry at most 1.5× a threshold vote), and require **agreement ≥ 2 non-zero same-sign legs** before |score| may exceed the current ±1-democracy value. This preserves the incident lesson: no single leg flips the label.
- **Fast/slow split (the incident's actual miss).** The lagging monthlies (`payrolls_trend`, `indpro_trend`, `gdpnow_trend`, `wei_trend`) padded growth during the fast rotation (`signals.md §1 fault 2`). In the P(Quad) engine, monthlies enter at **decayed weight during transitions** (weight × 0.5 when `transition_state ∈ {WEAKENING, TRANSITIONING}`) so market legs lead when the tape is turning. And **route sticky-CPI's growth-relevant signal**: today `sticky_cpi_direction` votes only on the inflation axis (`axes.py:60`), never touching the growth/stagflation call — the P(Quad) engine should let rising-sticky-CPI-with-falling-growth pull mass toward Q3, which the current hard-label architecture cannot express (`signals.md §1 fault 3`).

**THE P(QUAD) VECTOR CONTRACT the bot wants** (published beside the hard label, new top-level key in `latest.json`; name chosen to NOT collide with `playbook.next_quad_probs`):
```jsonc
"quad_vector": {
  "schema_version": 1,
  "asof": "2026-07-01",                    // true data timestamp, not build time
  "p": { "Q1": 0.34, "Q2": 0.41, "Q3": 0.19, "Q4": 0.06 },  // sums to 1.0
  "hard_label": "Q1",                       // == latest.quad; the argmax need NOT equal it
                                            //   (divergence is itself a WEAKENING tell)
  "confidence": 0.31,                       // documented scale [0,1]; == max(p) · axis_agreement
  "drivers": {                              // signed magnitude contribution per axis leg,
    "growth":    [ {"leg":"copper_gold","contrib":+0.22},
                   {"leg":"cyclical_defensive","contrib":-0.18},
                   {"leg":"payrolls_trend","contrib":+0.05,"decayed":true} ],
    "inflation": [ {"leg":"breakeven_10y","contrib":-0.14}, ... ]
  },
  "transition_momentum": {                  // d(p)/dt — which quad is GAINING mass
    "gaining": "Q4", "gaining_rate": +0.03, // per-session Δp of the fastest-rising quad
    "losing":  "Q1", "losing_rate": -0.02
  },
  "degraded": false, "degrade_reason": null // P2: on missing legs → widen p toward uniform,
                                            //   raise degraded=true, NEVER sharpen confidence
}
```

**Why each field is load-bearing for the bot** (maps to `regime_frame` needs):
- `p` + `confidence` feed the ONE budget equation (`regime_frame.budget()`, architecture §Stage-2) as a *softer, magnitude-aware* confidence than today's mean-of-two-agreements — the incident's `conf=0.327` at `flip_margin=0.05` becomes an explicit `max(p)=0.41` with `Q4` gaining, which the budget's `F` (flip-margin damp) reads directly instead of inferring.
- `hard_label ≠ argmax(p)` is a **new contradiction plane** the bot can consume as a P1 second-opinion: when the soft vector disagrees with the sticky hard label, shrink (never flip).
- `transition_momentum` is the *forward* tell `transition_state` can't give (it's a discrete state); `gaining:"Q4"` on the incident day is exactly the "mass leaving Goldilocks" the bot needed.
- `drivers` lets the bot's judgment seat (W4) *see why*, not just *what* — feeds the three-questions duty.

**P2 degrade rule (mandatory in the contract).** Missing/stale legs must **widen `p` toward uniform and set `degraded=true`** — never concentrate mass. A data outage must lower `confidence`, exactly the inverse of the old `missing-stockdata → confluence=1.0` failure. Freshness-gate on `asof` (>3–5 trading days stale → the whole vector goes `degraded`, argmax pinned to last-good).

**WHO builds it.** **HANDOFF to the hedgeye P(Quad) session**, with this contract as the interface spec. Rationale: they own continuous-quad; publishing `quad_vector` is their deliverable, and duplicating it here violates P7 and the "COMPLEMENT don't duplicate" instruction. **This program contributes only:** (a) the two guards above as ship-blockers on their design, and (b) a **small immediate fix** independent of them — surface the existing `contradicting` list (`run.py:111`) into `transition.state_machine` so that a `{growth_cyclical_defensive, wei_trend}` growth-contradiction pair forces at least WEAKENING regardless of the flag windows (`signals.md §4 item 3`). That fix is cheap, ships with item (1)'s ratchet PR, and hedges the label lag until `quad_vector` lands. Until `quad_vector` exists, the bot's `regime_frame` degrades to today's mean-confidence — no blocker.

---

## (3) Liquidity-quality on the label plane — publish W-I classifier fields in `latest.json`

**Problem (incident root-cause #3).** `liquidity_overlay` (`regime.py:100-113`) is a pure quantity-RoC with no quality dimension. The 07-01 "expanding" was one-day base-effect noise (net-liq flat ~$5,858bn for 5 sessions) composed of TGA drawdown against an RRP drained to $6.4bn — mechanical/stress, not benign Fed easing. **Every consumer of `latest.json` (not just Mastermind)** currently sees a bare `"liquidity_overlay": "expanding"`.

**Design.** The W-I liquidity-quality classifier is landing **on the bot side today** (`brain/regime_frame.budget()` consumer). The charter's P7 says one source of truth per concept — so the *classification logic* should live at the source (`engine/regime.py`), and the bot reads the label rather than recomputing from vendored FRED. Spec: add `liquidity_quality()` to `engine/regime.py` beside `liquidity_overlay()`, reading series already on disk (`data/fred`, `data/nyfed`, `data/treasury`):

```jsonc
"liquidity_overlay": "expanding",           // UNCHANGED — existing consumers keep working
"liquidity_quality": {                       // NEW top-level key
  "schema_version": 1,
  "asof": "2026-07-01",
  "label": "stress-expansion",               // benign-expansion | neutral | stress-expansion
                                             //   | contracting  (label ≠ overlay is the signal)
  "quantity_roc_bn": 68.9,                   // existing 20d RoC of net_liquidity_bn
  "rrp_buffer_bn": 6.4, "rrp_exhausted": true,   // RRPONTSYD < ~$100bn floor
  "composition": {                           // decompose the RoC swing
    "d_walcl": +31.3, "d_neg_rrp": -5.1, "d_neg_tga": -14.6,
    "mechanical": true                       // move driven by TGA/RRP, not Fed balance-sheet
  },
  "stress_overlay": {                        // credit/funding co-check
    "hy_oas_pct": 2.78, "hy_oas_chg_20d": +0.04, "hy_oas_z": -0.54,
    "nfci": -0.516, "nfci_trend": "loose",
    "confirming_stress": false               // credit not yet confirming; RRP+mechanical carry it
  },
  "walcl_stale_days": 5,                      // ffill staleness surfaced (was masking the flat)
  "degraded": false
}
```
Classification rule (from `signals.md §2`): `expanding` reclassifies to **`stress-expansion`** iff (`rrp_exhausted`) OR (`composition.mechanical`) OR (`confirming_stress`); else `benign-expansion`. Today's honest label: **"neutral, hollow"** → `stress-expansion`, not `expanding`.

**Migration safety.** Additive key; `liquidity_overlay` string untouched so no consumer breaks. The classifier is `try/except → None` wrapped exactly like the other `latest[...]` leaves at `run.py:124-199` (never fatal). Ship behind `engine.liquidity.quality_enabled` (default ON). The bot's `regime_frame.budget()` consumes `label` as a **shrink-only multiplier** (P2): `stress-expansion` shrinks the offensive budget; it may never un-cap.

**WHO builds it.** **This program (dashboard side), directly** — but coordinate with the W-I session so the classification thresholds are *identical* on both sides (one definition, P7). Rationale: the series live in the dashboard's `data/fred|nyfed|treasury`; computing at the source means all ~200 dashboard products and every book see the same "stress-expansion," not just Mastermind. The W-I bot classifier becomes a fail-closed fallback (reads the published label; recomputes only if the field is absent), matching the "freshness gates + fail-closed instead of un-vendoring" masterplan decision.

---

## (4) "Risk receding" badge ↔ risk_radar caution reconciliation — one risk voice per page

**Problem (incident root-cause #10 / `signals.md §4 item 4`).** The de-escalation panel's green "risk receding / pullback-odds receding" badge read the OLD June geopolitical scare fading, while the radar's own `state=caution: "Growth scare / defensive rotation (91/100) × Credit stress"` was live from 06-26 with `drawdown_prob.h21` **rising** 0.16→0.19 (lift 0.9→1.07×). Two risk voices, opposite signs, same page. Also `cap_leadership: False` at caution.

**Design — the badge becomes a *derivative* of the radar's dominant scare, not an independent panel.** One risk voice = the radar's `snapshot()` (`engine/risk_radar.py`, published at `run.py:436`); the de-escalation badge may only render "receding" when the radar itself is receding on the *currently-dominant* scare.

Reconciliation contract (published in `latest.json.risk_radar`, additive fields):
```jsonc
"risk_radar": {
  ... existing snapshot ...,
  "dominant_scare": "growth_defensive_rotation",   // NEW — the scare currently driving state
  "deescalation": {                                 // NEW — replaces the standalone panel's verdict
    "eligible": false,                              // receding badge may render ONLY if true
    "reason": "dominant scare = growth/defensive rotation; h21 drawdown_prob RISING",
    "receding_scare": "geopolitical",               // what IS fading (may differ from dominant)
    "drawdown_prob_h21": 0.19, "drawdown_prob_trend": "rising"
  },
  "cap_leadership": true                            // THRESHOLD REVIEW (below)
}
```
Gating rule: **the "receding" badge is suppressed whenever `dominant_scare` is risk-off-flavored AND `drawdown_prob_trend == "rising"`.** The panel may still say "the June geopolitical scare has faded" as *context*, but it cannot present an all-clear while a newer, hotter scare is escalating. This is the P1/P7 fix: the page has one risk verdict (`deescalation.eligible`), derived from the radar, not two.

**Threshold review (flagged, not auto-decided):** the incident argues `cap_leadership` should be **True at `caution`** when the dominant scare is a growth/defensive rotation with rising drawdown odds (`synthesis.md §4 item 6`). Spec this as a config knob `engine.risk_radar.cap_leadership_on_rotation_caution: true` with the incident as the falsifier — but leave the *default* to the risk_radar owner's judgment; do not silently change leadership-capping behavior firm-wide in this PR.

**Migration safety.** All additive to the existing `risk_radar` snapshot dict; the panel's front-end reads `deescalation.eligible` instead of computing its own verdict (one-line FE change on the dashboard; the bot reads `dominant_scare` + `drawdown_prob_trend` as a divergence plane). Degrades: if `risk_radar` is `None` (already possible, `run.py:448`), the badge falls back to today's standalone behavior — no regression.

**WHO builds it.** **This program (dashboard side), directly** — it is a dashboard-internal consistency fix (radar ↔ de-escalation panel), the exact "one risk voice per page" mandate. The bot benefit is a bonus consumption plane; it does not need the hedgeye work. Coordinate only with whoever owns the de-escalation panel FE (memory: `risk-radar-deescalation-panel`).

---

## (5) Rotation-tensor rendering surface — where it lives

**What "rotation tensor" is** (the user's mandate: "sector rotations and exactly how they are rotating and by how much"). The raw material already exists as **three separate published engines** that are today *not composed into one tensor*:
- `subsector_rotation.py` → RRG quadrants: per-node `rs_ratio` (leadership level) × `rs_mom` (momentum), `quadrant ∈ {Leading, Weakening, Lagging, Improving}`, `accel`, 268 subsectors × 40 themes (`site/data/subsector_scan/`). This is the RRG plane.
- `group_flow.py` → `flow_score`, `accel_z`, `breadth`, `absorption` (how one-factor the rotation is — the "late/narrow vs broad" tell), HHI concentration.
- `sector_cycles` (`data/sectordata/sector_cycles.json`) → per-sector cycle phase/pos/osc_slope (already consumed by the bot's `regime_frame.cycles()`).

**Design — publish a single composed `rotation_tensor.json` contract** (new dashboard product) that fuses the three into the "from → to, by how much" object the bot and the dashboard both want. This is the P7 "one source of truth for rotation" surface:
```jsonc
// data/rotation_tensor/latest.json  (mirrored to site/data/rotation_tensor/)
{
  "schema_version": 1, "asof": "...", "generated_utc": "...",
  "nodes": [                          // one per sector/subsector (RRG coordinates + cycle)
    { "key":"semis", "rs_ratio":-0.31, "rs_mom":-0.44, "quadrant":"Weakening",
      "accel":-0.12, "cycle_phase":"late", "flow_score":-0.6, "breadth":0.38 }
  ],
  "flows": [                          // the TENSOR: directed capital rotation edges
    { "from":"semis", "to":"healthcare", "magnitude":0.62,   // |Δ leadership| normalized
      "rs_spread":0.17, "confidence":0.7, "persistence_days":6 },
    { "from":"tech", "to":"utilities",  "magnitude":0.41, ... }
  ],
  "regime_context": {                 // ties the tensor to the label plane
    "offense_vs_defense_rs_20d": +0.093,  // the incident's defensive-RS differential
    "crossover_date": "2026-06-24", "persistence_days": 6,
    "breadth_narrowing": true, "absorption": 0.71   // one-factor rotation = late/fragile
  },
  "highlights": { "leaders":[...], "fading":[...], "emerging":[...], "laggards":[...] }
}
```
The **`flows` array is the tensor** — directed `from→to` edges with a `magnitude` ("by how much"), which no single existing engine emits. It is computed from pairwise `rs_ratio` deltas + `flow_score` sign, gated by `persistence_days` (so a 1-day flicker isn't an edge — same anti-whipsaw discipline as item 1's rotation-persistence flag). `regime_context.offense_vs_defense_rs_20d` is precisely the shrink-only nowcast leg the bot needs (`synthesis.md §3.4`).

**Rendering surface — two placements:**
1. **Dashboard:** a new **"Rotation" tab/section on `subsector_rotation.html`** (it already renders the RRG scatter). Add (a) the RRG scatter colored by cycle phase, and (b) a **Sankey/chord "flow" diagram** driven by `flows[]` — "$ leaving semis → healthcare/utilities, magnitude bars." This is the user's "exactly how they are rotating and by how much" made visual. Lives beside the existing RRG, not a new page (avoids nav sprawl per memory `nav-chrome-architecture`).
2. **Bot:** `regime_frame` consumes `rotation_tensor.json` as a **new perception plane** — `regime_context.offense_vs_defense_rs_20d` + `crossover_date` feed the W-I **rotation-evidence budget term** (`synthesis.md §3.5`) and DEF_SLEEVE's fragility signal, so the defensive sleeve isn't throttled solely by the corruptible risk-state dwell. Shrink-only (P2): rotation-into-defensives raises the DEF_SLEEVE floor, never adds offensive gross.

**Migration safety.** Entirely new artifact (`rotation_tensor/latest.json`) — zero existing-consumer risk. Assembled from already-published engines (cheap reads, no new LLM calls, per the cost mandate). Freshness-stamped `asof`; degrades to `nodes`-only (drop `flows`) if `persistence` history is too short. Bot degrades to today's behavior (no rotation-evidence term) if the file is absent.

**WHO builds it.** **This program (perception side), directly** — it is the literal "eyes" deliverable of the mandate and composes existing dashboard engines that this program owns. Not a hedgeye item (it's rotation/flow, not quad-probability). The FE Sankey is a dashboard-render task; coordinate the compute contract with the bot's W-I rotation-evidence-budget owner so `offense_vs_defense_rs_20d` is defined once.

---

## Build-ownership summary

| # | Item | Owner | Rationale |
|---|---|---|---|
| 1 | Transition ratchet + rotation-persistence flag | **This program (dashboard)** | Self-contained state-machine fix; bot replay falsifier exists; no P(Quad) dependency; highest-leverage incident fix |
| 2 | P(Quad) vector contract `quad_vector` | **HANDOFF → hedgeye session** (this program: 2 guards + immediate contradiction-surfacing fix) | They own continuous-quad; P7 forbids a competing magnitude engine here |
| 3 | `liquidity_quality` published field | **This program (dashboard)**, threshold-synced with W-I | Source-side classification → all ~200 products + all books see it, not just Mastermind |
| 4 | Radar ↔ de-escalation reconciliation | **This program (dashboard)** | Dashboard-internal one-risk-voice consistency fix |
| 5 | `rotation_tensor.json` + Rotation surface | **This program (perception)** | The literal "eyes" mandate; composes engines this program owns |

**Cross-cutting contract hygiene** (charter wishlist §6, applies to all five): every new object carries `schema_version`, a true-data `asof` (not build time), and a `degraded`/`degrade_reason` pair; all shrink-only on the bot side (P2); each is one canonical source (P7). None block the bot — every consumer degrades to today's `regime_frame` behavior when a field is absent (masterplan §4 guardrail 11).

**Primary-source touchpoints:** `engine/regime.py:100-113` (liquidity), `:137-164` (classify/confidence), `engine/transition.py:20-112` (flags + state machine), `engine/axes.py:19-97` + `engine/indicators.py:39-45` (the ±1 clip), `engine/run.py:68-119` (latest.json assembly), `:436` (risk_radar publish), `engine/playbook.py:170-185`+`:844` (the existing `next_quad_probs` Markov matrix — do not overload), `engine/subsector_rotation.py:198-214` + `engine/group_flow.py` (rotation-tensor inputs), `config.yml` `engine.{transition,quad,liquidity,scoring}`.
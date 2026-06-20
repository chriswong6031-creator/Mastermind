# Operating doctrine — thematic rotation & leadership participation

Adapted from an externally-authored framework into the narrator-bot's autonomous,
paper-only context. The original was written for a **human operator with discretion**
whose documented failure it critiques (≈100% in a high-conviction, inversely-correlated
crypto/COIN/BTC position during an AI/memory/semis leadership regime, with no dry powder
to rotate). Our system has **no discretion** — so the doctrine is split across two
consumers of one shared engine layer:

- **SELF mode** — the doctrine is the Claude brain's operating system for its **own paper
  book**: hard sizing vetoes, mechanical sleeves, time-stops on its own lots.
- **OPERATOR mode** *(optional)* — the same Stage / scorecard / detector fields run
  **read-only** over the user's real holdings (`WATCHLIST.md` → basket membership) and emit
  blunt advisories into the alert feed. Never an order.

> The asymmetric law: every market-wide engine stays `directional:false`. The doctrine's
> action verbs (TRIM / EXIT / full-size) apply only as **down-size / rotate on a *held*
> book**, never as a market short or a forward forecast. We do not predict ignition.

## Axioms (priors, not slogans)

- **A1 Confirmation over prediction.** We cannot time ignition; we can detect a theme that
  has already turned and early-follow with most of the move left. Bias toward confirmation.
- **A2 Return accrues to the binding constraint.** Within a theme, excess return concentrates
  in whatever input is scarce, non-substitutable, and slow to expand. Always name it.
- **A3 Conviction ≠ timing.** Being right early ≈ being wrong, in opportunity-cost terms.
  Capital in a correct-but-early thesis carries negative rent. → the time stop.
- **A4 Correlation-structure breaking is the earliest honest signal.** Old leadership going
  dead to good news while a new cohort makes highs into a choppy index fires before fundamentals.
- **A5 Sizing & rotation capacity dominate selection.** → subtract-only ½-Kelly + sized cash.
- **A6 Cash is a position** — the option premium that funds rotation; positive EV in rotations.
- **A7 Calibration is doctrine.** Probabilities, not certainties. Tag observed vs inferred
  `(unverified)`. This **==** our existing observed-vs-inferred / `directional:false` discipline,
  and every confluence score is fed to the Brier ledger as a falsifiable probability — never
  masqueraded as forward alpha.

## Three sleeves + firebreaks (portfolio architecture)

Guarantees **presence** in the current leader; does not claim to predict it.

| Sleeve | Engine | Behavior | Budget |
|---|---|---|---|
| **Leadership** | `narrative_rotation.allocate()` **as-is** | mechanical, equal-weight (rank-IC≈0), 200d-trend gated, monthly rebalance, **no narrative** | 40–60% |
| **Conviction** | `top_picks` + `stock_score` ½-Kelly + `thematic_desk` falsifiable lean | capped, falsifiable, paper | 20–40%, ≤25%/theme |
| **Cash** | `glide_path` + rotation floor | sized, never residual | 5–20% |

Firebreaks (the rules that would have prevented the failure):

- **Book theme cap 0.25, enforced cross-sleeve** — sum Leadership + Conviction exposure to the
  same theme. This is the NEW glue; neither engine does it. (Note three altitudes that must not
  be conflated: `narrative_rotation.POS_CAP=0.30` is per-theme *of the leadership sleeve*; our
  prior `theme_cap` was book-level; the doctrine's firebreak is **book-level cross-sleeve**.)
- **Single-name cap 0.08** — we keep our tighter value over the doctrine's 10–15%; under
  subtract-only, tighter always wins. Enforced at the conviction/`stock_score` layer.
- **Cash is sized, orthogonal to market risk.** The operator's failure was 0% cash in a *benign*
  tape (low `macro_risk` → our `(1−macro_risk)·1.4` gross alone would have allowed near-full).
  Binding cash = `max(macro-implied, rotation-floor)`; raise `no_rotation_capacity` when cash≈0
  and the book is concentrated.
- **Leadership sleeve stays equal-weight, not Kelly** (validated rank-IC≈0). Do not
  cross-contaminate sleeves.

## Stage 0–4 lifecycle (locate first; action depends on stage)

Fuses existing fields (`group_flow._stage`, `theme_scoring._label`, crowding/extension) into the
doctrine's five stages. Our four labels are coarser → split by crowding/breadth.

| Stage | Tell (real fields) | Action |
|---|---|---|
| 0 latent | `stage='quiet'` + `label='neutral'` + breadth<50% + flat rel-perf | watch, do not buy |
| 1 ignition | `stage='emerging'`/`label='emerging'` + `rs_pctile<0.65` + clean-entry | early-follower, initial size |
| 2 recognition | cohort breadth lifts + **binding catalyst prints** + accumulation | core, **full size** |
| 3 maturation | 4th-derivative junk runs + stretched + crowded | trim, tighten, stop adding |
| 4 distribution | leaders fail highs on good news + RS rolls + flow reverses | exit, rotate |

`fourth_deriv_tell` (Stage 3→4) = `theme_extension.pct_parabolic ≥ 0.30` AND `rs_pctile ≥ 0.8` —
the single most important late-cycle discriminator; a **reduce** signal, never a chase.

## Confirmation scorecard (confluence gate = meta-label)

Six dimensions; no single signal acts. Assembled from existing engines (≈70% already live):

1. **RS / leadership** — `sectors.rs_table` + `theme_scoring._trend_leg`/`rs_pctile`. *(have)*
   - **1b "holds up on down days"** — down-day-conditional member alpha. *(NEW, highest-value)*
2. **Breadth** — `theme_scoring._breadth_leg` (%>50d, %>200d, net NH−L) + `advanced_breadth`. *(have)*
3. **Institutional flow** — `holdings_signals.active_changes_dir` (watch the *change*) rolled up
   per-theme + a flow-divergence flag. *(partial → rollup)*
4. **Volume signature** — up-day vs down-day volume / OBV (Wyckoff). *(NEW — biggest gap)*
5. **Binding catalyst** — theme → key-ticker capex/contract/beat-and-guide-up. **The gate for
   full size.** *(NEW)*
6. **Cross-asset / regime fit** — `theme_scoring._macro_leg` + `cross_asset.absorption`. *(have)*

Gate: **dims 1+2 → initial** (small); **dim 5 + most of 1–4 → full**; one dim alone is forbidden.
The decision object must enumerate which dims are confirmed / absent / `(unverified)`.

## Bottleneck migration (A2 — second/third-order)

The constraint *moves*. Your baskets already encode the AI→electricity chain as real ids:

```
1st order (compute)   : ai_semiconductors
2nd order (enablers)  : memory_storage, semicap_equipment
3rd order (physical)  : data_center_power, grid_electrification, nuclear_power,
                         copper_steel_electrify, rare_earth_critical_min   ← CAT/FCX/EMCOR/FIX live here
4th order (adjacency) : SELL tell, not a buy
```

`order_layer ∈ {1,2,3,4}` becomes a **display-only** per-basket tag (never a scored axis —
thematic momentum rank-IC≈0). `constraint_migration()` diffs the RS rank of order-1/2/3 baskets
to detect the baton-pass. **RULE 5.1**: the brain names the current + next binding constraint
before any single name; the field is brain-authored but anchored to which order-layer basket is
*actually* leading on RS, so a hallucinated constraint is caught. De-overlap becomes order-aware
(≤1 per `(parent, order_layer)`) so the book can hold chips AND power at the right stage.

## Exit discipline — three stops per lot

1. **Price / technical stop** — bind held lot ↔ `ticker_alerts.technical_events`.
2. **Thesis-invalidation stop** — **this == our engine-derived falsifier** (`_derive_check`),
   widened with engine-state kinds (`cycle_blocked`, theme→deteriorating, RS<10th pctile). All
   ledgered + Brier-scorable; no LLM-graded falsifiers.
3. **Time stop** *(NEW — the part that failed)* — `window_td` set at entry; fires when elapsed ≥
   window AND the thesis is unresolved AND `RS_held < RS_leader` and widening. It does not say the
   call is wrong; it says the capital is dead *now* and must be redeployed (the reflexivity
   corollary). In OPERATOR mode the entry is unknown → degrade to "lagged the leader N weeks",
   tagged `(unverified)`.

- **6.1** Never average down into a position diverging from a rotating market.
- **6.2** Position sizing is the master variable. Size to survive being early.

## Failure-mode detectors (D1–D6) — two modes off one engine

| # | Detector | SELF (hard veto on bot's own sizing) | OPERATOR (read-only advisory) |
|---|---|---|---|
| D1 | thesis-defense / disposition | brain may not add to a name on confirming-news-only | flag operator message tone |
| D2 | late-stage reach (4th-deriv) | block buying `order_layer=4` adjacency | flag the reach, argue trim |
| D3 | no rotation capacity | block new gross when cash≈0 & concentrated | standing risk flag |
| D4 | avg-down into divergence | block adds to a lot diverging from a rotating leader | flag the proposed add |
| D5 | right-but-early / dead capital | = the time-stop surface | "lagged leader N weeks" |
| D6 | single-theme cap breach | clamp at book 0.25 | flag the breach |

Delivered bluntly, no moralizing — the operator chose a check on their own psychology over a
yes-man.

## Approach from all sides — the multi-lens decision matrix

A real decision triangulates every side, not just narrative/RS. `portfolio/lenses.py` assembles
a **decision matrix** per name/theme from the live engine — each lens a row with its read and an
honest status:

| Side | Lens(es) | Status |
|---|---|---|
| Fundamental | valuation (value_z, cheap, fwd P/E), quality (accounting), growth (rev/eps CAGR), solvency (Altman/Piotroski) | context/partial |
| Narrative | thematic leadership/stage, conviction band, within-basket leader | partial |
| Potential | upside/downside asymmetry (cone mfe vs dd) | partial |
| Risk | **drawdown cone (dd_tail)**, **extension veto (parabolic)**, crowding, macro stress | **validated** + context |
| Policy | administration tilt (targeted/starved), Fed path | context |
| Flows | 13F smart money, active-ETF accumulation, per-theme flow divergence | context |
| Positioning | options gamma/walls/expected move | context |
| Macro fit | rate/inflation sensitivity, cross-asset absorption | context |

**Three rules — never a weighted blend** (blended thematic momentum is rank-IC≈0; averaging 24
lenses would be the worst overfit here):
1. **Matrix** — show every lens with its tag; never average them. Validated lenses hold authority;
   context lenses inform conviction but can't drive size alone.
2. **Confluence gates size** — how many honest lenses agree, subject to the hard vetoes (parabolic /
   Altman distress / cycle-blocked → size 0 regardless of how bullish the rest).
3. **Divergence is the edge or the trap** — `distribution` (hot + expensive + 13F-exiting → avoid),
   `early_edge` (cheap + flows-in + quiet → asymmetry), `high_confluence_buy` (all sides align + vetoes
   pass), `crowded_top` (extended + flows rolling), `policy_early` (policy tailwind + cheap before the
   crowd). Name which side leads.

Armed Claude calls `get_decision_matrix` / `get_divergences` and must address every lens before a verdict.

## Operating protocol (a theme query)

1. Locate the **stage** (§ lifecycle). 2. Run the **scorecard** (enumerate, tag `(unverified)`).
3. Identify the **bottleneck** (current + next; map order layers). 4. Check **architecture**
(cash/room, cap breaches, sleeve placement). 5. Run **detectors**. 6. Return a **sized** rec with
all three exit stops defined at entry + the data that would upgrade/invalidate it. Register: blunt,
probabilities not certainties, `(unverified)` tags, psychology-and-incentives framing.

## What is genuinely new vs already-owned

- **Already in the engine (~60%):** RS/breadth/macro legs, crowding/extension, the 4-state
  lifecycle, `narrative_rotation` mechanical allocator, ½-Kelly subtract-only sizer, the
  `_derive_check` falsifiable ledgers, the order-layer basket decomposition.
- **Genuinely new:** the time stop, the 3-sleeve frame + cross-sleeve book cap + rotation-floor
  cash, the explicit confluence gate (catalyst = full-size gate), the Stage 0–4 classifier, the
  `order_layer` field + `constraint_migration()`, the D1–D6 detector suite, and two new compute
  legs (down-day RS, volume signature) that belong in the macro engine as additive `directional:false`
  leaves (a separate macro-repo PR; the bot consumes them).

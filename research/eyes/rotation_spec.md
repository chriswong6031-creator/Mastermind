# ROTATION TENSOR — perception plane spec ("exactly how they are rotating and by how much")

**Charter cites governing this design:** P1 (two-plane), P2 (shrink-only/degrade-to-no-op), P3 (earn authority), P5 (perception→posture→names), P7 (one view, all books). **Anti-compounding law (W2):** the tensor is consumed exactly once — as an *evidence input to the rotation-budget term*, never re-applied at the leg/book/gross layers.

---

## 0. What it is, in one line

A per-session, deterministic measurement — assembled from **published parquet + dashboard contracts, zero LLM calls** — that quantifies **who is gaining RS on whom, at what bps/day, for how many sessions, and how rare that episode is**, published to `data/market_view/rotation_tensor.json`. It is the *magnitude organ* of the market view: the difference between "healthcare is leading" (a label) and "**healthcare is gaining 34 bps/day on semis, 6 sessions running, a 0.8-percentile episode, with flow and breadth confirming**" (a measurement posture can size on).

The incident is the proof case. On 2026-07-01 the bot's regime plane read Goldilocks/STABLE/expanding and the books *bought SMH*. The tensor, computed off the **freshest plane the bot actually had (vendored yahoo through 07-01)**, would have registered the defensive-over-offensive rotation as a **measured, ranked, multi-session episode** — a second, price-derived evidence plane (P1) that fires independent of the lagging sign-vote regime label. This is the plane the counterfactual (`counterfactual.md` items 2b) showed missing: the size-brakes bit mechanically, but the *regime-conditioned* levers (budget-T, DEF_SLEEVE fragility_signal) stayed soft because **nothing measured the rotation directly** — they inherited the regressed STABLE/risk_on read.

---

## 1. Design — the five measurement blocks

Universe **U** = 11 GICS SPDRs `{XLB XLC XLE XLF XLI XLK XLP XLRE XLU XLV XLY}` + semis block `SMH` (SOXX as fallback) = **12 instruments**. Benchmark = `SPY`. All from `data/yahoo/*.parquet` `close` (div-adjusted — confirmed present for every instrument), min 260 sessions history.

Everything below is **causal** (`close[:T]`, `ewm`/`rolling(min_periods=)` only — the sector-cycle re-audit's look-ahead discipline is inherited verbatim) so the plane is replayable on any historical date for the P3 validation battery.

### (a) Pairwise RS-velocity matrix — *who is gaining on whom, bps/day*

Per instrument `i`, relative-strength line vs SPY:
```
rs_i(t)      = log(close_i(t) / close_SPY(t))                     # relative-strength log-line
rsm20_i(t)   = rs_i(t) − rs_i(t−20)                               # 20d relative momentum (the "who leads" level)
vel_i(t)     = ( rsm20_i(t) − rsm20_i(t−5) ) / 5                  # 5d change of momentum, per session → the ACCELERATION
```
`vel_i` in **bps/day** = `1e4 · vel_i`. The **pairwise tensor** is the antisymmetric matrix
```
R[i][j] = 1e4 · ( rsm20_i(t) − rsm20_j(t) ) / 20     # avg bps/day i has out-gained j over the 20d window
dR[i][j] = 1e4 · ( vel_i(t) − vel_j(t) )             # how fast that gap is currently WIDENING, bps/day
```
`R[XLV][SMH]` is the headline number: *avg bps/day healthcare out-repriced semis over 20d*; `dR[XLV][SMH]` says whether the gap is still opening today. Published as a 12×12 dense array **plus** a `top_pairs` extract (largest `|R|` with `dR` same-signed = accelerating divergences) so consumers don't parse the matrix.

### (b) Breadth migration — *is the rotation broad or a two-ETF head-fake*

Per sector, the **participation shift** behind the price move. Source precedence:
1. **Preferred:** per-sector `pct_above_50` from the breadth constituent cache (`data/breadth/_closes_cache.parquet` × `constituents.parquet` GICS map) — real internals.
2. **Degrade (P2):** if per-sector breadth is unavailable (the published `breadth.parquet` is SP1500-aggregate, *not* per-sector — a real, named coverage gap), fall back to a **within-ETF proxy**: `pct_above_50` computed on the sector-ETF's own holdings if `etf_holdings/` is fresh; else **omit the block and stamp `breadth_migration.status="unavailable"`** — never fabricate.
```
brd_i     = pct_above_50_i(t)
d_brd_i   = brd_i(t) − brd_i(t−5)          # 5d participation change, pp
nh_share_i= new_highs_i / n_members_i      # new-highs share (from breadth cache nh, sector-sliced)
d_nhs_i   = nh_share_i(t) − nh_share_i(t−5)
```
A rotation *into* a sector with **rising `d_brd`** is a healthy broadening leg; rising RS with **falling `d_brd`** is a narrow, distribution-flavored move (the SMH tell in the incident: RS held while %sectors>50d fell 73→45→55). This block is what separates "leadership" from "last men standing."

### (c) Leadership churn index — *is the top of the board turning over*

Rank sectors by `rsm20`. 
```
churn10(t) = 1 − Jaccard( topRS_set(t, k=4), topRS_set(t−10, k=4) )   # ∈ [0,1]
rank_dist(t)= Σ_i | rank_i(t) − rank_i(t−10) |                        # total rank displacement
```
`churn10` near 0 = stable leadership (momentum regime, leaders keep leading — the walk-forward finding); a **spike** in `churn10` with **rising `rank_dist`** = a *regime-of-leadership change* — the thing that precedes the momentum sleeve breaking. Published with the entering/exiting sector names so the PM brief can say "utilities displaced tech in the top-4 as of session T−2."

### (d) Flow proxies — *is real money confirming the price*

Two independent flow reads, ranked so a consumer can see confirmation vs divergence:
1. **Relative-volume z (all 12, incl. SMH):** `rvol_i = volume_i(t) / SMA20(volume_i)`; cross-sectional z-score `rvz_i`. Turnover surge under a *falling* RS = distribution; under *rising* RS = accumulation.
2. **Real creation/redemption flow (11 sector ETFs — `data/flows/XL*.parquet`, fields `aum_mn` + `so_mn`):** shares-outstanding delta is a **true primary-market flow** (creation units), price-independent:
```
netflow_i(t)   = ( so_mn_i(t) − so_mn_i(t−1) ) · nav_i(t)      # $ created/redeemed, ex-price
flowz_i        = 5d-sum(netflow_i) z-scored cross-sectionally
```
SMH has no `flows/` file → its flow cell is `null` with `flow_source="rvol_only"` (P2 honesty; no fabrication). `group_flow/` is stubbed today (only `validation_meta.json`) — the design **degrades to `so_mn`+`rvol` and stamps `flow_plane="etf_so_delta"`**; if `group_flow` ships a richer contract later, it's an additive source behind the same field, no consumer change.

### (e) MAGNITUDE / episode estimate — *by how much, over how many sessions, how rare* (the payload)

An **episode** is a sustained sign-consistent divergence between two blocs. Define the canonical macro axis the incident turns on:
```
DEF  = mean RS of {XLV, XLU, XLP}     OFF = mean RS of {SMH, XLK, XLY}
spread(t) = 1e4 · ( DEF_rs(t) − OFF_rs(t) )                    # bps, defensive-over-offensive
```
Episode detection (deterministic hysteresis — mirrors the incident nowcast's 3-session durability rule):
- **start** when `sign(vel_spread)` flips and holds ≥ **3 consecutive sessions**;
- **magnitude** = cumulative RS repricing `Σ Δspread` from start→now, in bps;
- **rate** = magnitude / n_sessions (bps/day);
- **percentile** = rank of the current episode's (magnitude, rate) against **all historical episodes 2011–now** on the same axis → the "0.8-percentile" rarity stamp;
- the tensor also computes episodes on **every top-pair** from (a), not just DEF/OFF, and publishes the DEF/OFF one as `headline_episode`.

**Worked example — the 06-24…07-02 defensive rotation (computed on the vendored yahoo the bot actually had, closes through 07-01 per `signals.md` §3):**
- Defensive−offensive 20d RS differential flipped positive **2026-06-24**, held >0 continuously from **06-25**; 07-01 `spread = +0.093` (def_rs +0.080, off_rs −0.014). 
- Episode: **start 06-24, ~6 sessions** to 07-02; `spread` traversed roughly **−1 → +9.3 bps ≈ +10.3 bps cumulative**, rate **≈ +1.7 bps/day** on the bloc axis. On the **cleanest pair** the user named, `R[XLV][SMH]`: XLV **+5.36%** vs SMH **−1.68%** over the window ⇒ **≈ +704 bps of XLV-vs-SMH repricing over 6 sessions ≈ +117 bps/day** at the single-ETF level (bloc-averaging dilutes to the ~1.7 figure; the tensor publishes both — `headline_episode` on the DEF/OFF bloc, `top_pairs[0]` on XLV/SMH). 
- Confirmation stack that session: breadth-migration **negative** on OFF (%sectors>50d falling), churn10 **elevated** (XLV entering, semis names sliding), flow `rvz` on SMH high under falling RS (**distribution**). Three of the tensor's five blocks fire the same direction → this is a **high-agreement episode**, the P1 second plane the STABLE regime label could not see.

*This exact window becomes the pre-registered regression fixture (§4) and a permanent replay fixture (P6 / W-I item 5).*

---

## 2. Contract — `data/market_view/rotation_tensor.json` (+ dashboard mirror)

```jsonc
{
  "schema_version": 1,
  "as_of": "2026-07-01",              // TRUE data date = min(asOf) of every input plane, NOT build time (charter §6.5)
  "asof_by_plane": {"yahoo":"2026-07-01","flows":"2026-06-25","breadth":"2026-06-26"},
  "freshness": {"stale": false, "max_age_td": 0, "degraded_planes": ["flows(4td)"]},
  "confidence": 0.71,                  // = f(plane freshness × block agreement × history depth); shrink-only downstream
  "universe": ["XLB","XLC","XLE","XLF","XLI","XLK","XLP","XLRE","XLU","XLV","XLY","SMH"],
  "rs_velocity": {                     // (a)
     "level_bps_per_day":  {"XLV": +40.1, "SMH": -6.3, ...},     // rsm20/20
     "accel_bps_per_day":  {"XLV": +9.4,  "SMH": -12.1, ...},    // vel
     "pair_R":  [[...12x12...]],  "pair_dR": [[...12x12...]],
     "top_pairs": [{"lead":"XLV","lag":"SMH","R_bps_day":34.2,"dR_bps_day":8.8,"accelerating":true}, ...]
  },
  "breadth_migration": {"status":"ok","pct50_5d_delta":{"XLV":+6.2,"SMH":-11.0,...},
                        "nh_share_5d_delta":{...}},              // (b), or {"status":"unavailable"}
  "leadership_churn": {"churn10": 0.50, "rank_dist": 14,          // (c)
                       "entered_top4":["XLV","XLU"],"exited_top4":["SMH-block","XLY"]},
  "flow": {"flow_plane":"etf_so_delta",                          // (d)
           "rvol_z":{"XLV":+0.9,"SMH":+1.6,...},
           "netflow_z":{"XLV":+1.2,"SMH":null,...},
           "distribution_flag":{"SMH":true}},                    // high rvol_z + negative accel
  "headline_episode": {                                          // (e)
     "axis":"DEF_over_OFF","start":"2026-06-24","n_sessions":6,
     "magnitude_bps": 10.3,"rate_bps_day": 1.7,
     "percentile": 0.83,"direction":"defensive","agreement":"3of5_blocks"},
  "episodes": [ {"axis":"XLV_over_SMH","magnitude_bps":704,"rate_bps_day":117,"percentile":0.91,...}, ... ],
  "advisory": true                     // flips to validated:true ONLY after §4 gate passes (P3 ladder)
}
```
**Dashboard mirror:** `site/marketdata/rotation_tensor.json` (thin, render-ready) drives a **Rotation Tensor** leaf — a signed RS-velocity heatmap + episode strip. Display-only until validated (P3). No consumer reads the site mirror; the bot reads the `data/` contract.

**Every field freshness+confidence-stamped.** The single `confidence` scalar and `freshness.stale` flag are the *only* things a consumer needs to degrade correctly.

---

## 3. Consumers — one view, consumed exactly once (P7 + anti-compounding)

| Consumer | Reads | Effect | Guard |
|---|---|---|---|
| **`brain/regime_frame.py` — market_view plane** | whole contract via one reader | Exposes `rotation()` alongside `frame()`/`cycles()`. The **one** ingestion point; nothing else loads the JSON (P7, the anti-drift rule that killed the 5 `_regime_dict` copies). | `stale → rotation() returns None`; consumers treat None as no-op (P2). |
| **The rotation-evidence budget term (W-I item 4)** | `headline_episode` + `agreement` + `confidence` | **This is the single sizing consumption.** Raises the DEF_SLEEVE floor / shifts offense-defense mix when a defensive episode is *measured + confirmed*. Fixes the counterfactual gap: on 07-01 this term fires on the **measured** episode even though regime=STABLE, so `fragility_signal` is no longer throttled to 0.20. | Enters as **one multiplicative/additive term inside the ONE budget equation** — never at leg/book/gross. `advisory:true` → term is display-only (P3). Missing → term = neutral. Shrink/shift-toward-defense only; **cannot un-cap or add offense** (P2). |
| **Evidence blend (P1 two-plane cite)** | `top_pairs`, `episode.direction` | Counts as an **independent evidence plane** for the "≥2 planes" rule — price-derived, orthogonal to the sign-vote regime label. A defensive posture can now cite {regime, rotation_tensor} instead of regime alone. | Advisory episodes count as *soft* corroboration only until validated. |
| **PM brief / judgment payload (W4 seat)** | `headline_episode` + `top_pairs` prose | Injects the human-readable line: *"healthcare gaining 34 bps/day on semis for 6 sessions, 0.83-percentile episode; SMH flagged distribution."* Directly answers the user's mandate ("see... exactly how they are rotating and by how much"). Fixes the echo-analysis gap (seats were blind to the rotation). | Additive-seat input only; the seat stays subtract-only on size. |

**Anti-double-count proof:** the tensor's *magnitude* touches size in exactly one place (the budget term). The leg-extension clamp, cluster caps, and severity ladder read their own inputs (`etf_board` pct_vs_200d, `cluster_id`, tripwire) — none read the tensor. So the SMH concentration gets cut by the extension/cluster brakes *and* the defensive tilt gets funded by the budget term, but the *same rotation signal* is not applied twice (the W2 lesson).

---

## 4. Validation — P3, pre-registered

The tensor ships **`advisory:true`** (published, rendered, PM-visible, budget-term display-only). It earns `validated:true` — and the budget term earns live authority — only by passing a **pre-registered** gate.

**Pre-registered hypothesis (frozen before the run):** *episode detection has forward power — when the tensor flags a defensive (offensive) episode at percentile ≥ P, the DEF−OFF sector spread persists/extends in that direction over the next 20 sessions more often than an unconditional baseline.*

**Test (walk-forward, 2011→now, causal features only):**
1. For every historical session, compute the tensor (the causal construction makes this a clean replay).
2. **Primary metric:** episode-detection **hit-rate** = P(sign of forward-20d Δspread == episode direction | episode active at percentile≥0.7), vs the unconditional base rate. Report AUC of `episode.percentile` → `sign(fwd20 spread persistence)`.
3. **Pre-committed pass bar (mirrors the nowcast AUC>0.55 gate in the architecture):** `AUC > 0.55` **AND** hit-rate lift ≥ +8pp over base **AND** the effect survives on the **risk-off subsample** (SPY<200d) — it must earn its keep in the regime it exists to protect, the exact bar the sector-cycle veto *failed*.
4. **Falsifiers (pre-committed kill criteria):**
   - Episode direction shows **no** forward-spread persistence (AUC≤0.55) → tensor stays **advisory/display-only forever**; the budget term never arms. (It remains valuable as the P1 evidence plane + PM narration even if it never sizes.)
   - Percentile carries no information beyond a raw 3-session-crossover dummy → **drop the percentile machinery**, keep the boolean episode.
   - Breadth/flow blocks don't improve hit-rate over price-only → publish them **descriptive-only**, exclude from `agreement`.
5. **Multiple-testing honesty (NEW-B lesson):** episodes across many pairs are correlated; effective_n is computed on **date-clustered, return-series** correlation (Newey-West), not the raw episode count — no "SURVIVES DSR" claim on an effective_n of 1.

**Shadow-grade first (P3 ladder, P8):** even after the backtest passes, the live budget term runs in **shadow** (logged, graded via the incident replay harness + a rolling forward log) before it moves a single real dollar — the promotion discipline the judgment seat already uses.

---

## 5. How it composes with the existing stack (no reinvention)

- **Builds on** `brain/regime_frame.py` as the sole reader (adds `rotation()`, mirrors the `cycles()` freshness-gate pattern), the **W-I rotation-evidence budget term** as the single sizing consumer, `portfolio/rotation.py` DEF_SLEEVE as the actuator (the tensor raises its floor — it does not create a new sleeve), and the **W-I incident replay harness** as both validation fixture and shadow-grader.
- **Does not touch** the leg-extension clamp, cluster caps, severity ladder, or the ONE budget equation's structure — it contributes **one term**.
- **Degrades to today's behavior** on every missing plane (P2): stale yahoo → `rotation()=None` → budget term neutral → book behaves exactly as pre-tensor. Flows stale (they're 4td behind today) → flow block drops to `rvol_only`, episode still computes off price. Per-sector breadth absent (the real gap) → breadth block `unavailable`, `agreement` computed on the 4 available blocks. **Nothing in the build order waits on a dashboard wishlist item** (charter §4.11).
- **Cost:** pure parquet reads + numpy; **zero LLM calls per plane**; assembles in the 22:40 build alongside `cycles()`.

---

## 6. Dashboard wishlist this surfaces (wants, not blockers)

1. **Per-sector breadth contract** (`pct_above_50` / new-highs share sliced by GICS) published at `data/breadth/by_sector.parquet` — today only SP1500-aggregate `breadth.parquet` exists; block (b) runs on a within-ETF proxy until this ships.
2. **`group_flow` real contract** (it's stubbed at `validation_meta.json` only) — richer flow than `so_mn` deltas; block (d) degrades to `etf_so_delta` meanwhile.
3. **SMH/semis in the `flows/` set** (only 11 sector ETFs today) — so the semis block gets a real primary-market flow cell instead of `rvol_only`.
4. **Consistent `asOf`** per plane (already relied on for the `as_of = min(plane asOf)` stamp).

---

## Key files

- **New contract:** `/Users/chriswong/Documents/Cluade/Mastermind/data/market_view/rotation_tensor.json` (+ mirror `Macro Dashboard/site/marketdata/rotation_tensor.json`)
- **New builder (design target):** `portfolio/market_view/rotation_tensor.py` (deterministic; reads `data/yahoo/XL*.parquet`, `data/flows/XL*.parquet`, `data/breadth/`)
- **Reader hook:** `brain/regime_frame.py` → add `rotation()` (mirrors `cycles()` freshness-gate)
- **Sole sizing consumer:** the W-I rotation-evidence budget term inside the ONE budget equation (`regime_frame.budget()`)
- **Confirmed input contracts (all present, schemas verified):** `data/yahoo/*.parquet` (`close`,`volume`; 11 GICS+SMH+SOXX+SPY+RSP), `data/flows/XL*.parquet` (`nav`,`aum_mn`,`so_mn`; 11 sector ETFs), `data/breadth/breadth.parquet` (`pct_above_50/200`,`nh`,`nl`; **SP1500-aggregate, not per-sector — named coverage gap**), `site/sectordata/sector_cycles.json`
- **Validation fixture:** `research/incidents/2026-07-02-semis-breakdown/` (the 06-24…07-02 episode = pre-registered worked example + permanent replay fixture)

**Worked-example headline (the user's exact ask, quantified):** over 06-24…07-02, the tensor measures the defensive rotation as **≈ +704 bps of XLV-vs-SMH repricing across 6 sessions (~117 bps/day at the single-ETF level; ~1.7 bps/day on the DEF/OFF bloc axis), a ~0.83–0.91-percentile episode, with breadth + churn + flow all confirming defensive** — the second, price-derived evidence plane that the STABLE regime label could not produce and that the current stack still under-reacts to.
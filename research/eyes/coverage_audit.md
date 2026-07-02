# COVERAGE AUDITOR — the true "fraction perceived" number

Audited against **master** of Mastermind (= prod-deploy-w0w3 + W4 + charter; production runs prod-deploy-w0w3, which is a strict ancestor — so the W4 additive-mind planes below are the ceiling, not the deployed floor). Consumption grepped across `brain/ bot/ portfolio/`. Publisher surface = `Macro Dashboard/site/**` + bot-relevant `data/**`.

## The load-bearing structural finding (changes the whole framing)

The bot's primary macro plane, `vendor/macro/data/regime/latest.json`, is **one fat contract that embeds ~50 sub-planes** — not a thin 7-field dict. The bot already loads this file every build (cheap read, zero incremental cost), then **slices it down to ~10 fields and discards the rest.** So "fraction perceived" is dominated by *field-drop inside an already-loaded artifact*, not by unread files. The dashboard's own pre-computed risk directives are sitting inside the JSON the bot reads and are thrown away.

Confirmed by grep — embedded sub-planes referenced by **zero** bot files: `risk_radar`, `froth_fragility`, `turning_point`, `vol_shock`, `mtf_signals`, `yield_curve`, `business_cycle`, `regime_hmm`, `subsector_scan`, `theme_emergence`, `base_effect`, `risk_coherence`, and the directive scalars `cap_leadership`, `gross_factor`, `drawdown_prob`, `favor_entries`.

The incident's "the bot never saw the caution radar because `risk_radar/` was stripped from the sparse set" is only half the story — **the radar is embedded in the regime JSON the bot already reads (`asof=2026-07-01`, fresh), the bot just never looks at that key.** Same for `mtf_signals` (per-ticker 3D-MACD + trail-stop, the exact SMH price-action tell), `froth_fragility` ("leaders distributing under a held index — narrowing-top risk", alert=True), and `risk_state.gross_factor=0.9`.

---

## THE INGESTION QUEUE — unconsumed contracts

Each row: what it sees · incident relevance (06-24→07-02) · freshness · validation status · priority score (0–100; = signal_value × incident_leverage × ingest_cheapness, my weighting stated at end).

**Tier A — embedded in already-loaded `regime/latest.json` (near-zero read cost, fresh daily):**

| Contract (embedded key) | What it sees (1 line) | Would it have helped 06-24..07-02? | Fresh | Validation | Pri |
|---|---|---|---|---|---|
| `regime.risk_radar` | Dominant-scare classifier + `drawdown_prob.h21` + `cap_leadership`/`favor_entries` directives | **YES, directly.** On the vendored 07-01 read: `dominant_scare='growth'`, label "Growth scare / defensive rotation", `drawdown_prob.h21=0.19` (lift 1.07× and *rising* 06-24→06-26). This is the plane the incident post-mortem names as the smoking gun. | 07-01 | `forward_log.jsonl` — empirical >=5% SPY pullback calibration 2006–2026 | **97** |
| `regime.mtf_signals` | Per-ticker 3D-MACD `state`, `weekly_bull`, `trail_stop`, `trail_breach`, last buy/sell | **YES.** The incident's shrink-only nowcast needs exactly this: SMH 3D-MACD bearish cross printed 06-25 & 07-01. Per-name trail_breach is a ready-made distribution tell. | 06-29 | walk-forward MTF (memory: single legs worthless, MTF confluence validated) | **94** |
| `regime.froth_fragility` | Crowding/distribution quadrant + `low_naaim_flag` + alert | **YES.** Reads "Euphoric, leaders distributing under a held index — narrowing-top risk", `alert=True`, headline 40.6 — the W-I Distribution Escalator's target signal, pre-computed. | 07-01 | dashboard grades it (parab_history.parquet); provisional flag present | **88** |
| `regime.risk_state` (`gross_factor`,`cap_leadership`) | Dashboard's own **pre-computed gross multiplier** | **YES.** `gross_factor=0.9`, `label='Caution'` — a ready shrink-only budget input the bot recomputes worse from scratch. | 07-01 | radar-derived, `is_context_only` flag | **85** |
| `regime.turning_point` | Regime turn detector (`present`, `put_state`, size note) | Partial — an inflection corroborator for the WEAKENING call. | 07-01 | graded (raw_fire vs defer) | 66 |
| `regime.vol_shock` | Forward vol-shock score + GEX-gate | Partial — coincident stress, front-runs less than mtf. | 07-01 | `gex_gate_scored` DSR-gated | 61 |
| `regime.yield_curve` | Curve shape/momentum + recession signal | Low for this incident (equity-momentum event, not rates). | 07-01 | `scored_status` | 44 |
| `regime.business_cycle` | CB leading/coincident phase + recession_now | Low (slow-moving; asof 07-31 stamp is suspect). | stale? | `calibrated` flag | 40 |
| `regime.regime_hmm` | HMM next-quad probs + dwell | Context only — could feed transition confidence. | 07-01 | n_obs-based | 38 |
| `regime.subsector_scan` / `theme_emergence` | Scarcity-aligned subsectors / emerging themes | Low for de-risking; useful for offense selection. | 07-01 / 06-24 | noted advisory | 34 |

**Tier B — separate published contracts, not in bot's read path:**

| Contract | What it sees | Incident relevance | Fresh | Validation | Pri |
|---|---|---|---|---|---|
| `site/marketdata/subsector_rotation.json` (RRG) | **268 subsectors × 8 timeframes RS + 5 momentum horizons** — literally "how sectors are rotating and by how much" (the user's exact words) | Medium-high — would have shown the defensive-vs-offensive rotation *magnitude*, not just direction. | 06-28 | finviz-themes source; dashboard RRG track-record | **72** |
| `site/marketdata` sector RS *magnitudes* (via `sector_cycles.now.rs_rank/rs_63d/rs_126d`) | Per-sector RS rank + 63/126d RS momentum in the `now` block | Medium — the def>offense RS crossover leg the nowcast wants. Bot's `cycles()` reads phase but **drops rs_63d/rs_126d/timing_state/dc_phase**. | 07-01 | walk-forward (cycle veto refuted; RS-entry tilt survives) | **68** |
| `data/froth_fragility/log.jsonl` (parabolic history) | Parabolic-extension history per name | Medium — extension veto corroboration. | daily | forward-graded | 52 |
| `data/ofr_fsi/*` (OFR Financial Stress Index, 5 sub-indices) | Funding/credit/vol/safe-asset stress decomposition | Medium — the liquidity-quality "stress vs benign" classifier wants this (RRP-buffer + funding stress). Not currently published as a site snapshot. | daily | official OFR series | 55 |
| `data/dislocation/state_log.parquet` | Cross-asset dislocation state | Low-med — coincident, `etf_board` reads the *embedded* dislocation already. | daily | state_log grades | 42 |
| `data/group_flow/` (sector fund-flow forecast) | 1,118-name flow-forecast legs | **Low — honestly advisory.** `verdict='display_only'`, deconf IC 0.08 t=1.86 p=0.06 — fails its own gate. Ingest as advisory only per charter P3. | 06-15 | **self-graded display_only (do not size)** | 22 |
| `data/smart_money/*` (13F clusters) | Fund crowding/overlap | Low for a 3-day tape (quarterly cadence). | quarterly | graded vs SPY | 20 |
| `data/treasury_auctions/*` | Auction tail stress | Low for this incident. | per-auction | display-only | 18 |

---

## CONSUMED-BUT-UNDERUSED (fields dropped from planes the bot already reads)

1. **`regime/latest.json` itself** — `build_board()` (etf_board.py:275) and `regime_frame` slice ~10 of ~50 embedded planes. **This is the single biggest gap in the whole audit** (see Tier A). Also drops `confirming`, `flip_condition` (read 1 file), `preference_check`, `base_effect`.
2. **`sector_cycles.json` `now` block** — `regime_frame.cycles()` (the sole reader) derives only the `late_cycle` boolean from `phase/pos/osc_slope`. It **drops `rs_rank`, `rs_63d`, `rs_126d`, `rs_above_trend`, `timing_state`, `dc_phase`, `w_macd_up`, `t3_macd_up`** — the RS-magnitude and MTF-MACD fields that would make the def-vs-offense rotation measurable rather than phase-only.
3. **`us_standouts.json`** — bot reads `gate_go`, `buy`, `score`, `ext_mult`, `entry_signal`, `buy_zone` but drops `dispersion_regime`, `concentration`, `laggards` (the short/avoid side of the rotation).
4. **`transmission/latest.json`** — `etf_board.build_board` reads only `state.rates` + per-name `net`; drops `headwinds`/`tailwinds`/`scenarios`/`chains`.
5. **`cross_asset` (embedded)** — `build_fragility` reads `absorption_pctile_5y` + `verdict`; drops `corr_matrix`, `pc1_loadings`, `dominant_cluster`, `top_pairs` (the actual correlation structure the firm cluster-caps in W3 should be keyed on).

---

## THE HONEST PERCEPTION METRIC

**Method (stated):** I enumerated the bot-relevant published signal surface as ~34 distinct decision-grade planes (the ~50 embedded regime sub-planes collapsed to the ~22 that carry independent decision value, plus ~12 standalone site/data contracts a US/CN/HK equity book would act on — deliberately excluding the ~150 dashboard products that are single-stock analyzers, intl/crypto/policy pages, and display leaves outside the bot's mandate). I weighted each plane 0–1 by **judgment of decision value to a perception-before-position posture engine** (risk_radar/mtf/froth/gross_factor ≈ 1.0; RRG/RS-magnitude ≈ 0.7; group_flow/auctions ≈ 0.2 given they self-grade display-only). "Perceived" = plane's fields materially reach a sizing or posture decision (not just logged).

- **By raw plane count:** ~13 of ~34 planes touched → **~38% perceived.**
- **Value-weighted (the honest number):** the *highest-value* planes are precisely the unconsumed ones — the risk_radar directive, mtf price-action, froth distribution, and the pre-computed `gross_factor` are all weight≈1.0 and all dropped. Weighting by decision value:

> **The bot perceives ~34% of the value-weighted published signal surface that its own mandate ("see incoming trucks, sector rotations and by how much, bubbles forming") calls for — and critically, it drops the four highest-value planes (radar/mtf/froth/gross_factor) that are already sitting inside the JSON it loads every build.** Weighted for *posture-relevant* (risk-off / rotation-detection) surface specifically, perception drops to **~25%**: the bot sees regime label + RS ranks + its own recomputed risk heuristics, but not the dashboard's calibrated drawdown-probability, distribution, or rotation-magnitude planes.

Two caveats keeping this honest: (a) some perceived planes are *underused* (fields dropped), so 34% overstates depth; (b) some unconsumed planes are correctly *advisory* (`group_flow` self-labels display_only) and should ingest damped, not sized — so 34% slightly understates *appropriate* coverage. Net: the real actionable gap is **the Tier-A embedded planes**, where cost≈0 and value≈max.

---

## TOP-15 RANKED INGESTION QUEUE for the `market_view` assembler

Ordered by priority. Items 1–4 are the highest leverage in the entire program because they are free reads (already-loaded JSON) of the exact planes the 07-02 incident proved decisive, and they compose with the existing budget/DEF_SLEEVE/severity machinery as **shrink-only** inputs (charter P2) consumed exactly once (W2 anti-compounding).

1. **`regime.risk_radar`** → `drawdown_prob.h21` + `dominant_scare` + `cap_leadership` as a shrink-only budget/severity input. *(embedded, forward-log-graded, cost≈0)*
2. **`regime.mtf_signals`** → per-name 3D-MACD state + `trail_breach` as the price-action nowcast / distribution-escalator feed (the SMH tell). *(embedded, MTF-validated)*
3. **`regime.froth_fragility`** → distribution quadrant + `alert` into the W-I Distribution Escalator severity bump. *(embedded)*
4. **`regime.risk_state.gross_factor` / `.cap_leadership`** → advisory second opinion on the budget equation (never raise; min-composed). *(embedded)*
5. **`sector_cycles.now` RS fields** (`rs_63d`,`rs_126d`,`rs_rank`,`timing_state`) — un-drop in `cycles()` to make def-vs-offense rotation *measurable*. *(already-read file, field un-drop)*
6. **`site/marketdata/subsector_rotation.json`** (RRG) — rotation magnitude across 268 subsectors × 8 TFs (the user's literal ask). *(new read, graded)*
7. **`regime.turning_point`** — WEAKENING-call corroborator into transition confidence. *(embedded)*
8. **`regime.cross_asset` full** (`corr_matrix`,`pc1_loadings`,`dominant_cluster`) — feed W3 firm cluster-caps with *real* correlation structure. *(embedded, field un-drop)*
9. **`data/ofr_fsi`** (funding/credit stress decomposition) — the liquidity-quality "stress vs benign" classifier's missing input. *(needs site snapshot; hand to dashboard)*
10. **`regime.vol_shock`** — forward vol-shock score as coincident stress corroborator. *(embedded)*
11. **`us_standouts.laggards`/`dispersion_regime`** — the avoid/short side of rotation (currently only the buy side is read). *(field un-drop)*
12. **`regime.yield_curve`** — recession/curve signal into cycle context (advisory). *(embedded)*
13. **`transmission` headwinds/tailwinds/scenarios** — richer duration/rate posture. *(field un-drop)*
14. **`regime.regime_hmm.next_quad_probs`** — transition-probability prior for the frame. *(embedded)*
15. **`data/group_flow`** — sector flow-forecast, **ingest as honestly-advisory only** (`verdict=display_only`, fails its own IC gate; charter P3 → never sizes). *(damped advisory)*

**Design note for the assembler (charter compliance):** items 1–4 and 8 all derive from `regime/latest.json`, which `regime_frame.py` already opens — so the `market_view` assembler should read the fat contract **once** and expose the embedded planes as freshness+confidence-stamped fields on the single frame (P7 one-view), each degrading to no-op when its `asof` is stale (P2), each entering the budget/severity stack exactly once as shrink-only (W2 anti-compounding). No new LLM calls, no new file reads for the top 8 items — the perception gap is overwhelmingly *a parsing gap, not a data gap.*

**Relevant paths:** readers to extend — `/Users/chriswong/Documents/Cluade/Mastermind/brain/etf_board.py` (build_board slices), `/Users/chriswong/Documents/Cluade/Mastermind/brain/regime_frame.py` (cycles() field-drop + the natural home for a `market_view()` assembler), `/Users/chriswong/Documents/Cluade/Mastermind/brain/risk_lens.py` (recomputes what `risk_state.gross_factor` already publishes). Publisher: `/Users/chriswong/Documents/Cluade/Macro Dashboard/data/regime/latest.json` (fat contract), `/Users/chriswong/Documents/Cluade/Macro Dashboard/site/marketdata/subsector_rotation.json` (RRG), `/Users/chriswong/Documents/Cluade/Macro Dashboard/site/sectordata/sector_cycles.json`.
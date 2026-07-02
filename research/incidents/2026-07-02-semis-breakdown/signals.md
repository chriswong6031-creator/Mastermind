Crucial contradiction found. The **MD risk_radar itself** (asof 06-26) already reads:
- `state: caution`, headline **"CAUTION: Growth scare / defensive rotation (91/100) × Credit stress. Leading: growth_defensives(93%), growth_cyc_def(89%)"**
- hot scares: growth, credit, vol
- drawdown h21 RISING 0.16→0.19 (lift 0.9→1.07x) over 06-24→06-25 — pullback odds INCREASING, not receding
- `favor_entries: True`, `cap_leadership: False` (caution is below the "elevated" threshold that would cap leadership)

So the dashboard's own radar flipped to "growth scare / defensive rotation" caution days ago, with pullback odds rising — yet the bot never saw it because `risk_radar/` is stripped from the vendored sparse set. The "risk receding" green badge is stale/mislabeled relative to the radar's own escalating drawdown_prob. I have everything. Let me compile the final autopsy.

Here is the F3 signal autopsy.

---

# F3 — SIGNAL AUTOPSY: why Goldilocks / STABLE / expanding-liquidity / risk-receding were wrong

Two data planes matter and they disagree. The **bot acts on** `vendor/macro_src/data/regime/latest.json` (07-01): `Q1 Goldilocks, growth +0.333, inflation −0.52, liquidity_overlay=expanding, transition_state=STABLE`. The **Macro-Dashboard's own** `data/regime/regime_history.parquet` (through 06-26) reads `liquidity=contracting, transition=WEAKENING`, and its `risk_radar` reads `caution: growth scare / defensive rotation (91/100)`. The bot is acting on the rosier of the two planes, and on the specific leg (`risk_radar/`) that is **missing from its sparse vendored set**.

## (1) REGIME LABEL — why it stays Q1 Goldilocks

**Which legs hold it.** Growth axis = weighted mean of **sign votes** (each component −1/0/+1 by 20d-slope z, `engine/axes.py:19-62`), threshold z=0.45. At +0.333 the confirming legs outvote the turn-tells:
- confirming (07-01): `copper_gold, iwm_spy, breadth_direction, payrolls_trend, indpro_trend` (+ inflation legs)
- contradicting: `growth_cyclical_defensive, growth_wei_trend, inflation_sticky_cpi_direction`

**Why it won't flip despite its own contradiction list.** Three structural faults:
1. **Sign-vote democracy, no magnitude.** A defensive leg that *just* crossed casts the same −1 as a deeply-negative one (`score_from_z` clips to −1/0/+1). The still-rising-but-decelerating legs (`iwm_spy`, `breadth_direction` — both 20d slopes still positive during the late-cycle *broadening* the user flagged) keep the mean positive.
2. **Lagging monthlies vote +.** `payrolls_trend` (63-bday sign), `indpro_trend` (yoy), `gdpnow_trend` are backward-looking and still print +, padding the growth axis exactly when the fast rotation turns.
3. **Sticky-CPI is buried.** `sticky_cpi_direction` sits on the *inflation* axis at weight 0.5 and votes there (making inflation *less* negative), so it never touches the growth call. Combined with `growth_cyclical_defensive` (the single best "risk-off rotation" tell) getting one −1 among ~8 growth legs, the label is architecturally incapable of registering a leadership rotation until the slow legs roll — i.e. it lags the exact move the user front-ran. `hysteresis_days=7` + `shock_override_z=0.85` (`config.yml`) then adds another week of stickiness before any flip confirms.

**Transition regression WEAKENING→STABLE (07-01→ the bot's read).** `engine/transition.py:85-112` is a **memoryless flag counter**: n≥3→TRANSITIONING, n≥2→WEAKENING, else STABLE. The six flags are momentary boolean windows (a 5-day slope-flip on the cyc/def ratio, a breadth-drop window, a 10-day confidence-decay diff, a GEX proximity gate). They flicker as the windows roll. The dashboard history proves the oscillation:

```
06-15 n=3 TRANSITIONING → 06-16 n=2 WEAKENING → 06-17 n=3 TRANSITIONING
06-18 n=1 STABLE → 06-22 n=0 STABLE → 06-25/26 n=2 WEAKENING
```

The bot's 07-01 feed shows **all six flags False → n=0 → STABLE**. The regression is `flag_confidence_decay` and `flag_gex` dropping out as their windows rolled — **not the rotation reversing**. (Note an internal inconsistency: the bot's regime feed reports `flag_gex:False, flag_confidence_decay:False`, while the same day's `data/macro_risk/2026-07-01/state.json` rationale asserts "flag_gex and flag_confidence_decay both firing" — the two planes are out of sync.) The state machine has no ratchet: once you've been WEAKENING, a single flag rolling off silently restores "STABLE / all clear."

## (2) LIQUIDITY QUALITY — the "expanding" read is a base-effect artifact, and the level is stress-flavored

**What it measures.** `engine/regime.py:100-113`: `net_liquidity_bn = WALCL − RRP − TGA` (`engine/inputs.py:279`), lagged 3 bdays, 20-day RoC, thresholds ±25bn (`config.yml`). No credit/funding/vol input at all — it is a pure quantity-of-reserves RoC.

**The 07-01 "expanding" is one-day noise.** Reconstructed RoC around the flip:
```
06-26 −26.5  06-29 −30.6  06-30 +4.9(neutral)  07-01 +68.9(EXPANDING)  07-02 +11.5(neutral)
```
Net-liquidity has been **dead-flat at ~$5,858bn for 5 sessions** (stale ffilled WALCL, flat since 06-25). The +68.9 spike is the 20-day-ago comparison point rolling off a low base — it evaporates the next day. The bot happened to buy on the single day the noise crossed +25.

**The user's stress-thesis holds — decompose the swing:**
```
dWALCL (Fed)  = +31.3
d(−RRP)       = −5.1   ← RRP already drained to $6.4bn (facility ~empty; no cushion left)
d(−TGA)       = −14.6  ← Treasury cash spend-down
```
The expansion is **not durable Fed easing** — it's TGA drawdown against an empty RRP. With RRP at the floor, the benign "RRP→reserves" plumbing that padded liquidity through 2024-25 is exhausted; further Treasury issuance now drains reserves directly. That is precisely the "forced/stress expansion, not benign" mechanism.

**Spec — LIQUIDITY-QUALITY classifier (benign vs stress).** Add a quality dimension the current overlay lacks. Exact series (all present in `data/fred`, `data/nyfed`, `data/treasury`):
- **Quantity RoC**: `net_liquidity_bn` 20d RoC (existing).
- **RRP-buffer flag**: `RRPONTSYD` level < ~$100bn → cushion exhausted (today **$6.4bn → TRUE**).
- **Composition**: share of the RoC from `dWALCL` (Fed, benign) vs `−dTGA/−dRRP` (mechanical). Today the WALCL leg is ffill-flat; the move is TGA/RRP → **mechanical**.
- **Stress overlay**: `BAMLH0A0HYM2` (HY OAS) 20d change and z; `NFCI`/`ANFCI` level & 4wk direction; add `TEDRATE`/SOFR-IORB spread if collected.
- **Classification**: `expanding` → **STRESS-expansion** iff (RRP-buffer exhausted) OR (composition mechanical) OR (OAS widening / NFCI tightening co-occurs); else **benign-expansion**.

**Reads today:** HY OAS 2.78% (+0.04pp/20d, z −0.54 — quiescent), NFCI −0.516 (loose). So credit is *not yet* confirming stress — but RRP-exhaustion + mechanical-composition already flip the classifier to **stress/hollow expansion**. The honest label today is **"neutral, hollow"**, not "expanding (benign)." The radar's own `credit` scare being hot (§4) is the leading edge the OAS *level* hasn't caught yet.

## (3) PRICE-ACTION SECOND OPINION (freshest data = vendored yahoo through 07-01)

- **Defensive−offensive 20d RS differential** (XLV+XLU+XLP vs SMH+XLK): flipped positive **2026-06-24**, held >0 continuously since **06-25** (07-01 diff = **+0.093**, def_rs +0.080 vs off_rs **−0.014** — offense now underperforming SPY outright). Durable 3-session crossover episode start: **2026-06-30**.
- **Breadth**: %sectors>20d rolled 64→55 (06-26→07-01); %sectors>50d 73→45→55 — deteriorating under a held index.
- **SMH distribution**: lower highs 668.91(06-22)→636→655→**620.46**(07-01), **−7.2% off the 20d high**, +0.2% over 5 sessions / +0.7% over 10 — flat-to-down while capital rotated.
- **SMH MACD MTF**:
  - **3-day bars: bearish crosses printed 06-25 and again 07-01** ✅ (user's "multiple sessions ago" — confirmed and dated).
  - **Weekly bars: still technically BULL (line>signal), but histogram 2nd-derivative rolled over**: +16.5(05-29)→+16.0(06-19)→+12.7(06-26)→+9.9 — momentum bleeding for 4 weeks; a weekly bearish cross is 1-2 weeks out on this trajectory, not yet printed. (So the user's "1W cross ticked" is directionally right as *momentum roll-over*; the strict weekly line-cross has not fired — the 3D is what has crossed, twice.)
  - **SMH daily RSI 51.9** (46th pctile) — momentum already neutralized off the highs.

**Shrink-only nowcast — would-have-fired date.** Rule: `defensive-RS diff > 0` **AND** `SMH 3D-MACD bearish` **AND** `%sectors>50d falling(5d)`. Composite **first fires 2026-07-01** — the day before Autonomous added $24.8k SMH. All three legs TRUE on 07-01. A 2-of-3 looser variant fired **2026-06-24** and again **06-30**. So a disciplined shrink-only nowcast says **"reduce offense" on 06-24 (soft) / 07-01 (hard)** — matching the user's "days early" read, and cleanly *ahead* of the books' re-risking.

## (4) DASHBOARD HANDOFF LIST — items the dashboard sessions must fix, with today's evidence

1. **`transition_state` regression (STABLE all-clear whipsaw)** — `engine/transition.py:85-112`. Memoryless flag counter; WEAKENING→STABLE on 07-01 is `flag_gex`/`flag_confidence_decay` windows rolling off, not the rotation reversing (history: 06-15..07-01 oscillated TRANSITIONING↔STABLE↔WEAKENING 5×). **Fix**: add a ratchet/dwell (once WEAKENING, require N consecutive n<2 days before returning to STABLE) or a slow "rotation-persistence" flag on the cyc/def ratio that doesn't reset on a 5-day window. Also **reconcile** the feed's `flag_gex/flag_confidence_decay=False` vs macro_risk's "both firing" — the two planes disagree same-day.

2. **`liquidity_overlay` = expanding (false positive)** — `engine/regime.py:100-113`. Pure quantity RoC with no quality gate; the 07-01 "expanding" is 1-day base-effect noise on a flat, ffill-stale WALCL, composed of TGA/RRP drain against an **empty RRP ($6.4bn)**. **Fix**: implement the liquidity-quality classifier in §2 (RRP-buffer flag + composition + OAS/NFCI overlay). Today it should read **"neutral / hollow-stress"**, not "expanding."

3. **Regime label lag (Q1 sticky)** — `engine/axes.py:19-62` + `config.yml` quad hysteresis. Sign-vote democracy + lagging monthlies out-vote the turn-tells; `growth_cyclical_defensive`/`growth_wei_trend` contradictions can't move the label. **Fix**: magnitude-weight the votes (or add a "leadership-rotation" super-flag), down-weight/lag the monthly econ during turns, and surface the `contradicting` list into the transition state (a growth-defensive + cyc/def + WEI trio should force at least WEAKENING regardless of the flag windows).

4. **"Risk receding" green badge + "pullback-odds receding" leg** — the de-escalation panel (`risk-radar-deescalation-panel`) reads the OLD June geopolitical scare fading, **but the radar's own `state` = `caution: "Growth scare / defensive rotation (91/100) × Credit stress"`** (`data/regime/latest.json` risk_radar, headline_en), with `drawdown_prob.h21` **rising 0.16→0.19 (lift 0.9→1.07×)** across 06-24→06-26 (`data/risk_radar/forward_log.jsonl`). Hot scares: **growth, credit, vol**. **Fix**: the "receding" badge must be gated by the *new* dominant scare — when the dominant scare flips to `growth/defensive rotation` and drawdown_prob is *rising*, the badge must not read "receding." The label is measuring the wrong scare fading.

5. **Vendored-set regression (bot blindness)** — `vendor/macro_src/data/risk_radar/` contains only `forward_log.jsonl`; the radar **snapshot JSON is stripped**. The bot therefore never sees the `caution / growth-scare / rising-drawdown` read that the dashboard already computed, and falls back to the rosy `latest.json`/`regime_timeline.json`. **Fix**: add the radar snapshot (and, ideally, the fred/treasury liquidity inputs so the bot can recompute quality rather than trust a baked overlay) to the sparse-checkout manifest.

**Net:** every "all-clear" the books traded on was a lagging/mislabeled artifact — Q1 from sign-vote democracy + hysteresis, STABLE from a memoryless flag oscillator, "expanding" from base-effect noise over an exhausted RRP, "risk receding" from the wrong scare fading — while the dashboard's own radar (caution, growth-scare, rising pullback odds) and a trivial shrink-only price nowcast both said **reduce offense by 06-24/07-01**, which is exactly what the user did.

Scratch code: `/tmp/mm_incident/f3_autopsy.py`, `/tmp/mm_incident/f3_axes.py`, `/tmp/mm_incident/f3_liq_price.py`, `/tmp/mm_incident/f3_nowcast.py`. Load-bearing source: `engine/transition.py:85-112`, `engine/axes.py:19-62`, `engine/regime.py:100-113`, `engine/inputs.py:279`, `engine/risk_radar.py:465-482`; feeds: `vendor/macro_src/data/regime/latest.json`, `Macro Dashboard/data/regime/regime_history.parquet`, `Macro Dashboard/data/regime/latest.json` (risk_radar), `data/macro_risk/2026-07-01/state.json`.
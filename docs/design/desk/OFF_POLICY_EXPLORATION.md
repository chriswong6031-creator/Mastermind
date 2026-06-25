# Off-policy exploration budget — design note

**Status:** decision needed (logging ships now; exploration is gated OFF pending this call)
**Date:** 2026-06-24 · **Owner:** desk · **Relates to:** `portfolio/rejections.py`, accelerator #2

## The problem in one paragraph

A deterministic, greedy desk only records what it **bought**. Every name it **rejected** (conviction
veto / research hold / timing withhold / committee drop) leaves no trace and no outcome — so we can
never answer two questions that bound our edge: *"did the gate veto winners?"* (false-negative rate)
and *"what would an alternative selection rule have earned?"* (off-policy value). The second question
is **inverse-propensity / doubly-robust off-policy evaluation (OPE)**, and OPE has a hard prerequisite:
the logged policy must assign a **non-zero probability** `π(buy | candidate)` to the names it rejected.
A greedy desk assigns exactly `0`, which makes the IPS weight `1/π` undefined — you cannot reconstruct
this retroactively. **If we don't bake a little logged randomness into selection now, the OPE signal is
permanently lost for every day we wait.** That is why this is the most time-sensitive accelerator.

## What logging *alone* already buys (shipping now, no behavior change)

`portfolio/rejections.py` logs every rejected name + the policy's propensity, then forward-grades its
21-bday rel-return vs SPY. Even with **zero exploration** this yields the **veto-regret / false-negative
read** — the rejected-cohort hit-rate vs SPY, split by stage — which is identifiable from outcomes alone
(it's the "veto-shadow cohort" the desk-architecture doc already calls for). This is pure observation,
no capital, no behavior change, and it ships ON today. What it *cannot* give without exploration is an
unbiased **value estimate** of a counterfactual policy — that needs propensities `> 0`.

## The decision: how much logged exploration on borderline names

| Option | Mechanism | What OPE you unlock | Cost / risk | 
|---|---|---|---|
| **A. None (log only)** | π=0 on rejects | Veto-regret cohort read only (no value estimate) | Zero. But IPS/DR stays undefined forever | 
| **B. ε-greedy on the borderline band (recommended)** | With prob ε, buy a *borderline* reject (research-hold / committee-trim / timing-withhold — **never a hard conviction veto**) at a small floor weight; log π=ε | Unbiased IPS/DR on the gate/research/timing layers — the layers we actually want to tune | Low: ε≈3–5% of borderline names, floor-sized; paper-only ⇒ no real capital at risk | 
| **C. Boltzmann / softmax** | π ∝ exp(score/τ) across the whole candidate set | Smoother, fuller OPE coverage | Higher: perturbs the *whole* book, harder to reason about, larger tracking error vs the real policy | 
| **D. Thompson over selection rules** | Sample among rule variants | Policy-level (not name-level) learning | Overlaps with accelerator #5 (bandit over shadow books); defer to that | 

## Recommendation: **Option B**, ε = 0.05, borderline-only, floor-weighted

- **Why ε-greedy, not softmax:** it perturbs *only* borderline rejects, leaving the high-conviction book
  byte-identical, so the live desk's behavior and NAV barely move while still producing `π>0` exactly
  where we want to measure (the gate / research / committee / timing layers). Softmax (C) muddies the
  whole book for marginal extra coverage.
- **Why borderline-only:** a hard conviction veto (parabolic / Altman distress / downtrend) is a
  *safety* rule we don't want to randomly override; exploring it buys little and risks real dumb trades.
  Restrict exploration to names rejected by *soft* gates (research-hold, committee-trim, timing-withhold)
  that sit within a small score margin of the buy threshold.
- **Why this is cheap *for us specifically:* paper-only.** The classic objection to exploration —
  "you're knowingly making -EV trades to learn" — costs a live desk real money. We have **no capital at
  risk**, so the only cost is a slightly noisier shadow NAV. That asymmetry makes a *higher* exploration
  budget rational for Mastermind than for a real fund. ε=0.05 is conservative; 0.10 is defensible.
- **Floor-weighted:** an explored buy enters at the new-position floor (~0.5%), so even a wrong
  exploration is a rounding error on the book.

## Wire-in (one point, flag-gated, default OFF)

The mechanism is already staged: `rejections._explore_eps()` reads `MASTERMIND_SELECTION_EXPLORE`
(default `0`) and `MASTERMIND_EXPLORE_EPS` (default `0.05`), and logs the correct propensity per row.
To **arm** it, the only remaining wire-in is in the Flagship selection path (`bot/phase2.run`, the
`research_held` / committee-drop branch): when `rejections._explore_enabled()` and the name is in the
borderline band, with prob ε *promote* it back into `confirmed_sized` at the floor weight and tag the
DecisionDoc `explored=True`. Until that flag is set, behavior is byte-identical to today and every
rejection logs `propensity=0.0, policy="deterministic"`.

## The decision I need from you

1. **Arm exploration?** Yes (Option B) / No (stay log-only).
2. If yes: **ε = 0.05** (conservative) or **0.10** (faster OPE convergence, still tiny under paper-only)?
3. **Borderline band width** — how close to the buy threshold a soft-reject must be to be eligible
   (e.g. `combined` within 5 points of the confirm bar). Default proposal: the research-confirm margin.

Once you answer, arming is a ~15-line flag-gated change to the `phase2` selection branch + a test. The
logging + veto-regret read ships now regardless.

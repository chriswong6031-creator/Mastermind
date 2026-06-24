# Flagship Desk — Deploy & Activation Checklist

The desk (P1 buy-side judgment + P2 veto/exit discipline + P3 accountability loop + observability)
is **built, tested (~204 tests green), and dry-run-proven**, but ships **entirely flag-OFF and inert**.
Activation is deliberate and reversible. Nothing changes live until a restart + the flags below.

> The live server is uvicorn :8000 from the MAIN checkout; code changes take effect only on **restart**.
> Every flag defaults OFF → the engine/Brain paths are **byte-identical** to today until you opt in.

---

## Step 0 — Deploy the always-on instrumentation (no flag; needs restart)
These run automatically once the new scheduler is live — they are the **precondition** for the loop:
- **Daily mark-to-market** (`_daily_mark_job`, 22:35 UTC Mon–Fri): re-marks every book so NAV advances
  daily and decisions become gradable. *Without this, nothing in the loop ever learns.*
- **4% cash sweep** (`accrue_cash_yield`, inside the mark job): idle cash earns ~4%/yr (tune with
  `CASH_YIELD_ANNUAL`).

**Restart the server**, then sanity-check after the next 22:35 UTC run:
- `data/portfolio/nav_history.jsonl` (and each `data/portfolios/<book>/nav_history.jsonl`) gained a
  fresh dated row.
- `account.json` cash grew by ~`cash × 0.04/252`.

## Step 1 — Turn on the buy-side judgment (Flagship)
```
export MASTERMIND_FLAGSHIP_JUDGMENT=1     # Macro Strategist + PM-Conviction build the book
export MASTERMIND_GATE_OFFICER=1          # portfolio-level veto → watchlist
export MASTERMIND_RISK_OFFICER=1          # daily judgment exits over held names
export MASTERMIND_TIMING_GATE=1           # deterministic entry-timing gate (Arm-A-backed)
```
Restart. Sanity-check after the nightly Flagship build:
- `data/committee/<asof>/_FLAGSHIP/strategist.json` exists (themes + backdrop).
- the Flagship book reflects the PM's target (single-name-led; ETFs trimmed by the Gate).
- `data/gate_officer/<asof>/decisions.json` + `data/risk_officer/<asof>/decisions.json` written.
- the **Desk** dashboard tab (`/desk`) renders today's themes, decisions, watchlist, scorecard.

## Step 2 — Turn on the learning loop (AFTER ~2–3 weeks of graded data)
The graders are **cold/inert until effective-n ≥ MIN_N=12** (the universe-fed seats via fast-arm arm
in weeks; owned-book seats slower). Only then do these do anything:
```
export MASTERMIND_SELF_MIRROR=1           # each seat/brain sees its own graded track record
export MASTERMIND_REPUTATION_WEIGHTING=1  # well-calibrated seats earn more (regime-conditional, capped)
```
Both are also internally gated on MIN_N, so enabling them early is harmless (no-op until data exists).
The CIO weekly note (`data/brain/cio/<week>.md`) starts populating as soon as Step 0 data accrues.

## Brain books (US / CN / HK / Heavyweight)
They get the cash sweep + cash-aware personas automatically (Step 0 + restart). `MASTERMIND_SELF_MIRROR`
also gives each brain its own track-record mirror once its book has ≥MIN_N graded picks.

---

## Rollback
Unset any flag + restart → that layer is byte-identical to before. The marks/cash sweep are additive
(never trade); to disable the sweep set `CASH_YIELD_ANNUAL=0`.

## ⚠️ Cost caution (not yet automated — see roadmap #3)
The desk runs several Opus seats per night (Strategist, armed PM ~$1+, Gate, Risk, per-name committee,
weekly CIO) across books. There is **no nightly $ budget cap yet** — watch `cost_usd` per run after
enabling Step 1, and consider lowering `*_MAX_TURNS` / the Opus tiers if spend outpaces the book's
realistic active return. A hard per-book budget tripwire is the next item to build before running daily
at scale.

## What is NOT yet built (post-merge roadmap)
Cost tripwire (#3); watchlist daily re-review/promotion loop (#4); firm-level cross-book exposure
monitor (#9). See the chat roadmap. The reputation/attribution modules exist but only SENTINEL's vote
is influence-weighted so far (Gate/Risk influence wiring is a drop-in follow-up).

Worktree removed cleanly. All computations done at the unit level (never phase2.run()). Here is the counterfactual replay.

---

# F2 — COUNTERFACTUAL REPLAY through the FIXED stack (master + W4)

All figures computed at the **unit level** against today's live inputs (regime `date=2026-07-01`, cycles `asOf=2026-07-02`, books `as_of=2026-07-02`). Throwaway worktree `/tmp/mm_incident/master_cf` @ master `6f8b0c0` removed. Nothing was written; `phase2.run()` never called.

## Per-item results

**(1) `regime_frame.budget()` — lead_budget 0.44905 vs old hardwired 0.50**
```
lead_budget = clamp(0.40 + 0.20 · 0.327 · T · F, 0.40, 0.60) = 0.44905
  T = 1.0  (transition STABLE → NO shrink)   F = 0.75  (flip_margin 0.05 < 0.15 → fragility damp fires)
```
Modest −5.1pp haircut vs the old 0.50 hardwire. **The fragility damp (F) does the work; the transition term (T) does NOT** — STABLE means the calm-tape multiplier is 1.0. (regime_frame.py:475-476, live output confirmed.)

**(2) `cycles()` — Technology entry-blocked; healthcare/utilities entry-favored**
cycles() is FRESH (asOf 2026-07-02, age 0 trading days — passes the 5-day gate):
| ETF | phase / label | pos | osc_slope | late_cycle | entry_favored |
|---|---|---|---|---|---|
| **XLK** | Peak / Topping | 80.8 | −18.4 | **True** | **False** |
| **XLV** | Expansion / Trending | 63.8 | +20.6 | False | **True** |
| **XLU** | Trough / Bottoming | 16.2 | +7.7 | False | **True** |

XLK `late_cycle=True` → any NEW semis leadership leg is halved. **No semis name would SEED** as a new leg (SMH/XLK map to XLK via `_leg_sector`). Healthcare (XLV) and Utilities (XLU) are `entry_favored=True` — exactly the user's real posture. (regime_frame.py:381-394; sleeves.py:195-210.)

**(3) `apply_leadership_caps` on live leadership legs** (SMH pct_vs_200d = **46.23%** from `etf_board.etf_trend`, > 40% cap):
| Book | Leg | from → to | binding brake |
|---|---|---|---|
| autonomous | SMH | 0.1621 → **0.0800** | overextension (even HELD — extension clamp is not held-exempt) |
| etf | QQQ, SMH | 0.0806→0.0403, 0.0196→0.0098 | late_cycle (new-leg halving) |
| heavyweight | SMH, XLK | 0.1499→0.0800, 0.13→0.065 | overextension (SMH), late_cycle (XLK) |

**(4) `enforce_book_caps` (name 0.08 / broad-index 0.15 / cluster semis_ai 0.35) on ACTUAL holdings:**
| Book | NAV | book-cap trim ($) |
|---|---|---|
| autonomous | $984,492 | **$103,765** (SMH −$80.8k, EME/URI/APH to 0.08) |
| etf | $980,029 | **$156,609** (SPY 0.27→0.15 −$117.4k; SGOV 0.12→0.08 −$39.2k*) |
| heavyweight | $988,099 | **$255,325** (all 7 megacap/semis names to 0.08) |

*Caveat/what it still gets wrong: SGOV (T-bills, cash-equivalent) is NOT on the broad-index allowlist, so the name cap wrongly trims it to 0.08 — a false positive that de-risks a book by cutting its **ballast**. Flag for W5.*

**(4b) `firm_exposure.clamp_book` (firm caps: name 0.10 / cluster semis_ai 0.30, default-ON):** Firm SMH name pile-up = 0.1621 + 0.0196 + 0.1499 = **0.3316** vs 0.10 cap; heavyweight semis_ai cluster alone = 0.3995 vs 0.30. Run post-hoc with peers = the *already-published* books, the clamp zeroes each book's SMH/semis contribution (autonomous −$210k, etf −$381k, heavyweight −$503k = **$1.09M**).
**Methodological caveat (important):** the clamp is designed to run *inside each book's finalize before publishing*. Running it against peers that already carry the full pile-up **double-counts** — in production the four finalizes run in sequence and the aggregate settles near the caps, not to zero per book. Treat the $1.09M as an **upper bound**; the *honest* firm-cap-implied reduction is bringing firm-SMH from 0.3316→0.10 (≈ −$220k of SMH across the firm) and firm semis_ai to 0.30.

**(5) `bot/derisk.py` eff_cap with today's severity-2 tripwire — the WEAK lever:**
`eff_cap = min(state_gross_cap 1.0, severity_cap 0.70) = 0.70`. This is the W2 fix (BUG-A: was `min` of state-only → 1.0 → the "hold" no-op). Applied to today's gross:
| Book | gross | vs eff_cap 0.70 | fixed-stack cut | ACTUAL |
|---|---|---|---|---|
| autonomous | 0.644 | under | **none** | hold/bought more |
| etf | 0.7108 | over | −0.0108 = **−$10.6k** | re-risked (sold SGOV→SPY) |
| heavyweight | 0.8984 | over | −0.1984 = **−$196k** | sat |
| flagship | 0.2478 | under | **none** | hold (3rd day) |

**What the gross-cap derisk STILL gets wrong:** it caps *gross*, not *composition*. It does NOTHING to autonomous or flagship (both already < 0.70) and never targets the SMH concentration. The real teeth today are items (3)/(4), not (5).

**(6) W0 sell path — flagship's queued rotation WOULD execute (sells first):** Confirmed at code level. Master rewrote the market-closed branch to `queue_orders(dict(_tw), ..., nav_base=None, fill_after=_next_open)` queuing the FULL rebalance, and `fill_pending` settles **PHASE 1 sells → PHASE 2 cash-bounded buys** (paper_account.py:842-899). Production `fix/bot-orphans-arming` still carries the literal `# market closed → queue buys ... No sells.` with `nav_base=_STARTING_NAV` (bot/phase2.py:976-978) — the exact structural freeze. So on master flagship's queued rotation executes; on prod it cannot sell.

**(7) W4 DEF_SLEEVE @ DEF_SLEEVE_MAX=0.35 — throttled by the same regressed reads:**
- `defensive_candidates.candidates()` returns **7 names TODAY**: XLP, XLV, USMV, SGOV (playbook broad_derisk) + XLC, XLY, XLU (cycles Trough/Recovery). `us_standouts gate_go=False` excludes 31 single-names (only sector-ETF/playbook sources fire). **Healthcare (XLV) and staples (XLP) are both in the pool** — matching the user's real book.
- `build_def_sleeve` fragility_signal = **0.2019**, so `def_budget = 0.35 × 0.2019 = 0.0707` (7.1% of NAV), fully funded by the 0.3151 freed leadership headroom → 7 legs at ~1.0% each.
- **What W4 still gets wrong:** the signal is crippled by today's regressed inputs. `dwell_level(risk_on)=0.0` and STABLE→no weakening bump, so the *only* term firing is `w_conf·(1−0.327)=0.20`. The defensive rotation is throttled to 7% by the very same risk_on/STABLE reads that are the incident's root cause. A correct WEAKENING+caution read would give signal ≈ 0.5·0.5 + 0.20 + 0.2 = ~0.65 → def_budget ~0.23.

## Consolidated counterfactual table (price assumption: today's marks; SMH @ $605.70)

| Book | NAV | ACTUAL today | FIXED-stack action | $ de-risked (fixed) |
|---|---|---|---|---|
| **autonomous** | $984k | gross 0.60; **BOUGHT +$24.8k SMH** (41 sh); SMH 16.2% | SMH→8% ext-clamp (−$80.8k) + EME/URI/APH→8%; firm-SMH blocks the +$24.8k buy entirely | **book-cap −$104k**, and the SMH add is **rejected** |
| **etf** | $980k | gross 0.71; **sold SGOV→SPY/RSP/XLI (re-risked)** | SPY→15% (−$117k), semis-cluster halved; derisk −$10.6k; SGOV re-risk **would not fire** (fixed stack de-grosses, not re-risks) | **book-cap −$157k** + derisk −$10.6k |
| **flagship (prod)** | paper | gross 0.25; **HOLD 3rd day, cannot sell** (pre-W0) | W0 sell path EXECUTES queued rotation (sells-first); eff_cap 0.70 (book already under) | rotation **unfreezes** (no gross cut needed today) |
| **heavyweight** | $988k | gross 0.90; **SAT** | SMH/XLK ext+late-cycle caps + all names→8% (−$255k) + derisk to 0.70 (−$196k) | **−$255k book-cap** (derisk −$196k mostly overlaps) |
| **DEF_SLEEVE (W4)** | — | none exists | +7 defensive legs (XLP/XLV/USMV/SGOV/XLC/XLY/XLU) @ 7.1% total | rotates **into** the user's defensives |

**Bottom line.** The fixed stack's decisive levers today are the **position-SIZE brakes (items 3+4)**, not the gross-cap tripwire (item 5) — the extension clamp (SMH +46% vs 200d) and firm SMH/semis caps force the concentrated semis line down across every book and **reject autonomous's +$24.8k SMH add**, while cycles() blocks any new semis seed and favors XLV/XLU. The W0 sell path unfreezes production flagship. **But the fixed stack still under-reacts in two documented ways:** (a) transition=STABLE means budget-T=1.0 and the W4 fragility_signal is throttled to 0.20 (def sleeve only 7%), so the *directional* regime read regressing risk_on/STABLE still suppresses the defensive response — the size caps bite mechanically, the regime-conditioned levers stay soft; and (b) the SGOV name-cap false-positive trims cash-ballast. Net fixed-stack de-risk across the three real books ≈ **$0.4–0.5M of forced trims** (book-cap dominated), plus a rejected $24.8k SMH buy, plus an unfrozen flagship rotation — versus the ~$0 actually cut and the SMH/SPY *adds* that actually happened.
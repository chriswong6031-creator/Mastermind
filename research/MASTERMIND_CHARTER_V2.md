# MASTERMIND CHARTER v2 — the constitution of the rebuild

**Authority:** Fable, program owner as of 2026-07-02 (user grant: sweeping changes across Mastermind and Macro Dashboard).
**Relationship to DOCTRINE.md:** DOCTRINE.md remains the tactical operating doctrine (sleeves, stages, detectors). This charter sits above it. Where they conflict, the charter wins and DOCTRINE.md gets amended.
**Enforcement:** every wave's integrator audits against these principles; violations are ship-blockers. The principles are numbered so code comments, runlogs, and reviews can cite them (`# charter P2`).

---

## The ten principles

**P1 — Nothing trades on a single plane of evidence.**
Every allocation decision must cite at least two independent evidence planes (price-action, regime, cycles, flows, risk, credit/liquidity). The 2026-07-02 incident — a whole firm eating one wrong Goldilocks label — must be structurally impossible. Corollary: the regime label is an *input to perception*, never perception itself.

**P2 — Wrong data shrinks, never flips.**
Missing, stale, or contradicted data may coarsen identity, freeze the book, or shrink size. It may never un-cap, raise authority, or flip direction. (Constitutionalized from the v2 architecture invariant; already enforced in W0–W4 code.)

**P3 — Every signal earns its authority.**
The ladder is: advisory → shadow-graded → walk-forward-validated → sizing input. No signal touches size without a pre-registered falsifier and a passed gate. Refuted signals go on the public do-not-build list and stay there (cycle-phase exit veto; generic exit rules). A signal that fails its gate ships honestly labeled advisory — never quietly promoted.

**P4 — The book must always be able to move.**
No position without a working exit path. Cash and defensives are first-class positions, not residue. Rotation capacity (cash ≥ floor, no cluster > cap) is a hard constraint checked every build. A frozen book is a system failure, not a market opinion.

**P5 — Perception before position.**
The daily cycle begins by assembling the full market view — every plane, freshness-stamped, confidence-tagged — and deciding *posture* (offense/defense/cash mix, risk budget). Only then are names selected, within posture. A bot that picks names first and asks "how risky is the world?" second is a momentum funnel with extra steps.

**P6 — Every mistake becomes machinery.**
Incidents become executable replay fixtures the stack must pass forever. Graded calls feed calibration. The benchmark that beats us (the user's defensive book) is a standing, named input to every seat's incentives. A lesson that lives only in a document is not learned.

**P7 — One source of truth per concept.**
One regime frame, one cluster identity, one defensive pool, one marking layer, one benchmark ledger, one market view. The five duplicated `_regime_dict` copies that let confidence-blindness persist are the cautionary tale. Duplication is drift; drift is death.

**P8 — Autonomy is earned in shadow.**
Any new authority — a seat, a signal, a book, a lever — runs in shadow against a pre-committed bogey with a pre-committed promotion rule and an automatic demotion rule. The judgment book's promotion gate is the template. Nothing self-promotes; nothing armed is unwatched (armory report).

**P9 — The firm is a portfolio of orthogonal experiments.**
Books must differ on a nameable axis (systematic-braked / free-form / concentrated / regional / defensive-benchmark) or be killed. Cross-book correlation of active returns is measured; a book that is a noisy mirror of another is dead weight with extra API costs.

**P10 — Deployment is part of the system.**
Code that isn't running protects nothing. Master ahead of production >24h triggers an alarm (deploy-lag tripwire). Every wave lands with its replay battery green and its arming state visible in the armory artifact. The 4-day gap between the fixes existing and the fixes running cost real capital — never again.

---

## The standing self-interrogation (run at every wave close — the user's questions, made procedure)

1. Is the bot powerful enough to perform highly *autonomously* — with no human catching its mistakes?
2. Would it still make the last incident's class of mistake? Prove it with the replay battery, not opinion.
3. Does it have enough visibility (what fraction of the published signal surface does it perceive)? Can it *act* on that visibility (does perception reach sizing)?
4. What is the single highest-leverage enrichment remaining? Schedule it.

Answers get written into the masterplan status log each wave. Unanswered = wave not closed.

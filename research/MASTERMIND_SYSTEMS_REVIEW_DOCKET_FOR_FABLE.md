# Mastermind Systems Review Docket for Fable

Prepared: 2026-07-05, America/Vancouver workspace context
Audience: Fable
Status: research docket, not an implementation plan or trading recommendation

## Executive Thesis

Mastermind has moved past its first existential architecture failure: it now has a charter, a risk spine, book-specific mandates, deterministic sizing/brake layers, shadow gates, benchmark ledgers, posture governors, and a maintenance loop. The next risk is subtler. The system has many correct parts, but it is still closer to a collection of bounded bots plus ledgers than to a controlled autonomous investment organization.

The sharpest upgrade is not another signal. It is a control plane.

Mastermind needs one authoritative layer that knows:

- which books exist and why they are allowed to exist,
- which signals and Neural Web artifacts are permitted to influence each decision,
- which failures stop a run versus merely shrink or annotate it,
- which experiments are mature because evidence arrived, not because a calendar date passed,
- which book has earned more capital, less capital, or no capital,
- which human/Fable approvals changed system authority,
- which public/operator endpoints are safe to expose,
- and which autonomous decisions were rejected, overridden, or later falsified.

The current architecture has strong doctrine but weak institutional plumbing. It can reason, gate, shrink, and journal, but it does not yet have a full operating system for itself.

## Scope Boundary

This docket intentionally does not re-litigate the already covered W0-W7 work in `MASTERMIND_FIX_MASTERPLAN.md` and `MASTERMIND_PROBLEM_AUDIT_FOR_FABLE.md`: fail-closed macro ingestion, risk spine, firebreaks, defensive candidate generation, perception layer, learning design, posture shadowing, and book mandate expansion. Those were necessary repairs.

This review focuses on second-order architecture:

- autonomous trading-bot organization design,
- multi-portfolio ecology,
- scheduler/run governance,
- Neural Web integration contracts,
- model and LLM authority,
- security and public/control-plane separation,
- learning-loop maturity,
- production operations,
- and the path from "paper-only but autonomous" toward an institution-grade system.

## Current System Map

Mastermind currently resembles a multi-book paper-equity desk:

- A FastAPI app presents portfolio/account surfaces, operator endpoints, and public dashboard surfaces.
- The scheduler runs multiple daily/weekly jobs, including macro refresh, daily marks, Flagship, Autonomous, ETF, Heavyweight, China, Hong Kong, weekly CIO, weekly improvement agenda, experiment maturity checks, and watch/derisk jobs.
- Seven portfolios are registered: `flagship`, `heavyweight`, `autonomous`, `etf`, `china`, `hk`, and `self_directed`.
- The Brain seats differ by mandate. Some seats are tightly gated by Mastermind doctrine; the Autonomous Opus Brain is deliberately more free-form, with deterministic clamps applied after its proposal.
- The deterministic layer owns marks, cash/no-leverage checks, sizing clamps, pending order queues, settlement, some firm exposure controls, benchmark ledgers, posture governance, book lifecycle recommendations, and experiment registry state.
- Macro Dashboard and Neural Web produce a growing signal-bus and governance ecosystem, with `config/synapse.yml`, stamped artifacts, authority levels, tiers, freshness metadata, and a constitutional ban on model-originated trade signals.
- Mastermind already consumes Macro/Neural Web-adjacent artifacts through vendored files, local readers, market views, breadth engines, and explicit signal paths, but the contract is not yet unified.

The architecture is strongest when a deterministic spine interprets bounded evidence and weakest where many small fail-open behaviors compose into an invisible operating state.

## High-Severity Findings

### 1. Mastermind lacks a single control plane

There is no one layer that serves as the firm operating system. Book registry, scheduler, experiments, ledgers, Brain prompts, dashboard endpoints, macro ingestion, firm exposure, posture governance, and deployment checks each own fragments of authority.

This creates a hidden failure mode: every subsystem can be locally conservative while the aggregate system becomes unclear, stale, or contradictory.

Required upgrade:

- Create a `control_plane` concept that owns run IDs, book authority, active config intent, enabled gates, hard-stop conditions, operator actions, Fable approvals, and current production health.
- Every autonomous run should produce a control-plane record before and after execution.
- Every authority-changing event should be logged with actor, reason, source artifact, before/after state, and rollback path.

Fable question:

- Should Mastermind adopt Neural Web's governance-ledger style directly, or keep a separate Mastermind authority ledger that can later sync to Neural Web?

### 2. Broad fail-soft handling is overused

A quick static count found 1,213 broad `except Exception` catches across `brain`, `bot`, `portfolio`, `app`, `data_layer`, `bridge`, `loop`, and `scripts`. Many are intentional: a dashboard should render, a telemetry pane should not crash the app, and missing optional context should degrade. But the same pattern is dangerous around execution, clamps, caps, auth, scheduler state, and inter-book controls.

The doctrine says missing/stale/wrong data may freeze, shrink, or coarsen, but never raise authority. The implementation needs a stronger mechanical distinction between harmless observability failures and guardrail failures.

Required upgrade:

- Introduce a typed severity taxonomy:
  - `HARD_STOP`: cannot place/queue/settle/size/arm.
  - `FREEZE`: no new risk, existing book maintained or de-risked.
  - `SHRINK`: proposals allowed only after deterministic exposure reduction.
  - `ADVISORY_ONLY`: dashboard or Brain context only.
  - `TELEMETRY_ONLY`: no decision effect.
- Replace silent guardrail exceptions with `GuardrailResult` or equivalent status objects.
- Persist all non-telemetry failures to a run ledger and dashboard health pane.
- Add tests that verify failures in caps, marks, auth, run locks, and peer-book files cannot result in higher authority or greater exposure.

Fable question:

- Which current exception classes are allowed to be invisible, and which must become run-blocking?

### 3. Experiment maturity is still calendar-shaped, not evidence-shaped

The agenda on 2026-07-05 shows mostly Fable-review items and no self-tunable or Opus-session agenda items. Several high-impact experiments are open because they require Fable review, effective sample size, 40 trims, or longer history. The registry schema contains useful maturity conditions, but current automatic maturation is primarily date-driven.

This means condition-only experiments can rot. The system can look disciplined while actual evidence thresholds are not being evaluated automatically.

Required upgrade:

- Build a condition evaluator for `maturity_condition`, not only `comeback_date`.
- Emit "blocked by missing evidence" versus "not old enough" versus "ready for Fable" as separate states.
- Create an expected-time-to-decision dashboard for each open experiment.
- Add automatic warnings for experiments with no comeback date and no evaluable condition.

Fable question:

- Are experiment gates part of Mastermind's native governance system, or should they be migrated into Neural Web's authority ladder/governance ledger?

### 4. Multi-portfolio design is not yet a portfolio-of-portfolios

The system has multiple books, but the organization layer does not yet act like a firm allocator. It knows book identities and some mandates, but it does not yet continuously allocate capital across books based on mandate compliance, active return, drawdown, correlation, capacity, and evidence quality.

The result is a desk of books, not yet a firm.

Required upgrade:

- Add a shadow `Firm Allocator` that produces book-level capital weights, risk budgets, and review flags.
- Measure active return and drawdown by book against the correct benchmark.
- Track cross-book factor, theme, and ticker overlap.
- Introduce book lifecycle actions: incubate, observe, promote, cap, shrink, quarantine, retire.
- Require every book to publish a mandate-compliance packet after each run.

Fable question:

- Which book is the house benchmark, which is the experimental alpha book, and which books are allowed to influence each other?

### 5. Self-Directed may be contaminating the Heavyweight benchmark relationship

Current Heavyweight code can source a firm-union universe from `flagship`, `autonomous`, `etf`, and `self_directed`. At the same time, Self-Directed is described elsewhere as a defensive yardstick/book comparator rather than a normal alpha source.

If Heavyweight can select names from Self-Directed, but Self-Directed also serves as a comparison target, the system risks leaking benchmark holdings into the evaluated book. This may be intentional, but it should be explicit because it changes what Heavyweight performance means.

Required upgrade:

- Fable should choose one of three policies:
  - Self-Directed is benchmark-only and can never seed Heavyweight.
  - Self-Directed can seed Heavyweight, but Heavyweight is no longer judged cleanly against it.
  - Self-Directed can seed only in a separately labeled "inspired by yardstick" shadow cohort.
- Update Heavyweight metadata and docstrings; some comments still describe Heavyweight as Flagship-only even though the implementation moved toward firm-union sourcing.

Fable question:

- Is Self-Directed an investable idea source, or a yardstick that must stay uncontaminated?

### 6. Neural Web and Mastermind have two partially overlapping constitutions

Mastermind has Charter V2 and doctrine. Neural Web has an authority ladder, provenance envelopes, a governance ledger, tier states, freshness metadata, and an origination ban. Both systems are converging on the same philosophical shape, but the enforcement boundary is not yet unified.

This is a risk because Mastermind can still rely on environment flags and local registry state while Neural Web is moving toward explicit authority transitions and config-based intent.

Required upgrade:

- Map Neural Web authority to Mastermind actions:
  - A0: display only.
  - A1: explanation and dashboards.
  - A2: attention routing and review queues.
  - A3: de-escalation, abstention, and tighter risk.
  - A4: quarantine/freeze proposals.
  - A5: governance/tier edits.
  - A6: bounded config proposals or pre-approved auto-apply.
  - A7: forbidden for trade origination.
- Ban invisible authority upgrades via raw environment flags.
- Require all arming/disarming/promotion/self-tune/book-lifecycle decisions to emit governance events.

Fable question:

- Should Mastermind inherit Neural Web's arming-predicate doctrine now, before deeper integration?

### 7. The signal interface is file-rich but contract-thin

Neural Web's signal bus lists artifacts, freshness, tiers, consumers, and external consumers. Mastermind still has many local readers and vendored paths. This makes integration possible but fragile: a path can exist, a file can be stale, and a Brain can still receive a stale or low-authority interpretation unless the consuming side enforces the contract.

Required upgrade:

- Add `mastermind/contracts.yml` or generated `signal_contracts.py` from the Neural Web signal bus.
- For every consumed external artifact, require:
  - owner,
  - schema/version,
  - freshness budget,
  - authority tier,
  - allowed decision effect,
  - fallback behavior,
  - and degradation class.
- Forbid new raw vendored reads outside typed adapter modules.
- Send Mastermind outcomes back to Neural Web: accepted decisions, rejected decisions, overridden Brain recommendations, realized PnL, drawdown, gate failures, and falsified theses.

Fable question:

- Should Mastermind consume Neural Web through one official adapter only, or allow domain-specific readers with generated contracts?

### 8. Autonomous Brain is tool-bounded but not fully packet-bounded

The autonomous Brain's filesystem access is narrowed, raw reads are removed, and final submissions are deterministically clamped. That is good. The remaining gap is that a proposal can still be semantically under-specified: not every autonomous decision is forced into a complete evidence/falsifier/portfolio-impact packet before the deterministic layer evaluates it.

Required upgrade:

- Require a standardized `DecisionPacket` for every Brain book:
  - mandate,
  - desired holdings,
  - evidence planes used,
  - source provenance,
  - explicit falsifiers,
  - liquidity/capacity notes,
  - portfolio delta,
  - expected failure mode,
  - and whether the proposal increases, preserves, or reduces risk.
- Reject incomplete packets before sizing.
- Store rejected packets as learning data.
- Let free-form reasoning stay free-form inside the packet, but make the external surface deterministic.

Fable question:

- Can the Autonomous Opus Brain remain "no doctrine" internally while still being required to satisfy Mastermind doctrine at the submission boundary?

### 9. Scheduler/run orchestration is too implicit for a multi-book autonomous desk

The scheduler has many jobs with different market clocks and books. Some manual and first-run paths can spawn background work. This is workable for a paper bot, but the run state should be durable, queryable, and lock-aware.

Required upgrade:

- Add a durable run table or append-only run ledger:
  - run ID,
  - job name,
  - book,
  - trigger type,
  - started/finished timestamps,
  - git/code version,
  - input artifact hashes,
  - status,
  - failure severity,
  - queued orders,
  - and output artifacts.
- Add one lock namespace per book and one global lock for shared operations.
- Make overlapping runs explicit: allow, block, queue, or cancel.
- Add retry policy and partial-output policy by job type.
- Publish scheduler health to the dashboard and the maintenance loop.

Fable question:

- Which jobs can overlap safely, and which jobs must be globally serialized?

### 10. Production topology and security need a stronger public/operator split

Mastermind has account plumbing and optional password auth. Prior deployment notes indicate a public bot mirror and disabled login in some contexts. For an autonomous LLM paper-trading system with token-spend and operator endpoints, optional auth is not a good default.

Required upgrade:

- Separate public dashboard routes from operator/control routes.
- Make auth mandatory for operator routes in production.
- Add rate limits to LLM-triggering endpoints.
- Add a startup health check that refuses production operator mode if auth is disabled.
- Publish a read-only public artifact bundle instead of exposing live control surfaces.
- Add a deployment provenance banner: code version, data snapshot time, and "paper only" status.

Fable question:

- Is the public surface meant to be a read-only mirror, or an authenticated operator application?

### 11. Learning loops have good doctrine but insufficient observation mass

The learning design is thoughtful: journals, improvement agenda, experiment registry, shadow gates, and Fable boundaries. But many evaluators are still sample-starved. The benchmark series is very young, posture governor needs effective sample sizes, and several experiments need long windows before judgment.

Required upgrade:

- Backfill where legitimate:
  - marks,
  - benchmark ledgers,
  - shadow decisions,
  - rejected Brain proposals,
  - gate outcomes,
  - and book-level active returns.
- Keep backfills explicitly labeled so they do not masquerade as live history.
- Add "time until evaluable" to every gate.
- Distinguish paper-PnL learning from replay learning and thesis-falsification learning.
- Use synthetic failure-injection replays for controls, not for alpha claims.

Fable question:

- Which gates are allowed to use replay/backfill evidence, and which require forward-only live paper history?

### 12. Marks and benchmarks are centralizing, but regional truth is still coarse

The marks layer is a major improvement: it avoids using average cost as a mark and carries last-good prices instead of fabricating precision. But regional books still need sharper benchmark and FX semantics. For example, China and Hong Kong evaluation cannot rely forever on rough proxies if those books are meant to earn authority.

Required upgrade:

- Define canonical benchmark per book.
- Add FX treatment rules for non-USD exposures.
- Track source quality per mark.
- Make benchmark degradation explicit in lifecycle and book allocator decisions.

Fable question:

- What is the minimum acceptable benchmark quality before a regional book can be promoted, capped, or judged?

### 13. Firm exposure controls depend on peer-book availability

Firm exposure logic has become more binding than old comments imply, but missing peer-book data can still turn parts of the clamp into effectively infinite headroom. This is reasonable if intentional, but dangerous if peer-book absence is a deployment or scheduler failure.

Required upgrade:

- Distinguish "no peer exposure exists" from "peer exposure data missing."
- Make missing peer-book files a severity-coded condition.
- In production, missing peer data should usually shrink or freeze, not silently uncap.
- Update stale docstrings so maintainers know which firm controls are actually binding.

Fable question:

- Should firm exposure caps be fail-closed when peer-book state is unavailable?

### 14. Documentation drift is becoming an operational risk

Some comments and metadata are behind implementation reality: Heavyweight is no longer simply Flagship's best ideas; firm exposure is no longer toothless; environment flag defaults in code comments can differ from production. Drift is normal, but in an autonomous system drift is a control failure because humans and LLMs both read docs as policy.

Required upgrade:

- Add a weekly doc-drift check to loop maintenance.
- Generate a "live architecture snapshot" from code and config.
- Require architecture docs to cite generated snapshots for jobs, books, gates, and enabled flags.

Fable question:

- Which documents are policy, which are history, and which are commentary?

## Neural Web Integration Proposal

Neural Web should not become Mastermind's trading brain. It should become Mastermind's perception, governance, provenance, and experiment substrate.

### Interface 1: Perception Contract

Mastermind consumes Neural Web artifacts only through typed contracts. Each artifact arrives with authority, freshness, owner, schema, and allowed effect. A low-authority artifact can explain or route attention; it cannot change risk.

### Interface 2: Governance Contract

Every Mastermind authority change becomes a governance event:

- gate armed,
- gate disarmed,
- signal promoted,
- signal demoted,
- self-tune parameter armed,
- posture decider armed,
- book promoted,
- book shrunk,
- book quarantined,
- Fable override,
- and production flag/config change.

### Interface 3: Feedback Contract

Mastermind sends Neural Web the outcomes it needs to learn:

- Brain recommendations,
- deterministic approvals/rejections,
- risk clamps triggered,
- realized marks,
- book active returns,
- thesis failures,
- false positives,
- false negatives,
- and "abstain was right/wrong" cases.

### Interface 4: Experiment Contract

The experiment registry becomes evidence-driven. Neural Web can help route, observe, and evaluate; Mastermind keeps final trading authority bounded by charter.

## Proposed Upgrade Program

### M0: System Census

Goal: create a generated map of the live system.

Deliverables:

- job graph,
- book graph,
- signal-consumer graph,
- endpoint classification,
- broad-exception audit,
- state/artifact ownership map,
- production config intent snapshot,
- and stale-doc mismatch list.

Acceptance:

- Fable can answer "what is allowed to influence what?" from one generated report.

### M1: Control Plane Hardening

Goal: make every run and authority state explicit.

Deliverables:

- run ledger,
- severity taxonomy,
- guardrail result objects,
- lock model,
- scheduler health,
- production auth checks,
- and failure dashboard.

Acceptance:

- A missing mark file, missing peer exposure file, failed cap, failed auth config, or overlapping run cannot silently result in greater risk.

### M2: Neural Web Signal Contract Bridge

Goal: replace path-based integration with contract-based integration.

Deliverables:

- Mastermind signal contract file generated from or reconciled with Neural Web signal bus,
- typed adapters,
- freshness tests,
- authority-to-action mapping,
- and contract breach reporting.

Acceptance:

- A signal's decision effect is known before any Brain sees it.

### M3: Portfolio-of-Portfolios Allocator

Goal: turn books into a managed firm ecology.

Deliverables:

- shadow book-capital weights,
- active-return ledger,
- cross-book overlap/correlation,
- mandate compliance packets,
- book lifecycle dashboard,
- and Self-Directed contamination policy.

Acceptance:

- Fable can decide whether a book deserves more capital, less capital, quarantine, or retirement based on evidence rather than narrative.

### M4: Decision Packet Protocol

Goal: standardize the submission boundary for every Brain.

Deliverables:

- `DecisionPacket` schema,
- validator,
- rejection ledger,
- evidence/falsifier fields,
- portfolio-impact calculation,
- and source provenance.

Acceptance:

- No LLM output can reach sizing without a complete, inspectable packet.

### M5: Evidence-Driven Experiment Maturity

Goal: make experiments mature when evidence arrives.

Deliverables:

- condition parser/evaluator,
- effective-n integration,
- forward-only versus replay evidence labels,
- expected-time-to-decision dashboard,
- and stuck-experiment alerts.

Acceptance:

- Condition-only experiments cannot sit open indefinitely without a visible reason.

### M6: Unified Governance Ledger

Goal: reconcile Mastermind and Neural Web authority.

Deliverables:

- shared event vocabulary,
- governance export/import,
- Fable approval records,
- config-intent events,
- and rollback metadata.

Acceptance:

- Every authority change is reviewable as a ledger event.

### M7: Failure-Injection Acceptance Drill

Goal: prove the system fails the right way.

Scenarios:

- stale Macro/Neural Web artifact,
- missing peer-book state,
- corrupted mark,
- disabled production auth,
- overlapping autonomous run,
- Brain submits invalid packet,
- model uses low-authority signal as buy thesis,
- benchmark unavailable,
- Fable-gated experiment tries to auto-arm,
- deployment code/data mismatch.

Acceptance:

- Each scenario has expected severity, dashboard status, run-ledger record, and no unauthorized risk increase.

## External Control Anchors

These are not claims that Mastermind is legally subject to the cited rules. They are useful design anchors for an autonomous trading system:

- SEC Rule 15c3-5 emphasizes documented risk controls, financial exposure limits, regulatory controls, regular review, and certification around market access. See: <https://www.sec.gov/files/rules/final/2010/34-63241-secg.htm>
- SEC Regulation SCI emphasizes written policies/procedures and resilient operation for automated systems central to regulated activity. See: <https://www.sec.gov/rules-regulations/2015/12/regulation-systems-compliance-integrity>
- FINRA algorithmic trading guidance emphasizes cross-disciplinary risk review, development controls, testing/validation, post-change trading-system review, compliance, and supervision. See: <https://www.finra.org/rules-guidance/key-topics/algorithmic-trading> and <https://www.finra.org/rules-guidance/notices/15-09>
- The Federal Reserve/OCC/FDIC revised model-risk guidance issued on 2026-04-17 supersedes SR 11-7 and stresses risk-based model governance, validation, and controls proportionate to model use. See: <https://www.federalreserve.gov/supervisionreg/srletters/SR2602.pdf>
- NIST AI RMF 1.0 frames AI risk management as continuous govern, map, measure, and manage activity; NIST notes the framework is being revised and has 2026 critical-infrastructure profile work. See: <https://www.nist.gov/itl/ai-risk-management-framework> and <https://airc.nist.gov/airmf-resources/airmf/5-sec-core/>

## Fable Review Queue

High-priority decisions:

1. Should Self-Directed be an investable source for Heavyweight, or a clean yardstick?
2. Should Mastermind authority changes move to a governance ledger before deeper Neural Web integration?
3. Which failure classes must hard-stop a run?
4. Should production operator routes refuse startup when auth is disabled?
5. Should experiment maturity be controlled by an evidence evaluator rather than comeback dates?
6. Should all Brain books share one mandatory `DecisionPacket` interface?
7. Should Mastermind consume Neural Web only through generated contracts?
8. Which gates may use replay/backfill evidence and which must be forward-only?
9. Should missing peer-book state fail-closed for firm exposure?
10. Which docs are policy documents that LLMs and operators may treat as authoritative?

## Immediate Low-Risk Fixes

These do not require changing alpha logic:

- Update stale Heavyweight and firm-exposure comments/docstrings.
- Add a system-census script that prints jobs, books, enabled gates, external artifacts, and endpoint classes.
- Add a warning for experiments with no comeback date and no evaluable maturity condition.
- Add scheduler failure records for swallowed exceptions in high-authority jobs.
- Add a production warning or startup refusal when operator auth is disabled.
- Add a generated "what signals can influence trading?" report.
- Add a test that missing peer-book state cannot silently increase firm-level exposure.

## Bottom Line

Mastermind should become less like "several smart bots with conservative rails" and more like a small autonomous investment firm with explicit operating authority.

The next order upgrade is:

1. control plane,
2. contract-based Neural Web integration,
3. evidence-driven experiment maturity,
4. portfolio-of-portfolios governance,
5. packet-bounded LLM autonomy,
6. production public/operator separation,
7. and failure-injection tests.

That program would preserve the current doctrine while making the system more inspectable, less dependent on invisible state, and much harder for a local failure to become a firm-level mistake.

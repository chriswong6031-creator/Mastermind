# MASTERMIND CONTROL PLANE MASTERPLAN — Fable program doc

**Program owner:** Fable (user grant of full ownership, 2026-07-02; this program authorized 2026-07-05).
**Predecessor:** the W0–W7 fix masterplan (`MASTERMIND_FIX_MASTERPLAN.md`) — COMPLETE; W7 runs as a clock.
**Source docket:** `MASTERMIND_SYSTEMS_REVIEW_DOCKET_FOR_FABLE.md` (Codex, 2026-07-05).
**Verification:** all 14 docket findings ground-truthed against master @477af10 by a 25-agent
workflow (14 sonnet verifiers + 7 census lanes + 4 opus adversarial domain reviews) on 2026-07-05.
Verdicts: 7 CONFIRMED, 7 PARTIAL (stale in detail, right in direction), 0 REFUTED.
**Constitution:** MASTERMIND_CHARTER_V2.md governs. This program adds *institutional plumbing* for
principles that already exist as doctrine (P2 shrink-only, P3 earned authority, P7 one source of
truth, P8 shadow-first, P10 deployment-is-the-system). It adds **no new signals and no new alpha**.

---

## 0. What verification changed about the docket

The docket's thesis — build a control plane, not another signal layer — **stands**. But the
evidence re-orders the work:

1. **The live holes outrank the architecture.** The deployed `.env` has **no
   `MASTERMIND_PASSWORD`** — auth is OFF in production right now, and the LLM-triggering
   endpoints (`POST /api/*/run`, `/reason`, `/chat`, `/research`, `/daily?force=1`) share the
   pass-through middleware with dashboard GETs. `/health` leaks filesystem paths unauthenticated
   even when auth is on. The `.env` holds live plaintext secrets (SUPABASE service-role key,
   OAuth token, API keys). None of this waits for a taxonomy.
2. **Several docket asks are already half-built.** Peer-file-vs-empty distinction exists
   (`firm_exposure._peer_exposure` probes `path.exists()`); the clamp is subtract-only by
   invariant, so the *risk-raising* direction of finding 13 is already closed. W6 shipped
   `book_lifecycle.py` (orthogonality matrix, HAC/effective-n probation/retire) and per-book
   benchmark ledgers. An unmerged branch `fable/nw-context-bridge` (39d5c67) already implements
   the M2 typed-adapter first pass. Build on these; don't rebuild them.
3. **New findings the docket missed** (from census + opus review), now in scope:
   - API-fallback model divergence: `brain/client.py` hardcodes deep→`claude-fable-5` while
     `config/agents.yml` resolves deep→opus — a silent 5-book Fable burn if the CLI bridge drops.
   - `portfolio/heavyweight_outcomes.py` source **deleted** but its ledger lives on — Heavyweight's
     accountability grading is frozen/orphaned.
   - `account.router` never registered and imports functions that don't exist in `app/auth.py`.
   - `MASTERMIND_SERVE_ONLY` guard exists **only in unmerged worktrees** — the public VPS mirror
     has no serve-only enforcement from master.
   - `GET /api/self_directed` settles pending orders on read (write-on-GET).
   - `data/posture_governor/` never created; `interim_marks.jsonl` never written.
   - china/hk are **governance orphans**: outside US lifecycle, and `build_regional()` is wired
     but never scheduled — regional benchmark dirs are empty.
   - Self-Directed contamination is sharper than the docket said: SD's published holdings are
     byte-identical to the `DEFENSIVE_BASKET` bogey, and SD is in Heavyweight's sourcing union
     (`_FIRM_UNION_BOOKS`) while excluded from its constraint set (`_FIRM_US_BOOKS`) — it can
     seed the evaluated book but not constrain it.

---

## 1. Rulings (the docket's Fable Review Queue, adjudicated 2026-07-05)

**R1 — Self-Directed is a clean yardstick. It never seeds Heavyweight.**
Remove `self_directed` from `bot/heavyweight.py::_FIRM_UNION_BOOKS`. SD's holdings ARE the
defensive bogey; letting the evaluated concentrator source its own benchmark names makes
`active_vs_defensive` self-referential. Heavyweight may still *hold* XLU/XLV/XLF/XLP if another
published book expresses them — the ban is on the yardstick as a *source*, not on tickers.
Annotate the `hw-firm-universe-ab` registry experiment with the universe amendment date.

**R2 — Mastermind gets its own governance ledger, schema-compatible with Neural Web's.**
Adopt NW's event contract (SHA-256 `event_id`, never-raise append, actor/reason/before-after/
rollback) as a Mastermind-local `data/governance/governance.jsonl`. Do NOT write into NW's ledger
and do NOT adopt NW's ladder wholesale now; a later export/sync is a format exercise because the
vocabularies match. (Answers docket F1's question and Q2.)

**R3 — Severity taxonomy and hard-stop classes.**
Five levels, enforced as a typed `GuardrailResult` on guardrail paths only (NOT a 1,213-except
rewrite): `HARD_STOP` — marks layer unavailable/corrupt, account state unreadable/unwritable,
same-book run-lock conflict, settlement failure mid-flight, auth disabled in production
(startup refusal). `FREEZE` (no new risk; holds and de-risking still allowed) — firm-clamp or
book-cap exception, all-expected-peers-missing, stale anchor artifacts beyond freshness budget.
`SHRINK` — partial peer data, stale non-anchor artifacts feeding sizing. `ADVISORY_ONLY` —
lens/journal/dashboard failures. `TELEMETRY_ONLY` — logging/cost. Charter P2 is the tiebreak:
any ambiguity resolves downward (less authority, less exposure).

**R4 — Production refuses to boot unauthenticated. Effective immediately.**
`MASTERMIND_REQUIRE_AUTH=1` in the deployed `.env`; `auth.install()` raises at startup when
required-but-unset. Set `MASTERMIND_PASSWORD` + `MASTERMIND_AUTH_TOKEN` now. The public face
becomes a *published read-only artifact bundle*, not the live app (MW6); operator/LLM routes get
a separate gate + rate limits (MW6). `/health` stops leaking paths (MW0).

**R5 — Experiment maturity becomes evidence-driven; experiments stay native to Mastermind.**
Build the tri-state evaluator: `not_old_enough` / `blocked_missing_evidence` / `ready_for_review`,
with `comeback_date` demoted to one evaluator among several. Maturation/resolution emit governance
events. No migration into NW's ladder. (NW's `metabolism.py` budget-per-week anti-mining law is a
candidate adoption, noted, not scheduled.)

**R6 — One `DecisionPacket` at every Brain's submission boundary; free-form inside.**
The Autonomous Opus Brain stays "no doctrine" internally — doctrine binds at the boundary.
Packet: mandate, desired holdings, evidence planes used, source provenance, explicit falsifiers,
liquidity/capacity notes, portfolio delta, expected failure mode, risk direction
(increase/preserve/reduce). Incomplete packet → rejected before sizing → rejection ledger
(learning data). Deterministic flagship emits packets too (it already computes most fields).

**R7 — One official NW adapter; contracts declared, not inferred; no big-bang migration.**
Ratify the `fable/nw-context-bridge` pattern (single reader, schema-versioned, per-artifact
freshness, `is_context_only` enforced) as THE adapter for NW artifacts. A generated
`config/contracts.yml` reconciled with `synapse.yml`'s 16 declared `mastermind:*` consumer
artifacts covers the legacy readers: per-artifact owner/schema/freshness-budget/tier/allowed-
effect/degradation-class. New raw vendored reads outside typed adapters = CI-blocked. Existing
8+ readers migrate opportunistically, not in one wave.

**R8 — Replay/backfill evidence is legal for CONTROLS, forward-only for AUTHORITY.**
Controls (incident replays, failure drills, severity plumbing) may use replay/synthetic evidence.
Anything that RAISES authority — posture arming, judgment promotion, book promotion/capital
increase, self-tune arming — requires forward-only live paper history. Backfills of *recorded*
series (benchmark series from the yahoo parquet, book active returns from existing nav_history)
are legal and must carry `derived`/`backfilled` labels (the `journal.py` pattern). Synthetic data
never supports an alpha claim.

**R9 — Missing peer state: keep the subtract-only invariant, add an expectation sentinel.**
The never-un-cap guarantee already holds (per-book caps bind regardless). What's missing is
telling "book legitimately empty" from "book expected to publish today but didn't." Build the
expectation check (book enabled + venue trading day ⇒ fresh `latest.json` expected); in
production, expected-but-missing ⇒ `FREEZE` new adds for the clamp domain + severity event.
De-risking always allowed.

**R10 — Document classes.** *Policy* (LLMs and operators may treat as binding):
`MASTERMIND_CHARTER_V2.md`, `DOCTRINE.md`, `config/doctrine.yml`, `MAINTENANCE.md`. *History*:
masterplan status logs, incident post-mortems. *Commentary*: dockets, audits, brainstorms.
*Generated state* (authoritative over prose): the M0 census snapshot (`scripts/system_census.py`,
MW1) — architecture docs must cite it; weekly doc-drift check goes into loop maintenance.

---

## 2. The waves

Sequencing law: close live holes → make failure visible → make authority explicit → make
contracts binding → make the firm allocate → make the public surface safe → prove it fails right.
Every wave: branch off fresh master in a worktree, targeted tests + incident replay battery green,
opus review, Fable merges locally (repo has no remote), production restart coordinated.
Routing: **Sonnet builds, Opus reviews, Fable adjudicates/merges.** Haiku only for mechanical sweeps.

### MW0 — Close the live holes (S items, same-day)
- **Auth hardening:** startup refusal under `MASTERMIND_REQUIRE_AUTH=1`; `/health` leak fix;
  deploy-time: set `MASTERMIND_PASSWORD`/`MASTERMIND_AUTH_TOKEN`/`MASTERMIND_REQUIRE_AUTH` in
  `.env` + restart (R4).
- **R1 enactment:** SD out of `_FIRM_UNION_BOOKS`; asymmetry comment; registry annotation; test.
- **Heavyweight outcomes writer restored** (orphaned-ledger fix; port the etf_outcomes pattern).
- **Model-routing fix:** API fallback deep→opus reconciled with `agents.yml`; three-way
  `brain.yml`/`agents.yml`/`client.py` inconsistency resolved.
- **Doc-drift pack (LLM-facing first):** `heavyweight_mcp.py` tool descriptions,
  `portfolio/registry.py:42` comment, `firm_exposure.py` "TOOTHLESS" header,
  `app/scheduler.py` "two jobs" header, `bot/__init__` submodule→symlink, `derisk.py`
  default-comment vs armed reality.
- **Small rot:** `data/posture_governor/` init; `interim_marks` never-written probe;
  `GET /api/self_directed` write-on-read fix (reads become pure; settlement stays in the
  scheduled mark path).
- Acceptance: replay battery green; full suite no new fails; production restarted with auth on;
  an unauthenticated `POST /api/autonomous/run` returns 401.

### MW1 — Failure visibility + run governance (docket M1 first half)
- `data/governance/run_events.jsonl` (append-only): swallowed exceptions in high-authority jobs
  (loop_maintenance's ~12 sub-steps, settle, derisk, watch, clamp paths) become records.
- Per-book run locks + global lock; cron/HTTP/first-run overlap policy = skip+log (fixes the
  TOCTOU daemon-thread race). Manual `force=1` paths emit run events.
- Run ledger v1: pre/post record per job — run_id, job, book, trigger type, git SHA, status,
  severity, input-artifact hashes, output artifacts. Extends `brain/runlog.py` scope.
- `GuardrailResult` taxonomy (R3) retrofitted to guardrail paths only; tests: failures in
  caps/marks/auth/locks/peer-reads can never raise authority or exposure.
- Peer-expectation sentinel (R9).
- `scripts/system_census.py` — generated live-architecture snapshot (18 jobs, 7 books, ~25 env
  flags, gates, endpoints, external artifacts) + env-flag snapshot in every run record +
  `/api/scheduler` health + dashboard pane. Weekend-firing job hygiene (daily_loop,
  publish_macro_snapshot) investigated and either filtered or documented as intended.
- Acceptance (docket M1): a missing mark file, missing peer file, failed cap, failed auth config,
  or overlapping run cannot silently pass — each yields a severity-coded, queryable record.

### MW2 — Governance ledger + evidence-driven experiments (docket M6 + M5)
- `governance.jsonl` (R2): events for flag-state diffs, experiment maturation/resolution,
  lifecycle recommendations, posture arming, operator script actions, `doctrine.yml` hash changes,
  Fable approvals. A0–A7 → Mastermind action mapping as `config/authority_map.yml` (doc + check,
  not a rebuild).
- Experiment maturity evaluator (R5): tri-state, per-experiment expected-time-to-decision on the
  dashboard, stuck-experiment warnings (4 of 14 current experiments are condition-only and can rot).
- Acceptance: every authority change reviewable as a ledger event; zero experiments without either
  an evaluable condition or a comeback date.

### MW3 — Contract bridge (docket M2)
- Review + land `fable/nw-context-bridge` (39d5c67) as the single NW adapter (R7).
- `config/contracts.yml` reconciled with synapse.yml's declared mastermind consumers; per-artifact
  freshness budgets at decision read sites; `MACRO_STALE_BLOCK` semantics upgraded warn→FREEZE
  per R3; R2-availability anchor beyond SPY.json; the ignored `scored_active` tier hint becomes
  enforced at its consumer.
- CI gate: no new raw vendored reads outside adapters.
- Feedback artifact v1 to NW (sibling of `macro_snapshot`): gate failures, rejected counts,
  falsified theses. (Full feedback contract completes after MW4 packets.)
- Acceptance (docket M2): a signal's decision effect is known before any Brain sees it.

### MW4 — DecisionPacket protocol (docket M4)
- Schema + validator; all 5 Brain books + flagship/judgment emit; incomplete → rejected
  pre-sizing → rejection ledger; packets reference governance events + run ledger.
- Acceptance: no LLM output reaches sizing without a complete, inspectable packet.

### MW5 — Portfolio-of-portfolios (docket M3)
- Mandate-compliance packet per book run (keystone; Heavyweight already computes most fields).
- Shadow Firm Allocator — display-only book capital weights/risk budgets/review flags consuming
  lifecycle grades + benchmark ledgers + overlap. P8: shadow with pre-committed bogey before any
  binding authority is even proposed.
- Regional repair: schedule `build_regional()` (currently never invoked — CN/HK benchmark dirs
  empty), `bogey_is_proxy` flag, regional lifecycle coverage for china/hk (today: governance
  orphans), 2800.HK when the store carries it.
- Labeled backfills per R8: benchmark series from yahoo parquet; book active returns from
  nav_history; `time_until_evaluable` on every gate.
- Acceptance (docket M3): capital/quarantine/retire decisions arguable from ledger evidence.

### MW6 — Public/operator split + failure drill (docket M1 second half + M7)
- Route split: operator/LLM-triggering routes behind bearer-token gate; rate limits; published
  read-only public bundle; `MASTERMIND_SERVE_ONLY` guard merged to master; deployment provenance
  banner (git SHA, data snapshot, "paper only"); `account.router` dead code fixed or removed.
- Failure-injection drill as permanent CI: the docket's 10 scenarios (stale artifact, missing
  peer, corrupted mark, auth-disabled, overlapping run, invalid packet, low-authority-as-thesis,
  benchmark unavailable, auto-arm attempt, deploy mismatch) each asserting expected severity +
  ledger record + no unauthorized risk increase.
- Acceptance (docket M7): every scenario fails the right way, provably, forever.

### Explicitly NOT building (this program)
- No new signals, no alpha logic changes, no binding firm allocator (shadow only).
- No wholesale A0–A7 rebuild of Mastermind's gates; mapping + events only.
- No 1,213-except rewrite; guardrail paths only.
- No experiment-registry migration into Neural Web.

---

## 3. Status log

- **2026-07-06 (Fable): MW0 SHIPPED + merged + DEPLOYED** (merges 7749010/a0759de/ca1fb90;
  uvicorn PID 13054, /health version ca1fb90). Auth ON in production: `MASTERMIND_PASSWORD` +
  `MASTERMIND_AUTH_TOKEN` + `MASTERMIND_REQUIRE_AUTH=1` in `.env`; startup now refuses
  unauthenticated boot; `/health` path-leak fixed (verified live: unauthenticated operator
  POST → 401, login → 303). R1 enacted: `_FIRM_UNION_BOOKS` = (flagship, autonomous, etf);
  `hw-firm-universe-ab` annotated (condition changed mid-window). Heavyweight accountability
  restored (`portfolio/heavyweight_outcomes.py` recreated on master lineage per the etf
  pattern, wired into run path + Brain prompt line). Model-routing fix: API-fallback deep →
  `claude-opus-4-8` (was silently `claude-fable-5` for all 5 Brain books). Doc-drift pack
  landed (firm_exposure header, scheduler header, LLM-facing heavyweight_mcp descriptions,
  registry comment, symlink-not-submodule, derisk armed-in-prod note). Write-on-GET fixed
  (both `self_directed.book()` GET callers read_only). Reviews: APPROVE ×1,
  APPROVE_WITH_NITS ×2 — nits fixed pre-merge. Suite: 19/19 replays green; only catalogued
  pre-existing failures (`test_distribution_tells` reds proved to be live-mutated
  vendor/macro_src data drift — identical at 477af10; hermetic-fixture item queued into MW1).
  Concurrent session landed `w-nw1` (NW context bridge dark-shipped, arm after 5 present
  builds, come-back 2026-07-19) mid-wave — MW3's first deliverable DONE; MW3 rescoped to
  contracts.yml + per-artifact freshness + feedback artifact. MW0 findings: `interim_marks`
  absence is BY DESIGN (no-op until theses age past 5/10bd checkpoints); posture_governor
  mkdir pre-fixed on HEAD.
- **2026-07-05 (Fable): program authored.** Docket verified (25-agent workflow), 10 rulings
  issued, waves MW0–MW6 defined. MW0 build dispatched.

# MAINTENANCE.md — Mastermind system runbook

**For:** any maintenance session — Opus-only, Fable-supervised, or human-relayed.
**Precondition:** zero context. Read this file top-to-bottom before touching anything.
**Authority:** this runbook is read-only reference material. To change it, you need
Fable sign-off (charter P8). The only action the runbook authorises on its own is
reading artifacts and executing already-reviewed agenda items.

---

## 0. The system in one paragraph

Mastermind is a multi-book paper-equity desk (7 books: flagship, autonomous,
heavyweight, china, hk, etf, self_directed) governed by a layered doctrine.  Each
book decides after its market's close; a scheduler (app/scheduler.py) fires every
build job on a UTC cron.  The Brain seats (STRATEGIST, PM-CONVICTION, GATE OFFICER,
RISK OFFICER) are graded against realized outcomes; their calibration multipliers feed
their own system prompts (via brain/self_mirror.py).  A weekly CIO review
(brain/cio.py) and a daily loop-maintenance job advance the learning substrate.

The **hard invariant** (memorise this before reading anything else):

> Missing / stale / wrong data may coarsen identity, freeze the book, or shrink size
> — it may NEVER un-cap, raise authority, or flip direction.  (charter P2)

---

## 1. Entry sequence (every maintenance session, in this order)

### Step 1 — Read the agenda

```
cat data/agenda/AGENDA.md          # the ranked agenda from the last improvement run
```

If `data/agenda/AGENDA.md` does not exist or is stale (older than 7 days):

```python
from brain.improvement_agenda import write
write()           # regenerates data/agenda/<date>.json + data/agenda/AGENDA.md; LLM-free fast path
```

The top items tell you what to execute.  Do NOT skip this step.

### Step 2 — Check the experiment registry for matured items

```python
from brain.experiment_registry import summary
import json
print(json.dumps(summary(), indent=2))
```

Items with `status == "matured"` are overdue for a judgment.  They appear at the
top of the agenda automatically, but surfacing them here lets you see the raw
gate language before acting.

### Step 3 — Check the armory and deploy lag

```python
from scripts import check_deploy_lag
import json
lag = check_deploy_lag.check()
print(json.dumps(lag, indent=2))
```

If `lag["lag_hours"] > 24`: **stop everything** and bring production onto master
before doing any other work (section 4 below).

Read `data/deploy_lag.json` for the persisted state.

### Step 4 — Check the replay battery

```
cd /path/to/mastermind_root
pytest tests/incident_replays/ -q
```

All six incident-replay tests must be green before you proceed.  A red replay
test means a regression was introduced — do not execute agenda items until it is
diagnosed and fixed.

### Step 5 — Execute the top `owner: opus-session` agenda items

Items marked `owner: fable-review` require Fable sign-off — do not execute them
autonomously.  Items marked `owner: self-tune` are handled by the weekly self-tune
job, not by a manual session.

For each `opus-session` item, the agenda entry contains:
- `evidence`: the data backing the finding
- `suggested_fix`: what to do
- `fix_type`: config-tune | prompt-edit | code-change | experiment
- `expected_impact`: what metric should move

Execute the fix, run the full test suite, verify the cited metric moved, then
update the masterplan status log and close the experiment (section 6 below).

### Step 6 — Close the loop

After executing items:

1. Update `research/MASTERMIND_FIX_MASTERPLAN.md` status log with a dated entry.
2. Answer the four standing self-interrogation questions (charter §self-interrogation).
3. If an experiment was judged, call:
   ```python
   from brain.experiment_registry import resolve
   resolve("experiment-id", verdict="one-sentence verdict")
   ```
4. Re-run `write()` from `brain.improvement_agenda` to refresh AGENDA.md.

---

## 2. Hard boundaries — what requires Fable review

The following changes MUST NOT be made autonomously, even if the agenda says to:

| What | Why |
|---|---|
| Editing `research/MASTERMIND_CHARTER_V2.md` | The charter is the constitution. |
| Editing `tests/incident_replays/` fixtures | Replay batteries are the invariant test. |
| Editing `loop/harness.py` (the frozen judge) | The judge cannot judge itself. |
| Raising any hard cap (gross cap, firm cap, cluster cap ceiling) | Caps may only shrink on bad data, never self-raise. |
| Arming `brain/self_tune.py` for a new parameter family | P8: Fable sign-off required before any live tune. |
| Arming `brain/posture_governor.py` | P8: governor needs effective_n>=8 + Fable sign-off. |
| Arming `posture_decider.py` (W-E.2 seam) | P8: arm seam is hard-coded dark; activation is a Fable decision. |
| Promoting any shadow signal to sizing input | P3: promotion needs a passed gate + Fable sign-off. |
| Editing `data/experiments/registry.json` gate or maturity_condition fields | Gate language is set by the program at creation time. |

If you are uncertain whether an action falls in this list, it does.  Record what
you found and surface it as a `fable-review` agenda item instead.

---

## 3. Deploy / restart runbook

**Context:** the bot runs as a uvicorn process.  `app/scheduler.py:start()` fires
every build job.  Code changes reach production ONLY after a process restart on the
checkout the process runs from.  A merged PR does nothing until restart.

### Checking the running process

```bash
# Find the PID and its checkout
ps aux | grep uvicorn | grep -v grep
# Note the PID; then:
lsof -p <PID> | grep cwd         # confirms which repo root it's running from
```

### Bringing production onto master

```bash
cd /Users/chriswong/Documents/Cluade/Mastermind
git fetch origin
git checkout master
git pull origin master            # or rebase if dirty
# Confirm tests pass:
pytest -q --tb=short 2>&1 | tail -20
# Restart the bot (coordinate with any other active session first).
# Production (Mac) runs under launchd — com.mastermind.bot, KeepAlive — so
# kickstart it; NEVER background uvicorn by hand (orphans don't survive reboot):
launchctl kickstart -k gui/$UID/com.mastermind.bot
# On the VPS: systemctl restart mastermind
# Verify:
curl -s http://127.0.0.1:8000/health   # {"status":"ok",...,"version":"<new HEAD sha>"}
```

The launchd agent spawns `scripts/launch_uvicorn.py` with python3 **directly** —
launchd-spawned `/bin/bash` is TCC-blocked from reading `~/Documents` (probed
2026-07-17), so a bash entrypoint dies at `source .env` after every reboot; the
miniconda python3 binary holds the Documents grant. Bot serves on
`127.0.0.1:8000`; launchd log at `data/uvicorn.launchd.log`.

**Coordinate restarts.** If another session is in the middle of a build, a restart
kills the in-flight LLM call.  Check `data/portfolio/nav_history.jsonl` —
if today's date is already there, the build completed.

### Verifying flags after restart

The `.env` file in the repo root persists the `MASTERMIND_*` flags.  After restart:

```bash
# confirm key flags
grep MASTERMIND /proc/$(pgrep -f uvicorn)/environ 2>/dev/null \
  | tr '\0' '\n' | grep MASTERMIND
```

Expected in production (as of 2026-07-03 baseline; cost caps added 2026-07-25):
```
MASTERMIND_FAST_DERISK=1
MASTERMIND_MACRO_RISK=1
MASTERMIND_SELF_MIRROR=1
MASTERMIND_SELECTION_EXPLORE=1
MASTERMIND_FLAGSHIP_JUDGMENT=1   # shadow book ARMED
MASTERMIND_FIRM_CAPS=1           # default ON in code (no env var needed)
MASTERMIND_TIMING_GATE=1         # default ON in code (no env var needed)
MASTERMIND_NIGHTLY_USD_CAP=6.0   # per-book nightly shadow-USD cap (2026-07-25 cost ruling)
FLAGSHIP_PM_MAX_TURNS=12         # armed PM loop 30→12 (same ruling)
```

Flags that are OFF by default and must NOT be enabled without Fable review:
- `MASTERMIND_RISK_GOVERNOR` (governor not yet built / armed)
- `MASTERMIND_STUDENT` (CatBoost student, advisory only until gate)

---

## 4. Where every artifact lives

| Artifact | Path | Owner |
|---|---|---|
| Program plan + status log | `research/MASTERMIND_FIX_MASTERPLAN.md` | Fable |
| Charter | `research/MASTERMIND_CHARTER_V2.md` | Fable |
| Problem register | `research/mastermind_problem_register.json` | Fable |
| Learning design | `research/MASTERMIND_LEARNING_DESIGN.md` | Fable |
| Architecture | `research/MASTERMIND_V2_ARCHITECTURE.md` | Fable |
| Agenda (human) | `data/agenda/AGENDA.md` | auto (improvement_agenda.py) |
| Agenda (machine) | `data/agenda/<date>.json` | auto |
| Experiment registry | `data/experiments/registry.json` | brain/experiment_registry.py |
| CIO weekly review | `data/brain/cio/<isoweek>.{json,md}` | brain/cio.py (Sunday job) |
| Per-seat calibration | `data/brain/calibration.json` | brain/calibration.py (nightly) |
| Benchmark ledger | `data/benchmark/<date>.json` | brain/benchmark_ledger.py (daily mark) |
| Benchmark time-series | `data/benchmark/_series.json` | app/scheduler.py daily mark |
| Deploy-lag status | `data/deploy_lag.json` | scripts/check_deploy_lag.py |
| Incident replays | `tests/incident_replays/` | frozen CI fixtures |
| Incident docs | `research/incidents/2026-07-02-semis-breakdown/` | Fable |
| Flagship_judgment shadow | `data/portfolios/flagship_judgment/` | bot/scheduler |
| Shadow books | `data/shadow/` | portfolio/shadow_books.py |
| Doctrine constants | `config/doctrine.yml` | tagged (unverified-prior) |
| Cluster config | `config/clusters.yml` | Fable (never auto-edit) |
| Journal (per seat) | `data/journal/<seat>/` | brain/journal.py (not yet built — L2) |
| Self-tune log | `data/self_tune/` | brain/self_tune.py (not yet built — L4) |

---

## 5. The scheduler jobs and their UTC cron

| Job id | Time (UTC) | What it does |
|---|---|---|
| `macro_refresh` | every 3h :30 | pull macro vendor data; staleness tripwire |
| `daily_mark` | Mon–Fri 22:35 | mark ALL books to market (never trades) |
| `daily_loop` | 22:40 | flagship book build |
| `autonomous_daily` | Mon–Fri 23:10 | autonomous Brain book |
| `etf_daily` | Mon–Fri 23:15 | ETF Brain book |
| `heavyweight_daily` | Mon–Fri 23:25 | Heavyweight Brain book |
| `china_daily` | Mon–Fri 08:00 | China Brain book (Asia clock) |
| `hk_daily` | Mon–Fri 09:00 | HK Brain book (Asia clock) |
| `settle_pending` | Mon–Fri 15:00 | fill flagship queued orders at open |
| `settle_brain_asia` | Mon–Fri 01:35 | fill China/HK queued orders at A-share open |
| `watch_us_overnight` | Mon–Fri 02,06,11:20 | overnight watch, US books |
| `watch_asia_overnight` | Mon–Fri 14,20,00:20 | overnight watch, Asia books |
| `derisk_us_intraday` | Mon–Fri 14–20:00,30 | fast de-risk sweep (FAST_DERISK flag) |
| `publish_macro_snapshot` | 12:25, 22:25 | push snapshot to Macro Dashboard |
| `cio_weekly` | Sunday 10:00 | CIO review; writes data/brain/cio/ |
| `loop_maintenance` | Mon–Fri 23:45 | forward clocks: predictions, outcomes, calibration |

All times configurable via env vars in `.env` (see app/scheduler.py for names).

---

## 6. Worktree and branch conventions

The fix program uses isolated git worktrees to prevent concurrent sessions from
clobbering each other:

```
/Users/chriswong/Documents/Cluade/Mastermind/                 # main checkout (production)
/Users/chriswong/Documents/Cluade/Mastermind/.claude/worktrees/fable-wl/   # THIS worktree (W-L)
```

**Critical:** always verify which worktree you are in before editing files.  Edits
to absolute paths under the WRONG worktree silently modify the wrong branch.

Branching convention: `fable/<wave-name>` (e.g. `fable/wl-learning`).  Always
branch off master, squash-merge same-day to keep the history clean.

After merging, verify that the production checkout is updated and the process is
restarted (charter P10; the 4-day gap between fix and deployment cost real capital).

---

## 7. Anatomy of a test run

```bash
cd /Users/chriswong/Documents/Cluade/Mastermind/.claude/worktrees/fable-wl
pytest -q --tb=short 2>&1 | tail -40
```

**Known pre-existing failures (do NOT chase):**
- `test_china_book` (4 failures — data dependency)
- `test_bot_mcp` intake
- `test_d5` string_lean
- `test_doctrine_completeness`
- `test_falling_knife_commodity`
- `test_phase1`
- `test_research_paper`
- `test_translate` zh-cache
- `tests/test_auth/`, `tests/test_desk_api/`, `tests/test_research_pdf/` — collection-ignored

Any NEW failure introduced by an agenda-item execution is a ship-blocker.  Diagnose
before proceeding.

---

## 8. The four self-interrogation questions (answer these at every wave close)

From `research/MASTERMIND_CHARTER_V2.md`:

1. Is the bot powerful enough to perform highly autonomously — with no human catching
   its mistakes?
2. Would it still make the last incident's class of mistake? Prove it with the replay
   battery, not opinion.
3. Does it have enough visibility (what fraction of the published signal surface does
   it perceive)? Can it act on that visibility (does perception reach sizing)?
4. What is the single highest-leverage enrichment remaining? Schedule it.

Write the answers into `research/MASTERMIND_FIX_MASTERPLAN.md` status log before
closing the wave.

---

## 9. Signals that need human escalation immediately

Stop work and contact Fable if any of the following occur:

- `data/deploy_lag.json` shows `lag_hours > 48`
- Any incident-replay test turns red (pytest tests/incident_replays/ has a failure)
- `brain/calibration.json` shows a seat multiplier < 0.5 (severe miscalibration)
- The experiment registry shows an item with `comeback_date` more than 14 days past
  its due date with no judgment
- The benchmark ledger shows a book underperforming its defensive bogey by more than
  5pp over 30 days (data/benchmark/<latest>.json `leaderboard` field)
- Any code change touches the denylist items from section 2

---

## OAuth key pool / rotation

### How it works

`brain/key_rotor.py` manages a pool of up to 7 numbered OAuth tokens
(`CLAUDE_CODE_OAUTH_TOKEN_1..7`) plus the legacy bare `CLAUDE_CODE_OAUTH_TOKEN`.
Before each `reason()` call, the rotation layer calls `candidates()` which returns
numbered slots ordered by: non-cooling first (ascending 5h-window load then slot
number), cooling ones appended last as emergency fallback.  The legacy bare token
is included **only** when zero numbered slots are present (macro V11 suppression rule).

`reason()` iterates over candidates.  If the SDK or subprocess returns a result whose
text or error field matches a key-failure pattern, `mark_cooling()` is called for that
key_id and the loop moves to the next candidate.  Non-key failures (network errors,
tool errors) break the loop immediately.

`chat_stream()` picks the first healthy candidate for the stream (no mid-stream
rotation), then runs `classify_failure()` on the first 600 chars of streamed text
after the stream ends, marking the key cooling if it matched.

REDLINE: token VALUES never appear in logs, ledger rows, or return values.  Only
key_id strings (e.g. `claude_code_oauth_3`) and env-var NAMES are stored.

### Cooling kinds and TTLs

| Kind    | Trigger                                               | Default TTL   | Cleared by              |
|---------|-------------------------------------------------------|---------------|-------------------------|
| window  | 429 / "usage limit reached" / "rate limit" / 529     | 5 hours       | reset_hint passage only |
| weekly  | "weekly usage limit" / "limit reached ... week"      | 7 days        | reset_hint passage only |
| auth    | 401 / 403 / "disabled claude subscription access"    | 24 hours      | later ok row OR TTL     |

### Ledger path

`data/metabolism/key_ledger.jsonl` — append-only JSONL, schema `metabolism.key_ledger.v1`.
Each row: `{schema, ts, key_id, cycle_id, stage, est_tokens, outcome, [cool_kind], [reset_hint]}`.

The ledger is stateless-cattle: any process resumes from disk state.  All public
functions are NEVER-RAISE — a pool failure never aborts a trading lane.

A filtered copy of the ledger (last 14 days, max 20 000 rows) is exported to the macro
repo as `data/mastermind/key_events.jsonl` on each snapshot push, where it is surfaced
in the macro admin AI Cost tab.

### How to add a key

1. Obtain a new Claude Code OAuth token (from a second account or subscription).
2. Add it to `.env` as `CLAUDE_CODE_OAUTH_TOKEN_N` where N is the next free slot (3..7).
3. Add the slot number to `METAB_KEYS_ENABLED` (comma-separated, e.g. `3,4,5`).
4. Restart the app — the new slot is discovered automatically on the next `reason()` call.
5. Verify: `data/metabolism/key_ledger.jsonl` should show `claude_code_oauth_N` rows
   after the first successful session.

The macro admin AI Cost tab shows key pool status once `data/mastermind/key_events.jsonl`
has been published by the next snapshot push.

### METAB_KEYS_ENABLED

Controls which numbered slots are eligible.  Set in `.env`:

```
METAB_KEYS_ENABLED=3,4,5,6,7
```

Digits → `claude_code_oauth_N`; the literal string `legacy` → bare legacy token.
If the filter would remove ALL present slots, the filter is ignored (fail-open, R-V10-3).
Absent/empty → all present slots enabled.

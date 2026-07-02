Now I have everything I need. Here is the complete F4 report.

---

## F4 — Deployment & Ops Forensics

---

### (1) Which fixes does the running production process actually have?

**Branch reality:** the prompt said the checkout was on `fix/bot-orphans-arming`. That is no longer true. The active checkout at `/Users/chriswong/Documents/Cluade/Mastermind` is on **`prod-deploy-w0w3`**, a branch that is `master + 1 commit` (commit `eb9fd0b`, "fix(feed): restore data/risk_radar to the vendored sparse set", authored 2026-07-02 15:20 local). There are zero commits on master that are not on this branch; there is one commit on this branch not on master.

W0–W3 are therefore **deployed**. The running process has:
- W0: fail-closed data gating, flagship sell path (`queue_orders` queues sells for dropped names, `nav_base=None`), close reasons, tripwire anchors
- W1: `brain/regime_frame.py`, dwell state machine in `brain/macro_risk.py`, severity ladder in `bot/derisk.py` (`_severity_cap`, `eff_cap = min(state_gross_cap, sev_cap)`)
- W2: leadership caps (`portfolio/sleeves.py`), graded extension, shortlist kill, ONE budget equation via `regime_frame.budget()`
- W3: cluster identity via `config/clusters.yml` + `portfolio/cluster_config.py`, book cluster caps and firm headroom in `enforce_book_caps()`, `MASTERMIND_FIRM_CAPS` default ON, semis_ai cluster cap = 0.35

**The `fix/bot-orphans-arming` branch still exists** as a local branch, 33 commits ahead of the W0–W3 common ancestor (commit `b201699`). It is NOT running. Its 33 commits contain forward work (armory, corr_sizing, risk_prior, us_freshness, china_freshness, theme_lens, detector_arming, and more) that is also NOT on master/prod-deploy-w0w3 — this work predates or runs parallel to the fix program and is orthogonal.

**Running process:**
- PID: **84236** (started 15:20 today, immediately after the prod-deploy-w0w3 commit; the earlier PID 72791 is gone)
- CWD: `/Users/chriswong/Documents/Cluade/Mastermind` (confirmed via `lsof`)
- Branch at CWD: `prod-deploy-w0w3`

**Live `MASTERMIND_*` env flags on PID 84236:**
```
MASTERMIND_EXPLORE_EPS=0.05
MASTERMIND_FAST_DERISK=1      ← derisk.enabled() = True
MASTERMIND_MACRO_RISK=1       ← macro risk officer armed
MASTERMIND_RESEARCH_LLM=0
MASTERMIND_SELECTION_EXPLORE=1
MASTERMIND_SELF_MIRROR=1
MASTERMIND_THEME_LENS=1
```

**Flags NOT in env but defaulting ON in W0–W3 code:**
- `MASTERMIND_FIRM_CAPS`: `portfolio/firm_exposure.py:499` default `"1"` → **armed ON** without env var
- `MASTERMIND_TIMING_GATE`: `bot/phase2.py:150` default `"1"` → **armed ON** without env var

**Flags defaulting OFF (correct):**
- `MASTERMIND_RISK_OFFICER`: default `"0"` → OFF
- `MASTERMIND_FLAGSHIP_JUDGMENT`: default `"0"` → OFF (W4 shadow book, correct)

**Why the derisk artifacts still show `gross_cap=1.0`:** the today's flagship and autonomous derisk artifacts (timestamps 07:30 and 13:30) were written by the OLD code running before the 15:20 W0-W3 deploy. The NEW code writes `gross_cap=eff_cap` and `eff_cap` and `severity_cap` fields. For the record: even with the new code, **hold was the correct answer** both times — flagship gross=0.2478 and autonomous gross=0.644 are both below `eff_cap = min(1.0, 0.70) = 0.70` for severity=2 with state=risk_on. The Flagship hasn't been sitting full-gross awaiting a sell; it is genuinely 25% gross. The autonomous book that bought more SMH ($24.8k, 41 shares at $605.70) did so because the phase2 build assigned a non-zero target weight to SMH and the book's total gross at 60.5% remains below the severity-2 eff_cap=0.70. The severity ladder does not block adds below the eff_cap — it only trims above it.

---

### (2) The `vendor/macro_src` sparse gap: risk_radar and other missing contracts

**The risk_radar gap is now CLOSED at the data level.** The prod-deploy-w0w3 commit (`eb9fd0b`) extended `_SPARSE_PATHS` in `data_layer/macro_refresh.py` from:
```python
("site", "data/regime", "engine", "lib", "data/yahoo")
```
to:
```python
("site", "data/regime", "engine", "lib", "data/yahoo", "data/risk_radar")
```
The vendor/macro_src sparse checkout already reflects this: `data/risk_radar/forward_log.jsonl` exists there (4.2KB, last updated today at 15:13). The latest entry is `asof=2026-07-01, state=caution, growth score 75.4 elevated`. The prod `brain/risk_prior.py` reads `data/regime/regime_one.json` (present) and degrades gracefully when `_load_risk_radar` is absent (it exists on the `fix/bot-orphans-arming` branch code but NOT on master/prod-deploy-w0w3 — `risk_prior.py` on master does not have a `_load_risk_radar` method; that is a W4/P-NEW-1 contract that `tests/test_risk_prior.py` on master marks as `importorskip` pending).

**Complete sweep of vendor path reads in master branch code vs sparse set:**

| Path read by master code | In sparse set | Present in vendor | Degrades how |
|---|---|---|---|
| `data/regime/latest.json` | Yes | Yes | — |
| `data/regime/regime_one.json` | Yes (under data/regime) | Yes | — |
| `data/risk_radar/forward_log.jsonl` | **Yes (added in eb9fd0b)** | Yes | — |
| `site/factordata/us_standouts.json` | Yes (site) | Yes | — |
| `site/sectordata/sector_cycles.json` | Yes (site) | Yes | — |
| `site/stockdata/*.json` | Not via git (R2 migration) | Yes via `_sync_r2()` | fail-closed conviction gate blocks entries if absent |
| `data/china_regime/latest.json` | **No** | **No** | `_read_raw()` returns `{}` → `regime_frame('china')` returns empty dict → china/HK budget equation returns fallback defaults |

**The one remaining sparse gap is `data/china_regime`.** `brain/regime_frame.py:59-60` maps `region='china'` and `region='hk'` to `vendor/macro/data/china_regime/latest.json`. This path is not in `_SPARSE_PATHS`. The file exists in the Macro Dashboard repo (`data/china_regime/latest.json` is git-tracked on main). The graceful degradation in `_read_raw()` (lines 109-124 of regime_frame.py) returns `{}` on any miss, so China/HK budget equations produce fallback defaults rather than crashing. This is a silent degradation: the China and HK bot books run with no regime frame input (no quad, no liquidity_overlay, no budget multiplier from the cycle position). **Recommended fix: add `"data/china_regime"` to `_SPARSE_PATHS`** in the next patch.

No other vendor data paths outside the sparse set are load-bearing for master-branch code. The `data/altdata`, `data/flow`, `data/forex`, `data/intelligence`, `data/news`, `data/policy`, `data/vol`, `data/transmission` patterns found are local `data/` paths (the bot's own output dir), not vendor reads.

---

### (3) Deploy Runbook: bringing prod onto master (+ risk_radar fix) safely

**Context:** the current prod checkout (`prod-deploy-w0w3`) is master+1. The in-flight W4 work is in a separate worktree at `.claude/worktrees/fable-w4` on branch `fable/w4-additive-mind` with 13 uncommitted modified files — that worktree is isolated and will not be touched by the steps below. The untracked files in the main checkout (app/account.py, app/static/account.js, and data dirs) are not code changes; data dirs are runtime state.

**Step 0 — Verify the process state before touching anything**
```bash
ps aux | grep uvicorn | grep -v grep
# Note the PID. Confirm CWD = /Users/chriswong/Documents/Cluade/Mastermind
# Confirm branch:
cd /Users/chriswong/Documents/Cluade/Mastermind && git branch --show-current
# Expected: prod-deploy-w0w3
```

**Step 1 — Commit the eb9fd0b risk_radar fix onto master itself**

The prod-deploy-w0w3 commit is NOT on master. It should be — master needs `data/risk_radar` in its sparse set or the next `macro_refresh.refresh()` will silently re-remove the risk_radar dir.

```bash
cd /Users/chriswong/Documents/Cluade/Mastermind
git checkout master
git cherry-pick eb9fd0b
# Commit message is already good. Push:
git push origin master
```

**Step 2 — Switch the main checkout to master**
```bash
cd /Users/chriswong/Documents/Cluade/Mastermind
git checkout master
# Verify:
git log --oneline -3
# Should show the cherry-picked risk_radar fix as HEAD
```

**Step 3 — Handle the untracked app/account.py + app/static/account.js**

These are untracked new files from a prior session. Decide: if they are meant to ship, commit them to master now before restart. If not ready, they are harmless (untracked files don't affect the running code; uvicorn only loads what's imported at startup).

```bash
# Option A: commit them
git add app/account.py app/static/account.js
git commit -m "feat(app): account/profile broker endpoint (cross-site SSO)"
git push origin master

# Option B: leave untracked (safe — uvicorn won't load them unless app/main.py imports them)
# Check if main.py already imports account:
grep 'account' app/main.py
# If the import is present on master → must commit or the server errors on startup
```

Note: the `from app import account` import is in the **uncommitted diff on fix/bot-orphans-arming** (that's where the diff was observed), NOT in the master/prod-deploy-w0w3 `app/main.py`. Master's `app/main.py` does not import `account`, so the untracked files are harmless.

**Step 4 — Restart uvicorn with the complete W0–W3 + W4-shadow env**
```bash
cd /Users/chriswong/Documents/Cluade/Mastermind

# Kill the running process:
kill $(ps aux | grep uvicorn | grep -v grep | awk '{print $2}')

# Wait 2 seconds, then restart with the full env:
MASTERMIND_MACRO_RISK=1 \
MASTERMIND_FAST_DERISK=1 \
MASTERMIND_SELF_MIRROR=1 \
MASTERMIND_SELECTION_EXPLORE=1 \
MASTERMIND_EXPLORE_EPS=0.05 \
MASTERMIND_THEME_LENS=1 \
MASTERMIND_RESEARCH_LLM=0 \
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --log-level warning &

# DO NOT set MASTERMIND_FLAGSHIP_JUDGMENT=1 yet — W4 is still in-flight in its worktree,
# uncommitted. Arm this flag ONLY after W4 merges to master. When that happens, add:
#   MASTERMIND_FLAGSHIP_JUDGMENT=1
# to the restart command to enable the shadow judgment book (shadow-only, never trades live).
```

Note: `MASTERMIND_FIRM_CAPS` and `MASTERMIND_TIMING_GATE` do NOT need to be in the env — both default ON in the code (see `portfolio/firm_exposure.py:499` and `bot/phase2.py:150`). Explicitly setting them avoids any ambiguity; if you want to make it visible:
```bash
MASTERMIND_FIRM_CAPS=1 MASTERMIND_TIMING_GATE=1 ...
```

**Step 5 — POST-DEPLOY VERIFICATION CHECKLIST**

A. **Armory arming state** — run this within 5 minutes of restart:
```bash
cd /Users/chriswong/Documents/Cluade/Mastermind
python3 -c "
import sys; sys.path.insert(0, '.'); sys.path.insert(0, 'vendor/macro_src')
import bot
from bot.armory import build
import json
r = build('2026-07-02')
print(json.dumps({'armed': r['armed'], 'disarmed': r['disarmed'], 'n_armed': r['n_armed']}, indent=2))
"
# Expected armed list must include: macro_risk, fast_derisk
# FIRM_CAPS and TIMING_GATE are not declared in the prod-branch armory (they're in master armory
# as default-ON systems); confirm by checking phase2._timing_gate_on() and firm_exposure.firm_caps_on()
python3 -c "
import sys; sys.path.insert(0, '.'); sys.path.insert(0, 'vendor/macro_src'); import bot
from bot.phase2 import _timing_gate_on
from portfolio.firm_exposure import firm_caps_on
print('timing_gate:', _timing_gate_on(), '  firm_caps:', firm_caps_on())
"
# Expected: timing_gate: True   firm_caps: True
```

B. **Dry phase2 budget check** — confirm budget < 0.50 and SMH clamped:
```bash
python3 -c "
import sys; sys.path.insert(0, '.'); sys.path.insert(0, 'vendor/macro_src'); import bot
from brain.regime_frame import budget
b = budget('us')
print('lead_budget:', b.get('lead_budget'))
print('inputs:', b.get('inputs', {}).get('regime_factor'), b.get('inputs', {}).get('cycle_mult'))
"
# With Tech=Topping/pos=80.8 and stagflation transition, budget should be well below 1.0
# The late_cycle_mult=0.5 in the overextension config should reduce leadership allocation
```

C. **Severity eff_cap verification** — dry-run a tripwire to confirm the new code path:
```bash
python3 -c "
import sys; sys.path.insert(0, '.'); sys.path.insert(0, 'vendor/macro_src'); import bot
from bot.derisk import tripwire, _severity_cap
tw = tripwire('flagship', '2026-07-02')
sev = tw.get('severity', 0)
scap = _severity_cap(sev)
print(f'trigger={tw[\"trigger\"]} severity={sev} severity_cap={scap}')
print(f'state_gross_cap={tw.get(\"risk_state\", {}).get(\"gross_cap\")}')
import min as _min  # just arithmetic
eff = min(tw.get('risk_state', {}).get('gross_cap') or 1.0, scap) if scap else tw.get('risk_state', {}).get('gross_cap')
print(f'eff_cap={eff}')
" 2>/dev/null || python3 -c "
import sys; sys.path.insert(0, '.'); sys.path.insert(0, 'vendor/macro_src'); import bot
from bot.derisk import tripwire, _severity_cap
tw = tripwire('flagship', '2026-07-02')
sev = tw.get('severity', 0)
scap = _severity_cap(sev)
state_cap = (tw.get('risk_state') or {}).get('gross_cap') or 1.0
eff = min(state_cap, scap) if scap else state_cap
print(f'trigger={tw[\"trigger\"]} severity={sev} severity_cap={scap} eff_cap={eff}')
"
# Expected: severity=2, severity_cap=0.70, eff_cap=0.70
```

D. **Sells queue check** — trigger a dry flagship derisk and confirm the artifact has eff_cap field:
```bash
python3 -c "
import sys; sys.path.insert(0, '.'); sys.path.insert(0, 'vendor/macro_src'); import bot
from bot.derisk import derisk_flagship
r = derisk_flagship('2026-07-02')
print('action:', r.get('action'))
print('eff_cap:', r.get('eff_cap'))
print('severity_cap:', r.get('severity_cap'))
print('gross:', r.get('gross'))
"
# If action=hold: confirm eff_cap=0.70 (new format), not eff_cap=None (old format)
# If action=sell: sells queue fires; check data/macro_risk/2026-07-02/derisk_flagship.json for exits
```

E. **risk_radar sparse check** — confirm it persists after a refresh:
```bash
python3 -c "
import sys; sys.path.insert(0, '.'); sys.path.insert(0, 'vendor/macro_src'); import bot
from data_layer.macro_refresh import refresh_and_check
r = refresh_and_check()
print('asof:', r.get('asof'), 'stale:', r.get('stale'))
import pathlib
rr = pathlib.Path('vendor/macro_src/data/risk_radar/forward_log.jsonl')
print('risk_radar present after refresh:', rr.exists())
"
# Expected: risk_radar still present (the new SPARSE_PATHS includes it)
```

---

### (4) Ops-structure change: eliminate "fixed but not deployed for days"

**Root cause:** the armory already self-declares every safety system's arming state per-turn, but nothing measures the gap between the running code and master. The tripwire mechanism exists in the data layer but was never applied to the deploy lag itself.

**Recommended spec — deploy-lag tripwire:**

Add a check to `bot/armory.py` (or a standalone `scripts/check_deploy_lag.py`) that runs on every build and alerts when master is N commits ahead of the running code:

```python
# scripts/check_deploy_lag.py  (read-only, run by the scheduler or manually)
import subprocess, json, os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MAX_LAG_COMMITS = 0      # alert on ANY master commit not deployed
_MAX_LAG_HOURS = 24       # alert if lag > 24h regardless of commit count

def deploy_lag():
    # The running code's HEAD commit
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=_ROOT,
                          capture_output=True, text=True).stdout.strip()
    # Commits on master not in HEAD
    behind = subprocess.run(["git", "log", "--oneline", f"{head}..origin/master"],
                             cwd=_ROOT, capture_output=True, text=True).stdout.strip().splitlines()
    n_behind = len([l for l in behind if l.strip()])
    # Date of oldest undeployed commit
    oldest_ts = None
    if behind:
        ts_raw = subprocess.run(["git", "log", "-1", "--format=%aI", behind[-1].split()[0]],
                                 cwd=_ROOT, capture_output=True, text=True).stdout.strip()
        oldest_ts = ts_raw
    return {"head": head, "n_commits_behind_master": n_behind,
            "oldest_undeployed_commit_ts": oldest_ts, "behind_commits": behind[:5]}

if __name__ == "__main__":
    lag = deploy_lag()
    if lag["n_commits_behind_master"] > _MAX_LAG_COMMITS:
        print(f"DEPLOY LAG ALERT: master is {lag['n_commits_behind_master']} commits ahead "
              f"of running code. Oldest: {lag.get('oldest_undeployed_commit_ts')}. "
              f"Commits: {lag['behind_commits']}")
    else:
        print(f"Deploy lag: OK (HEAD={lag['head'][:8]}, 0 commits behind master)")
```

Wire this into the **daily build cron** (`app/scheduler.py`) so it runs after every `phase2.run()` and writes a row to `data/brain/deploy_lag.jsonl`. The armory's `build()` function should read this file and surface `deploy_lag` as a declared system:

```python
# In bot/armory.py build():
systems.append({
    "system": "deploy_lag",
    "kind": "deploy_lag",
    "armed": lag["n_commits_behind_master"] == 0,
    "predicate": "armed ⇔ master N-behind == 0 (no undeployed fixes)",
    "distance_to_arming": {"n_commits_behind": lag["n_commits_behind_master"],
                           "oldest_commit": lag.get("oldest_undeployed_commit_ts")} 
                          if lag["n_commits_behind_master"] > 0 else {},
    "what_it_does": "alerts when safety fixes on master have not been deployed to the running process",
})
```

**Escalation:** if `n_commits_behind > 0` for more than 24 hours, send a Telegram alert through the existing bot notification path. The alert text should name the commit subjects (not just a count) so the operator knows immediately whether the undeployed fix is a critical safety system or a documentation commit.

**Process discipline:** the existing `b46ad65` commit message already notes "prod rebase+restart pending" — the failure was that this note existed for days with no enforcement. The deploy-lag tripwire makes that note unnecessary: every build that runs on stale code surfaces a loud, unambiguous artifact that shows exactly what is missing.

---

**Key numbers summary:**

| Fact | Value |
|---|---|
| Running PID | 84236 (started 15:20 today) |
| Running branch | `prod-deploy-w0w3` = master + 1 commit |
| W0–W3 on prod | YES — all four waves deployed |
| fix/bot-orphans-arming relationship | 33 commits ahead of the W0–W3 common ancestor (b201699), NOT running |
| FIRM_CAPS armed | Yes (default ON, no env var needed) |
| TIMING_GATE armed | Yes (default ON, no env var needed) |
| MACRO_RISK armed | Yes (env MASTERMIND_MACRO_RISK=1) |
| FAST_DERISK armed | Yes (env MASTERMIND_FAST_DERISK=1) |
| RISK_OFFICER armed | No (default OFF, correct) |
| FLAGSHIP_JUDGMENT armed | No (default OFF, correct — W4 not merged) |
| risk_radar in sparse set | Yes (added in eb9fd0b today) |
| risk_radar on disk | Yes — asof=2026-07-01, state=caution |
| china_regime in sparse set | No — degrades to empty regime frame for China/HK books |
| Flagship gross today | 0.2478 — already below eff_cap=0.70; hold is correct |
| Autonomous gross today | 0.605 — below eff_cap=0.70; SMH buy was within cap limits |
| Derisk artifacts from today (pre-deploy) | Written at 07:30 and 13:30, before the 15:20 W0-W3 restart; show old format (`gross_cap=1.0` not `eff_cap`) — next build will produce new format |
| Action needed | Add `data/china_regime` to SPARSE_PATHS; wire deploy-lag tripwire into armory |
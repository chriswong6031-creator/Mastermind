#!/bin/bash
# One-way Mac -> VPS deploy of the Mastermind APPLICATION CODE (companion to sync_state_to_vps.sh).
#
# The daily push (sync_state_to_vps.sh) keeps the box's data/ fresh, but it ONLY pushes data/ — so
# the box's *code* was a frozen rsync snapshot (mastermind.service last (re)started 2026-06-29) and
# every feature/fix shipped locally since then never reached bot.mastermind-x.com. This mirrors the
# code tree Mac -> /opt/mastermind and, ONLY when something actually changed, restarts the authoritative
# service (uvicorn on :8001, the target of the Caddy `bot.mastermind-x.com` reverse_proxy) and verifies
# /health — rolling back to the pre-deploy snapshot if the app fails to come up.
#
# Topology (verified 2026-07-02): Caddy :443 `bot.mastermind-x.com` -> 127.0.0.1:8001 = mastermind.service
# (WorkingDirectory /opt/mastermind, /opt/mastermind/.venv, authoritative systemd drop-in). /opt/mastermind is
# NOT a git checkout (rsync-deployed). The :8000 uvicorn is a DIFFERENT app (/opt/macro-api) — leave it.
#
# Guards: no-op when explicitly serve-only (MASTERMIND_SERVE_ONLY); kill-switch MASTERMIND_VPS_CODE_DEPLOY=0;
# change-gated (NO restart when nothing changed); additive (no --delete) like the data sync; best-effort
# (never aborts the caller). Breadcrumb log: /tmp/mm_vps_deploy.log. Safe to run by hand any time.
set -uo pipefail

LOG=/tmp/mm_vps_deploy.log
log(){ echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }

# never push from the serve-only mirror to itself; honor the kill-switch
[ -n "${MASTERMIND_SERVE_ONLY:-}" ] && exit 0
[ "${MASTERMIND_VPS_CODE_DEPLOY:-1}" = "0" ] && exit 0

SRC="/Users/chriswong/Documents/Cluade/Mastermind/"
BOXHOST="root@146.190.142.17"
DPATH="/opt/mastermind"
DEST="$BOXHOST:$DPATH/"
KEY="/Users/chriswong/.ssh/macro_dashboard_deploy_v2"
SSH="ssh -i $KEY -o BatchMode=yes -o ConnectTimeout=20"
SVC="mastermind.service"
HEALTH="http://127.0.0.1:8001/health"

# Importable Python packages (+ config) — the snapshot/rollback scope: exactly what can break app boot.
DIRS="app brain bot portfolio data_layer loop bridge scripts config"

# Deploy scope = the whole repo MINUS runtime state, secrets, vendored data, and build/test artifacts.
# .env is excluded so we never clobber the box's own secrets; data/ and vendor/ are pushed/managed
# elsewhere; {_DB} is a stray artifact dir; catboost_info is training telemetry (box doesn't train).
EXC=(
  --exclude='.git' --exclude='data' --exclude='vendor' --exclude='vendor_*'
  --exclude='.venv' --exclude='venv' --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo'
  --exclude='.claude' --exclude='node_modules' --exclude='*.sqlite' --exclude='*.sqlite-*'
  --exclude='*.db' --exclude='*.log' --exclude='.DS_Store' --exclude='*.lock'
  --exclude='.pytest_cache' --exclude='.mypy_cache' --exclude='.ruff_cache'
  --exclude='.env' --exclude='tmp' --exclude='catboost_info' --exclude='.codex'
  --exclude='{_DB}' --exclude='tests' --exclude='.github' --exclude='.deploy_prev'
)

# 1. CHANGE GATE — what would this deploy actually transfer? (dry-run, additive)
RAW="$(rsync -azn --out-format='%n' -e "$SSH" "${EXC[@]}" "$SRC" "$DEST" 2>>"$LOG")"; rc=$?
if [ $rc -ne 0 ]; then
  log "code deploy: dry-run rsync rc=$rc (box unreachable / ssh fail) — skip this tick"; exit 0
fi
CHANGED="$(printf '%s\n' "$RAW" | grep -vE '/$' | grep -v '^$' || true)"
N="$(printf '%s\n' "$CHANGED" | grep -c . || true)"
if [ "${N:-0}" -eq 0 ]; then
  log "code deploy: no change (box code in sync)"; exit 0
fi
log "code deploy: $N file(s) changed —"
printf '%s\n' "$CHANGED" | sed 's/^/    /' >> "$LOG"

# 2. SNAPSHOT current code (single rolling generation) for rollback
$SSH "$BOXHOST" "cd $DPATH && rm -rf .deploy_prev && mkdir -p .deploy_prev && for d in $DIRS; do [ -e \"\$d\" ] && cp -a \"\$d\" .deploy_prev/ ; done" 2>>"$LOG" \
  || log "code deploy: WARN snapshot failed (continuing — rollback may be unavailable)"

# 3. DEPLOY (additive; no --delete so excluded runtime dirs are never removed)
if rsync -az --out-format='%n' -e "$SSH" "${EXC[@]}" "$SRC" "$DEST" >>"$LOG" 2>&1; then
  log "code deploy: rsync applied"
else
  log "code deploy: rsync FAILED — not restarting (box keeps prior code)"; exit 1
fi

# 4. RESTART the authoritative service + verify /health comes back 200
$SSH "$BOXHOST" "systemctl restart $SVC" 2>>"$LOG"
ok=0
for i in 1 2 3 4 5 6 7 8; do
  sleep 3
  code="$($SSH "$BOXHOST" "curl -s -o /dev/null -w '%{http_code}' -m 6 $HEALTH" 2>>"$LOG")"
  [ "$code" = "200" ] && { ok=1; break; }
done

if [ "$ok" = "1" ]; then
  log "code deploy: OK — $SVC healthy (/health 200) after $N file(s) deployed"
  $SSH "$BOXHOST" "cd $DPATH && rm -rf .deploy_prev" 2>>"$LOG"
  exit 0
fi

# 5. ROLLBACK — the new code did not come up healthy; restore the snapshot and restart
log "code deploy: FAILED /health (last=$code) — ROLLING BACK to snapshot"
$SSH "$BOXHOST" "cd $DPATH && for d in $DIRS; do if [ -e \".deploy_prev/\$d\" ]; then rm -rf \"\$d\" && mv \".deploy_prev/\$d\" \"\$d\"; fi; done && systemctl restart $SVC" 2>>"$LOG"
sleep 4
code="$($SSH "$BOXHOST" "curl -s -o /dev/null -w '%{http_code}' -m 6 $HEALTH" 2>>"$LOG")"
log "code deploy: rollback complete — /health now $code"
exit 1

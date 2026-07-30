#!/usr/bin/env bash
# Transactional deployment of a clean Git archive to the authoritative VPS.
# Call through deploy_from_git.sh; arbitrary local working trees are refused.
set -euo pipefail

LOG="${MASTERMIND_DEPLOY_LOG:-/tmp/mm_vps_deploy.log}"
log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG"
}

case "$(printf '%s' "${MASTERMIND_SERVE_ONLY:-}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes) log "deploy refused: serve-only mode"; exit 2 ;;
esac
if [[ "${MASTERMIND_VPS_CODE_DEPLOY:-1}" == "0" ]]; then
  log "deploy refused: MASTERMIND_VPS_CODE_DEPLOY=0"
  exit 2
fi

SRC="${MASTERMIND_DEPLOY_SOURCE:-}"
EXPECTED_SHA="${MASTERMIND_DEPLOY_EXPECT_SHA:-}"
if [[ -z "$SRC" || ! -d "$SRC" ]]; then
  echo "Use scripts/deploy_from_git.sh; a clean archive source is required." >&2
  exit 2
fi
if [[ ! "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "A full merged git SHA is required for deployment provenance." >&2
  exit 2
fi
SRC="${SRC%/}/"

BOXHOST="${MASTERMIND_VPS_HOST:-root@146.190.142.17}"
DPATH="${MASTERMIND_VPS_PATH:-/opt/mastermind}"
KEY="${MASTERMIND_VPS_KEY:-/Users/chriswong/.ssh/macro_dashboard_deploy_v2}"
SVC="${MASTERMIND_VPS_SERVICE:-mastermind.service}"
HEALTH="${MASTERMIND_VPS_HEALTH:-http://127.0.0.1:8001/health}"

if [[ ! -f "$KEY" ]]; then
  log "deploy failed: SSH key is missing at $KEY"
  exit 1
fi

SSH=(ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=20)
RSYNC_SSH="ssh -i $KEY -o BatchMode=yes -o ConnectTimeout=20"

DIRS="app brain bot portfolio data_layer loop bridge control_plane scripts config ops"
FILES="pyproject.toml DOCTRINE.md README.md AGENTS.md"
EXC=(
  --exclude='.git' --exclude='.github' --exclude='.claude' --exclude='.codex'
  --exclude='.worktrees' --exclude='.venv' --exclude='venv'
  --exclude='data' --exclude='vendor' --exclude='vendor_*'
  --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo'
  --exclude='node_modules' --exclude='*.sqlite' --exclude='*.sqlite-*'
  --exclude='*.db' --exclude='*.log' --exclude='.DS_Store' --exclude='*.lock'
  --exclude='.pytest_cache' --exclude='.mypy_cache' --exclude='.ruff_cache'
  --exclude='.env' --exclude='.env.*' --exclude='tmp' --exclude='catboost_info'
  --exclude='{_DB}' --exclude='tests' --exclude='.deploy_prev'
)

RAW="$(rsync -azn --delete --out-format='%n' -e "$RSYNC_SSH" \
  "${EXC[@]}" "$SRC" "$BOXHOST:$DPATH/")" || {
    log "deploy failed: VPS dry-run/SSH check failed"
    exit 1
  }
CHANGED="$(printf '%s\n' "$RAW" | grep -vE '/$|^$' || true)"
N="$(printf '%s\n' "$CHANGED" | grep -c . || true)"

if [[ "${N:-0}" -eq 0 ]]; then
  CODE="$("${SSH[@]}" "$BOXHOST" \
    "curl -s -o /dev/null -w '%{http_code}' -m 6 '$HEALTH'")"
  [[ "$CODE" == "200" ]] || {
    log "deploy failed: code is in sync but health returned $CODE"
    exit 1
  }
  "${SSH[@]}" "$BOXHOST" \
    "printf '%s\n' '$EXPECTED_SHA' > '$DPATH/.deployed_git_sha'"
  log "deploy no-op: $EXPECTED_SHA already in sync and healthy"
  exit 0
fi

log "deploying $EXPECTED_SHA ($N changed path(s))"
printf '%s\n' "$CHANGED" | sed 's/^/    /' | tee -a "$LOG"

"${SSH[@]}" "$BOXHOST" \
  "cd '$DPATH' &&
   rm -rf .deploy_prev &&
   mkdir -p .deploy_prev &&
   for d in $DIRS; do [ ! -e \"\$d\" ] || cp -a \"\$d\" .deploy_prev/; done &&
   for f in $FILES; do [ ! -e \"\$f\" ] || cp -a \"\$f\" .deploy_prev/; done"

if ! rsync -az --delete --out-format='%n' -e "$RSYNC_SSH" \
  "${EXC[@]}" "$SRC" "$BOXHOST:$DPATH/" >>"$LOG" 2>&1; then
  log "deploy failed: rsync did not complete; service was not restarted"
  exit 1
fi

"${SSH[@]}" "$BOXHOST" "systemctl restart '$SVC'"
CODE="000"
for _ in 1 2 3 4 5 6 7 8; do
  sleep 3
  CODE="$("${SSH[@]}" "$BOXHOST" \
    "curl -s -o /dev/null -w '%{http_code}' -m 6 '$HEALTH'" || true)"
  [[ "$CODE" == "200" ]] && break
done

if [[ "$CODE" == "200" ]]; then
  "${SSH[@]}" "$BOXHOST" \
    "printf '%s\n' '$EXPECTED_SHA' > '$DPATH/.deployed_git_sha'"
  log "deploy OK: $SVC healthy at commit $EXPECTED_SHA"
  exit 0
fi

log "deploy failed health check ($CODE); rolling back"
"${SSH[@]}" "$BOXHOST" \
  "cd '$DPATH' &&
   for d in $DIRS; do
     if [ -e \".deploy_prev/\$d\" ]; then
       rm -rf \"\$d\" && cp -a \".deploy_prev/\$d\" \"\$d\"
     fi
   done &&
   for f in $FILES; do
     if [ -e \".deploy_prev/\$f\" ]; then
       cp -a \".deploy_prev/\$f\" \"\$f\"
     fi
   done &&
   systemctl restart '$SVC'"
sleep 4
CODE="$("${SSH[@]}" "$BOXHOST" \
  "curl -s -o /dev/null -w '%{http_code}' -m 6 '$HEALTH'" || true)"
log "rollback complete; health returned $CODE"
exit 1

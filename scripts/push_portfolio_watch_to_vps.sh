#!/usr/bin/env bash
# Push portfolio_watch state files to VPS (best-effort, never raises).
#
# Env vars (all optional — script exits 0 if any are missing):
#   VPS_HOST   e.g. user@vps.example.com
#   VPS_KEY    path to SSH private key (default: ~/.ssh/id_rsa)
#   VPS_DEST   remote destination path (default: ~/mastermind/data/portfolio_watch/)
#
# Only syncs state files; excludes the ohlcv/ price cache (large, host-local).
# PRD-R7: no entry prices or position sizes are included in these files.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="$REPO_ROOT/data/portfolio_watch"

VPS_HOST="${VPS_HOST:-root@146.190.142.17}"
VPS_KEY="${VPS_KEY:-$HOME/.ssh/macro_dashboard_deploy_v2}"
VPS_DEST="${VPS_DEST:-/opt/mastermind/data/portfolio_watch/}"

# If VPS_HOST is not set, exit silently (best-effort)
if [[ -z "$VPS_HOST" ]]; then
  echo "[push_portfolio_watch] VPS_HOST not set — skipping push" >&2
  exit 0
fi

# If data dir does not exist, nothing to push
if [[ ! -d "$DATA_DIR" ]]; then
  echo "[push_portfolio_watch] $DATA_DIR does not exist — skipping push" >&2
  exit 0
fi

SSH_OPTS=(-o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes)
if [[ -f "$VPS_KEY" ]]; then
  SSH_OPTS+=(-i "$VPS_KEY")
fi

# Ensure remote directory exists
ssh "${SSH_OPTS[@]}" "$VPS_HOST" "mkdir -p $VPS_DEST" 2>/dev/null || true

# rsync only the state files; exclude ohlcv/ price cache
rsync -az \
  --exclude='ohlcv/' \
  -e "ssh ${SSH_OPTS[*]}" \
  "$DATA_DIR/" \
  "$VPS_HOST:$VPS_DEST" \
  2>&1 | head -20 || true

echo "[push_portfolio_watch] push complete" >&2
exit 0

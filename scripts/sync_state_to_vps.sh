#!/bin/bash
# One-way Mac -> VPS sync of the Mastermind live paper-trading state (data/).
#
# The Mac is the SINGLE canonical writer (it runs the builds + the Brain). The droplet
# (bot.mastermind-x.com, mastermind.service, serve-only) only READS this state to render the
# dashboard, so we push it one direction: Mac -> box. NEVER sync box -> Mac.
#
# DRIVEN BY app.scheduler._vps_state_sync_job (every 15 min) — deliberately NOT a launchd job:
# launchd agents on this Mac are TCC-denied from reading ~/Documents, so the sync must run from
# the always-on Brain process, which HAS ~/Documents access and is the sole writer. The old
# com.mastermind.vpssync LaunchAgent could never work for that reason and was disabled 2026-06-28;
# the box then only refreshed on manual deploys and silently froze for ~5 days (last push
# 2026-07-02) until this job replaced it. Still safe to run by hand any time.
#
# Additive (no --delete) so a transient box-side write is harmless and gets corrected on the next
# push. scheduler.sqlite is excluded (the box scheduler is disabled).
set -euo pipefail

# Never push from the serve-only mirror to itself (box safety — the scheduler is already disabled
# under this flag, but guard the script too so a by-hand run on the box can't loop back).
[ -n "${MASTERMIND_SERVE_ONLY:-}" ] && exit 0

SRC="/Users/chriswong/Documents/Cluade/Mastermind/data/"
DEST="root@146.190.142.17:/opt/mastermind/data/"
KEY="/Users/chriswong/.ssh/macro_dashboard_deploy_v2"

rsync -az \
  -e "ssh -i $KEY -o BatchMode=yes -o ConnectTimeout=20" \
  --exclude='scheduler.sqlite' \
  --exclude='*.lock' \
  --exclude='.DS_Store' \
  "$SRC" "$DEST"

echo "$(date '+%Y-%m-%d %H:%M:%S') sync ok"

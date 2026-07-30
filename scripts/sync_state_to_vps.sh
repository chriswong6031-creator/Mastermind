#!/bin/bash
# RETIRED one-way Mac -> VPS bootstrap of the Mastermind paper-trading state (data/).
#
# Since the 2026-07-29 cutover, the VPS is the canonical scheduler/writer and its
# live state is isolated at /opt/mastermind-live-data.  This legacy helper still
# targets /opt/mastermind/data only as a recoverable bootstrap/debug surface;
# the public service does not mount that host path.
#
# DRIVEN BY app.scheduler._vps_state_sync_job (every 15 min) — deliberately NOT a launchd job.
# launchd child tools can lose the Python executable's ~/Documents TCC grant, so the trusted
# Python binary first stages data into /private/tmp and rsync reads that TCC-safe copy. The old
# com.mastermind.vpssync LaunchAgent could never work for that reason and was disabled 2026-06-28;
# the box then only refreshed on manual deploys and silently froze for ~5 days (last push
# 2026-07-02) until this job replaced it. Still safe to run by hand any time.
#
# Additive (no --delete) so a transient box-side write is harmless and gets corrected on the next
# push. scheduler.sqlite is excluded (the box scheduler is disabled).
set -euo pipefail
umask 077

# Never push from the serve-only mirror to itself (box safety — the scheduler is already disabled
# under this flag, but guard the script too so a by-hand run on the box can't loop back). Treat
# only truthy values as armed; MASTERMIND_SERVE_ONLY=0 is canonical-writer mode, not serve-only.
case "$(printf '%s' "${MASTERMIND_SERVE_ONLY:-}" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes) exit 0 ;;
esac

SRC="/Users/chriswong/Documents/Cluade/Mastermind/data/"
STAGE="/private/tmp/mastermind-vps-sync/data/"
DEST="root@146.190.142.17:/opt/mastermind/data/"
KEY="/Users/chriswong/.ssh/macro_dashboard_deploy_v2"
PYTHON="/opt/homebrew/Caskroom/miniconda/base/bin/python3"
STAGER="/Users/chriswong/Documents/Cluade/Mastermind/scripts/stage_state_for_vps.py"
LOG="/private/tmp/mastermind-vps-sync.log"

mkdir -p "$(dirname "$STAGE")"
chmod 700 "$(dirname "$STAGE")"
TOKEN="$("$PYTHON" "$STAGER" "$SRC" "$STAGE")"

/usr/bin/rsync -az \
  -e "/usr/bin/ssh -i $KEY -o BatchMode=yes -o ConnectTimeout=20" \
  --exclude='scheduler.sqlite' \
  --exclude='*.lock' \
  --exclude='.DS_Store' \
  "$STAGE" "$DEST"

REMOTE_VERIFY="$(/usr/bin/ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=20 \
  root@146.190.142.17 \
  "cd /opt/mastermind/data \
   && test \"\$(cat .vps_sync_token 2>/dev/null)\" = '$TOKEN' \
   && /usr/bin/sha256sum -c .vps_sync_manifest.sha256 >/dev/null \
   && printf verified" || true)"
if [ "$REMOTE_VERIFY" != "verified" ]; then
  printf '%s sync verification failed token=%s remote=%s\n' \
    "$(date '+%Y-%m-%d %H:%M:%S')" "$TOKEN" "${REMOTE_VERIFY:-missing}" >> "$LOG"
  echo "sync verification failed: VPS token or portfolio manifest mismatch" >&2
  exit 1
fi

printf '%s sync verified token=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$TOKEN" >> "$LOG"
echo "sync verified token=$TOKEN"

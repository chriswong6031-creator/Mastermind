#!/bin/bash
# Terminal-session launcher for the Mastermind bot. NOT the launchd
# entrypoint: launchd-spawned /bin/bash is TCC-blocked from reading
# ~/Documents (probed 2026-07-17), so com.mastermind.bot spawns
# scripts/launch_uvicorn.py with python3 directly instead.
# Sources .env (auth + flags), execs uvicorn.
# Incident 2026-07-06: never launch this from an agent-session shell —
# sandbox provenance on files created by that tree broke the sqlite
# jobstore hours later. launchd is the only supported production launcher.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a
source .env
set +a
exec /opt/homebrew/Caskroom/miniconda/base/bin/python3 -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8000 --log-level warning

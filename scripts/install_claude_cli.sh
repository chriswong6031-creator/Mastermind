#!/usr/bin/env bash
# Foundational setup: install the Claude Code CLI + Agent SDK into the bot server
# so the FastAPI app can drive Claude headlessly as its LLM reasoning layer.
set -euo pipefail

echo "== 1. Claude Code CLI =="
if command -v claude >/dev/null 2>&1; then
  echo "  already installed: $(claude --version)"
else
  npm install -g @anthropic-ai/claude-code   # native: curl -fsSL https://claude.ai/install.sh | bash
  echo "  installed: $(claude --version)"
fi

echo "== 2. Claude Agent SDK (Python) =="
python3 -m pip install --upgrade claude-agent-sdk pyyaml

echo "== 3. Auth (subscription tokens, no API key) =="
cat <<'EOF'
  The reasoning layer uses the subscription's included tokens, NOT a metered API key.
  - Local dev: a normal `claude` login (keychain) is picked up automatically.
  - A headless server: mint a long-lived token once on a machine with a browser:
        claude setup-token            # prints a ~1-year OAuth token
    then on the server:
        export CLAUDE_CODE_OAUTH_TOKEN="<token>"
  Credential resolution order: ANTHROPIC_AUTH_TOKEN > ANTHROPIC_API_KEY >
  CLAUDE_CODE_OAUTH_TOKEN > subscription login.  Force the metered API instead with
  BOT_LLM_BACKEND=api + ANTHROPIC_API_KEY.

== 4. Verify ==
  claude doctor
  python -c "from brain import cli_bridge; print(cli_bridge.available())"
EOF

#!/opt/homebrew/Caskroom/miniconda/base/bin/python3
"""launchd launcher for com.mastermind.bot — python3 end-to-end, NO bash.

launchd-spawned /bin/bash is TCC-blocked from reading ~/Documents (probed
2026-07-17: cd into the repo works, file reads return EPERM), so a plist that
runs scripts/run_uvicorn.sh via bash can never boot after a reboot. The
miniconda python3 binary holds the Documents grant, so launchd must spawn
python3 directly and everything — .env parsing included — happens in-process.
Keep bash (and any other interpreter) out of the ProgramArguments chain.

Mirrors scripts/run_uvicorn.sh (the terminal-session launcher): cd repo,
export .env (`set -a; source .env` semantics), exec uvicorn on 127.0.0.1:8000.

MM_LAUNCH_DRYRUN=1 prints the resolved launch plan and exits without starting
uvicorn (used by the launchd TCC probe; safe while another instance owns 8000).
"""
import os

REPO = "/Users/chriswong/Documents/Cluade/Mastermind"
PYTHON = "/opt/homebrew/Caskroom/miniconda/base/bin/python3"
ARGV = [PYTHON, "-m", "uvicorn", "app.main:app",
        "--host", "127.0.0.1", "--port", "8000", "--log-level", "warning"]


def load_env():
    path = os.path.join(REPO, ".env")
    try:
        from dotenv import dotenv_values
        return {k: v for k, v in dotenv_values(path).items() if v is not None}
    except ImportError:
        # stdlib fallback — the .env is plain KEY=VALUE (no quoting/expansion)
        env = {}
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip().strip("'\"")
        return env


# launchd's default PATH (/usr/bin:/bin:/usr/sbin:/sbin) lacks the claude CLI
# (~/.local/bin) and homebrew (node, git-lfs, …).  Without these, cli_bridge
# .available() is False and EVERY brain seat silently skips — observed live
# 2026-07-17 09:00Z hk_daily (run_id null, "claude CLI not installed").
_PATH_PREPEND = [
    "/Users/chriswong/.local/bin",
    "/opt/homebrew/bin",
]


def _fix_path() -> None:
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for p in reversed(_PATH_PREPEND):
        if p not in parts:
            parts.insert(0, p)
    os.environ["PATH"] = os.pathsep.join(parts)


def main():
    os.chdir(REPO)
    env = load_env()
    os.environ.update(env)
    _fix_path()
    if os.environ.get("MM_LAUNCH_DRYRUN"):
        from shutil import which
        print("DRYRUN_OK env_keys=%d require_auth_present=%s claude_on_path=%s argv=%s"
              % (len(env), "MASTERMIND_REQUIRE_AUTH" in env,
                 bool(which("claude")), " ".join(ARGV)),
              flush=True)
        return
    os.execv(PYTHON, ARGV)


if __name__ == "__main__":
    main()

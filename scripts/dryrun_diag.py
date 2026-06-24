"""Surface the RAW errors the desk seats swallow — why did STRATEGIST return None and the PM
return ran=False, while the RISK OFFICER's client.call_model worked? Read-only; no trades."""
import os
import sys
import json
import traceback
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["MASTERMIND_FLAGSHIP_JUDGMENT"] = "1"
import bot  # noqa: E402,F401

ASOF = date.today().isoformat()
try:
    regime = json.load(open("vendor/macro/data/regime/latest.json"))
except Exception:
    regime = {}


def hdr(s):
    print("\n############ " + s + " ############", flush=True)


hdr("STRATEGIST — raw input + assess")
try:
    from brain import strategist as S
    print("enabled():", S.enabled())
    inp = S._strategist_input(regime, ASOF)
    print("input keys:", list(inp.keys()))
    print("input sizes:", {k: (len(v) if hasattr(v, "__len__") else v) for k, v in inp.items()})
    out = S.strategist_assess(ASOF, regime)
    print("strategist_assess ->", type(out).__name__, ("None" if out is None else str(out)[:400]))
except Exception:
    traceback.print_exc()

hdr("CLI_BRIDGE — minimal headless call (the PM's path)")
try:
    from brain import cli_bridge
    fn = getattr(cli_bridge, "reason_sync", None)
    if fn is None:
        print("no reason_sync; trying reason via asyncio")
        import asyncio
        r = asyncio.run(cli_bridge.reason("Reply with the single word OK.", role="deep", max_turns=2))
    else:
        r = fn("Reply with the single word OK.", role="deep", max_turns=2)
    if isinstance(r, dict):
        print("reason result keys:", list(r.keys()))
        print("  ok:", r.get("ok"), "| backend:", r.get("backend"), "| model:", r.get("model"),
              "| error:", r.get("error"))
        print("  text:", str(r.get("text"))[:200])
    else:
        print("reason result:", type(r).__name__, str(r)[:300])
except Exception:
    traceback.print_exc()

hdr("AUTONOMOUS_MCP — build_servers (the PM's tool surface)")
try:
    from brain import autonomous_mcp
    s = autonomous_mcp.build_servers()
    print("build_servers OK:", list(s.keys()) if isinstance(s, dict) else type(s).__name__)
    print("allowed_tools count:", len(autonomous_mcp.allowed_tools()))
except Exception:
    traceback.print_exc()

print("\n############ DIAG DONE ############", flush=True)

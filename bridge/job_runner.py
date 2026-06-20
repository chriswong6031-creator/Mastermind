"""File-based job orchestration (the reliable batch pattern).

The web app packages a unit of work into a job directory, then runs Claude headlessly
inside it: Claude reads the input files, reasons, and writes result.json back. Simple and
very reliable — no MCP needed. (For interactive back-and-forth + write-back actions, use
the armed MCP path in brain/cli_bridge.research instead.)

    jobs/<job_id>/
      input.json          # the data snapshot
      <extra files...>    # csv / md / json the app drops in
      instructions.md     # the task
      output_schema.json  # optional: the shape Claude must return
      result.json         # <- Claude writes this
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import bot  # noqa: F401

from brain import cli_bridge

_ROOT = Path(__file__).resolve().parent.parent
_JOBS = _ROOT / "jobs"


def create_job(name: str, *, files: dict[str, str], instructions: str,
               output_schema: dict | None = None) -> str:
    """Write a job directory. `files` maps filename -> text content. Returns the job_id."""
    job_id = f"{int(time.time())}_{re.sub(r'[^a-z0-9]+', '-', name.lower())[:32].strip('-')}"
    d = _JOBS / job_id
    d.mkdir(parents=True, exist_ok=True)
    for fn, content in files.items():
        (d / fn).write_text(content)
    (d / "instructions.md").write_text(instructions)
    if output_schema is not None:
        (d / "output_schema.json").write_text(json.dumps(output_schema, indent=2))
    return job_id


async def run_job(job_id: str, *, role: str = "analyst", max_turns: int = 12,
                  arm: bool = False) -> dict:
    """Run Claude inside the job dir; it must write result.json. Returns {ok, result, raw, job_id}."""
    d = _JOBS / job_id
    if not d.exists():
        return {"ok": False, "job_id": job_id, "error": "job not found"}
    schema_note = ""
    if (d / "output_schema.json").exists():
        schema_note = "\nYour result.json MUST conform to output_schema.json in this directory."
    prompt = (f"Read every file in the current directory (start with instructions.md), do the work, "
              f"and WRITE your final answer to result.json in this directory.{schema_note} "
              f"Reply with a one-line summary when done.")
    r = await cli_bridge.reason(prompt, role=role, max_turns=max_turns, cwd=str(d), arm=arm,
                                allowed_tools=(None if arm else ["Read", "Grep", "Glob", "Write"]))
    result = None
    rp = d / "result.json"
    if rp.exists():
        try:
            result = json.loads(rp.read_text())
        except Exception:
            result = {"_raw": rp.read_text()[:4000]}
    return {"ok": bool(result) and r.get("ok", False), "job_id": job_id,
            "result": result, "summary": r.get("text"), "tools_used": r.get("tools_used"),
            "cost_usd": r.get("cost_usd"), "backend": r.get("backend"), "error": r.get("error")}

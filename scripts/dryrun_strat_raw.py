"""Capture the RAW client.call_model response for the STRATEGIST so we can see WHY it returns None
(API error vs prose-not-JSON vs truncated JSON). Read-only; no trades."""
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

from brain import strategist as S  # noqa: E402
from brain import client  # noqa: E402

inp = S._strategist_input(regime, ASOF)
user = json.dumps(inp, default=str)
print("payload chars:", len(user))
print("client.available:", client.available())
try:
    txt, meta = client.call_model(S._STRATEGIST_SYS, user, role="deep", max_tokens=2800)
    print("RETURN ok. meta:", meta)
    print("txt type:", type(txt).__name__, "| len:", len(txt or ""))
    print("---- txt[:600] ----")
    print((txt or "")[:600])
    print("---- txt[-400:] ----")
    print((txt or "")[-400:])
    print("---- parse result ----")
    print(S._parse_json(txt))
except Exception:
    print("client.call_model RAISED:")
    traceback.print_exc()

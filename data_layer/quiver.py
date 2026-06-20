"""Quiver Quant strategy scraper — competitive intelligence on their AI portfolios.

Authenticated HTTP only (no browser). Their strategy pages embed all the data as JS:
  topTenPicks = [{stock, justification, model, retrieved_at}, ...]   (the AI's reasoning)
  'portfolio'/'recently_opened'/'recently_closed'/'recently_rebalanced'/'upcoming_trades'
      = single-quote-wrapped, double-encoded JSON arrays
We log in, fetch each strategy, decode those, snapshot to a PIT store, and diff for trade
changes. Credentials come from env (QUIVER_USER / QUIVER_PASS) — never committed.

Goal: learn their investment PROCESS (not copy trades) and find where we can beat it.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

_ROOT = Path(__file__).resolve().parent.parent
_STORE = _ROOT / "data" / "quiver"
BASE = "https://www.quiverquant.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

STRATEGIES = {
    "chatgpt_enhanced": "ChatGPT - Quiver Enhanced",
    "claude_enhanced": "Claude - Quiver Enhanced",
    "chatgpt_standard": "ChatGPT - Standard",
    "claude_standard": "Claude - Standard",
}
_METRIC_LABELS = ["Sharpe Ratio", "Max Drawdown", "Volatility", "Win Rate", "Alpha", "Beta",
                  "Total Return", "Annualized Return", "Sortino"]


def login() -> requests.Session:
    user, pw = os.environ.get("QUIVER_USER"), os.environ.get("QUIVER_PASS")
    if not (user and pw):
        raise RuntimeError("set QUIVER_USER and QUIVER_PASS in the environment")
    s = requests.Session()
    s.headers["User-Agent"] = UA
    r = s.get(f"{BASE}/login/", timeout=20)
    csrf = BeautifulSoup(r.text, "lxml").find("input", {"name": "csrfmiddlewaretoken"})["value"]
    s.post(f"{BASE}/login/", data={"csrfmiddlewaretoken": csrf, "username": user, "password": pw},
           headers={"Referer": f"{BASE}/login/"}, timeout=20)
    return s


def _arr(html: str, key: str):
    m = re.search(r"'" + key + r"'\s*:\s*'([^']*)'", html, re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(1))
    except Exception:
        return []


def _picks(html: str):
    m = re.search(r"(?:const|var|let)\s+topTenPicks\s*=\s*", html)
    if not m:
        return []
    try:
        return json.JSONDecoder().raw_decode(html, m.end())[0]
    except Exception:
        return []


def _metrics(html: str) -> dict:
    out = {}
    for lab in _METRIC_LABELS:
        m = re.search(re.escape(lab) + r"\s*</[^>]+>\s*<[^>]+>\s*(-?[\d.]+%?)", html) or \
            re.search(r"(-?[\d.]+%?)\s*<[^>]*>\s*" + re.escape(lab), html)
        if m:
            out[lab] = m.group(1)
    return out


def _holdings(rows):
    return [{"ticker": r[0], "value": float(r[1]), "pct_nav": round(float(r[2]), 3),
             "return_since_open": round(float(r[4]), 2) if len(r) > 4 and r[4] not in ("", None) else None}
            for r in rows if len(r) > 2]


def _trades(rows, kind):
    out = []
    for r in rows:
        if len(r) < 3:
            continue
        if kind == "rebalanced":
            out.append({"ticker": r[0], "date": r[1], "action": r[2],
                        "pct_nav": _f(r[3]), "shares_delta": _f(r[4]), "value_delta": _f(r[5]) if len(r) > 5 else None})
        else:
            out.append({"ticker": r[0], "date": r[1], "direction": r[2],
                        "shares": _f(r[3]) if len(r) > 3 else None, "value": _f(r[4]) if len(r) > 4 else None})
    return out


def _f(x):
    try:
        return float(x)
    except Exception:
        return None


def fetch_strategy(s: requests.Session, key: str) -> dict:
    slug = STRATEGIES[key]
    html = s.get(f"{BASE}/strategies/s/{slug}/", timeout=30).text
    picks = _picks(html)
    return {
        "strategy": key, "slug": slug, "scraped": date.today().isoformat(),
        "model": picks[0].get("model") if picks else None,
        "metrics": _metrics(html),
        "holdings": _holdings(_arr(html, "portfolio")),
        "upcoming_trades": _trades(_arr(html, "upcoming_trades"), "open"),
        "recently_opened": _trades(_arr(html, "recently_opened"), "open"),
        "recently_closed": _trades(_arr(html, "recently_closed"), "close"),
        "recently_rebalanced": _trades(_arr(html, "recently_rebalanced"), "rebalanced"),
        "top_picks": [{"stock": p.get("stock"), "model": p.get("model"),
                       "retrieved_at": p.get("retrieved_at"), "justification": p.get("justification")}
                      for p in picks],
    }


def snapshot(data: dict) -> Path:
    p = _STORE / data["strategy"] / f"{data['scraped']}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, default=str))
    return p


def latest_snapshot(key: str) -> dict | None:
    d = _STORE / key
    snaps = sorted(d.glob("*.json")) if d.exists() else []
    return json.loads(snaps[-1].read_text()) if snaps else None


def diff_holdings(key: str) -> dict:
    """Compare the two most recent snapshots' holdings -> added / dropped / reweighted."""
    d = _STORE / key
    snaps = sorted(d.glob("*.json")) if d.exists() else []
    if len(snaps) < 2:
        return {"note": "need >=2 snapshots"}
    prev = {h["ticker"]: h["pct_nav"] for h in json.loads(snaps[-2].read_text())["holdings"]}
    cur = {h["ticker"]: h["pct_nav"] for h in json.loads(snaps[-1].read_text())["holdings"]}
    return {"added": sorted(set(cur) - set(prev)), "dropped": sorted(set(prev) - set(cur)),
            "reweighted": {t: [prev[t], cur[t]] for t in set(cur) & set(prev) if abs(cur[t] - prev[t]) > 0.5}}


def pull_all() -> dict:
    s = login()
    out = {}
    for key in STRATEGIES:
        d = fetch_strategy(s, key)
        snapshot(d)
        out[key] = d
    return out

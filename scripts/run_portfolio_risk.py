"""CLI runner for the portfolio held-risk lane composer.

Fetches operator positions from Supabase PostgREST (using service-role key),
then calls portfolio.held_risk.compose() and optionally writes the result.

Usage:
    python -m scripts.run_portfolio_risk [--dry-run] [--positions-json PATH] [--out PATH]

Env vars consumed (names only — values never printed):
    SUPABASE_URL               e.g. https://fsldfzlxyavsuwqbceod.supabase.co
    SUPABASE_SERVICE_ROLE_KEY  service-role JWT
    MASTERMIND_OPERATOR_EMAIL  operator email (default: demo@mastermind.test)

PRD-R7: positions/entry prices never logged, never committed.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("run_portfolio_risk")

_DEFAULT_EMAIL = "demo@mastermind.test"
_DEFAULT_OUT = _REPO_ROOT / "data" / "portfolio_watch" / "risk_state.json"


# ---------------------------------------------------------------------------
# Supabase helpers (~40 lines, self-contained)
# ---------------------------------------------------------------------------

def _supabase_url() -> str | None:
    return os.environ.get("SUPABASE_URL")


def _supabase_key() -> str | None:
    return os.environ.get("SUPABASE_SERVICE_ROLE_KEY")


def _operator_email() -> str:
    return os.environ.get("MASTERMIND_OPERATOR_EMAIL") or _DEFAULT_EMAIL


def _resolve_operator_uuid(base_url: str, key: str, email: str) -> str | None:
    """Resolve operator UUID via Supabase auth admin API (/auth/v1/admin/users)."""
    try:
        import httpx
        resp = httpx.get(
            f"{base_url}/auth/v1/admin/users",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            params={"email": email},
            timeout=10,
        )
        if resp.status_code != 200:
            log.warning("auth admin lookup returned %s", resp.status_code)
            return None
        data = resp.json()
        users = data.get("users") or data if isinstance(data, list) else []
        for u in users:
            if isinstance(u, dict) and u.get("email") == email:
                return u.get("id")
    except Exception as exc:
        log.warning("operator UUID resolution failed: %s", exc)
    return None


def _fetch_positions(base_url: str, key: str, operator_uuid: str) -> list[dict]:
    """Fetch active positions for operator from Supabase PostgREST."""
    try:
        import httpx
        resp = httpx.get(
            f"{base_url}/rest/v1/portfolio_positions",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            params={
                "user_id": f"eq.{operator_uuid}",
                "status": "eq.active",
                "select": "id,ticker,shares,entry_price,entry_date,status,notes",
            },
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning("positions fetch returned HTTP %s", resp.status_code)
            return []
        return resp.json() or []
    except Exception as exc:
        log.warning("positions fetch failed: %s", exc)
        return []


def _load_positions_from_supabase() -> list[dict]:
    """Try to fetch operator positions; fail-soft, return [] on any error."""
    url = _supabase_url()
    key = _supabase_key()
    if not url or not key:
        log.info("SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set — skipping fetch")
        return []
    email = _operator_email()
    uid = _resolve_operator_uuid(url, key, email)
    if not uid:
        log.info("operator UUID not found for %s — skipping", email)
        return []
    positions = _fetch_positions(url, key, uid)
    log.info("fetched %d active positions for operator %s", len(positions), email[:4] + "***")
    return positions


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Run portfolio held-risk composer")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print composed state to stdout; do not write to disk",
    )
    parser.add_argument(
        "--positions-json", metavar="PATH",
        help="Load positions from a JSON file instead of Supabase (for testing)",
    )
    parser.add_argument(
        "--out", metavar="PATH", default=str(_DEFAULT_OUT),
        help=f"Output path for risk_state.json (default: {_DEFAULT_OUT})",
    )
    args = parser.parse_args()

    # Load positions
    if args.positions_json:
        try:
            with open(args.positions_json) as f:
                positions = json.load(f)
            if not isinstance(positions, list):
                log.error("--positions-json must be a JSON array; got %s", type(positions).__name__)
                sys.exit(1)
            log.info("loaded %d positions from %s", len(positions), args.positions_json)
        except Exception as exc:
            log.error("failed to load positions from %s: %s", args.positions_json, exc)
            sys.exit(1)
    else:
        positions = _load_positions_from_supabase()
        if not positions:
            log.info("no positions to compose — exiting (exit 0)")
            sys.exit(0)

    # Import composer
    from portfolio.held_risk import VENDOR_DEFAULT, DATA_DEFAULT, compose, write_state

    # Determine vendor_root
    vendor_root = VENDOR_DEFAULT

    try:
        state = compose(
            positions,
            vendor_root=vendor_root,
            data_root=DATA_DEFAULT,
            now=date.today(),
        )
    except Exception as exc:
        log.error("compose() failed: %s", exc)
        sys.exit(1)

    if args.dry_run:
        print(json.dumps(state, indent=2, default=str))
        log.info("dry-run: %d positions composed, not written", len(state.get("positions", [])))
    else:
        out_path = Path(args.out)
        write_state(state, out_path)
        log.info("wrote risk_state.json to %s (%d positions)", out_path, len(state.get("positions", [])))


if __name__ == "__main__":
    main()

"""Public-snapshot privacy projection (RUL-CL-6b BRIDGE law).

The macro repo's site/mastermind/mastermind_snapshot.json is a PUBLIC artifact
(public GitHub repo + registered-user serving path).  The macro-side rulings
(config/ruling_graph.yml site_privacy_tokens, enforced by
scripts/check_ruling_conflicts.py H2 and the FB-R13 partial key scan in
scripts/check_private_boundary.py) ban held-book/fill economics there:
cost_basis, avg_cost, fill_price, position_size, plus sibling fields the
FB-R13 forbidden-key list names (entry_price, shares, notional, avg_price).

Regression pin for the 2026-08-01 exposure: bridge/macro_snapshot.py passed
each book's latest.json positions through WHOLESALE, so 37 cost_basis keys
(plus entry_price/shares/current_price/market_value/unrealized_*) sat on the
public macro main for weeks (surfaced by macro PR #4209, fixed at source by
the _project() whitelist these tests cover).
"""
from __future__ import annotations

from bridge import macro_snapshot

# Keys that must NEVER survive the projection into the public snapshot —
# the union of the macro H2 site_privacy_tokens and the FB-R13 forbidden
# economics keys that actually flow through book latest.json files.
_BANNED = {
    "cost_basis", "avg_cost", "fill_price", "position_size", "held_book",
    "book_positions", "fills_ledger",
    "entry_price", "current_price", "market_value", "unrealized_pnl",
    "unrealized_pct", "shares", "notional", "avg_price",
}


def test_whitelists_carry_no_banned_keys() -> None:
    """The whitelists themselves must never drift into banned territory."""
    assert not (_BANNED & set(macro_snapshot._PUBLIC_POSITION_KEYS))
    assert not (_BANNED & set(macro_snapshot._PUBLIC_PENDING_ORDER_KEYS))


def test_position_projection_strips_fill_economics() -> None:
    """A realistic brain-book position loses every economics field and keeps
    the composition/reasoning fields the public page renders."""
    raw = [{
        "ticker": "0700.HK",
        "name": "Tencent",
        "sleeve": "brain",
        "venue": "HK",
        "weight": 0.1391,
        "verdict": "hold",
        "conviction": "high",
        "rationale": "why we hold it",
        "opened_at": "2026-06-22T16:02:17+00:00",
        "held_days": 37,
        "cost_basis": 448.3611,
        "current_price": 447.2,
        "market_value": 139720.38,
        "unrealized_pnl": -362.75,
        "unrealized_pct": -0.26,
        "entry_price": 448.0,
        "shares": 312.0,
        "thesis_full": {"summary": "thesis"},
    }]
    out = macro_snapshot._project(raw, macro_snapshot._PUBLIC_POSITION_KEYS)
    assert len(out) == 1
    got = out[0]
    assert not (_BANNED & set(got)), f"banned keys survived: {_BANNED & set(got)}"
    for kept in ("ticker", "name", "sleeve", "venue", "weight", "verdict",
                 "conviction", "rationale", "opened_at", "held_days", "thesis_full"):
        assert kept in got, f"public field {kept!r} was dropped"


def test_pending_order_projection_strips_share_counts() -> None:
    """Pending orders queue real share counts (portfolio/paper_account.py);
    only the intent (ticker/side/status) is public."""
    raw = [{"ticker": "XLE", "side": "buy", "status": "queued", "shares": 120.5}]
    out = macro_snapshot._project(raw, macro_snapshot._PUBLIC_PENDING_ORDER_KEYS)
    assert out == [{"ticker": "XLE", "side": "buy", "status": "queued"}]


def test_projection_tolerates_junk_rows() -> None:
    """Non-list input and non-dict rows must not raise (scheduler-safe)."""
    assert macro_snapshot._project(None, macro_snapshot._PUBLIC_POSITION_KEYS) == []
    assert macro_snapshot._project("junk", macro_snapshot._PUBLIC_POSITION_KEYS) == []
    assert macro_snapshot._project(
        [None, 42, {"ticker": "A"}], macro_snapshot._PUBLIC_POSITION_KEYS
    ) == [{"ticker": "A"}]

"""Confirmation scorecard (doctrine §4) — the confluence gate / meta-label.

Six dimensions assembled from existing engines (see DOCTRINE.md):
  rs, breadth, flow, volume, catalyst, regime
Each arrives as one of: 'confirmed' | 'absent' | 'unverified'  (A7 honesty tag).

The pure confluence GATE is implemented here; the per-dimension reads that fill the
dict come from the engine via data_layer (rs/breadth/regime exist today; flow is a
rollup; volume + binding-catalyst are the two genuinely-new engine leaves).
"""
from __future__ import annotations

from bot.doctrine_config import load_doctrine

DIMS = ("rs", "breadth", "flow", "volume", "catalyst", "regime")


def confluence_gate(dims: dict[str, str]) -> dict:
    """Map confirmed/absent dims -> a sizing verdict. Doctrine rules 4.1-4.3.

    - one confirmed dim alone -> 'none' (single-signal action forbidden)
    - rs AND breadth confirmed -> 'initial' (Stage-1 early-follower, small size)
    - catalyst confirmed AND >=3 of {rs,breadth,flow,volume} confirmed -> 'full'
    """
    cfg = load_doctrine()["scorecard"]["confluence"]
    confirmed = {d for d in DIMS if dims.get(d) == "confirmed"}
    unverified = {d for d in DIMS if dims.get(d) == "unverified"}

    if cfg["forbid_single_dim"] and len(confirmed) <= 1:
        verdict = "none"
    elif cfg["full_size_gate"] in confirmed and len(confirmed & {"rs", "breadth", "flow", "volume"}) >= 3:
        verdict = "full"
    elif all(d in confirmed for d in cfg["initial_dims"]):
        verdict = "initial"
    else:
        verdict = "none"

    return {
        "size": verdict,
        "confirmed": sorted(confirmed),
        "missing": sorted(d for d in DIMS if d not in confirmed),
        "unverified": sorted(unverified),
        "catalyst_present": cfg["full_size_gate"] in confirmed,  # the full-size gate
    }


def read_dims(theme_id: str, region: str = "us") -> dict[str, str]:
    """Assemble the 6-dim read for a theme from the engine. TODO(data_layer).

    rs/breadth/regime -> theme_scoring legs; flow -> holdings_signals rollup;
    volume -> volume_signature leaf (NEW); catalyst -> theme_catalyst_binder (NEW).
    """
    raise NotImplementedError("wire to engine.theme_scoring + the new leaves")

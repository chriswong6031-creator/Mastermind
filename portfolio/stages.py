"""Theme lifecycle Stage 0-4 classifier (doctrine §3).

Fuses existing per-theme fields (group_flow._stage, theme_scoring._label,
crowding/extension) into the doctrine's five stages and splits our coarser
4-label scheme. Pure decision logic here; field reads come from the engine.
"""
from __future__ import annotations

from dataclasses import dataclass

from bot.doctrine_config import load_doctrine


@dataclass
class ThemeFields:
    label: str            # theme_scoring: dominant|emerging|fading|deteriorating|neutral
    flow_stage: str       # group_flow._stage: quiet|emerging|confirmed|cooling
    rs_pctile: float      # 0..1
    breadth_pct50: float  # 0..1, fraction of basket above 50d
    crowded_z: float
    pct_parabolic: float  # theme_extension
    catalyst_confirmed: bool
    clean_entry: bool


def classify_stage(f: ThemeFields) -> dict:
    """Return {stage:0..4, fourth_deriv_tell:bool, action:str}."""
    s = load_doctrine()["stages"]
    fourth = f.pct_parabolic >= s["fourth_deriv_pct_parabolic"] and f.rs_pctile >= 0.80

    if f.label == "deteriorating" or f.flow_stage == "cooling":
        stage, action = 4, "exit / rotate"
    elif fourth or (f.crowded_z >= s["crowded_z"] and f.label in ("dominant", "fading")):
        stage, action = 3, "trim / tighten / stop adding"
    elif f.label == "dominant" or (f.catalyst_confirmed and f.breadth_pct50 >= 0.5):
        stage, action = 2, "core participation / add (full size if catalyst)"
    elif (f.label == "emerging" or f.flow_stage == "emerging") and f.rs_pctile < s["ignition_rs_pctile_max"] and f.clean_entry:
        stage, action = 1, "early-follower entry / initial size"
    elif f.rs_pctile < s["latent_rs_pctile_max"] and f.breadth_pct50 < s["latent_breadth_pct50_max"]:
        stage, action = 0, "watch / do not buy"
    else:
        stage, action = 1, "early-follower entry / initial size"

    return {"stage": stage, "fourth_deriv_tell": fourth, "action": action}

"""Phase 0 acceptance test — the live regime recompute matches the published one."""
import json
from pathlib import Path

import bot  # noqa: F401  -> vendor/macro bootstrap


def test_engine_imports_and_reproduces_regime():
    from lib import config
    from engine import inputs, regime

    data_dir = Path(config.data_dir())
    latest = json.loads((data_dir / "regime" / "latest.json").read_text())

    cls = regime.classify(inputs.build_features())
    live_quad = str(cls.iloc[-1]["quad"])

    assert live_quad == latest["quad"], (
        f"live recompute {live_quad} != published {latest['quad']}"
    )

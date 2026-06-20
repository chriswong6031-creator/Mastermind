"""Mastermind — autonomous narrative-investing agent.

Importing anything under `bot.` first bootstraps the vendored macro
dashboard onto sys.path so `engine.*` and `lib.*` import as a library.
In production `vendor/macro` is a pinned git submodule; for local Phase 0
it is a symlink to the working macro checkout (so `data/` is populated).
"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_VENDOR = _ROOT / "vendor" / "macro"
if _VENDOR.exists() and str(_VENDOR) not in sys.path:
    # insert at 0 so the pinned engine wins over any stray global install
    sys.path.insert(0, str(_VENDOR))

# load local secrets (.env) — gitignored, never committed. Explicit env vars win.
for _name in (".env", ".env.local"):
    _f = _ROOT / _name
    if _f.exists():
        for _line in _f.read_text().splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

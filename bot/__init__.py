"""narrator-bot — autonomous narrative-investing agent.

Importing anything under `bot.` first bootstraps the vendored macro
dashboard onto sys.path so `engine.*` and `lib.*` import as a library.
In production `vendor/macro` is a pinned git submodule; for local Phase 0
it is a symlink to the working macro checkout (so `data/` is populated).
"""
import sys
from pathlib import Path

_VENDOR = Path(__file__).resolve().parent.parent / "vendor" / "macro"
if _VENDOR.exists() and str(_VENDOR) not in sys.path:
    # insert at 0 so the pinned engine wins over any stray global install
    sys.path.insert(0, str(_VENDOR))

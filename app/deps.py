"""FastAPI dependency bootstrap.

Ensures the vendored macro engine is importable before any route handler
touches `engine.*`. Importing `bot` performs the sys.path insert; this module
re-exports a couple of helpers the API layer will lean on so route code can
`from app.deps import engine_root, secret`.
"""
import bot  # noqa: F401  -> vendor/macro onto sys.path

from lib import config  # noqa: E402  (must come after the bootstrap)


def engine_root() -> str:
    return str(config.ROOT)


def data_dir() -> str:
    return str(config.data_dir())


def secret(name: str, default: str | None = None) -> str | None:
    """Pass-through to the macro config secret resolver (env-backed)."""
    try:
        return config.secret(name) or default
    except Exception:
        return default

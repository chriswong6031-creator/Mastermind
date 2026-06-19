"""Loader for config/doctrine.yml (the tunable doctrine parameters)."""
from __future__ import annotations

import functools
from pathlib import Path

import yaml

_PATH = Path(__file__).resolve().parent.parent / "config" / "doctrine.yml"


@functools.lru_cache(maxsize=1)
def load_doctrine() -> dict:
    return yaml.safe_load(_PATH.read_text())

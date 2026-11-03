"""Where depfix keeps per-user data (approval key, documentation index). Never inside a repo."""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    base = os.environ.get("DEPFIX_HOME")
    return Path(base) if base else Path.home() / ".config" / "depfix"


def index_path() -> Path:
    return home() / "index.db"

from __future__ import annotations

import sys
from pathlib import Path


def project_root() -> Path:
    bundled_root = getattr(sys, "_MEIPASS", None)
    if bundled_root:
        return Path(bundled_root)
    return Path(__file__).resolve().parent.parent


def asset_path(name: str) -> Path:
    path = project_root() / "assets" / name
    if not path.is_file():
        raise FileNotFoundError(f"Missing asset: {path}")
    return path


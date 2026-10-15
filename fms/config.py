"""FMS configuration: data directories and file locations.

Windows-compatible: uses %APPDATA%\\FMS on Windows, ~/.fms elsewhere.
Set the FMS_DATA_DIR environment variable to override (useful for testing).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "FMS"


def resource_path(*parts: str) -> Path:
    """Path to a bundled resource (templates, static, ...).

    Works in dev (source tree) and frozen (PyInstaller onefile via sys._MEIPASS).
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).resolve().parent
    return base.joinpath(*parts)


def data_dir() -> Path:
    override = os.environ.get("FMS_DATA_DIR")
    if override:
        base = Path(override)
    elif os.name == "nt" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"]) / APP_NAME
    else:
        base = Path.home() / ".fms"
    base.mkdir(parents=True, exist_ok=True)
    return base


def db_path() -> Path:
    return data_dir() / "fms.db"


def key_path() -> Path:
    return data_dir() / "fms.key"


def profiles_dir() -> Path:
    p = data_dir() / "profiles"
    p.mkdir(parents=True, exist_ok=True)
    return p


def categories_path() -> Path:
    return data_dir() / "categories.json"

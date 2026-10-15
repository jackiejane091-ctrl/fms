#!/usr/bin/env python3
"""Build FMS.exe — small app-only Windows executable (PyInstaller onefile).

SPLIT BUNDLE (since v0.2.0): the exe no longer bundles Chromium (~600 MB).
Instead:
  - FMS.exe (~50 MB): the app only. Run ON WINDOWS:  python build_exe.py
  - browsers-windows.zip: built separately by the GitHub workflow
    (.github/workflows/build-windows.yml, job "build-browsers") from
    `python -m playwright install chromium` on a Windows runner.
    The user downloads it ONCE via the dashboard's one-time setup banner;
    FMS then points Playwright at it via PLAYWRIGHT_BROWSERS_PATH
    (see fms/browsers.py).

Prerequisites:  pip install -r fms/requirements.txt pyinstaller
Output: dist/FMS.exe
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("Installing PyInstaller…")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "pyinstaller"])


def main() -> None:
    ensure_pyinstaller()

    datas = [
        (str(ROOT / "fms" / "templates"), "templates"),
        (str(ROOT / "fms" / "static"), "static"),
    ]
    add_data = []
    for src, dest in datas:
        add_data += ["--add-data", f"{src}{os.pathsep}{dest}"]

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", "FMS",
        "--onefile",
        # Windowed: no console window for the non-technical user.
        # (Build with --console instead while debugging.)
        "--windowed",
        *add_data,
        # uvicorn's protocol/loop modules are imported dynamically.
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols",
        "--hidden-import", "uvicorn.protocols.http",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan",
        "--hidden-import", "uvicorn.lifespan.on",
        "--hidden-import", "multipart",
        # Playwright's driver (node) ships as package data. The *browser
        # binaries* are intentionally NOT bundled (split bundle).
        "--collect-data", "playwright",
        str(ROOT / "fms" / "launcher.py"),
    ]
    print("Running PyInstaller (app-only, no bundled browsers)…")
    subprocess.check_call(cmd, cwd=str(ROOT))

    exe = ROOT / "dist" / ("FMS.exe" if os.name == "nt" else "FMS")
    size_mb = exe.stat().st_size / 1e6
    print(f"\nBuilt: {exe}  ({size_mb:.0f} MB)")
    print("Smoke test on the target machine: double-click FMS.exe -> "
          "dashboard should open; the one-time browsers setup banner appears "
          "on first launch.")


if __name__ == "__main__":
    main()

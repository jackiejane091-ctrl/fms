"""FMS - personal Facebook Marketplace auto-listing tool."""

__version__ = "0.2.79"
__phase__ = 2

# Frozen (PyInstaller onefile) bootstrap: point Playwright at the local
# browsers folder (split bundle — Chromium is downloaded once on first
# launch, not bundled into the exe). See fms/browsers.py.
import os as _os
import sys as _sys

if getattr(_sys, "frozen", False):
    try:
        from fms.browsers import browsers_dir, browsers_ready

        _bdir = browsers_dir()
        if browsers_ready(_bdir):
            _os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_bdir))
    except Exception:
        pass

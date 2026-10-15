"""FMS browsers bootstrap: one-time Chromium download for the split bundle.

The Windows FMS.exe no longer bundles Chromium (~600 MB). Instead, on first
launch FMS looks for a local ``browsers/`` folder:

  1. next to FMS.exe (portable: <exe_dir>/browsers) — if it already exists
     there, or the exe dir is writable so we can create it;
  2. %LOCALAPPDATA%\\FMS\\browsers on Windows;
  3. ~/.fms/browsers (dev / fallback).

``FMS_BROWSERS_DIR`` env var overrides everything (used by tests).

If the folder is missing or has no Chromium in it, the dashboard shows a
one-time setup banner and downloads a zip (built on the Windows CI runner
from ``python -m playwright install chromium``) whose URL comes from the
version file on Google Drive. The zip contains the ``chromium-XXXX/``
folder at its root; after extraction Playwright is pointed at the folder
via PLAYWRIGHT_BROWSERS_PATH.
"""
from __future__ import annotations

import os
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

CHUNK = 1024 * 256  # 256 KB streaming chunks


def _exe_dir() -> Path | None:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return None


def browsers_dir() -> Path:
    """Resolve where the browsers folder should live (created on demand)."""
    override = os.environ.get("FMS_BROWSERS_DIR")
    if override:
        p = Path(override)
        p.mkdir(parents=True, exist_ok=True)
        return p
    exe_dir = _exe_dir()
    if exe_dir is not None:
        existing = exe_dir / "browsers"
        if existing.is_dir():
            return existing
        # Portable layout: keep everything next to the exe when we can write.
        try:
            existing.mkdir(parents=True, exist_ok=True)
            return existing
        except OSError:
            pass
        if os.name == "nt":
            p = Path(os.environ.get("LOCALAPPDATA", str(exe_dir))) / "FMS" / "browsers"
        else:
            p = Path.home() / ".fms" / "browsers"
        p.mkdir(parents=True, exist_ok=True)
        return p
    # Dev / tests.
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        p = Path(os.environ["LOCALAPPDATA"]) / "FMS" / "browsers"
    else:
        p = Path.home() / ".fms" / "browsers"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _chromium_exe_candidates() -> tuple[str, ...]:
    if os.name == "nt":
        return ("chrome-win/chrome.exe",)
    return ("chrome-linux/chrome", "chrome-linux/headless_shell")


def find_chromium(folder: Path) -> Path | None:
    """Return the Chromium executable inside a browsers folder, if present."""
    for sub in sorted(folder.glob("chromium-*")):
        if not sub.is_dir():
            continue
        for cand in _chromium_exe_candidates():
            exe = sub / cand
            if exe.is_file():
                return exe
    return None


def browsers_ready(folder: Path | None = None) -> bool:
    """True when the folder exists and contains a usable Chromium."""
    folder = folder or browsers_dir()
    return find_chromium(folder) is not None


def download_and_extract(url: str, folder: Path | None = None,
                         progress_cb=None) -> Path:
    """Download the browsers zip, extract it, verify Chromium, clean up.

    progress_cb(downloaded_bytes, total_bytes_or_None) is called as the
    download streams. Raises RuntimeError on failure; the folder is left
    untouched on download errors.
    """
    folder = folder or browsers_dir()
    folder.mkdir(parents=True, exist_ok=True)

    fd, tmp_zip = tempfile.mkstemp(prefix="fms_browsers_", suffix=".zip")
    os.close(fd)
    downloaded = 0
    total = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "FMS"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            length = resp.headers.get("Content-Length")
            total = int(length) if length and length.isdigit() else None
            with open(tmp_zip, "wb") as f:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        progress_cb(downloaded, total)
        if total and downloaded != total:
            raise RuntimeError(
                f"Incomplete download: got {downloaded} of {total} bytes.")
        with zipfile.ZipFile(tmp_zip) as zf:
            # Basic zip-slip guard.
            for member in zf.namelist():
                dest = (folder / member).resolve()
                if not str(dest).startswith(str(folder.resolve())):
                    raise RuntimeError("Unsafe path in browsers zip.")
            zf.extractall(folder)
    finally:
        try:
            os.unlink(tmp_zip)
        except OSError:
            pass

    exe = find_chromium(folder)
    if exe is None:
        raise RuntimeError(
            "Downloaded browsers are incomplete (no Chromium executable found).")
    # On POSIX the extracted chrome needs the executable bit.
    if os.name != "nt":
        try:
            exe.chmod(exe.stat().st_mode | 0o111)
        except OSError:
            pass
    return folder

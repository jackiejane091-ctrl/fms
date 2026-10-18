"""FMS self-updater: version check + small-exe download + swap on relaunch.

How it works (Windows):
  1. The dashboard's Update button calls /api/update/check, which fetches
     VERSION_URL (a small JSON file on the user's Google Drive, shared
     "anyone with the link") and compares versions.
  2. /api/update/download streams the new FMS.exe to FMS.new.exe next to
     the running exe (never touches the running file — Windows locks it).
  3. The dashboard prompts the user to quit and reopen FMS.
  4. On the next launch, launcher.py calls apply_pending_update(): if
     FMS.new.exe exists and looks complete, it replaces FMS.exe
     (keeping FMS.prev.exe as a rollback) before the server starts.

fms_version.json schema on Drive:
  {"version": "0.2.0",
   "exe_url": "https://drive.google.com/uc?id=<FILE_ID>&export=download",
   "exe_size": 52428800,
   "browsers_url": "https://drive.google.com/uc?id=<FILE_ID>&export=download",
   "changelog": "…"}
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

from fms import __version__ as CURRENT_VERSION

# Set at release time to the Drive direct-download URL of fms_version.json.
# (See ROADMAP.md "Release process".) Until then, update checks are skipped.
VERSION_URL = ("https://drive.google.com/uc"
               "?id=1ct7xfbLlUVRQ7WFAZ_0USm1wv7ijLNpN&export=download")

CHUNK = 1024 * 256
MIN_EXE_SIZE = 10_000_000  # sanity: a real FMS.exe is tens of MB


def _exe_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _version_tuple(v: str) -> tuple[int, ...]:
    parts = []
    for piece in str(v).strip().split("."):
        num = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts) or (0,)


def is_newer(latest: str, current: str = CURRENT_VERSION) -> bool:
    return _version_tuple(latest) > _version_tuple(current)


def fetch_version_info(url: str = VERSION_URL, timeout: int = 15) -> dict:
    """Download and parse the version JSON. Raises RuntimeError on failure."""
    if "REPLACE_WITH" in url:
        raise RuntimeError("Update server not configured in this build.")
    req = urllib.request.Request(url, headers={"User-Agent": "FMS"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            info = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Could not reach the update server: {exc}")
    if not isinstance(info, dict) or "version" not in info:
        raise RuntimeError("Update server returned an unexpected response.")
    return info


def check_for_update() -> dict:
    """Return {current, latest, update_available, changelog, exe_size_mb}."""
    info = fetch_version_info()
    latest = str(info.get("version", ""))
    return {
        "current": CURRENT_VERSION,
        "latest": latest,
        "update_available": bool(latest) and is_newer(latest),
        "changelog": info.get("changelog", ""),
        "exe_size_mb": round((info.get("exe_size") or 0) / 1e6, 1),
        "browsers_url": info.get("browsers_url", ""),
    }


# ---------------------------------------------------------------- downloads
# Single in-flight download, shared by the dashboard progress poller.
_download_state: dict = {"state": "idle", "downloaded": 0, "total": None,
                         "message": ""}
_download_lock = threading.Lock()


def download_state() -> dict:
    with _download_lock:
        return dict(_download_state)


def _set_state(**kw):
    with _download_lock:
        _download_state.update(kw)


def new_exe_path() -> Path:
    return _exe_dir() / "FMS.new.exe"


def download_update(exe_url: str, expected_size: int | None = None) -> None:
    """Stream the new exe to FMS.new.exe in a background thread.

    Raises RuntimeError if a download is already running or the URL is empty.
    """
    if not exe_url:
        raise RuntimeError("No download URL for this update.")
    with _download_lock:
        if _download_state["state"] == "downloading":
            raise RuntimeError("A download is already in progress.")
        _download_state.update(state="downloading", downloaded=0,
                               total=expected_size, message="")

    def _run():
        target = new_exe_path()
        # v0.2.80: Clean up stale temp files from previous failed updates.
        try:
            for _stale in target.parent.glob("fms_update_*.exe"):
                try:
                    _stale.unlink()
                except OSError:
                    pass
        except OSError:
            pass
        fd, tmp = tempfile.mkstemp(prefix="fms_update_", suffix=".exe",
                                   dir=str(target.parent))
        os.close(fd)
        downloaded = 0
        try:
            req = urllib.request.Request(exe_url, headers={"User-Agent": "FMS"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                length = resp.headers.get("Content-Length")
                total = int(length) if length and length.isdigit() else expected_size
                _set_state(total=total)
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        _set_state(downloaded=downloaded)
            if downloaded < MIN_EXE_SIZE:
                raise RuntimeError(
                    f"Downloaded file is too small ({downloaded} bytes) — "
                    "the update may be incomplete.")
            if expected_size and downloaded != expected_size:
                raise RuntimeError(
                    f"Incomplete download: got {downloaded} of "
                    f"{expected_size} bytes.")
            # v0.2.80: Remove existing FMS.new.exe with retries
            # (antivirus may lock it). This fixes WinError 5.
            import time as _time
            _removed = False
            for _attempt in range(5):
                try:
                    if target.is_file():
                        target.unlink()
                    _removed = True
                    break
                except OSError:
                    if _attempt < 4:
                        _time.sleep(1)
                    continue
            if not _removed:
                raise RuntimeError(
                    "Cannot replace FMS.new.exe — file is locked. "
                    "Close FMS completely, manually delete FMS.new.exe "
                    "from Downloads, then try the update again.")
            os.replace(tmp, target)
            _set_state(state="done", message="Download complete. "
                       "Quit FMS and open it again to apply the update.")
        except Exception as exc:
            _set_state(state="error", message=str(exc))
            try:
                os.unlink(tmp)
            except OSError:
                pass

    threading.Thread(target=_run, daemon=True).start()


def pending_update_path() -> Path | None:
    p = new_exe_path()
    return p if p.is_file() else None


def apply_pending_update() -> bool:
    """Swap FMS.new.exe over the running exe. Call at startup (not running).

    Keeps FMS.prev.exe as a one-step rollback. Returns True if a swap
    happened. Never raises.
    """
    try:
        if not getattr(sys, "frozen", False):
            return False
        new_exe = new_exe_path()
        if not new_exe.is_file() or new_exe.stat().st_size < MIN_EXE_SIZE:
            return False
        exe = Path(sys.executable).resolve()
        backup = exe.parent / "FMS.prev.exe"
        try:
            if backup.is_file():
                backup.unlink()
        except OSError:
            pass
        if exe.is_file():
            os.replace(exe, backup)
        os.replace(new_exe, exe)
        return True
    except OSError:
        return False

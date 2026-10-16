"""FMS.exe entry point (PyInstaller --windowed, no console).

Double-click behavior:
  1. Find a free localhost port (8000+).
  2. Start the dashboard server (uvicorn) in a background thread.
  3. Open the dashboard in its OWN desktop window (pywebview);
     falls back to the default browser if webview is unavailable.
  4. Create a Desktop shortcut on first launch (Windows).

Quit via the "Quit FMS" button in the dashboard footer.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

import uvicorn

import fms  # noqa: F401  (frozen bootstrap: local Playwright browsers folder)
from fms.app import create_app
from fms.updater import apply_pending_update


def _ensure_stdio() -> None:
    """PyInstaller --windowed builds have no console: sys.stdout/sys.stderr
    are None, which crashes uvicorn's log formatter ('isatty' on NoneType).
    Point them at devnull so logging (and any stray print) is safe."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")  # noqa: PTH123
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")  # noqa: PTH123


def _free_port(start: int = 8000) -> int:
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("Could not find a free localhost port.")


def _wait_for_server(url: str, timeout: int = 60) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def _open_in_browser_when_ready(url: str, timeout: int = 60) -> None:
    if _wait_for_server(url, timeout):
        webbrowser.open(url)


class _FMSApi:
    """JS bridge for native dialogs (pywebview desktop window only)."""

    def pick_folder(self) -> str:
        try:
            import webview

            wins = webview.windows
            if not wins:
                return ""
            result = wins[0].create_file_dialog(webview.FOLDER_DIALOG)
            if result:
                return result[0]
        except Exception:
            pass
        return ""


def _maybe_create_desktop_shortcut() -> None:
    """Create a Desktop shortcut (Windows, frozen only) on first launch."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    try:
        from fms.config import data_dir

        marker = data_dir() / "desktop_shortcut_created"
        if marker.exists():
            return
        exe = sys.executable
        # PowerShell via WScript.Shell — no extra packages needed.
        ps = (
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
            "$env:USERPROFILE+'\\Desktop\\FMS.lnk');"
            f"$s.TargetPath='{exe}';$s.IconLocation='{exe},0';"
            "$s.WorkingDirectory='" + os.path.dirname(exe).replace("'", "''") + "';"
            "$s.Save()"
        )
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-Command", ps],
                       capture_output=True, timeout=30)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
    except Exception:
        pass


def main() -> None:
    _ensure_stdio()
    # Self-update: if a previous session downloaded FMS.new.exe, swap it in
    # now (the running exe can't be replaced while it's open on Windows).
    apply_pending_update()
    _maybe_create_desktop_shortcut()
    port = _free_port(8000)
    url = f"http://127.0.0.1:{port}/"
    app = create_app(driver_name="playwright")

    try:
        import webview  # pywebview: native desktop window

        threading.Thread(
            target=lambda: uvicorn.run(
                app, host="127.0.0.1", port=port, log_level="warning"),
            daemon=True).start()
        if _wait_for_server(url):
            # Compact professional size — fits on smaller laptop screens.
            webview.create_window("FMS", url, width=1024, height=680,
                                  min_size=(900, 560), js_api=_FMSApi())
            webview.start()
        return
    except ImportError:
        pass

    # Fallback: open in the default browser (uvicorn in main thread).
    threading.Thread(target=_open_in_browser_when_ready,
                     args=(url,), daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()

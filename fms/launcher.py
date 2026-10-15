"""FMS.exe entry point (PyInstaller --windowed, no console).

Double-click behavior:
  1. Find a free localhost port (8000+).
  2. Start the dashboard server (uvicorn) in the main thread.
  3. Open the dashboard in the default browser once it's ready.

Quit via the "Quit FMS" button in the dashboard footer.
"""
from __future__ import annotations

import socket
import threading
import time
import urllib.request
import webbrowser

import uvicorn

import fms  # noqa: F401  (frozen bootstrap: local Playwright browsers folder)
from fms.app import create_app
from fms.updater import apply_pending_update


def _free_port(start: int = 8000) -> int:
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("Could not find a free localhost port.")


def _open_when_ready(url: str, timeout: int = 60) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    webbrowser.open(url)
                    return
        except Exception:
            time.sleep(0.5)


def main() -> None:
    # Self-update: if a previous session downloaded FMS.new.exe, swap it in
    # now (the running exe can't be replaced while it's open on Windows).
    apply_pending_update()
    port = _free_port(8000)
    url = f"http://127.0.0.1:{port}/"
    app = create_app(driver_name="playwright")
    # uvicorn runs in the MAIN thread (signal handling only works there);
    # the browser-opener goes in a background thread.
    threading.Thread(target=_open_when_ready, args=(url,), daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()

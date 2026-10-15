"""FMS split-bundle tests: updater + browsers bootstrap.

Run:  python fms/tests/updater_test.py
Covers: version comparison, version-file fetch (mocked HTTP), exe download
to FMS.new.exe (mocked HTTP), pending-update swap logic, browsers folder
resolution/readiness, browsers zip download+extract (mocked HTTP) incl. the
zip-slip guard. No network, no real Facebook, no real Drive.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="fms_upd_")
os.environ["FMS_DATA_DIR"] = _TMP
os.environ["FMS_BROWSERS_DIR"] = str(Path(_TMP) / "browsers")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fms import browsers as browsers_mod  # noqa: E402
from fms import updater as updater_mod  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, fn) -> None:
    try:
        fn()
        results.append((PASS, name, ""))
        print(f"  [{PASS}] {name}")
    except Exception as exc:
        results.append((FAIL, name, f"{type(exc).__name__}: {exc}"))
        print(f"  [{FAIL}] {name}: {type(exc).__name__}: {exc}")


class _FakeResp:
    """Minimal urlopen stand-in: headers + chunked read()."""

    def __init__(self, data: bytes):
        self._buf = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data))}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_urlopen(data: bytes):
    return mock.patch.object(urllib.request, "urlopen",
                             return_value=_FakeResp(data))


# ------------------------------------------------------------- version logic
def t_version_compare():
    assert updater_mod.is_newer("0.2.0", "0.1.0")
    assert updater_mod.is_newer("0.10.0", "0.9.0")
    assert not updater_mod.is_newer("0.1.0", "0.1.0")
    assert not updater_mod.is_newer("0.1.0", "0.2.0")
    assert updater_mod.is_newer("1.0", "0.99.9")


def t_fetch_version_info():
    payload = {"version": "0.2.0", "exe_url": "http://x/fms.exe",
               "exe_size": 123, "browsers_url": "http://x/b.zip",
               "changelog": "split bundle"}
    with _patch_urlopen(json.dumps(payload).encode()):
        info = updater_mod.fetch_version_info("http://x/version.json")
    assert info["version"] == "0.2.0"
    assert info["changelog"] == "split bundle"


def t_fetch_version_info_not_configured():
    try:
        updater_mod.fetch_version_info("https://x/REPLACE_WITH_ID")
        raise AssertionError("should have raised")
    except RuntimeError:
        pass


def t_check_for_update_newer():
    payload = {"version": "9.9.9", "changelog": "big"}
    with mock.patch.object(updater_mod, "fetch_version_info",
                           return_value=payload):
        r = updater_mod.check_for_update()
    assert r["update_available"] is True
    assert r["current"] == updater_mod.CURRENT_VERSION
    assert r["latest"] == "9.9.9"


def t_check_for_update_current():
    payload = {"version": updater_mod.CURRENT_VERSION}
    with mock.patch.object(updater_mod, "fetch_version_info",
                           return_value=payload):
        r = updater_mod.check_for_update()
    assert r["update_available"] is False


# ------------------------------------------------------------- exe download
def t_download_update_writes_new_exe():
    d = Path(tempfile.mkdtemp(prefix="fms_exe_"))
    data = b"E" * (updater_mod.MIN_EXE_SIZE + 100)
    import time
    # NOTE: the download runs in a background thread, so the urlopen mock
    # must stay active until the thread finishes (no race).
    with mock.patch.object(updater_mod, "_exe_dir", return_value=d):
        with _patch_urlopen(data):
            updater_mod.download_update("http://x/fms.exe",
                                        expected_size=len(data))
            end = time.time() + 10
            while time.time() < end:
                if updater_mod.download_state()["state"] in ("done", "error"):
                    break
                time.sleep(0.1)
            st = updater_mod.download_state()
    assert st["state"] == "done", st
    target = d / "FMS.new.exe"
    assert target.is_file() and target.stat().st_size == len(data)
    # reset shared state for other tests
    updater_mod._set_state(state="idle", downloaded=0, total=None, message="")


def t_download_update_rejects_empty_url():
    try:
        updater_mod.download_update("")
        raise AssertionError("should have raised")
    except RuntimeError:
        pass


def t_apply_pending_update_not_frozen():
    assert updater_mod.apply_pending_update() is False


def t_apply_pending_update_swaps():
    d = Path(tempfile.mkdtemp(prefix="fms_swap_"))
    old_exe = d / "FMS.exe"
    old_exe.write_bytes(b"O" * (updater_mod.MIN_EXE_SIZE + 10))
    new_exe = d / "FMS.new.exe"
    new_exe.write_bytes(b"N" * (updater_mod.MIN_EXE_SIZE + 10))
    had_frozen = getattr(sys, "frozen", None)
    sys.frozen = True  # type: ignore[attr-defined]
    real_executable = sys.executable
    sys.executable = str(old_exe)
    try:
        assert updater_mod.apply_pending_update() is True
        assert old_exe.read_bytes().startswith(b"N")
        assert (d / "FMS.prev.exe").read_bytes().startswith(b"O")
        assert not new_exe.exists()
    finally:
        if had_frozen is None:
            delattr(sys, "frozen")
        else:
            sys.frozen = had_frozen  # type: ignore[attr-defined]
        sys.executable = real_executable


def t_apply_pending_update_ignores_tiny_file():
    d = Path(tempfile.mkdtemp(prefix="fms_swap2_"))
    old_exe = d / "FMS.exe"
    old_exe.write_bytes(b"O" * 100)
    (d / "FMS.new.exe").write_bytes(b"tiny")
    had_frozen = getattr(sys, "frozen", None)
    sys.frozen = True  # type: ignore[attr-defined]
    real_executable = sys.executable
    sys.executable = str(old_exe)
    try:
        assert updater_mod.apply_pending_update() is False
        assert old_exe.read_bytes() == b"O" * 100
    finally:
        if had_frozen is None:
            delattr(sys, "frozen")
        else:
            sys.frozen = had_frozen  # type: ignore[attr-defined]
        sys.executable = real_executable


# ------------------------------------------------------------- browsers
def _fake_browsers_tree(root: Path) -> Path:
    sub = root / "chromium-1234"
    if os.name == "nt":
        exe = sub / "chrome-win" / "chrome.exe"
    else:
        exe = sub / "chrome-linux" / "chrome"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"fake-chrome")
    return exe


def t_browsers_dir_override():
    p = browsers_mod.browsers_dir()
    assert p == Path(os.environ["FMS_BROWSERS_DIR"])
    assert p.is_dir()


def t_browsers_ready_true_false():
    d = Path(tempfile.mkdtemp(prefix="fms_br_"))
    assert browsers_mod.browsers_ready(d) is False
    _fake_browsers_tree(d)
    assert browsers_mod.browsers_ready(d) is True


def t_download_and_extract():
    d = Path(tempfile.mkdtemp(prefix="fms_brx_"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("chromium-9999/chrome-linux/chrome", b"fake")
        zf.writestr("chromium-9999/chrome-win/chrome.exe", b"fake")
    seen = []

    def _cb(done, total):
        seen.append((done, total))

    with _patch_urlopen(buf.getvalue()):
        out = browsers_mod.download_and_extract("http://x/b.zip", folder=d,
                                                progress_cb=_cb)
    assert out == d
    assert browsers_mod.browsers_ready(d) is True
    assert seen, "progress callback never fired"
    assert seen[-1][0] == len(buf.getvalue())


def t_download_zip_slip_blocked():
    d = Path(tempfile.mkdtemp(prefix="fms_brz_"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../evil.txt", b"evil")
    with _patch_urlopen(buf.getvalue()):
        try:
            browsers_mod.download_and_extract("http://x/b.zip", folder=d)
            raise AssertionError("zip-slip should have raised")
        except RuntimeError:
            pass
    assert not (d.parent / "evil.txt").exists()


def main():
    print("FMS updater/browsers tests")
    for name, fn in sorted(
            [(k, v) for k, v in globals().items() if k.startswith("t_")]):
        check(name, fn)
    passed = sum(1 for s, _, _ in results if s == PASS)
    failed = [(n, e) for s, n, e in results if s == FAIL]
    print(f"\n{passed}/{len(results)} passed")
    if failed:
        print("FAILURES:", failed)
        sys.exit(1)


if __name__ == "__main__":
    main()

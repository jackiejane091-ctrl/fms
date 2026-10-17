"""v0.2.56 self-diagnosis test. Run:  python fms/tests/selfsnap_test.py

Covers: PlaywrightDriver._snap saves a page screenshot under the data dir and
_note_field_error appends the screenshot path to the error message shown
in the per-account Logs. Uses a fake page object — NEVER touches a real
browser or Facebook.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="fms_selfsnap_")
os.environ["FMS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fms.browser import PlaywrightDriver  # noqa: E402

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


class FakePage:
    """Minimal Playwright page stand-in: records screenshot calls."""

    def __init__(self, fail_shot: bool = False):
        self.fail_shot = fail_shot
        self.shot_paths: list[str] = []

    def screenshot(self, path: str):
        if self.fail_shot:
            raise RuntimeError("screenshot boom")
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\nfakepng")
        self.shot_paths.append(path)


def _driver(account_label: str = "test-account") -> PlaywrightDriver:
    # PlaywrightDriver.__init__ only stores the account dict — no browser launched.
    return PlaywrightDriver({"label": account_label}, headless=True)


def t_snap_saves_png():
    d = _driver()
    page = FakePage()
    shot = d._snap(page, "category")
    assert shot is not None, "expected a screenshot path"
    assert shot.endswith(".png"), shot
    assert Path(shot).exists(), f"screenshot file missing: {shot}"
    assert "category" in Path(shot).name, Path(shot).name
    assert d._last_screenshot == shot


def t_snap_never_raises():
    d = _driver()
    page = FakePage(fail_shot=True)
    assert d._snap(page, "category") is None  # must not raise


def t_note_field_error_appends_screenshot():
    d = _driver("id-7")
    page = FakePage()
    d._note_field_error(page, "condition",
                        "Condition 'New' not selected: TimeoutError")
    err = d._last_form_error
    assert "Condition 'New' not selected" in err, err
    assert "Screenshot: " in err, err
    assert Path(page.shot_paths[0]).exists(), "screenshot not saved"
    assert "id-7" in Path(page.shot_paths[0]).name, page.shot_paths[0]


def t_note_field_error_survives_shot_failure():
    d = _driver()
    d._note_field_error(FakePage(fail_shot=True), "location-select",
                        "Location 'X' suggestion not selected: TimeoutError")
    assert "Screenshot: (failed)" in d._last_form_error, d._last_form_error


def t_snap_dir_is_data_dir():
    d = _driver()
    page = FakePage()
    shot = d._snap(page, "publish")
    assert str(Path(_TMP, "screenshots")) in shot, shot


def t_snap_prunes_to_newest_50():
    d = _driver()
    page = FakePage()
    import time as _t
    for i in range(55):
        d._snap(page, f"prune-{i:02d}")
        _t.sleep(0.012)  # distinct mtimes for the prune ordering
    shots = list(Path(_TMP, "screenshots").glob("*.png"))
    assert len(shots) == 50, f"expected 50 kept, got {len(shots)}"
    names = sorted(p.name for p in shots)
    assert not any("prune-00" in n for n in names), "oldest should be pruned"
    assert any("prune-54" in n for n in names), "newest should be kept"


check("snap saves a png under the data dir", t_snap_saves_png)
check("snap never raises on screenshot failure", t_snap_never_raises)
check("note_field_error appends screenshot path", t_note_field_error_appends_screenshot)
check("note_field_error survives shot failure", t_note_field_error_survives_shot_failure)
check("screenshots live in the data dir", t_snap_dir_is_data_dir)
check("snap prunes folder to newest 50", t_snap_prunes_to_newest_50)

fails = [r for r in results if r[0] == FAIL]
print(f"\n{len(results) - len(fails)}/{len(results)} passed")
sys.exit(1 if fails else 0)

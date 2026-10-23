"""Regression checks for v0.2.122 recording-off and maximize-first patch."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_recording_disabled_and_maximize_hook_present():
    source = (ROOT / "browser.py").read_text(encoding="utf-8")
    assert "record_video_dir=" not in source
    assert "record_video_size=" not in source
    assert "def _maximize_window(self)" in source
    assert source.count("self._maximize_window()") >= 2

def test_screenshot_capture_disabled():
    source = (ROOT / "browser.py").read_text(encoding="utf-8")
    snap = source[source.index("    def _snap("):source.index("    def _note_field_error(")]
    assert "return None" in snap
    assert "page.screenshot(" not in snap

if __name__ == "__main__":
    test_recording_disabled_and_maximize_hook_present()
    test_screenshot_capture_disabled()
    print("2 targeted checks PASS")

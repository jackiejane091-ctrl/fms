"""Regression tests for recording FMS image cooldown at the Publish click."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]

class PublishClickCooldownSourceTests(unittest.TestCase):
    def test_publish_click_invokes_cooldown_callback_before_wait(self):
        text = (ROOT / "single_post_test.py").read_text(encoding="utf-8")
        click = text.index('human.human_click(page, publish_button)')
        callback = text.index('payload.get("_on_publish_clicked")', click)
        wait = text.index('human.human_pause(4000, 6000)', click)
        self.assertLess(click, callback)
        self.assertLess(callback, wait)

    def test_engine_installs_callback_before_running_publish_flow(self):
        text = (ROOT / "engine.py").read_text(encoding="utf-8")
        callback = text.index('payload["_on_publish_clicked"] = _on_publish_clicked')
        run = text.index('_spt.run_single_post_test(driver, payload)', callback)
        self.assertLess(callback, run)

    def test_cooldown_records_all_payload_image_paths(self):
        text = (ROOT / "engine.py").read_text(encoding="utf-8")
        self.assertIn('_record_published_image_usage(\n                            self.db, _aid, _pid, _payload.get("image_paths"))', text)

if __name__ == "__main__":
    unittest.main()

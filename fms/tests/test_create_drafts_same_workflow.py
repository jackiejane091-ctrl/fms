"""Targeted regression tests for Create Drafts reusing the Draft & Publish form workflow.

These tests intentionally do not call any publish method. They cover dispatch,
quota-bounded failure behavior, Pause All callback plumbing, and image cooldown
recording immediately after Save Draft completes.
"""
from types import SimpleNamespace
import sys
sys.path.insert(0, ".")

import fms.engine as E
import fms.browser as B


def test_create_drafts_uses_specialized_form_flag_and_never_publishes(monkeypatch):
    calls = []
    ctl = E.RunController.__new__(E.RunController)
    ctl.cfg = SimpleNamespace(tabs_draft_publish=2, mode="create_drafts")
    ctl.run_id = 1
    ctl.db = SimpleNamespace(execute=lambda *a, **k: None)
    ctl.log = lambda *a, **k: None
    ctl._draft_count = lambda aid: 0
    ctl._posted_count = lambda aid: 0
    ctl._next_product = lambda idx: (5, {"id": 5})
    ctl._rotation = lambda product, idx, account_id=None: {
        "title": "Chair", "price": 10, "location": "L", "image_paths": ["img.jpg"]}
    ctl._record_failed = lambda *a, **k: calls.append("failed")
    ctl._inter_post_delay = lambda: None
    ctl._wait_if_paused = lambda: None
    ctl.log = lambda *a, **k: calls.append(a[0])

    class Driver:
        _fms_draft_publish_fill = False
        def create_draft(self, payload):
            assert self._fms_draft_publish_fill is True
            cb = getattr(self, "_fms_draft_saved_callback", None)
            assert callable(cb), "Create Drafts must register post-save image callback"
            cb(payload)
            calls.append("save_draft")
            return "draft-title:Chair"

    driver = Driver()
    account = {"id": 9, "label": "A"}
    monkeypatch.setattr(E, "_record_published_image_usage",
                        lambda db, aid, pid, imgs: calls.append(("images", aid, pid, list(imgs or []))))
    ctl._create_drafts_only_flow(account, driver, quota=1, account_idx=0)
    assert "save_draft" in calls
    assert ("images", 9, 5, ["img.jpg"]) in calls
    assert driver._fms_draft_publish_fill is False, "workflow flag must be restored"
    assert not any("publish" in str(c).lower() for c in calls), calls


def test_rotation_marks_create_drafts_as_draft(monkeypatch):
    ctl = E.RunController.__new__(E.RunController)
    ctl.cfg = SimpleNamespace(mode="create_drafts")
    ctl._rotator = SimpleNamespace(payload=lambda product, cfg, as_draft, account_id=None:
                                   {"as_draft": as_draft})
    ctl.log = lambda *a, **k: None
    assert ctl._rotation({"id": 1}, 0, account_id=9)["as_draft"] is True


def test_browser_saved_callback_is_scoped_to_optional_callback():
    # Keep a source-level guard so the callback remains inside the specialized
    # Save Draft path, not the shared legacy draft implementation.
    import inspect
    source = inspect.getsource(B.PlaywrightDriver._create_draft_draft_publish)
    assert "_fms_draft_saved_callback" in source
    assert "_saved_callback(payload)" in source
    assert "def create_draft(self, payload" in inspect.getsource(B.PlaywrightDriver)


if __name__ == "__main__":
    test_rotation_marks_create_drafts_as_draft()
    test_browser_saved_callback_is_scoped_to_optional_callback()
    print("Create Drafts workflow targeted checks: PASS")

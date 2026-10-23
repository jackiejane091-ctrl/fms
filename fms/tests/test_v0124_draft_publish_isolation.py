"""v0.2.124 targeted tests — Draft & Publish isolation fix.

Covers:
  1. _expand_more_details_draft_publish is STATE-AWARE: it never clicks
     an already-expanded "More details" toggle (a click would collapse
     it), clicks exactly once when collapsed, never blind-clicks an
     unreadable toggle, and verifies via the Description control.
  2. create_draft dispatches to _fill_form_draft_publish ONLY when the
     Draft & Publish flow raised _fms_draft_publish_fill; otherwise the
     shared _fill_form is used exactly as before (Create Drafts path).
  3. _create_drafts_batch(bounded=True) terminates when a whole group
     creates no draft (no endless retry), and still fills the quota
     when drafts succeed.
  4. _draft_publish_flow sets/restores the flag and passes bounded=True.
"""
import sys
from types import SimpleNamespace

sys.path.insert(0, ".")
import fms.browser as B  # noqa: E402
import fms.engine as E  # noqa: E402


# ---------- fakes for the More-details helper ----------

class FakeBtn:
    def __init__(self, state):
        self._state = state
    def evaluate(self, _script):
        return self._state


class FakeBtnWrap:
    def __init__(self, state):
        self._state = state
        self.first = FakeBtn(state)
    def count(self):
        return 0 if self._state is None else 1


class FakeTextLoc:
    def __init__(self, state, on_text=False):
        self._state = state
        self._on_text = on_text
    def locator(self, _xpath):
        return FakeBtnWrap(None if self._on_text else self._state)
    def evaluate(self, _script):
        return self._state


class FakeRaw:
    def __init__(self, text_loc):
        self._tl = text_loc
        self.first = text_loc
    def count(self):
        return 0 if self._tl is None else 1


class FakePageMD:
    """get_by_text('More details') -> toggle with the given aria state."""
    def __init__(self, state, on_text=False):
        self._state = state
        self._on_text = on_text
    def get_by_text(self, _label, exact=False):
        if self._state is None and not self._on_text:
            return FakeRaw(None)
        return FakeRaw(FakeTextLoc(self._state, self._on_text))


class FakeDescLoc:
    def __init__(self, present):
        self._present = present
    def wait_for(self, timeout=None):
        if not self._present:
            raise TimeoutError("description control absent")


def run_helper(state, desc_present, on_text=False):
    clicks = []
    notes = []
    orig_click, orig_pause = B.human.human_click, B.human.human_pause
    orig_tp = B.S.to_playwright
    B.human.human_click = lambda page, loc: clicks.append(loc)
    B.human.human_pause = lambda *a, **k: None
    B.S.to_playwright = lambda page, sel: FakeDescLoc(desc_present)
    try:
        drv = B.PlaywrightDriver.__new__(B.PlaywrightDriver)
        drv._note_field_error = lambda page, field, msg: notes.append((field, msg))
        ok = drv._expand_more_details_draft_publish(
            FakePageMD(state, on_text=on_text))
    finally:
        B.human.human_click, B.human.human_pause = orig_click, orig_pause
        B.S.to_playwright = orig_tp
    return ok, clicks, notes


def test_helper_state_aware():
    # Already expanded -> NO click (a click would collapse it), open.
    ok, clicks, _ = run_helper("true", True)
    assert ok is True and len(clicks) == 0, (ok, clicks)
    # Collapsed -> exactly one click, then verified open.
    ok, clicks, _ = run_helper("false", True)
    assert ok is True and len(clicks) == 1, (ok, clicks)
    # Collapsed but section never opens -> one click, False + honest note.
    ok, clicks, notes = run_helper("false", False)
    assert ok is False and len(clicks) == 1 and notes, (ok, clicks, notes)
    # Toggle unreadable -> NO blind click; fields reachable -> open.
    ok, clicks, _ = run_helper(None, True)
    assert ok is True and len(clicks) == 0, (ok, clicks)
    # Toggle unreadable + fields unreachable -> no click, False + note.
    ok, clicks, notes = run_helper(None, False)
    assert ok is False and len(clicks) == 0 and notes, (ok, clicks, notes)
    # aria-expanded carried on the text node itself (fallback branch).
    ok, clicks, _ = run_helper("true", True, on_text=True)
    assert ok is True and len(clicks) == 0, (ok, clicks)
    print("1. helper state-aware expansion: PASS (6 scenarios)")


# ---------- dispatch test ----------

class FakePageDraft:
    url = "https://www.facebook.com/marketplace/create/item"
    def locator(self, _sel):
        return SimpleNamespace(all=lambda: [])
    def reload(self, **_kw):
        pass


def test_create_draft_dispatch():
    used = []
    orig_click, orig_pause = B.human.human_click, B.human.human_pause
    orig_tp = B.S.to_playwright
    B.human.human_click = lambda page, loc: None
    B.human.human_pause = lambda *a, **k: None
    B.S.to_playwright = lambda page, sel: SimpleNamespace()
    try:
        drv = B.PlaywrightDriver.__new__(B.PlaywrightDriver)
        drv._ensure = lambda: None
        drv._page = FakePageDraft()
        drv._fill_form = lambda page, payload: used.append("shared") or True
        drv._fill_form_draft_publish = (
            lambda page, payload: used.append("draft_publish") or True)
        # Flag unset (Create Drafts / scripts) -> shared _fill_form.
        drv.create_draft({"title": "x"})
        assert used == ["shared"], used
        # Flag set (Draft & Publish flow) -> isolated filler.
        drv._fms_draft_publish_fill = True
        drv.create_draft({"title": "x"})
        assert used == ["shared", "draft_publish"], used
    finally:
        B.human.human_click, B.human.human_pause = orig_click, orig_pause
        B.S.to_playwright = orig_tp
    print("2. create_draft dispatch (flag off=shared, on=isolated): PASS")


# ---------- batch termination + flow flag tests ----------

def make_controller(driver):
    ctl = E.RunController.__new__(E.RunController)
    ctl.cfg = SimpleNamespace(tabs_draft_publish=2)
    ctl.run_id = 1
    ctl.db = SimpleNamespace(execute=lambda *a, **k: None)
    ctl.logs = []
    ctl.failed = []
    ctl.log = lambda msg, level="info", account_id=None: ctl.logs.append(msg)
    ctl._draft_count = lambda aid: 0
    ctl._posted_count = lambda aid: 0
    ctl._next_product = lambda idx: (7, {"id": 7})
    ctl._rotation = lambda product, idx, account_id=None: {
        "title": "T", "price": 5, "location": "L", "image_paths": []}
    ctl._record_failed = lambda account, pid, payload, why: (
        ctl.failed.append(why))
    ctl._inter_post_delay = lambda: None
    return ctl


def test_batch_bounded_terminates():
    # Driver whose drafts always fail: bounded batch must STOP after one
    # zero-progress group instead of looping forever.
    drv = SimpleNamespace(create_draft=lambda payload: None)
    ctl = make_controller(drv)
    created = ctl._create_drafts_batch(
        {"id": 1, "label": "A"}, drv, quota=3, account_idx=0, bounded=True)
    assert created == 0, created
    assert len(ctl.failed) == 2, ctl.failed  # one group of 2, then stop
    assert any("stopped" in m for m in ctl.logs), ctl.logs
    # Succeeding driver: quota still fills completely.
    drv2 = SimpleNamespace(create_draft=lambda payload: "123456")
    ctl2 = make_controller(drv2)
    created = ctl2._create_drafts_batch(
        {"id": 1, "label": "A"}, drv2, quota=3, account_idx=0, bounded=True)
    assert created == 3, created
    print("3. bounded batch terminates on zero progress, fills quota: PASS")


def test_draft_publish_flow_flag_and_bounded():
    seen = {}
    drv = SimpleNamespace()
    ctl = make_controller(drv)
    def fake_batch(account, driver, quota, account_idx, bounded=False):
        seen["flag_during"] = getattr(driver, "_fms_draft_publish_fill", False)
        seen["bounded"] = bounded
        return 0
    ctl._create_drafts_batch = fake_batch
    ctl._publish_draft_rows = lambda account, driver, rows: None
    ctl._draft_rows = lambda aid: []
    ctl._draft_publish_flow({"id": 1, "label": "A"}, drv, quota=1, account_idx=0)
    assert seen == {"flag_during": True, "bounded": True}, seen
    assert getattr(drv, "_fms_draft_publish_fill", False) is False
    print("4. draft_publish_flow sets+restores flag, bounded=True: PASS")


if __name__ == "__main__":
    test_helper_state_aware()
    test_create_draft_dispatch()
    test_batch_bounded_terminates()
    test_draft_publish_flow_flag_and_bounded()
    print("ALL v0.2.124 DRAFT & PUBLISH ISOLATION TESTS PASS")

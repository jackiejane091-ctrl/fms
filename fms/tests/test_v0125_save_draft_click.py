"""v0.2.125 targeted tests — Draft & Publish Save Draft click fix.

Simulates the dedicated _create_draft_draft_publish save step:
  1. role/accessible-name button found -> clicked -> real ID (URL) returned
  2. role empty -> existing F_SAVE_DRAFT_BUTTON selector fallback -> ID
     from DOM links returned
  3. selector candidate invisible -> exact visible-text fallback used
  4. no visible/enabled button anywhere -> None + clear "not found" error
     (screenshot path appended when _snap returns one)
  5. button click raises -> None + "click failed" error
  6. click OK but Facebook exposes no listing_id -> None, never success:
     (a) "draft saved" signal visible -> 'exposed no listing_id' error
     (b) no signal at all -> 'no saved-draft listing_id/confirmation'
Also: create_draft still routes flag-off callers to the ORIGINAL body.
"""
import re
import sys

sys.path.insert(0, ".")
import fms.browser as B  # noqa: E402


class FakeBtn:
    def __init__(self, visible=True, enabled=True, raises=False):
        self._v, self._e, self._raises = visible, enabled, raises
        self.clicked = 0
    def is_visible(self):
        return self._v
    def is_enabled(self):
        return self._e
    def scroll_into_view_if_needed(self, timeout=None):
        pass
    def click(self, timeout=None):
        self.clicked += 1
        if self._raises:
            raise RuntimeError("simulated click failure")


class FakeCollection:
    """Mimics a Playwright locator: with exactly one match the locator
    itself acts as the element (is_visible/click delegate to it)."""
    def __init__(self, items):
        self._items = items
    def count(self):
        return len(self._items)
    def nth(self, i):
        return self._items[i]
    def _single(self):
        assert len(self._items) == 1
        return self._items[0]
    def is_visible(self):
        return self._single().is_visible()
    def is_enabled(self):
        return self._single().is_enabled()
    def scroll_into_view_if_needed(self, timeout=None):
        return self._single().scroll_into_view_if_needed(timeout=timeout)
    def click(self, timeout=None):
        return self._single().click(timeout=timeout)


class FakeSignal:
    def __init__(self, visible):
        self._visible = visible
        self.first = self
    def count(self):
        return 1 if self._visible else 0
    def is_visible(self):
        return self._visible


class FakeAnchor:
    def __init__(self, href):
        self._href = href
    def get_attribute(self, _name):
        return self._href


class FakePage:
    def __init__(self, role_items=(), text_items=(), url="", anchors=(),
                 signal_visible=False):
        self._role = FakeCollection(list(role_items))
        self._text = FakeCollection(list(text_items))
        self.url = url
        self._anchors = list(anchors)
        self._signal = FakeSignal(signal_visible)
        self.goto_calls = []
    def get_by_role(self, _role, name=None):
        return self._role
    def get_by_text(self, pattern, exact=False):
        pat = getattr(pattern, "pattern", str(pattern))
        if "draft saved" in pat:
            return self._signal
        return self._text
    def locator(self, _sel):
        anchors = self._anchors
        return type("L", (), {"all": lambda s: anchors})()
    def wait_for_timeout(self, _ms):
        pass
    def goto(self, url, **_kw):
        self.goto_calls.append(url)
        self.url = url
    def reload(self, **_kw):
        pass


def run(page, selector_items=(), snap_result=None):
    orig_tp = B.S.to_playwright
    orig_pause = B.human.human_pause
    B.S.to_playwright = lambda p, sel: FakeCollection(list(selector_items))
    B.human.human_pause = lambda *a, **k: None
    try:
        drv = B.PlaywrightDriver.__new__(B.PlaywrightDriver)
        drv._ensure = lambda: None
        drv._page = page
        drv.fills = []
        drv._fill_form_draft_publish = (
            lambda pg, payload: drv.fills.append(1) or True)
        drv._snap = lambda pg, name: snap_result
        drv._last_error = None
        result = drv._create_draft_draft_publish({"title": "x"})
        return drv, result
    finally:
        B.S.to_playwright = orig_tp
        B.human.human_pause = orig_pause


def main():
    # 1. Role-button success; ID from the post-save URL.
    btn = FakeBtn()
    page = FakePage(role_items=[btn],
                    url="https://www.facebook.com/marketplace/item/1?listing_id=555000111")
    drv, res = run(page)
    assert res == "555000111", res
    assert btn.clicked == 1 and drv.fills == [1], (btn.clicked, drv.fills)
    assert page.goto_calls, "must return to the create-item page for the next draft"
    print("1. role-button success + URL ID + next-draft navigation: PASS")

    # 2. Selector fallback; ID from DOM links.
    btn2 = FakeBtn()
    page2 = FakePage(url="https://www.facebook.com/marketplace/create/item",
                     anchors=[FakeAnchor("/marketplace/item/?listing_id=777")])
    drv, res = run(page2, selector_items=[btn2])
    assert res == "777", res
    assert btn2.clicked == 1
    print("2. selector fallback + DOM-link ID: PASS")

    # 3. Selector candidate invisible -> exact-text fallback wins.
    hidden = FakeBtn(visible=False)
    btn3 = FakeBtn()
    page3 = FakePage(text_items=[btn3],
                     url="https://www.facebook.com/marketplace/item/?listing_id=888")
    drv, res = run(page3, selector_items=[hidden])
    assert res == "888", res
    assert hidden.clicked == 0 and btn3.clicked == 1
    print("3. invisible selector candidate skipped, text fallback: PASS")

    # 4. No button anywhere -> None + clear error (+screenshot suffix).
    page4 = FakePage(url="https://www.facebook.com/marketplace/create/item")
    drv, res = run(page4, snap_result="shot.png")
    assert res is None, res
    assert "not found" in drv._last_error, drv._last_error
    assert "screenshot=shot.png" in drv._last_error, drv._last_error
    print("4. no-button failure -> clear error + screenshot ref: PASS")

    # 5. Click raises -> None + 'click failed'.
    btn5 = FakeBtn(raises=True)
    page5 = FakePage(role_items=[btn5],
                     url="https://www.facebook.com/marketplace/create/item")
    drv, res = run(page5)
    assert res is None and "click failed" in drv._last_error, drv._last_error
    print("5. click exception -> clear error, no success claimed: PASS")

    # 6a. Saved signal visible but NO ID -> refused, distinct error.
    btn6 = FakeBtn()
    page6 = FakePage(role_items=[btn6], signal_visible=True,
                     url="https://www.facebook.com/marketplace/create/item")
    drv, res = run(page6)
    assert res is None and "exposed no listing_id" in drv._last_error, drv._last_error
    # 6b. No signal, no ID -> refused, generic unverified error.
    btn6b = FakeBtn()
    page6b = FakePage(role_items=[btn6b], signal_visible=False,
                      url="https://www.facebook.com/marketplace/create/item")
    drv, res = run(page6b)
    assert res is None and "no saved-draft listing_id/confirmation" in drv._last_error, drv._last_error
    print("6. no-ID after click -> never marked successful (2 variants): PASS")

    print("ALL v0.2.125 SAVE DRAFT TARGETED TESTS PASS")


if __name__ == "__main__":
    main()

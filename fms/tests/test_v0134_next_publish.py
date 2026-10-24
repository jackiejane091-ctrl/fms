"""v0.2.134 targeted test — Next -> Publish in each opened draft tab
(inside the Draft & Publish 1 title-token branch of publish_draft).

The editor tab is modelled as TWO screens, like Facebook's
edit-listing form:
  screen 1: the draft title + a Next action (Publish NOT present);
  screen 2 (only after Next is clicked): the final Publish action.
  1. Full flow: Next appears only on poll 3, Publish only on
     poll 2 of screen 2 -> both clicked IN ORDER, True, tab closed.
     (Proves: no fixed cutoff, Next is never skipped, and the
     Publish wait does not run ahead of Next.)
  2. Next never appears and the tab is destroyed mid-wait ->
     False + the actual error, tab closed (the wait's real escape
     hatch: a closed tab raises; it never times out into success).
  3. Next clicked, but screen-2 Publish never appears and the tab
     is destroyed mid-wait -> False + actual error, tab closed.
The fakes mimic Playwright locators; wait_for_timeout polls are
counted to prove the loops really waited instead of failing fast.
"""
import sys

sys.path.insert(0, ".")
import fms.browser as B  # noqa: E402

SELLING_URL = "https://www.facebook.com/marketplace/you/selling?state=DRAFT"
CREATE_URL = "https://www.facebook.com/marketplace/create/item"


class FakeBtn:
    def __init__(self, on_click=None):
        self.clicked = 0
        self._on_click = on_click
    def is_visible(self):
        return True
    def is_enabled(self):
        return True
    def scroll_into_view_if_needed(self, timeout=None):
        pass
    def click(self, timeout=None):
        self.clicked += 1
        if self._on_click:
            self._on_click()
    def wait_for(self, state=None, timeout=None):
        pass


class FakeCollection:
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
    @property
    def first(self):
        return self._items[0] if self._items else FakeBtn()
    def wait_for(self, state=None, timeout=None):
        pass


class TitleWait:
    def __init__(self):
        self.first = self
    def wait_for(self, state=None, timeout=None):
        assert timeout == 0, f"expected unbounded wait, got timeout={timeout}"
        return None


class FakeAnchor:
    def __init__(self, href):
        self._href = href
    def get_attribute(self, _name):
        return self._href


class FakeNode:
    def __init__(self, links=(), parent=None):
        self._links = [FakeAnchor(h) for h in links]
        self._parent = parent
    def locator(self, selector):
        if selector.startswith("xpath="):
            return self._parent if self._parent is not None else FakeNode()
        return FakeCollection(list(self._links))
    def wait_for(self, state=None, timeout=None):
        pass


class PollCollection:
    """A button that becomes available on poll N of its own counter."""
    def __init__(self, available_on_poll, on_click=None):
        self._n = available_on_poll
        self._polls = 0
        self._clicked = False
        self._on_click = on_click
    def count(self):
        self._polls += 1
        if self._clicked:
            return 0
        return 1 if self._polls >= self._n else 0
    def nth(self, i):
        return FakeBtn(on_click=self._click)
    def _click(self):
        self._clicked = True
        if self._on_click:
            self._on_click()
    def _single(self):
        return self.nth(0)
    def is_visible(self):
        return True
    def is_enabled(self):
        return True
    def scroll_into_view_if_needed(self, timeout=None):
        pass
    def click(self, timeout=None):
        self._click()


class FakePage:
    def __init__(self):
        self.url = CREATE_URL
        self.closed = False
        self.wait_timeouts = 0
    def get_by_role(self, role, name=None):
        return FakeCollection([])
    def get_by_text(self, pattern, exact=False):
        pat = getattr(pattern, "pattern", str(pattern))
        if "publish" in pat.lower() or "next" in pat.lower():
            return FakeCollection([])
        return TitleWait()
    def locator(self, sel):
        return FakeCollection([])
    def wait_for_timeout(self, ms):
        self.wait_timeouts += 1
    def wait_for_load_state(self, state, timeout=None):
        assert timeout == 0, f"expected unbounded load wait, got {timeout}"
    def goto(self, url, **kw):
        assert kw.get("timeout") == 0, f"expected unbounded goto, got {kw}"
        self.url = url
    def close(self):
        self.closed = True


class EditorTab(FakePage):
    """Two-screen editor: Next (screen 1) -> Publish (screen 2).

    destroy_after: if set, wait_for_timeout raises after that many
    polls, modelling the tab/browser being closed mid-wait.
    """
    def __init__(self, next_on_poll=3, publish_on_poll=2, destroy_after=None):
        super().__init__()
        self.screen = 1
        self.next_clicked = False
        self.publish_clicked = False
        self._destroy_after = destroy_after
        self._next = PollCollection(next_on_poll, self._on_next)
        self._publish = PollCollection(publish_on_poll, self._on_publish)
    def _on_next(self):
        self.next_clicked = True
        self.screen = 2
    def _on_publish(self):
        self.publish_clicked = True
    def wait_for_timeout(self, ms):
        super().wait_for_timeout(ms)
        if self._destroy_after is not None and self.wait_timeouts >= self._destroy_after:
            raise RuntimeError("simulated tab closed during wait")
    def get_by_role(self, role, name=None):
        if role != "button":
            return FakeCollection([])
        pat = getattr(name, "pattern", str(name)).lower()
        if "next" in pat:
            return self._next if self.screen == 1 else FakeCollection([])
        if "publish" in pat:
            return self._publish if self.screen == 2 else FakeCollection([])
        return FakeCollection([])
    def get_by_text(self, pattern, exact=False):
        pat = getattr(pattern, "pattern", str(pattern)).lower()
        if "next" in pat or "publish" in pat:
            return FakeCollection([])
        return TitleWait()
    def locator(self, sel):
        return FakeCollection([])


class FakeContext:
    def __init__(self, tab):
        self.tab = tab
    def new_page(self):
        return self.tab


def make_driver(page, context):
    orig_tp = B.S.to_playwright
    orig_pause = B.human.human_pause
    B.S.to_playwright = lambda p, sel: FakeCollection([])
    B.human.human_pause = lambda *a, **k: None
    drv = B.PlaywrightDriver.__new__(B.PlaywrightDriver)
    drv._ensure = lambda: None
    drv._page = page
    drv._context = context
    drv._snap = lambda pg, name: None
    drv._last_error = None
    return drv, orig_tp, orig_pause


def restore(orig_tp, orig_pause):
    B.S.to_playwright = orig_tp
    B.human.human_pause = orig_pause


def list_page_with_card():
    parent = FakeNode(links=["/marketplace/item/999001"])
    node = FakeNode(parent=parent)
    page = FakePage()
    page.get_by_text = lambda pattern, exact=False: (
        FakeCollection([node]) if "publish" not in str(getattr(pattern, "pattern", pattern)).lower()
        and "next" not in str(getattr(pattern, "pattern", pattern)).lower()
        else FakeCollection([]))
    return page


def main():
    # 1. Full Next -> Publish flow, both late-appearing.
    tab = EditorTab(next_on_poll=3, publish_on_poll=2)
    drv, tp, hp = make_driver(list_page_with_card(), FakeContext(tab))
    try:
        ok = drv.publish_draft("draft-title:Oak Chair")
    finally:
        restore(tp, hp)
    assert ok is True, drv._last_error
    assert tab.next_clicked is True, "Next must be clicked first"
    assert tab.publish_clicked is True, "Publish must be clicked after Next"
    assert tab.screen == 2, "Publish click must happen on screen 2"
    assert tab.closed is True, "tab must be closed"
    assert tab.wait_timeouts >= 3, f"loops must have waited, got {tab.wait_timeouts} polls"
    print(f"1. Next (poll 3) -> Publish (screen 2, poll 2), in order, tab closed: PASS ({tab.wait_timeouts} polls)")

    # 2. Next never appears; tab destroyed mid-wait -> False + error.
    tab = EditorTab(next_on_poll=999, destroy_after=4)
    drv, tp, hp = make_driver(list_page_with_card(), FakeContext(tab))
    try:
        ok = drv.publish_draft("draft-title:Oak Chair")
    finally:
        restore(tp, hp)
    assert ok is False, ok
    assert "Draft-list publish failed" in (drv._last_error or ""), drv._last_error
    assert tab.next_clicked is False and tab.publish_clicked is False
    assert tab.closed is True, "tab must be closed even on failure"
    print("2. Next never appears, tab closed mid-wait -> False + actual error: PASS")

    # 3. Next clicked, screen-2 Publish never appears; tab destroyed.
    tab = EditorTab(next_on_poll=1, publish_on_poll=999, destroy_after=4)
    drv, tp, hp = make_driver(list_page_with_card(), FakeContext(tab))
    try:
        ok = drv.publish_draft("draft-title:Oak Chair")
    finally:
        restore(tp, hp)
    assert ok is False, ok
    assert tab.next_clicked is True, "Next must have been clicked"
    assert tab.publish_clicked is False, "Publish must NOT be clicked when absent"
    assert tab.closed is True
    print("3. Next clicked, Publish never appears -> False, no skipped click: PASS")

    print("ALL v0.2.134 NEXT -> PUBLISH TESTS PASS")


if __name__ == "__main__":
    main()

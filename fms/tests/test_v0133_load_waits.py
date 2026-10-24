"""v0.2.133 targeted test — unbounded loading waits in the Draft &
Publish 1 title-token path (exactly as supplied; no fixed cutoff).

  1. Save path: Drafts-list visit waits for document load AND the
     exact draft title to become visible (timeout=0 in the patch);
     the token is returned once the title renders.
  2. Save path, TRUE navigation error: if opening the Drafts list
     raises, save returns None with the actual error recorded
     ("could not open Your Drafts yet") — reported, never silently
     treated as success.
  3. Token publish, draft tab: the Publish poll loop waits instead
     of failing fast — a Publish control that only becomes visible
     + enabled on a later poll is still found and clicked, and the
     tab is closed afterwards.
  4. Token publish, tab navigation error: a tab goto failure returns
     False with the actual error, and the tab is closed.
The fake page mimics Playwright locators; wait_for_timeout calls
are counted to prove the loop really waited (polls > 1).
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
    """A locator whose first.wait_for succeeds (title rendered)."""
    def __init__(self):
        self.first = self
    def wait_for(self, state=None, timeout=None):
        assert timeout == 0, f"expected unbounded wait, got timeout={timeout}"
        return None


class FakePage:
    def __init__(self, goto_raises=False):
        self.url = CREATE_URL
        self._goto_raises = goto_raises
        self.goto_calls = []
        self.closed = False
        self.wait_timeouts = 0
    def get_by_role(self, role, name=None):
        if role == "button":
            return FakeCollection([FakeBtn()])
        return FakeCollection([])
    def get_by_text(self, pattern, exact=False):
        pat = getattr(pattern, "pattern", str(pattern))
        if "publish" in pat.lower():
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
        if self._goto_raises and url == SELLING_URL:
            raise RuntimeError("simulated network failure")
        self.goto_calls.append(url)
        self.url = url
    def reload(self, **kw):
        pass
    def close(self):
        self.closed = True


class PollCollection:
    """Publish candidate that only becomes available on poll N."""
    def __init__(self, available_on_poll, on_found):
        self._n = available_on_poll
        self._polls = 0
        self._on_found = on_found
        self._clicked = False
    def count(self):
        self._polls += 1
        if self._clicked:
            return 0
        return 1 if self._polls >= self._n else 0
    def nth(self, i):
        return FakeBtn(on_click=self._click)
    def _click(self):
        self._clicked = True
        self._on_found()
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


class FakeTab(FakePage):
    """Draft editor tab: title renders, Publish appears on poll 3.

    v0.2.134: the tab is two-screen — a distinct Next control on
    screen 1 must be clicked before the Publish control on screen 2
    is looked for. The old single-button fake returned the SAME
    object for the Next and Publish lookups, so after the Next click
    hid it, the Publish loop could never find a control — that is a
    fake artefact, not shipped behaviour (on real Facebook the two
    controls are distinct elements). get_by_role therefore answers
    the Next and Publish lookups with separate controls.
    """
    def __init__(self):
        super().__init__()
        self.publish_clicked = False
        self._poll = PollCollection(3, lambda: setattr(self, "publish_clicked", True))
    def get_by_role(self, role, name=None):
        if role == "button":
            pat = getattr(name, "pattern", str(name)).lower()
            if "next" in pat:
                return FakeCollection([FakeBtn()])
            return self._poll
        return FakeCollection([])
    def get_by_text(self, pattern, exact=False):
        pat = getattr(pattern, "pattern", str(pattern))
        if "publish" in pat.lower() or "next" in pat.lower():
            return FakeCollection([])
        return TitleWait()
    def locator(self, sel):
        return FakeCollection([])


class FakeContext:
    def __init__(self, tab):
        self.tab = tab
    def new_page(self):
        return self.tab


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


def make_driver(page, context=None):
    orig_tp = B.S.to_playwright
    orig_pause = B.human.human_pause
    B.S.to_playwright = lambda p, sel: FakeCollection([])
    B.human.human_pause = lambda *a, **k: None
    drv = B.PlaywrightDriver.__new__(B.PlaywrightDriver)
    drv._ensure = lambda: None
    drv._page = page
    drv._context = context
    drv._fill_form_draft_publish = lambda pg, payload: True
    drv._snap = lambda pg, name: None
    drv._last_error = None
    return drv, orig_tp, orig_pause


def restore(orig_tp, orig_pause):
    B.S.to_playwright = orig_tp
    B.human.human_pause = orig_pause


def main():
    # 1. Save path waits for list load + exact title, then tokens.
    page = FakePage()
    drv, tp, hp = make_driver(page)
    try:
        res = drv._create_draft_draft_publish({"title": "Oak Chair"})
    finally:
        restore(tp, hp)
    assert res == "draft-title:Oak Chair", res
    assert SELLING_URL in page.goto_calls, page.goto_calls
    print("1. save waits for list load + title, returns token: PASS")

    # 2. Save path, true navigation error -> None + actual error.
    page = FakePage(goto_raises=True)
    drv, tp, hp = make_driver(page)
    try:
        res = drv._create_draft_draft_publish({"title": "Oak Chair"})
    finally:
        restore(tp, hp)
    assert res is None, res
    assert "could not open Your Drafts yet" in (drv._last_error or ""), drv._last_error
    assert "simulated network failure" in (drv._last_error or ""), drv._last_error
    print("2. save, navigation error -> None + actual error reported: PASS")

    # 3. Token publish: Publish appears only on poll 3 — loop waits,
    #    clicks, closes the tab. (Old code would have returned False
    #    immediately with 'Publish button was not found'.)
    parent = FakeNode(links=["/marketplace/item/999001"])
    node = FakeNode(parent=parent)
    list_page = FakePage()
    list_page.get_by_text = lambda pattern, exact=False: (
        FakeCollection([node]) if "publish" not in str(getattr(pattern, "pattern", pattern)).lower()
        else FakeCollection([]))
    tab = FakeTab()
    drv, tp, hp = make_driver(list_page, FakeContext(tab))
    try:
        ok = drv.publish_draft("draft-title:Oak Chair")
    finally:
        restore(tp, hp)
    assert ok is True, drv._last_error
    assert tab.publish_clicked is True, "Publish must be clicked"
    assert tab.closed is True, "tab must be closed"
    assert tab.wait_timeouts >= 2, f"loop must have waited, got {tab.wait_timeouts} polls"
    print(f"3. tab Publish appears late -> loop waited ({tab.wait_timeouts} polls), clicked, tab closed: PASS")

    # 4. Token publish, tab navigation error -> False + actual error.
    class BadTab(FakeTab):
        def goto(self, url, **kw):
            raise RuntimeError("simulated tab navigation failure")
    list_page = FakePage()
    list_page.get_by_text = lambda pattern, exact=False: (
        FakeCollection([node]) if "publish" not in str(getattr(pattern, "pattern", pattern)).lower()
        else FakeCollection([]))
    tab = BadTab()
    drv, tp, hp = make_driver(list_page, FakeContext(tab))
    try:
        ok = drv.publish_draft("draft-title:Oak Chair")
    finally:
        restore(tp, hp)
    assert ok is False, ok
    assert "Draft-list publish failed" in (drv._last_error or ""), drv._last_error
    assert tab.closed is True, "tab must be closed even on failure"
    print("4. tab navigation error -> False + actual error, tab closed: PASS")

    print("ALL v0.2.133 LOAD WAIT TESTS PASS")


if __name__ == "__main__":
    main()

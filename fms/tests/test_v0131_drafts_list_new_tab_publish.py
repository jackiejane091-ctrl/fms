"""v0.2.131 targeted test — Drafts-list -> new tab -> Publish.

Covers the NEW Draft & Publish 1 contract (v0.2.130's real-ID
recovery contract is superseded by design; its test is retired):
  1. Save path returns a workflow-local "draft-title:<title>" token
     after a successful Save Draft click (no listing_id required).
  2. Token publish opens the selling?state=DRAFT list, finds the
     newest exact-title card, opens its link in a NEW tab, clicks
     Publish there, closes the tab, and returns True.
  3. Token publish with no matching card -> False, clear error,
     and no tab left open.
  4. Numeric-ID publish path is UNCHANGED from v0.2.130: exact-ID
     card Publish on the drafts page, success when the draft link
     disappears.
  5. Refusals preserved: empty title token and "draft" -> False.
The fake page mimics Playwright locators: a single-match locator
acts as the element; ancestor walk is via xpath=.. .
"""
import sys

sys.path.insert(0, ".")
import fms.browser as B  # noqa: E402

SELLING_URL = "https://www.facebook.com/marketplace/you/selling?state=DRAFT"
CREATE_URL = "https://www.facebook.com/marketplace/create/item"
DRAFTS_URL = "https://www.facebook.com/marketplace/you/drafts/"


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


class FakeAnchor:
    def __init__(self, href):
        self._href = href
    def get_attribute(self, _name):
        return self._href


class FakeNode:
    """A DOM node: links it owns, plus a parent for xpath=.. walks."""
    def __init__(self, links=(), parent=None):
        self._links = [FakeAnchor(h) for h in links]
        self._parent = parent
    def locator(self, selector):
        if selector.startswith("xpath="):
            return self._parent if self._parent is not None else FakeNode()
        return FakeCollection(list(self._links))
    def wait_for(self, state=None, timeout=None):
        pass


class FakePage:
    """Dual-mode fake: drafts-list page (title nodes) and draft editor tab."""
    def __init__(self, title_nodes=(), publish_button=True):
        self.url = CREATE_URL
        self._title_nodes = list(title_nodes)
        self._publish_button = publish_button
        self._publish_clicked = False
        self.goto_calls = []
        self.closed = False
    def get_by_role(self, role, name=None):
        if role == "button" and not self._publish_clicked:
            return FakeCollection([FakeBtn(on_click=lambda: setattr(self, "_publish_clicked", True))])
        return FakeCollection([])
    def get_by_text(self, pattern, exact=False):
        pat = getattr(pattern, "pattern", str(pattern))
        if "publish" in pat.lower():
            if self._publish_button:
                return FakeCollection([FakeBtn()])
            return FakeCollection([])
        if self.url == SELLING_URL:
            return FakeCollection(list(self._title_nodes))
        return FakeCollection([])
    def locator(self, sel):
        if "aria-label='Publish'" in sel or 'aria-label*=\'Publish\'' in sel:
            return FakeCollection([FakeBtn()]) if self._publish_button else FakeCollection([])
        return FakeCollection([])
    def wait_for_timeout(self, _ms):
        pass
    def wait_for_load_state(self, _state, timeout=None):
        pass
    def goto(self, url, **_kw):
        self.goto_calls.append(url)
        self.url = url
    def reload(self, **_kw):
        pass
    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, tab):
        self.tab = tab
        self.new_page_calls = 0
    def new_page(self):
        self.new_page_calls += 1
        return self.tab


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
    # 1. Save path returns the title token (no ID needed).
    page = FakePage()
    drv, tp, hp = make_driver(page)
    try:
        res = drv._create_draft_draft_publish({"title": "Oak Chair"})
    finally:
        restore(tp, hp)
    assert res == "draft-title:Oak Chair", res
    assert SELLING_URL in page.goto_calls, page.goto_calls
    assert page.goto_calls[-1] == CREATE_URL, page.goto_calls
    print("1. save returns draft-title token, visits list, back to create: PASS")

    # 2. Token publish: newest exact-title card -> new tab -> Publish -> close.
    parent = FakeNode(links=["/marketplace/item/999001"])
    node = FakeNode(parent=parent)
    list_page = FakePage(title_nodes=[node])
    tab = FakePage(publish_button=True)
    drv, tp, hp = make_driver(list_page, FakeContext(tab))
    try:
        ok = drv.publish_draft("draft-title:Oak Chair")
    finally:
        restore(tp, hp)
    assert ok is True, drv._last_error
    assert list_page.goto_calls[0] == SELLING_URL, list_page.goto_calls
    assert tab.goto_calls and tab.goto_calls[0] == "https://www.facebook.com/marketplace/item/999001", tab.goto_calls
    assert tab.closed is True, "new tab must be closed after publish"
    print("2. token publish via new tab, tab closed: PASS")

    # 3. Token publish with no matching card -> False, no tab opened.
    list_page = FakePage(title_nodes=[])
    tab = FakePage()
    drv, tp, hp = make_driver(list_page, FakeContext(tab))
    try:
        ok = drv.publish_draft("draft-title:Missing Chair")
    finally:
        restore(tp, hp)
    assert ok is False, ok
    assert "could not find" in (drv._last_error or ""), drv._last_error
    assert tab.goto_calls == [], "no tab navigation on missing card"
    print("3. token publish, no card -> False + clear error: PASS")

    # 4. Numeric-ID path unchanged: card Publish, draft link disappears.
    class NumericCard:
        """Anchor-like card: href carries the ID; Publish lives on the card."""
        def __init__(self, href):
            self._href = href
        def get_attribute(self, name):
            return self._href
        def locator(self, sel):
            if sel.startswith("xpath="):
                return self
            if "Publish" in sel:
                return FakeCollection([FakeBtn()])
            return FakeCollection([])

    class NumericPage(FakePage):
        def __init__(self):
            super().__init__()
            self.published = False
        def locator(self, sel):
            if "listing_id=555" in sel or "/555" in sel:
                if self.published:
                    return FakeCollection([])
                return FakeCollection([NumericCard("/marketplace/item/?listing_id=555")])
            return FakeCollection([])
    # The numeric path clicks via human_click on the card's Publish button.
    num_page = NumericPage()
    drv, tp, hp = make_driver(num_page)
    orig_click = B.human.human_click
    B.human.human_click = lambda pg, btn: (btn.click(), setattr(num_page, "published", True))
    try:
        ok = drv.publish_draft("555")
    finally:
        B.human.human_click = orig_click
        restore(tp, hp)
    assert ok is True, drv._last_error
    assert num_page.goto_calls and num_page.goto_calls[0] == DRAFTS_URL, num_page.goto_calls
    print("4. numeric-ID publish path unchanged (v0.2.130 behaviour): PASS")

    # 5. Refusals preserved.
    page = FakePage()
    drv, tp, hp = make_driver(page)
    try:
        assert drv.publish_draft("draft-title:") is False
        assert drv.publish_draft("draft") is False
    finally:
        restore(tp, hp)
    print("5. empty token / 'draft' refused: PASS")

    # 6. Save with no visible/enabled Save Draft button -> None (v0.2.125
    #    safety coverage, carried into the new contract).
    class NoButtonPage(FakePage):
        def get_by_role(self, role, name=None):
            return FakeCollection([])
    page = NoButtonPage()
    drv, tp, hp = make_driver(page)
    try:
        res = drv._create_draft_draft_publish({"title": "Oak Chair"})
    finally:
        restore(tp, hp)
    assert res is None, res
    assert "not found" in (drv._last_error or ""), drv._last_error
    print("6. save, no Save Draft button -> None + clear error: PASS")

    # 7. Save Draft click raises -> None + click-failed error.
    class RaisingBtn(FakeBtn):
        def click(self, timeout=None):
            raise RuntimeError("simulated click failure")
    class ClickFailPage(FakePage):
        def get_by_role(self, role, name=None):
            return FakeCollection([RaisingBtn()])
    page = ClickFailPage()
    drv, tp, hp = make_driver(page)
    try:
        res = drv._create_draft_draft_publish({"title": "Oak Chair"})
    finally:
        restore(tp, hp)
    assert res is None, res
    assert "click failed" in (drv._last_error or ""), drv._last_error
    print("7. save, click raises -> None + click-failed error: PASS")

    print("ALL v0.2.131 DRAFTS-LIST NEW-TAB PUBLISH TESTS PASS")


if __name__ == "__main__":
    main()

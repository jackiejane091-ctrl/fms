"""v0.2.130 targeted test — Your Drafts ID recovery in
_create_draft_draft_publish.

Simulates a save where Facebook does NOT expose listing_id in the
editor URL or immediate DOM, so the new recovery block runs:
  1. Unique title match in Your Drafts -> the recovered real ID is
     returned (and the flow returns to the create-item page).
  2. Ambiguous title match (two distinct draft IDs) -> None; neither
     ID is returned, so no unrelated draft can be published.
  3. Title not found in Your Drafts -> None; no fabricated ID.
  4. Recovery navigation raises -> None; clear failure, no success.
The fake page mimics Playwright locators: a single-match locator
acts as the element; ancestor walk is via xpath=.. ; the drafts
page only yields title nodes once navigated to.
"""
import sys

sys.path.insert(0, ".")
import fms.browser as B  # noqa: E402

DRAFTS_URL = "https://www.facebook.com/marketplace/you/drafts/"
CREATE_URL = "https://www.facebook.com/marketplace/create/item"


class FakeBtn:
    def __init__(self):
        self.clicked = 0
    def is_visible(self):
        return True
    def is_enabled(self):
        return True
    def scroll_into_view_if_needed(self, timeout=None):
        pass
    def click(self, timeout=None):
        self.clicked += 1


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


class FakeSignal:
    def __init__(self, visible):
        self._visible = visible
        self.first = self
    def count(self):
        return 1 if self._visible else 0
    def is_visible(self):
        return self._visible


class FakePage:
    def __init__(self, title_nodes=(), drafts_goto_raises=False):
        self.url = CREATE_URL
        self._title_nodes = list(title_nodes)
        self._drafts_goto_raises = drafts_goto_raises
        self.goto_calls = []
    def get_by_role(self, _role, name=None):
        return FakeCollection([FakeBtn()])
    def get_by_text(self, pattern, exact=False):
        pat = getattr(pattern, "pattern", str(pattern))
        if "draft saved" in pat:
            return FakeSignal(False)
        if self.url == DRAFTS_URL:
            return FakeCollection(list(self._title_nodes))
        return FakeCollection([])
    def locator(self, _sel):
        return FakeCollection([])
    def wait_for_timeout(self, _ms):
        pass
    def wait_for_load_state(self, _state, timeout=None):
        pass
    def goto(self, url, **_kw):
        if url == DRAFTS_URL and self._drafts_goto_raises:
            raise RuntimeError("simulated drafts navigation failure")
        self.goto_calls.append(url)
        self.url = url
    def reload(self, **_kw):
        pass


def run(page):
    orig_tp = B.S.to_playwright
    orig_pause = B.human.human_pause
    B.S.to_playwright = lambda p, sel: FakeCollection([])
    B.human.human_pause = lambda *a, **k: None
    try:
        drv = B.PlaywrightDriver.__new__(B.PlaywrightDriver)
        drv._ensure = lambda: None
        drv._page = page
        drv._fill_form_draft_publish = lambda pg, payload: True
        drv._snap = lambda pg, name: None
        drv._last_error = None
        result = drv._create_draft_draft_publish({"title": "Oak Chair"})
        return drv, result
    finally:
        B.S.to_playwright = orig_tp
        B.human.human_pause = orig_pause


def main():
    # 1. Unique match: title node -> parent carries one draft link.
    parent = FakeNode(links=["/marketplace/item/?listing_id=999001"])
    node = FakeNode(parent=parent)
    page = FakePage(title_nodes=[node])
    drv, res = run(page)
    assert res == "999001", res
    assert DRAFTS_URL in page.goto_calls, page.goto_calls
    assert page.goto_calls[-1] == CREATE_URL, page.goto_calls
    print("1. unique title match -> recovered real ID, back to create: PASS")

    # 2. Ambiguous: two distinct IDs under the title -> refused.
    p1 = FakeNode(links=["/marketplace/item/?listing_id=111"])
    p2 = FakeNode(links=["/marketplace/item/?listing_id=222"])
    page = FakePage(title_nodes=[FakeNode(parent=p1), FakeNode(parent=p2)])
    drv, res = run(page)
    assert res is None, res
    print("2. ambiguous title match -> None, no unrelated draft chosen: PASS")

    # 3. Title absent from Your Drafts -> refused, nothing fabricated.
    page = FakePage(title_nodes=[])
    drv, res = run(page)
    assert res is None, res
    print("3. no title match -> None, no fabricated ID: PASS")

    # 4. Recovery navigation raises -> refused, clear failure.
    page = FakePage(title_nodes=[node], drafts_goto_raises=True)
    drv, res = run(page)
    assert res is None, res
    assert drv._last_error, "expected a recorded failure reason"
    print("4. recovery navigation failure -> None + recorded reason: PASS")

    print("ALL v0.2.130 DRAFT ID RECOVERY TESTS PASS")


if __name__ == "__main__":
    main()

"""v0.2.136 targeted test — scroll-based Renew Listings page workflow.

The new PlaywrightDriver.renew_listings_from_page scans the renew
dialog's own scrollbar instead of refreshing after every pass.
The fake dialog exposes overlapping windows of rows, like a real
scrolling list:
  1. max_count=3, windows [A,B] -> [B,C] -> [C,D]: A,B clicked,
     scroll reveals C (B is NOT re-clicked), renewed 3, exactly
     1 scroll.
  2. max_count=10, windows [A,B] -> [B,C]: A,B,C clicked, bottom
     reached, refresh -> empty state -> renewed 3, no_eligible
     False (some were renewed).
  3. Empty state on arrival -> renewed [], no_eligible True.
  4. max_count=1 -> only the first row clicked, zero scrolls.
The fakes mimic Playwright locators; no live Facebook involved.
"""
import sys

sys.path.insert(0, ".")
import fms.browser as B  # noqa: E402

RENEW_URL = "https://www.facebook.com/marketplace/selling/renew_listings/?is_routable_dialog=true"
FEED_BODY = ("Welcome to your feed. " * 10).strip()
MKT_BODY = ("Marketplace — buy and sell near you. " * 5).strip()
RENEW_BODY = ("Your listings eligible for renewal are shown below. " * 3).strip()
EMPTY_BODY = "You have no eligible listings to renew right now."


class FakeLink:
    def __init__(self, href, text):
        self._href = href
        self._text = text
    def get_attribute(self, _name):
        return self._href
    def inner_text(self):
        return self._text


class FakeLinkColl:
    def __init__(self, link):
        self._link = link
    @property
    def first(self):
        return self._link


class FakeCard:
    def __init__(self, link):
        self._link = link
    def locator(self, sel):
        return FakeLinkColl(self._link)
    def inner_text(self):
        return self._link.inner_text()


class FakeBtn:
    def __init__(self, link, page, idx):
        self._link = link
        self._page = page
        self._idx = idx
    def is_visible(self):
        return True
    def is_enabled(self):
        return True
    def locator(self, sel):
        return FakeCard(self._link)
    def click(self, timeout=None):
        assert timeout == 0, f"expected unbounded click, got {timeout}"
        self._page.clicks.append(self._idx)


class CountLoc:
    def __init__(self, n):
        self._n = n
    def count(self):
        return self._n


class BodyLoc:
    def __init__(self, page):
        self._page = page
    def inner_text(self, timeout=None):
        return self._page.body_text()


class ButtonsLoc:
    def __init__(self, page):
        self._page = page
    def count(self):
        if self._page.reloaded_empty:
            return 0
        return len(self._page.window())
    def nth(self, i):
        return self._page.window()[i]


class DialogLoc:
    """Simulates the renew dialog's scrollable list."""
    def __init__(self, page):
        self._page = page
    def evaluate(self, _js):
        if self._page.win_idx < len(self._page.windows) - 1:
            self._page.win_idx += 1
            self._page.scrolls += 1
            return {"moved": True, "top": 100 * self._page.win_idx,
                    "max": 100 * (len(self._page.windows) - 1)}
        return {"moved": False, "top": 100 * self._page.win_idx,
                "max": 100 * (len(self._page.windows) - 1)}


class FakePage:
    def __init__(self, windows, renew_body=RENEW_BODY):
        self.windows = windows
        self.win_idx = 0
        self._renew_body = renew_body
        self.url = ""
        self.gotos = []
        self.reloads = 0
        self.reloaded_empty = False
        self.clicks = []
        self.scrolls = 0
    def window(self):
        return self.windows[self.win_idx]
    def body_text(self):
        if self.url == RENEW_URL:
            return EMPTY_BODY if self.reloaded_empty else self._renew_body
        if "facebook.com/" in self.url and "marketplace" not in self.url:
            return FEED_BODY
        return MKT_BODY
    def goto(self, url, **kw):
        assert kw.get("timeout") == 0, f"expected unbounded goto, got {kw}"
        self.url = url
        self.gotos.append(url)
    def reload(self, **kw):
        assert kw.get("timeout") == 0, f"expected unbounded reload, got {kw}"
        self.reloads += 1
        self.reloaded_empty = True
    def locator(self, sel):
        if sel == "body":
            return BodyLoc(self)
        if sel in ("[role='feed'], [role='main']", "[role='main']"):
            return CountLoc(1)
        if sel == "div[role='dialog']":
            return DialogLoc(self)
        if "Renew" in sel:
            return ButtonsLoc(self)
        return CountLoc(0)


def make_driver(page):
    orig_pause = B.human.human_pause
    B.human.human_pause = lambda *a, **k: None
    drv = B.PlaywrightDriver.__new__(B.PlaywrightDriver)
    drv._ensure = lambda: None
    drv._page = page
    drv._pause_checkpoint = lambda: None
    return drv, orig_pause


def rows_for(page, specs):
    return [FakeBtn(FakeLink(f"/marketplace/item/{lid}/", title), page, i)
            for i, (lid, title) in enumerate(specs)]


def main():
    # 1. Scroll reveals new rows; overlapping rows are NOT re-clicked.
    page = FakePage([])
    a, b, c, d = rows_for(page, [("111001", "Chair"), ("111002", "Table"),
                                 ("111003", "Lamp"), ("111004", "Sofa")])
    page.windows = [[a, b], [b, c], [c, d]]
    drv, hp = make_driver(page)
    try:
        res = drv.renew_listings_from_page(3)
    finally:
        B.human.human_pause = hp
    assert [r["id"] for r in res["renewed"]] == ["111001", "111002", "111003"], res
    assert res["no_eligible"] is False and res["failed"] == 0, res
    assert page.clicks == [0, 1, 2], page.clicks
    assert page.scrolls == 1, page.scrolls
    print("1. scroll reveals C, B not re-clicked, max 3, 1 scroll: PASS")

    # 2. Bottom reached -> refresh -> empty state -> stop with renewed.
    page = FakePage([])
    a, b, c = rows_for(page, [("111001", "Chair"), ("111002", "Table"),
                              ("111003", "Lamp")])
    page.windows = [[a, b], [b, c]]
    drv, hp = make_driver(page)
    try:
        res = drv.renew_listings_from_page(10)
    finally:
        B.human.human_pause = hp
    assert len(res["renewed"]) == 3, res
    assert res["no_eligible"] is False, res
    assert page.reloads == 1, page.reloads
    print("2. bottom -> refresh -> empty -> renewed 3, no_eligible False: PASS")

    # 3. Empty state on arrival.
    page = FakePage([[]], renew_body=EMPTY_BODY)
    drv, hp = make_driver(page)
    try:
        res = drv.renew_listings_from_page(5)
    finally:
        B.human.human_pause = hp
    assert res["renewed"] == [] and res["no_eligible"] is True, res
    assert page.clicks == [] and page.scrolls == 0, (page.clicks, page.scrolls)
    print("3. empty state -> renewed [], no_eligible True, no clicks/scrolls: PASS")

    # 4. Max reached mid-view -> no scrolling at all.
    page = FakePage([])
    a, b = rows_for(page, [("111001", "Chair"), ("111002", "Table")])
    page.windows = [[a, b]]
    drv, hp = make_driver(page)
    try:
        res = drv.renew_listings_from_page(1)
    finally:
        B.human.human_pause = hp
    assert len(res["renewed"]) == 1 and res["renewed"][0]["id"] == "111001", res
    assert page.scrolls == 0, page.scrolls
    print("4. max 1 mid-view -> 1 click, 0 scrolls: PASS")

    print("ALL v0.2.136 RENEW SCROLL TESTS PASS")


if __name__ == "__main__":
    main()

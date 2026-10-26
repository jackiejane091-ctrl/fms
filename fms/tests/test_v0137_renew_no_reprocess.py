"""v0.2.137 targeted test — Renew Listing: no reprocessing after
refresh + sub-second inter-click gap (user requirements 5 and 1).

  1. Refresh must NOT re-click a renewed listing: after the first
     refresh the renewed listing A is STILL shown, plus a new
     listing B. A is skipped, B is clicked once, and after the
     second refresh (empty state) the run ends with renewed
     [A, B] and clicks [A, B] — A exactly once. (On the v0.2.136
     code this test FAILS: processed.clear() made A click again.)
  2. Inter-click gap: every post-click human_pause recorded
     during a 2-click run has hi <= 1000 ms, so sequential
     clicks stay under one second apart (plus fast DOM reads).
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
        return 0 if self._page.empty_now else len(self._page.window())
    def nth(self, i):
        return self._page.window()[i]


class DialogLoc:
    def __init__(self, page):
        self._page = page
    def evaluate(self, _js):
        # Single-window list: already at the bottom.
        return {"moved": False, "top": 0, "max": 0}


class FakePage:
    """Reload stages: after reload N, (windows, empty?) switches.

    stages[0] is the initial state; reload i moves to stages[i].
    """
    def __init__(self, stages):
        self.stages = stages
        self.stage = 0
        self.url = ""
        self.reloads = 0
        self.clicks = []
    @property
    def empty_now(self):
        return self.stages[self.stage][1]
    def window(self):
        return self.stages[self.stage][0]
    def body_text(self):
        if self.url == RENEW_URL:
            return EMPTY_BODY if self.empty_now else RENEW_BODY
        if "facebook.com/" in self.url and "marketplace" not in self.url:
            return FEED_BODY
        return MKT_BODY
    def goto(self, url, **kw):
        assert kw.get("timeout") == 0, f"expected unbounded goto, got {kw}"
        self.url = url
    def reload(self, **kw):
        assert kw.get("timeout") == 0, f"expected unbounded reload, got {kw}"
        self.reloads += 1
        if self.reloads < len(self.stages):
            self.stage = self.reloads
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


def make_driver(page, pause_log=None):
    orig_pause = B.human.human_pause
    if pause_log is not None:
        B.human.human_pause = lambda a, b: pause_log.append((a, b))
    else:
        B.human.human_pause = lambda *a, **k: None
    drv = B.PlaywrightDriver.__new__(B.PlaywrightDriver)
    drv._ensure = lambda: None
    drv._page = page
    drv._pause_checkpoint = lambda: None
    return drv, orig_pause


def main():
    page = FakePage([])
    a = FakeBtn(FakeLink("/marketplace/item/111001/", "Chair"), page, 0)
    b = FakeBtn(FakeLink("/marketplace/item/111002/", "Table"), page, 1)
    # Stage 0: [A]. Stage 1 (after 1st refresh): [A still shown, B new].
    # Stage 2 (after 2nd refresh): empty.
    page.stages = [([a], False), ([a, b], False), ([], True)]
    drv, hp = make_driver(page)
    try:
        res = drv.renew_listings_from_page(10)
    finally:
        B.human.human_pause = hp
    assert [r["id"] for r in res["renewed"]] == ["111001", "111002"], res
    assert page.clicks == [0, 1], f"A must be clicked exactly once, got {page.clicks}"
    assert page.reloads == 2, page.reloads
    print("1. refresh keeps A processed, B clicked once, empty ends run: PASS")

    # 2. Post-click pauses stay under one second.
    page = FakePage([])
    a = FakeBtn(FakeLink("/marketplace/item/111001/", "Chair"), page, 0)
    b = FakeBtn(FakeLink("/marketplace/item/111002/", "Table"), page, 1)
    page.stages = [([a, b], False)]
    pauses = []
    drv, hp = make_driver(page, pause_log=pauses)
    try:
        res = drv.renew_listings_from_page(2)
    finally:
        B.human.human_pause = hp
    assert len(res["renewed"]) == 2, res
    assert pauses, "expected post-click pauses to be recorded"
    assert all(hi <= 1000 for _lo, hi in pauses), pauses
    print(f"2. sequential clicks, post-click pauses {pauses} all <= 1000 ms: PASS")

    print("ALL v0.2.137 RENEW NO-REPROCESS TESTS PASS")


if __name__ == "__main__":
    main()

"""v0.2.138 targeted test — Renew button click fix.

  1. A button labelled only "Renew" (not "Renew listing") is
     found and clicked: the fake answers ONLY the new third
     selector (has-text('Renew')) and nothing to the aria-label
     or 'Renew listing' selectors, so the old chain (which fell
     back to an inner span) could never have clicked it.
  2. A click that fails once stays retryable: with a second
     button succeeding in the same pass, the failed button is
     retried on the next pass and renewed; clicked buttons
     vanish from the list, as on real Facebook, and the run
     then ends on the empty state.
  3. A button that always fails does NOT refresh forever: the
     run returns an explicit error, zero reloads, zero renewals.
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
    def __init__(self, link, page, idx, fail_times=0):
        self._link = link
        self._page = page
        self._idx = idx
        self._fail_times = fail_times
        self.clicks = 0
        self.vanished = False
    def is_visible(self):
        return True
    def is_enabled(self):
        return True
    def locator(self, sel):
        return FakeCard(self._link)
    def click(self, timeout=None):
        assert timeout == 0, f"expected unbounded click, got {timeout}"
        self.clicks += 1
        self._page.clicks.append(self._idx)
        if self.clicks <= self._fail_times:
            raise RuntimeError("simulated click failure")
        self.vanished = True


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
    def _live(self):
        return [b for b in self._page.buttons if not b.vanished]
    def count(self):
        return len(self._live())
    def nth(self, i):
        return self._live()[i]


class DialogLoc:
    def __init__(self, page):
        self._page = page
    def evaluate(self, _js):
        return {"moved": False, "top": 0, "max": 0}


class FakePage:
    def __init__(self, buttons, renew_text_only=False):
        self.buttons = buttons
        self.renew_text_only = renew_text_only
        self.url = ""
        self.reloads = 0
        self.clicks = []
    def body_text(self):
        if self.url == RENEW_URL:
            live = [b for b in self.buttons if not b.vanished]
            return EMPTY_BODY if not live else RENEW_BODY
        if "facebook.com/" in self.url and "marketplace" not in self.url:
            return FEED_BODY
        return MKT_BODY
    def goto(self, url, **kw):
        assert kw.get("timeout") == 0, f"expected unbounded goto, got {kw}"
        self.url = url
    def reload(self, **kw):
        assert kw.get("timeout") == 0, f"expected unbounded reload, got {kw}"
        self.reloads += 1
    def locator(self, sel):
        if sel == "body":
            return BodyLoc(self)
        if sel in ("[role='feed'], [role='main']", "[role='main']"):
            return CountLoc(1)
        if sel == "div[role='dialog']":
            return DialogLoc(self)
        if "Renew" in sel:
            if self.renew_text_only:
                # Only the new 'Renew' text selector matches; the
                # aria-label and 'Renew listing' selectors match
                # nothing, and no span is ever offered.
                if "has-text('Renew')" in sel and "Renew listing" not in sel:
                    return ButtonsLoc(self)
                return CountLoc(0)
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


def main():
    # 1. Only a "Renew" (not "Renew listing") button exists.
    page = FakePage([], renew_text_only=True)
    btn = FakeBtn(FakeLink("/marketplace/item/111001/", "Chair"), page, 0)
    page.buttons = [btn]
    drv, hp = make_driver(page)
    try:
        res = drv.renew_listings_from_page(5)
    finally:
        B.human.human_pause = hp
    assert len(res["renewed"]) == 1 and res["renewed"][0]["id"] == "111001", res
    assert btn.clicks == 1, btn.clicks
    print("1. 'Renew'-only button found via new selector and clicked: PASS")

    # 2. Fail-once button retried after a sibling succeeds.
    page = FakePage([])
    a = FakeBtn(FakeLink("/marketplace/item/111001/", "Chair"), page, 0, fail_times=1)
    b = FakeBtn(FakeLink("/marketplace/item/111002/", "Table"), page, 1)
    page.buttons = [a, b]
    drv, hp = make_driver(page)
    try:
        res = drv.renew_listings_from_page(5)
    finally:
        B.human.human_pause = hp
    assert [r["id"] for r in res["renewed"]] == ["111002", "111001"], res
    assert a.clicks == 2 and b.clicks == 1, (a.clicks, b.clicks)
    assert "error" not in res, res
    print("2. failed click retried on next pass, both renewed, clean end: PASS")

    # 3. Always-failing button -> explicit error, no refresh loop.
    page = FakePage([])
    bad = FakeBtn(FakeLink("/marketplace/item/111001/", "Chair"), page, 0,
                  fail_times=99)
    page.buttons = [bad]
    drv, hp = make_driver(page)
    try:
        res = drv.renew_listings_from_page(5)
    finally:
        B.human.human_pause = hp
    assert res["renewed"] == [], res
    assert res["failed"] == 1, res
    assert "refresh loop" in (res.get("error") or ""), res
    assert page.reloads == 0, page.reloads
    print("3. always-failing button -> explicit error, 0 reloads: PASS")

    print("ALL v0.2.138 RENEW CLICK FIX TESTS PASS")


if __name__ == "__main__":
    main()

"""v0.2.135 targeted test — Renew Listings page workflow.

Driver level (PlaywrightDriver.renew_listings_from_page, fake page):
  1. 3 Renew buttons, max_count=2 -> exactly 2 clicked (reverse
     order), no reload needed, no_eligible False.
  2. Empty state on arrival -> renewed [], no_eligible True,
     no clicks.
  3. Fewer eligible than requested (1 button, max 5) -> clicked,
     page refreshed, empty state -> renewed 1, no_eligible False.
  4. A button that stops being clickable between the readiness
     wait and the pass -> error 'Renew buttons were present but
     none could be clicked', renewed [], no false success.
Engine level (RunController._renew_flow, in-memory SQLite):
  5. Page path (driver HAS renew_listings_from_page): 2 of 3
     fake listings renewed -> 2 'renewed' DB rows + log line.
  6. Page path, nothing eligible -> 'No renew listings
     available' log, no DB rows.
  7. Legacy fallback (driver WITHOUT the page method) still
     works: 2/2 renewed via per-listing renew_listing.
The fakes mimic Playwright locators; no live Facebook involved.
"""
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, ".")
import fms.browser as B  # noqa: E402
import fms.engine as E  # noqa: E402

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


class FakeBtn:
    def __init__(self, link, page, idx, enabled=True, flaky=False):
        self._link = link
        self._page = page
        self._idx = idx
        self._enabled = enabled
        self._flaky = flaky
        self._enabled_calls = 0
    def is_visible(self):
        return True
    def is_enabled(self):
        self._enabled_calls += 1
        if self._flaky:
            return self._enabled_calls == 1
        return self._enabled
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
        return len(self._page.buttons) if not self._page.reloaded_empty else 0
    def nth(self, i):
        return self._page.buttons[i]


class FakePage:
    def __init__(self, buttons, renew_body=RENEW_BODY):
        self.buttons = buttons
        for b in buttons:
            b._page = self
        self._renew_body = renew_body
        self.url = ""
        self.gotos = []
        self.reloads = 0
        self.reloaded_empty = False
        self.clicks = []
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
        if "Renew" in sel:
            return ButtonsLoc(self)
        return CountLoc(0)


def make_driver(page):
    drv = B.PlaywrightDriver.__new__(B.PlaywrightDriver)
    drv._ensure = lambda: None
    drv._page = page
    drv._pause_checkpoint = lambda: None
    return drv


def btns_for(page, specs):
    out = []
    for i, (lid, title, kw) in enumerate(specs):
        out.append(FakeBtn(FakeLink(f"/marketplace/item/{lid}/", title), page, i, **kw))
    return out


class ShimDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE listings (
                id INTEGER PRIMARY KEY, run_id INTEGER, account_id INTEGER,
                product_id INTEGER, title_used TEXT, status TEXT,
                fb_listing_id TEXT, images_used TEXT, error TEXT,
                posted_at TEXT);
            CREATE TABLE runs (id INTEGER PRIMARY KEY, total_posted INTEGER);
            INSERT INTO runs (id, total_posted) VALUES (1, 0);
            """)
        self.conn.commit()
    def execute(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur
    def fetchall(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]


def make_controller(db, lo, hi):
    ctl = E.RunController.__new__(E.RunController)
    ctl.cfg = SimpleNamespace(renew_min_count=lo, renew_max_count=hi)
    ctl.run_id = 1
    ctl.db = db
    ctl.logs = []
    ctl.log = lambda msg, level="info", account_id=None: ctl.logs.append(msg)
    ctl._wait_if_paused = lambda: None
    return ctl


def main():
    # 1. max_count caps the pass; reverse order; no reload.
    page = FakePage([])
    page.buttons = btns_for(page, [("111001", "Chair", {}), ("111002", "Table", {}),
                                   ("111003", "Lamp", {})])
    res = make_driver(page).renew_listings_from_page(2)
    assert len(res["renewed"]) == 2, res
    assert res["no_eligible"] is False and res["failed"] == 0, res
    assert page.clicks == [2, 1], page.clicks
    assert page.reloads == 0, page.reloads
    assert page.gotos[0] == "https://www.facebook.com/"
    assert page.gotos[-1] == RENEW_URL, page.gotos
    print("1. 3 buttons, max 2 -> 2 clicked in reverse order, feed->marketplace->renew: PASS")

    # 2. Empty state on arrival.
    page = FakePage([], renew_body=EMPTY_BODY)
    res = make_driver(page).renew_listings_from_page(5)
    assert res["renewed"] == [] and res["no_eligible"] is True, res
    assert page.clicks == [], page.clicks
    print("2. empty state -> renewed [], no_eligible True, no clicks: PASS")

    # 3. Fewer eligible than requested: click, refresh, empty.
    page = FakePage([])
    page.buttons = btns_for(page, [("111001", "Chair", {})])
    res = make_driver(page).renew_listings_from_page(5)
    assert len(res["renewed"]) == 1, res
    assert res["no_eligible"] is False and res["failed"] == 0, res
    assert page.reloads == 1, page.reloads
    print("3. 1 button, max 5 -> clicked, refreshed, empty -> renewed 1: PASS")

    # 4. Button unclickable between wait and pass -> error, no success.
    page = FakePage([])
    page.buttons = btns_for(page, [("111001", "Chair", {"flaky": True})])
    res = make_driver(page).renew_listings_from_page(5)
    assert res["renewed"] == [], res
    assert "none could be clicked" in (res.get("error") or ""), res
    assert page.clicks == [], page.clicks
    print("4. present-but-unclickable -> error, no false success: PASS")

    # 5. Engine page path: 2 of 3 fake listings -> DB rows + log.
    B.MockDriver.reset()
    B.MockDriver.fake_listings = [{"id": "111001", "title": "Chair"},
                                  {"id": "111002", "title": "Table"},
                                  {"id": "111003", "title": "Lamp"}]
    db = ShimDB()
    ctl = make_controller(db, 2, 2)
    mock = B.MockDriver({"label": "Acct"})
    ctl._renew_flow({"id": 9, "label": "Acct"}, mock, 0)
    rows = db.fetchall("SELECT * FROM listings WHERE status = 'renewed'")
    assert len(rows) == 2, rows
    assert {r["fb_listing_id"] for r in rows} == {"111001", "111002"}, rows
    assert any("renewed 2 listing(s)" in m for m in ctl.logs), ctl.logs
    print("5. engine page path -> 2 'renewed' DB rows + log: PASS")

    # 6. Engine page path, nothing eligible.
    B.MockDriver.reset()
    db = ShimDB()
    ctl = make_controller(db, 1, 3)
    ctl._renew_flow({"id": 9, "label": "Acct"}, B.MockDriver({"label": "Acct"}), 0)
    assert db.fetchall("SELECT * FROM listings") == [], "no rows expected"
    assert any("No renew listings available" in m for m in ctl.logs), ctl.logs
    print("6. engine page path, none eligible -> log, no rows: PASS")

    # 7. Legacy fallback still works for drivers without the page method.
    class LegacyDriver:
        def get_my_listings(self):
            return [{"id": "111001"}, {"id": "111002"}]
        def renew_listing(self, lid):
            return True
    db = ShimDB()
    ctl = make_controller(db, 2, 2)
    ctl._inter_post_delay = lambda: None
    ctl._renew_flow({"id": 9, "label": "Acct"}, LegacyDriver(), 0)
    assert any("renewed 2/2" in m for m in ctl.logs), ctl.logs
    print("7. legacy fallback (no page method) -> 2/2 renewed: PASS")

    B.MockDriver.reset()
    print("ALL v0.2.135 RENEW LISTING TESTS PASS")


if __name__ == "__main__":
    main()

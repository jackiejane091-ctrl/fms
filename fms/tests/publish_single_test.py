"""Safety tests for the gated single-listing LIVE publish (v0.2.100).

Layer 1 drives the REAL PlaywrightDriver.publish_filled_form against a
fake page (no browser, no Facebook): the single-click rule, the
disabled/unexpected-screen aborts, the publish-screen reuse
(v0.2.107) and the live/uncertain verification are exercised directly.
Since v0.2.108 the real driver has NO in-app approval gate: the
approval callback is never consulted.

Layer 2 drives the engine's publish_single flow with MockDriver and a
stubbed single-post-test fill report: approval freshness (stale signals
are discarded), Stop/timeout, duplicate protection, verification gate
and the uncertain-outcome no-retry rule. (The MockDriver still honors
the approval callback, so Layer 2 keeps covering the engine's
bookkeeping around the callback contract.)

Run:  python -m fms.tests.publish_single_test
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fms import accounts as accounts_mod          # noqa: E402
from fms import human as human_mod                # noqa: E402
from fms import products as products_mod          # noqa: E402
from fms import single_post_test as spt_mod       # noqa: E402
from fms.browser import MockDriver, PlaywrightDriver  # noqa: E402
from fms.db import Database                       # noqa: E402
from fms.engine import ListingEngine, RunConfig   # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, fn) -> None:
    global PASS, FAIL
    try:
        fn()
    except Exception as exc:
        FAIL += 1
        print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    else:
        PASS += 1
        print(f"  ok    {name}")


def wait_until(fn, timeout: float = 30.0, poll: float = 0.2):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(poll)
    return fn()


# ============================================================ fake page ====
class _FakeLocator:
    def __init__(self, name, present=True, enabled=True):
        self.name = name
        self.present = present
        self.enabled = enabled

    @property
    def first(self):
        return self

    def wait_for(self, state=None, timeout=None):
        if not self.present:
            raise Exception("TimeoutError: locator not found")
        if state == "enabled" and not self.enabled:
            raise Exception("TimeoutError: locator not enabled")


class _FakeAnchor:
    def __init__(self, href):
        self._href = href

    def get_attribute(self, name):
        return self._href if name == "href" else None


class _FakePage:
    def __init__(self, next_enabled=True, publish_present=True,
                 body_text="", confirm_links=()):
        self.next_loc = _FakeLocator("next", enabled=next_enabled)
        self.pub_loc = _FakeLocator("publish", present=publish_present)
        self._body = body_text
        self._links = [_FakeAnchor(h) for h in confirm_links]
        self.url = "https://www.facebook.com/marketplace/create/item"
        self.on_publish = None  # callable(page) fired by the click recorder
        self.on_next = None     # callable(page) fired on a Next click

    def get_by_text(self, text, exact=False):
        return self.next_loc

    def locator(self, css):
        if "/marketplace/item/" in css:
            page = self

            class _AnchorList:
                def all(self):
                    return page._links
            return _AnchorList()
        return self.pub_loc

    def inner_text(self, sel):
        return self._body


class _ClickRecorder:
    """Patches human.human_click/human_pause during driver tests."""

    def __init__(self):
        self.clicks: list[str] = []
        self._orig_click = human_mod.human_click
        self._orig_pause = human_mod.human_pause

    def __enter__(self):
        rec = self

        def fake_click(page, locator, **kw):
            name = getattr(locator, "name", "?")
            rec.clicks.append(name)
            if name == "publish" and getattr(page, "on_publish", None):
                page.on_publish(page)
            if name == "next" and getattr(page, "on_next", None):
                page.on_next(page)
            return True

        human_mod.human_click = fake_click
        human_mod.human_pause = lambda *a, **k: None
        return self

    def __exit__(self, *exc):
        human_mod.human_click = self._orig_click
        human_mod.human_pause = self._orig_pause
        return False


def _driver():
    d = PlaywrightDriver.__new__(PlaywrightDriver)
    d._snap = lambda page, reason: None
    return d


PAYLOAD = {"title": "Oak Table", "price": "5.00",
           "location": "Coventry, United kingdom"}


# ------------------------------------------------------- layer 1: driver ---
def t_driver_no_approval_gate():
    # v0.2.108: the in-app Approve Publish gate is REMOVED from the real
    # driver. The approval callback is never consulted — even one that
    # would deny cannot block. On the already-open Publish screen the
    # flow re-validates the control and clicks Publish exactly once.
    # (This fake page shows no confirmation afterwards, so the verified
    # outcome is 'uncertain': clicked once, never retried.)
    page = _FakePage(body_text="Oak Table review")
    with _ClickRecorder() as rec:
        res = _driver().publish_filled_form(page, PAYLOAD, lambda: False)
    assert rec.clicks == ["publish"], rec.clicks
    assert res["status"] == "uncertain", res


def t_driver_next_disabled():
    # Next path only exists when the final Publish screen is NOT open.
    page = _FakePage(next_enabled=False, publish_present=False)
    with _ClickRecorder() as rec:
        res = _driver().publish_filled_form(page, PAYLOAD, lambda: True)
    assert res["status"] == "aborted" and res["step"] == "next_disabled", res
    assert rec.clicks == [], rec.clicks


def t_driver_unexpected_screen():
    page = _FakePage(publish_present=False)
    with _ClickRecorder() as rec:
        res = _driver().publish_filled_form(page, PAYLOAD, lambda: True)
    assert res["status"] == "aborted" and res["step"] == "publish_screen", res
    assert "publish" not in rec.clicks, rec.clicks


def t_driver_approved_one_click_live():
    page = _FakePage(body_text="Oak Table review")
    page.on_publish = lambda p: setattr(
        p, "url", "https://www.facebook.com/marketplace/item/424242/")
    with _ClickRecorder() as rec:
        res = _driver().publish_filled_form(page, PAYLOAD, lambda: True)
    assert res["status"] == "live" and res["listing_id"] == "424242", res
    assert rec.clicks.count("publish") == 1, rec.clicks


def t_driver_uncertain_no_retry():
    page = _FakePage(body_text="Oak Table review")  # no url change, no links
    with _ClickRecorder() as rec:
        res = _driver().publish_filled_form(page, PAYLOAD, lambda: True)
    assert res["status"] == "uncertain", res
    assert rec.clicks.count("publish") == 1, rec.clicks  # clicked once, never retried


def t_driver_reuse_publish_screen_no_next():
    # v0.2.107 headline case: the single-post test already clicked Next
    # and landed on the final screen — Publish present, Next ALSO still
    # resolvable. The workflow must reuse the screen: Next is never
    # clicked again, approval still gates, Publish clicked once.
    page = _FakePage(body_text="Oak Table review")
    page.on_publish = lambda p: setattr(
        p, "url", "https://www.facebook.com/marketplace/item/515151/")
    with _ClickRecorder() as rec:
        res = _driver().publish_filled_form(page, PAYLOAD, lambda: True)
    assert res["status"] == "live" and res["listing_id"] == "515151", res
    assert rec.clicks == ["publish"], rec.clicks  # no "next" at all


def t_driver_form_screen_next_clicked_once():
    # The form screen is still showing (no Publish control yet): Next
    # may be clicked exactly once, which reveals the Publish screen;
    # then approval gates the single Publish click as before.
    page = _FakePage(publish_present=False, body_text="Oak Table review")
    page.on_next = lambda p: setattr(p.pub_loc, "present", True)
    page.on_publish = lambda p: setattr(
        p, "url", "https://www.facebook.com/marketplace/item/626262/")
    with _ClickRecorder() as rec:
        res = _driver().publish_filled_form(page, PAYLOAD, lambda: True)
    assert res["status"] == "live" and res["listing_id"] == "626262", res
    assert rec.clicks == ["next", "publish"], rec.clicks


# ------------------------------------------------------- layer 2: engine ---
def _row(field, expected, status):
    return {"field": field, "expected_value": expected,
            "actual_value": expected, "status": status, "detail": ""}


GOOD_ROWS = [
    _row("photos", 1, "VERIFIED"),
    _row("title", "T1", "VERIFIED"),
    _row("price", "100.00", "VERIFIED"),
    _row("category", "Furniture", "VERIFIED"),
    _row("condition", "New", "VERIFIED"),
    _row("description", "d", "VERIFIED"),
    _row("availability", "In Stock", "VERIFIED"),
    _row("location", "Manchester, United kingdom", "VERIFIED"),
    _row("delivery:door_dropoff", "door_dropoff", "VERIFIED"),
    # Not configured -> SKIPPED with a falsy expected value: must NOT block.
    _row("hide_from_friends", False, "SKIPPED"),
    _row("more_details", True, "INFO"),
    _row("next_button", "not clicked", "INFO"),
]


def _report(rows):
    return {"fields": rows, "summary": {}, "publish_attempted": False,
            "next_clicked": False}


def _make_engine():
    MockDriver.reset()
    return ListingEngine(Database(), driver_factory=MockDriver)


def _seed(eng, with_images=False):
    db = eng.db
    gid = accounts_mod.create_group(db, f"g-{time.time_ns()}")
    accounts_mod.add_account(db, gid, "user0@x.com", "pw0")
    img_folder = None
    if with_images:
        img_folder = tempfile.mkdtemp()
        Path(img_folder, "a.jpg").write_bytes(b"x")
    pid = products_mod.create_product(db, "Prod", base_price=100.0,
                                      image_folder=img_folder)
    products_mod.set_titles(db, pid, ["T1"])
    products_mod.set_locations(db, pid, ["Manchester, United Kingdom"])
    return gid, pid


def _start(eng, gid, pid, timeout_s=None):
    cfg = RunConfig(group_id=gid, product_ids=[pid], mode="publish_single",
                    batch_size=1)
    rid = eng.start_run(cfg)
    if timeout_s is not None:
        ctl = eng._controller(rid)
        assert ctl is not None
        ctl.publish_approval_timeout_s = timeout_s
    return rid


def _clicks(event):
    return sum(1 for c in MockDriver.calls if c["event"] == event)


def _ready(eng, rid):
    rows = eng.db.fetchall("SELECT message FROM logs WHERE run_id = ?",
                           (rid,))
    return any("READY TO PUBLISH" in (r["message"] or "") for r in rows)


def _listing_statuses(eng):
    rows = eng.db.fetchall("SELECT status FROM listings")
    return [r["status"] for r in rows]


def _run_with_fill(rows):
    """Start a publish_single run with the fill stubbed to `rows`."""
    eng = _make_engine()
    gid, pid = _seed(eng)
    orig = spt_mod.run_single_post_test
    spt_mod.run_single_post_test = lambda driver, payload: _report(rows)
    return eng, gid, pid, orig


def _restore(orig):
    spt_mod.run_single_post_test = orig


def t_engine_no_approval():
    eng, gid, pid, orig = _run_with_fill(GOOD_ROWS)
    try:
        rid = _start(eng, gid, pid, timeout_s=2.0)
        assert wait_until(lambda: eng.get_run(rid)["status"] == "completed", 40)
    finally:
        _restore(orig)
    assert _clicks("publish_gate") == 1, MockDriver.calls
    assert _clicks("publish_click") == 0, MockDriver.calls


def t_engine_timeout_and_stop():
    # Stop during the approval wait -> zero Publish clicks.
    eng, gid, pid, orig = _run_with_fill(GOOD_ROWS)
    try:
        rid = _start(eng, gid, pid, timeout_s=120.0)
        assert wait_until(lambda: _ready(eng, rid), 30)
        eng.stop_run(rid)
        assert wait_until(
            lambda: eng.get_run(rid)["status"] in ("completed", "stopped"), 40)
    finally:
        _restore(orig)
    assert _clicks("publish_click") == 0, MockDriver.calls


def t_engine_stale_signal_then_fresh():
    eng, gid, pid, orig = _run_with_fill(GOOD_ROWS)
    try:
        rid = _start(eng, gid, pid, timeout_s=60.0)
        # Stale approval: sent BEFORE the gate starts waiting. The wait
        # discards it, so nothing may publish yet.
        assert wait_until(lambda: eng.signal_run(rid, "approve_publish"), 10)
        assert wait_until(lambda: _ready(eng, rid), 30)
        time.sleep(1.0)
        assert _clicks("publish_click") == 0, MockDriver.calls
        # Fresh approval -> exactly one Publish click.
        assert eng.signal_run(rid, "approve_publish")
        assert wait_until(lambda: eng.get_run(rid)["status"] == "completed", 40)
    finally:
        _restore(orig)
    assert _clicks("publish_click") == 1, MockDriver.calls
    assert "posted" in _listing_statuses(eng), _listing_statuses(eng)


def t_engine_duplicate_manual_review():
    eng, gid, pid, orig = _run_with_fill(GOOD_ROWS)
    MockDriver.fake_listings = [{"id": "dup1", "title": "T1"}]
    try:
        rid = _start(eng, gid, pid, timeout_s=5.0)
        assert wait_until(lambda: eng.get_run(rid)["status"] == "completed", 40)
    finally:
        _restore(orig)
    assert _clicks("publish_gate") == 0, MockDriver.calls
    assert _clicks("publish_click") == 0, MockDriver.calls
    assert "manual_review" in _listing_statuses(eng), _listing_statuses(eng)


def t_engine_failed_verification():
    rows = [dict(r) for r in GOOD_ROWS]
    for r in rows:
        if r["field"] == "location":
            r["status"] = "FAILED"
    eng, gid, pid, orig = _run_with_fill(rows)
    try:
        rid = _start(eng, gid, pid, timeout_s=5.0)
        assert wait_until(lambda: eng.get_run(rid)["status"] == "completed", 40)
    finally:
        _restore(orig)
    assert _clicks("publish_gate") == 0, MockDriver.calls
    assert _clicks("publish_click") == 0, MockDriver.calls


def t_engine_photos_gate():
    # Product WITH images but the fill report never verified photos.
    eng = _make_engine()
    gid, pid = _seed(eng, with_images=True)
    rows = [r for r in GOOD_ROWS if r["field"] != "photos"]
    orig = spt_mod.run_single_post_test
    spt_mod.run_single_post_test = lambda driver, payload: _report(rows)
    try:
        rid = _start(eng, gid, pid, timeout_s=5.0)
        assert wait_until(lambda: eng.get_run(rid)["status"] == "completed", 40)
    finally:
        _restore(orig)
    assert _clicks("publish_gate") == 0, MockDriver.calls
    assert _clicks("publish_click") == 0, MockDriver.calls


def t_engine_uncertain_no_retry_and_blocked():
    eng, gid, pid, orig = _run_with_fill(GOOD_ROWS)
    MockDriver.publish_outcome = "uncertain"
    try:
        rid = _start(eng, gid, pid, timeout_s=60.0)
        assert wait_until(lambda: _ready(eng, rid), 30)
        assert eng.signal_run(rid, "approve_publish")
        assert wait_until(lambda: eng.get_run(rid)["status"] == "completed", 40)
        assert _clicks("publish_click") == 1, MockDriver.calls
        assert "uncertain" in _listing_statuses(eng), _listing_statuses(eng)
        # A second run for the same product/account must be blocked by the
        # uncertain record BEFORE any publish gate is reached.
        rid2 = _start(eng, gid, pid, timeout_s=5.0)
        assert wait_until(lambda: eng.get_run(rid2)["status"] == "completed", 40)
    finally:
        _restore(orig)
    assert _clicks("publish_gate") == 1, MockDriver.calls  # only run 1 got there
    assert _clicks("publish_click") == 1, MockDriver.calls


def main() -> int:
    print("FMS publish_single safety tests (mock only — no real Facebook)\n")
    print("Layer 1 — driver gate (real publish_filled_form, fake page):")
    check("approval gate removed -> denying callback cannot block",
          t_driver_no_approval_gate)
    check("Next disabled -> zero clicks", t_driver_next_disabled)
    check("unexpected screen -> zero Publish clicks", t_driver_unexpected_screen)
    check("approved -> exactly one click, LIVE", t_driver_approved_one_click_live)
    check("uncertain result -> one click, no retry", t_driver_uncertain_no_retry)
    check("publish screen already open -> Next NOT clicked again",
          t_driver_reuse_publish_screen_no_next)
    check("form screen -> Next clicked exactly once, then Publish",
          t_driver_form_screen_next_clicked_once)
    print("\nLayer 2 — engine flow (MockDriver):")
    check("no approval (timeout) -> zero Publish calls", t_engine_no_approval)
    check("Stop during wait -> zero Publish calls", t_engine_timeout_and_stop)
    check("stale signal discarded; fresh -> exactly one", t_engine_stale_signal_then_fresh)
    check("duplicate title -> manual review, safe abort", t_engine_duplicate_manual_review)
    check("failed form verification -> zero Publish calls", t_engine_failed_verification)
    check("photos not verified -> zero Publish calls", t_engine_photos_gate)
    check("uncertain -> recorded, re-run blocked", t_engine_uncertain_no_retry_and_blocked)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

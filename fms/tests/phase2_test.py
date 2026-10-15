"""FMS Phase 2 tests. Run:  python fms/tests/phase2_test.py

Covers: script engine (ordering, data binding, wait-yield, grouping, shuffle),
draft&publish full (all-together + one-by-one gap), manual-publish tabs,
scrap merge priority, relist/edit, interrupted script resume, new dashboard
routes. Uses MockDriver only — NEVER touches real Facebook.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="fms_phase2_")
os.environ["FMS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fms import accounts as accounts_mod          # noqa: E402
from fms import products as products_mod          # noqa: E402
from fms import scrap as scrap_mod                # noqa: E402
from fms import scripts as scripts_mod            # noqa: E402
from fms.browser import MockDriver                # noqa: E402
from fms.db import Database                       # noqa: E402
from fms.engine import ListingEngine, RunConfig    # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, fn) -> None:
    try:
        fn()
        results.append((PASS, name, ""))
        print(f"  [{PASS}] {name}")
    except Exception as exc:
        results.append((FAIL, name, f"{type(exc).__name__}: {exc}"))
        print(f"  [{FAIL}] {name}: {type(exc).__name__}: {exc}")


def wait_until(fn, timeout: float = 60.0, poll: float = 0.2):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(poll)
    return False


DB = Database()


def make_engine():
    MockDriver.reset()
    MockDriver.activity_scale = 0.001
    return ListingEngine(Database(), driver_factory=MockDriver)


def seed_group(db: Database, n_accounts: int, prefix: str = "p2"):
    gid = accounts_mod.create_group(db, f"{prefix}-{time.time_ns()}")
    for i in range(n_accounts):
        accounts_mod.add_account(db, gid, f"{prefix}u{i}@x.com", f"pw{i}")
    return gid


def seed_product(db: Database, name: str, titles, price: float = 100.0):
    pid = products_mod.create_product(db, name, base_price=price)
    products_mod.set_titles(db, pid, list(titles))
    products_mod.set_locations(db, pid, ["Manchester, United Kingdom"])
    return pid


def post_starts():
    return [c for c in MockDriver.calls if c["event"] == "post_start"]


# ------------------------------------------------------- script basics ---
def t_script_order_and_binding():
    """(a) Post(2)->Wait(0)->Post(1): order + per-action dataset binding."""
    eng = make_engine()
    gid = seed_group(eng.db, 1)
    pa = seed_product(eng.db, "Bags", ["TA1", "TA2"])
    pb = seed_product(eng.db, "Wardrobe", ["TB1"])
    sid = scripts_mod.create_script(eng.db, "order-test", [
        scripts_mod.make_block("post", "b1", product_id=pa, count_min=2, count_max=2),
        scripts_mod.make_block("wait_next_turn", "b2", wait_minutes=0),
        scripts_mod.make_block("post", "b3", product_id=pb, count_min=1, count_max=1),
    ])
    cfg = RunConfig(group_id=gid, mode="script", script_id=sid, batch_size=1,
                    activity_scale=0.001)
    rid = eng.start_run(cfg)
    assert wait_until(lambda: eng.get_run(rid)["status"] == "completed", 60), \
        eng.get_run(rid)
    starts = post_starts()
    assert len(starts) == 3, starts
    titles = [s["title"] for s in starts]
    assert titles[0] in ("TA1", "TA2") and titles[1] in ("TA1", "TA2"), titles
    assert titles[2] == "TB1", titles  # dataset B bound to block b3
    # script progress recorded
    run = eng.get_run(rid)
    sp = run["script_progress"][0]
    assert sp["finished"] is True and sp["done_count"] == 3, sp


def t_wait_yields_queue():
    """(b) Wait For Next Turn: account yields so the next account's turn starts."""
    eng = make_engine()
    gid = seed_group(eng.db, 2, prefix="w2")
    pa = seed_product(eng.db, "WProd", ["WA"])
    sid = scripts_mod.create_script(eng.db, "yield-test", [
        scripts_mod.make_block("post", "b1", product_id=pa, count_min=1, count_max=1),
        # 1.2 s guaranteed gap: the yielding account genuinely waits
        scripts_mod.make_block("wait_next_turn", "b2", wait_minutes=0.02),
        scripts_mod.make_block("post", "b3", product_id=pa, count_min=1, count_max=1),
    ])
    cfg = RunConfig(group_id=gid, mode="script", script_id=sid, batch_size=1,
                    activity_scale=0.001)
    rid = eng.start_run(cfg)
    assert wait_until(lambda: eng.get_run(rid)["status"] == "completed", 60)
    labels = [s["label"] for s in post_starts()]
    # acc0 posts, yields to acc1 (genuinely waiting), then acc0's turn returns
    assert labels == ["w2u0@x.com", "w2u1@x.com", "w2u0@x.com", "w2u1@x.com"], labels
    # the guaranteed gap was honored between acc0's two turns
    t0 = [s["t"] for s in post_starts() if s["label"] == "w2u0@x.com"]
    assert t0[1] - t0[0] >= 1.0, t0


def t_script_activities_and_group():
    """Activities run with probabilities; K-of-N grouping diverges."""
    eng = make_engine()
    gid = seed_group(eng.db, 2, prefix="ag")
    pa = seed_product(eng.db, "AProd", ["GA"])
    sid = scripts_mod.create_script(eng.db, "act-test", [
        scripts_mod.make_block("scroll_feed", "b1", minutes=1,
                               p_keep_scrolling=0.55, p_like_post=0.15),
        scripts_mod.make_block("watch_reels", "b2", minutes=1),
        scripts_mod.make_block("group", "b3", members=["b1", "b2"],
                               min_select=1, max_select=1),
        scripts_mod.make_block("post", "b4", product_id=pa,
                               count_min=1, count_max=1),
    ], shuffle=False)
    cfg = RunConfig(group_id=gid, mode="script", script_id=sid, batch_size=2,
                    activity_scale=0.001)
    rid = eng.start_run(cfg)
    assert wait_until(lambda: eng.get_run(rid)["status"] == "completed", 60)
    acts = [c for c in MockDriver.calls if c["event"].startswith("activity:")]
    assert acts, "no activity events recorded"
    # each account posted exactly once (group members don't run standalone)
    assert len(post_starts()) == 2, post_starts()


def t_script_validation():
    eng = make_engine()
    # post without dataset -> error
    try:
        scripts_mod.create_script(eng.db, "bad", [
            scripts_mod.make_block("post", "b1")])
        raise AssertionError("should have raised")
    except ValueError:
        pass
    # duplicate ids -> error
    try:
        scripts_mod.create_script(eng.db, "bad2", [
            scripts_mod.make_block("goto_messages", "b1"),
            scripts_mod.make_block("goto_messages", "b1")])
        raise AssertionError("should have raised")
    except ValueError:
        pass
    # script mode without script_id -> error
    try:
        RunConfig(group_id=1, mode="script")
        raise AssertionError("should have raised")
    except ValueError:
        pass


# ------------------------------------------------------- draft&publish ---
def t_draft_publish_all_together():
    """(c1) N drafts created, then all published together."""
    eng = make_engine()
    gid = seed_group(eng.db, 1, prefix="dp")
    pid = seed_product(eng.db, "DPProd", ["D1", "D2", "D3"])
    cfg = RunConfig(group_id=gid, product_ids=[pid], mode="draft_publish",
                    batch_size=1, min_ads=3, max_ads=3,
                    min_delay_s=0.01, max_delay_s=0.02,
                    publish_mode="all_together")
    rid = eng.start_run(cfg)
    assert wait_until(lambda: eng.get_run(rid)["status"] == "completed", 60)
    calls = MockDriver.calls
    drafts = [c for c in calls if c["event"] == "create_draft"]
    pubs = [c for c in calls if c["event"] == "publish_draft"]
    assert len(drafts) == 3, drafts
    assert len(pubs) == 3, pubs
    # all drafts created BEFORE any publish
    assert max(c["t"] for c in drafts) < min(c["t"] for c in pubs)
    rows = eng.db.fetchall("SELECT status FROM listings WHERE run_id=?", (rid,))
    assert all(r["status"] == "posted" for r in rows), rows
    assert eng.get_run(rid)["total_posted"] == 3


def t_draft_publish_one_by_one_gap():
    """(c2) one-by-one with gap: publishes sequential with the gap between."""
    eng = make_engine()
    gid = seed_group(eng.db, 1, prefix="dg")
    pid = seed_product(eng.db, "DGProd", ["G1", "G2"])
    cfg = RunConfig(group_id=gid, product_ids=[pid], mode="draft_publish",
                    batch_size=1, min_ads=2, max_ads=2,
                    min_delay_s=0.01, max_delay_s=0.02,
                    publish_mode="one_by_one", publish_gap_min=0.02)  # 1.2 s gap
    rid = eng.start_run(cfg)
    assert wait_until(lambda: eng.get_run(rid)["status"] == "completed", 60)
    pubs = [c for c in MockDriver.calls if c["event"] == "publish_draft"]
    assert len(pubs) == 2, pubs
    gap = pubs[1]["t"] - pubs[0]["t"]
    assert gap >= 1.0, f"gap too small: {gap}"  # 0.02 min = 1.2 s


# ------------------------------------------------------- manual publish ---
def t_manual_publish():
    """(d) N tabs filled, nothing published."""
    eng = make_engine()
    gid = seed_group(eng.db, 1, prefix="mp")
    pid = seed_product(eng.db, "MPProd", ["M1", "M2"])
    cfg = RunConfig(group_id=gid, product_ids=[pid], mode="manual_publish",
                    batch_size=1, min_ads=2, max_ads=2,
                    min_delay_s=0.01, max_delay_s=0.02)
    rid = eng.start_run(cfg)
    assert wait_until(lambda: eng.get_run(rid)["status"] == "completed", 60)
    fills = [c for c in MockDriver.calls if c["event"] == "fill_form"]
    assert len(fills) == 2, fills
    assert not post_starts(), "must not publish"
    assert not [c for c in MockDriver.calls if c["event"] == "publish_draft"]
    tabs = eng.db.fetchall("SELECT * FROM manual_tabs WHERE run_id=?", (rid,))
    assert len(tabs) == 2 and all(t["status"] == "filled" for t in tabs), tabs
    run = eng.get_run(rid)
    assert len(run["tabs"]) == 2
    # user marks one published
    assert eng.set_tab_status(rid, tabs[0]["id"], "published") is True
    assert eng.set_tab_status(rid, 999999, "published") is False


# ------------------------------------------------------- scrap ---
def t_scrap_priority():
    """(e) customized > global override > original."""
    eng = make_engine()
    MockDriver.fake_scrapes = {
        "http://fb/item/1": {
            "title": "Raw Title 1", "price": "100", "description": "Raw desc 1",
            "photo_urls": [], "location": "Horsham", "availability": "In Stock",
            "url": "http://fb/item/1",
        },
        "http://fb/item/2": {
            "title": "Raw Title 2", "price": "200", "description": "",
            "photo_urls": [], "location": "", "availability": "In Stock",
            "url": "http://fb/item/2",
        },
    }
    driver = MockDriver({"id": 1, "label": "s", "password": "p"})
    items = scrap_mod.scrape_listings(
        driver,
        ["http://fb/item/1", "http://fb/item/2"],
        category="Furniture",
        overrides={"titles": ["OT1", "OT2"], "price": "999"},
        customize={"http://fb/item/1": {"title": "CT1"}},
        fallback_description="FB",
        country_append="United Kingdom",
        backup_location="London, United Kingdom",
        download_photos=False,
    )
    assert len(items) == 2, items
    i1, i2 = items
    assert i1["title"] == "CT1", i1                       # customized wins
    assert i1["price"] == "999", i1                       # override wins
    assert i1["description"] == "Raw desc 1", i1          # original kept
    # url2: title from override rotation (url1 didn't consume it)
    assert i2["title"] == "OT1", i2
    assert i2["description"] == "FB", i2                  # fallback used
    assert i2["location"] == "London, United Kingdom", i2  # backup location
    # country-append on original location (url1 had no location override)
    assert i1["location"] == "Horsham United Kingdom", i1

    pid = scrap_mod.save_as_product(eng.db, "Scraped Test", items)
    prod = products_mod.get_product(eng.db, pid)
    assert prod["titles"] == ["CT1", "OT1"], prod["titles"]
    assert prod["scraped_items"] and len(prod["scraped_items"]) == 2


# ------------------------------------------------------- relist/edit ---
def t_relist_edit():
    """(f) existing listings rewritten, recorded as relisted."""
    eng = make_engine()
    MockDriver.fake_listings = [
        {"id": "L1", "title": "Old 1"},
        {"id": "L2", "title": "Old 2"},
        {"id": "L3", "title": "Old 3"},
    ]
    gid = seed_group(eng.db, 1, prefix="re")
    pid = seed_product(eng.db, "REProd", ["R1", "R2"])
    cfg = RunConfig(group_id=gid, product_ids=[pid], mode="relist_edit",
                    batch_size=1, min_ads=2, max_ads=2,
                    min_delay_s=0.01, max_delay_s=0.02)
    rid = eng.start_run(cfg)
    assert wait_until(lambda: eng.get_run(rid)["status"] == "completed", 60)
    edits = [c for c in MockDriver.calls if c["event"] == "edit_listing"]
    assert len(edits) == 2, edits
    assert not post_starts(), "must not create new listings"
    rows = eng.db.fetchall(
        "SELECT status, fb_listing_id FROM listings WHERE run_id=?", (rid,))
    assert len(rows) == 2 and all(r["status"] == "relisted" for r in rows), rows
    assert {r["fb_listing_id"] for r in rows} == {"L1", "L2"}


# ------------------------------------------------------- script resume ---
def t_script_resume_skips_done():
    """(g) interrupted script run resumes without repeating completed blocks."""
    eng = make_engine()
    gid = seed_group(eng.db, 1, prefix="sr")
    pa = seed_product(eng.db, "SRProd", ["S1"])
    sid = scripts_mod.create_script(eng.db, "resume-test", [
        scripts_mod.make_block("post", "b1", product_id=pa, count_min=2, count_max=2),
        # 6-second interruptible rest: stop lands here deterministically
        scripts_mod.make_block("wait_next_turn", "b2", wait_minutes=0.1),
        scripts_mod.make_block("post", "b3", product_id=pa, count_min=2, count_max=2),
    ])
    cfg = RunConfig(group_id=gid, mode="script", script_id=sid, batch_size=1,
                    activity_scale=0.001)
    rid = eng.start_run(cfg, fresh=True)
    # stop while the middle wait block is resting (before b3 starts)
    assert wait_until(lambda: len(post_starts()) >= 2, 60)
    time.sleep(0.5)  # let the worker enter b2's rest
    assert eng.stop_run(rid) is True
    assert wait_until(lambda: eng.get_run(rid)["status"] == "stopped", 60)
    n_before = len(post_starts())
    assert n_before == 2, n_before  # only block b1 ran
    rid2 = eng.start_run(cfg, fresh=False)
    assert rid2 == rid, (rid2, rid)
    assert wait_until(lambda: eng.get_run(rid)["status"] == "completed", 60)
    # b1 skipped on resume (2 posts), b3 ran (2 posts) -> 4 total
    assert len(post_starts()) == 4, len(post_starts())


# ------------------------------------------------------- dashboard ---
def t_dashboard_phase2():
    from fastapi.testclient import TestClient

    from fms.app import create_app

    app = create_app(driver_name="mock")
    c = TestClient(app)
    assert c.get("/scripts").status_code == 200
    # create script via API
    r = c.post("/api/scripts", json={"name": "WebScript", "blocks": [
        {"id": "b1", "type": "goto_messages", "params": {}}]})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    assert c.get(f"/api/scripts/{sid}").status_code == 200
    assert c.get(f"/scripts/{sid}").status_code == 200
    assert c.get("/api/block-types").status_code == 200
    # update blocks
    r = c.put(f"/api/scripts/{sid}", json={
        "blocks": [{"id": "b1", "type": "scroll_feed",
                    "params": {"minutes": 1, "p_keep_scrolling": 0.5,
                               "p_like_post": 0.1}}]})
    assert r.status_code == 200, r.text
    # invalid blocks rejected
    r = c.put(f"/api/scripts/{sid}", json={
        "blocks": [{"id": "b1", "type": "post", "params": {}}]})
    assert r.status_code == 400, r.status_code
    assert c.delete(f"/api/scripts/{sid}").status_code == 200

    # script run via HTTP
    gid = c.get("/api/groups").json()[-1]["id"] \
        if c.get("/api/groups").json() else None
    if gid is None:
        r = c.post("/api/groups", data={"name": "p2web"})
        gid = c.get("/api/groups").json()[-1]["id"]
    c.post("/api/accounts", json={"group_id": gid, "label": "w2@x.com",
                                  "password": "pw"})
    pid = c.post("/api/products", json={"name": "P2Web", "base_price": 10,
                                        "titles": ["T"]}).json()["id"]
    sid = c.post("/api/scripts", json={"name": "RunMe", "blocks": [
        {"id": "b1", "type": "post",
         "params": {"product_id": pid, "count_min": 1, "count_max": 1}}]}).json()["id"]
    r = c.post("/api/runs", json={"group_id": gid, "mode": "script",
                                  "script_id": sid, "batch_size": 1})
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    assert wait_until(lambda: c.get(f"/api/runs/{rid}").json()["status"]
                      == "completed", 60), c.get(f"/api/runs/{rid}").json()
    assert c.get(f"/runs/{rid}").status_code == 200

    # scrap API (mock)
    MockDriver.fake_scrapes = {
        "http://fb/item/9": {"title": "Web Scrap", "price": "50",
                             "description": "d", "photo_urls": [],
                             "location": "Leeds", "availability": "In Stock",
                             "url": "http://fb/item/9"},
    }
    r = c.post("/api/scrap", json={"group_id": gid,
                                   "urls": ["http://fb/item/9"],
                                   "category": "Furniture"})
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 1 and items[0]["title"] == "Web Scrap", items
    r = c.post("/api/scrap/save", json={"name": "WebScraped", "items": items})
    assert r.status_code == 200 and r.json()["product_id"] > 0, r.text


def main() -> int:
    print("FMS Phase 2 tests  (MockDriver only — no real Facebook)\n")
    check("script: order + per-action data binding", t_script_order_and_binding)
    check("script: wait-for-next-turn yields queue", t_wait_yields_queue)
    check("script: activities + K-of-N grouping", t_script_activities_and_group)
    check("script: validation errors", t_script_validation)
    check("draft&publish: all-together", t_draft_publish_all_together)
    check("draft&publish: one-by-one with gap", t_draft_publish_one_by_one_gap)
    check("manual-publish: fills tabs, never publishes", t_manual_publish)
    check("scrap: customized > override > original", t_scrap_priority)
    check("relist/edit: rewrites existing listings", t_relist_edit)
    check("script: interrupted run resumes, skips done", t_script_resume_skips_done)
    check("dashboard: scripts pages + APIs + script run + scrap",
          t_dashboard_phase2)
    print()
    failed = [r for r in results if r[0] == FAIL]
    print(f"{len(results) - len(failed)}/{len(results)} passed.")
    for _, name, err in failed:
        print(f"  FAILED: {name}: {err}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

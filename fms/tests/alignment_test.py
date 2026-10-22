"""FMS alignment tests (v6.2.0.0). Run:  python fms/tests/alignment_test.py

Covers: (a) the new actions execute via the engine and are recorded;
(b) /api/actions returns all 20 actions in the exact v6.2.0.0 order
    ("Wait For Next Turn" is script-builder-only now);
(c) settings round-trip — account start gap, retry count, parallel browser
cap, hide-from-friends flag, exclude list;
(d) the dashboard run form pages return 200 and accept the new settings;
(e) pass #5: Browser Launch Fast, Delete & Relist, Listing Number
    Verification, per-activity settings params (round-trip + engine
    honoring), drag-&-drop script builder page.
Uses MockDriver only — NEVER touches real Facebook.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="fms_align_")
os.environ["FMS_DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fms import accounts as accounts_mod          # noqa: E402
from fms import products as products_mod          # noqa: E402
from fms import scripts as scripts_mod            # noqa: E402
from fms.browser import MockDriver                # noqa: E402
from fms.db import Database, utcnow               # noqa: E402
from fms.engine import ACTIONS, MODES, ListingEngine, RunConfig  # noqa: E402
from fms.scripts import BLOCK_TYPES  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, fn) -> None:
    # Fresh MockDriver state for every test: class-level call tracking and
    # fake fixtures are global, so reset unconditionally for isolation.
    MockDriver.reset()
    MockDriver.activity_scale = 0.001
    MockDriver.post_delay = 0.05
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


def make_engine():
    MockDriver.reset()
    MockDriver.activity_scale = 0.001
    MockDriver.post_delay = 0.05
    return ListingEngine(Database(), driver_factory=MockDriver)


def seed_group(db: Database, n_accounts: int, prefix: str):
    gid = accounts_mod.create_group(db, f"{prefix}-{time.time_ns()}")
    for i in range(n_accounts):
        accounts_mod.add_account(db, gid, f"{prefix}u{i}@x.com", f"pw{i}")
    return gid


def seed_product(db: Database, name: str, price: float = 100.0):
    pid = products_mod.create_product(db, name, base_price=price)
    products_mod.set_titles(db, pid, [f"{name} title"])
    products_mod.set_locations(db, pid, ["Manchester, United Kingdom"])
    return pid


def run_to_end(eng: ListingEngine, rid: int) -> dict:
    assert wait_until(
        lambda: eng.get_run(rid)["status"] == "completed", 60), \
        eng.get_run(rid)
    return eng.get_run(rid)


def start_cfg(db, gid, mode, **kw) -> RunConfig:
    kw.setdefault("account_start_gap_s", 0)   # tests opt into gaps explicitly
    return RunConfig(group_id=gid, mode=mode, **kw)


def events(name: str, label: str | None = None):
    return [c for c in MockDriver.calls
            if c["event"] == name and (label is None or c["label"] == label)]


# ------------------------------------------------------------------- (a) ---
# v6.2.0.0 order: 18 actions. "Wait For Next Turn" left the main
# dropdown (script-builder-only now); "Listing Number Verification" was
# dropped per the user (2026-10-15: do not add it); "AI Behaviour Training"
# dropped per the user (2026-10-15: not needed for now).
EXPECTED_ORDER = ["Post", "Relist & Edit", "Draft & Publish", "Create Drafts",
                  "Publish Drafts", "Browser Launch Fast", "Browser Launch",
                  "Delete Duplicate", "Delete Previous",
                  "Delete Draft", "Delete Take Action Listings",
                  "Delete All Invalid Listings", "Renew Listing",
                  "Delete & Relist", "Login", "Download Images & Titles",
                  "Change Marketplace Location", "Scrap Data"]


def t_actions_order():
    assert [label for _, label in ACTIONS] == EXPECTED_ORDER
    # There is exactly one visible Post action, routed through the established
    # Test Single Post publishing flow. The old complete flow remains internal.
    post_actions = [(key, label) for key, label in ACTIONS if label == "Post"]
    assert post_actions == [("test_single_post", "Post")], post_actions
    assert "test_single_post" in MODES
    assert "complete" in MODES  # backward compatibility for saved/API runs


def t_delete_duplicate():
    db = Database()
    gid = seed_group(db, 1, "aldd")
    pid = seed_product(db, "DupSofa")
    eng = ListingEngine(db, driver_factory=MockDriver)
    MockDriver.fake_listings = [
        {"id": "d1", "title": "Red Sofa"},
        {"id": "d2", "title": "red  sofa"},   # duplicate of d1
        {"id": "d3", "title": "Blue Bed"},
    ]
    rid = eng.start_run(start_cfg(db, gid, "delete_duplicate"))
    run_to_end(eng, rid)
    assert len(events("delete_duplicates")) == 1
    assert events("delete_duplicates")[0]["count"] == 1, MockDriver.calls
    deleted = db.fetchall("SELECT * FROM listings WHERE run_id = ? "
                          "AND status = 'deleted'", (rid,))
    assert len(deleted) == 1 and deleted[0]["fb_listing_id"] == "d2", deleted
    assert pid  # product not required for this mode, seed just for symmetry


def t_delete_previous():
    db, eng = Database(), make_engine()
    gid = seed_group(db, 1, "aldp")
    pid = seed_product(db, "PrevSofa")
    eng = ListingEngine(db, driver_factory=MockDriver)
    # First run posts one listing.
    rid1 = eng.start_run(start_cfg(db, gid, "complete", product_ids=[pid],
                                   min_ads=1, max_ads=1))
    run_to_end(eng, rid1)
    posted = db.fetchall("SELECT * FROM listings WHERE run_id = ? "
                         "AND status = 'posted'", (rid1,))
    assert len(posted) == 1
    fb_id = posted[0]["fb_listing_id"]
    # Second run deletes listings from previous runs.
    rid2 = eng.start_run(start_cfg(db, gid, "delete_previous"))
    run_to_end(eng, rid2)
    assert events("delete_listing"), MockDriver.calls
    assert any(c["listing_id"] == fb_id for c in events("delete_listing"))
    deleted = db.fetchall("SELECT * FROM listings WHERE run_id = ? "
                          "AND status = 'deleted'", (rid2,))
    assert len(deleted) == 1 and deleted[0]["fb_listing_id"] == fb_id


def t_delete_draft():
    db = Database()
    gid = seed_group(db, 1, "aldr")
    pid = seed_product(db, "DraftSofa")
    eng = ListingEngine(db, driver_factory=MockDriver)
    rid1 = eng.start_run(start_cfg(db, gid, "create_drafts",
                                   product_ids=[pid], min_ads=1, max_ads=1))
    run_to_end(eng, rid1)
    drafts = db.fetchall("SELECT * FROM listings WHERE run_id = ? "
                         "AND status = 'draft'", (rid1,))
    assert len(drafts) == 1
    rid2 = eng.start_run(start_cfg(db, gid, "delete_draft"))
    run_to_end(eng, rid2)
    assert len(events("delete_drafts")) == 1
    gone = db.fetchall("SELECT * FROM listings WHERE account_id = ? "
                       "AND status = 'draft'", (drafts[0]["account_id"],))
    assert not gone, gone
    assert db.fetchone("SELECT COUNT(*) AS c FROM listings WHERE run_id = ? "
                       "AND status = 'deleted'", (rid1,))["c"] == 1


def t_delete_take_action():
    db = Database()
    gid = seed_group(db, 1, "alta")
    eng = ListingEngine(db, driver_factory=MockDriver)
    MockDriver.fake_take_action = [
        {"id": "ta1", "title": "Flagged Sofa"},
        {"id": "ta2", "title": "Flagged Bed"},
    ]
    rid = eng.start_run(start_cfg(db, gid, "delete_take_action"))
    run_to_end(eng, rid)
    assert len(events("get_take_action")) == 1
    deleted_ids = {c["listing_id"] for c in events("delete_listing")}
    assert deleted_ids == {"ta1", "ta2"}, deleted_ids
    n = db.fetchone("SELECT COUNT(*) AS c FROM listings WHERE run_id = ? "
                    "AND status = 'deleted'", (rid,))["c"]
    assert n == 2, n


def t_delete_all_invalid():
    db = Database()
    gid = seed_group(db, 1, "aliv")
    pid = seed_product(db, "InvalidSofa")
    eng = ListingEngine(db, driver_factory=MockDriver)
    rid_prev = eng.start_run(start_cfg(db, gid, "complete",
                                       product_ids=[pid],
                                       min_ads=1, max_ads=1))
    run_to_end(eng, rid_prev)
    aid = accounts_mod.list_accounts(db, gid)[0]["id"]
    db.execute(
        """INSERT INTO listings
           (run_id, account_id, product_id, title_used, price_used,
            location_used, images_used, status, fb_listing_id, posted_at)
           VALUES (?, ?, ?, 'Bad Sofa', 10, 'X', '[]', 'failed', 'fb-bad-1', ?)""",
        (rid_prev, aid, pid, utcnow()))
    rid = eng.start_run(start_cfg(db, gid, "delete_all_invalid"))
    run_to_end(eng, rid)
    assert any(c["listing_id"] == "fb-bad-1"
               for c in events("delete_listing")), MockDriver.calls
    row = db.fetchone("SELECT status FROM listings WHERE fb_listing_id = ?",
                      ("fb-bad-1",))
    assert row["status"] == "deleted", row


def t_renew_listing():
    db = Database()
    gid = seed_group(db, 1, "alrn")
    eng = ListingEngine(db, driver_factory=MockDriver)
    MockDriver.fake_listings = [
        {"id": "r1", "title": "Old Sofa"},
        {"id": "r2", "title": "Old Bed"},
    ]
    rid = eng.start_run(start_cfg(db, gid, "renew_listing",
                                  renew_min_count=2, renew_max_count=2))
    run_to_end(eng, rid)
    renewed = {c["listing_id"] for c in events("renew_listing")}
    assert renewed == {"r1", "r2"}, renewed
    n = db.fetchone("SELECT COUNT(*) AS c FROM listings WHERE run_id = ? "
                    "AND status = 'renewed'", (rid,))["c"]
    assert n == 2, n


def t_renew_listing_min_max_limits():
    # Min/Max Actions bound how many listings get renewed (confirmed
    # 2026-10-15: user sets lower and upper limit).
    db = Database()
    gid = seed_group(db, 1, "alrnmm")
    eng = ListingEngine(db, driver_factory=MockDriver)
    MockDriver.fake_listings = [{"id": f"r{i}", "title": f"L{i}"}
                                for i in range(10)]
    rid = eng.start_run(start_cfg(db, gid, "renew_listing",
                                  renew_min_count=3, renew_max_count=3))
    run_to_end(eng, rid)
    renewed = events("renew_listing")
    assert len(renewed) == 3, len(renewed)


def t_login():
    db = Database()
    gid = seed_group(db, 1, "allg")
    eng = ListingEngine(db, driver_factory=MockDriver)
    rid = eng.start_run(start_cfg(db, gid, "login"))
    run_to_end(eng, rid)
    assert len(events("login")) == 1
    assert not events("post_start"), MockDriver.calls
    logs = eng.recent_logs(rid, 50)
    assert any("login verified" in l["message"] for l in logs), logs


def t_download_images_titles():
    # Manual-assist flow (confirmed 2026-10-15): browser opens on
    # Marketplace, the user opens products in new tabs, then the dashboard
    # "Start Download" signal fires and FMS downloads from all open tabs.
    db = Database()
    gid = seed_group(db, 1, "aldl")
    pid = seed_product(db, "DlSofa")
    eng = ListingEngine(db, driver_factory=MockDriver)
    MockDriver.fake_tabs = [
        {"title": "Tabbed Leather Sofa", "photo_urls": ["http://img/1.jpg"]},
        {"title": "Tabbed Oak Bed",
         "photo_urls": ["http://img/2.jpg", "http://img/3.jpg"]},
    ]
    rid = eng.start_run(start_cfg(db, gid, "download_images_titles",
                                  product_ids=[pid],
                                  download_folder="/tmp/fms-dl-test"))
    # signal the download shortly after the run starts waiting
    import threading as _th
    _th.Timer(2.0, lambda: eng.signal_run(rid, "download_start")).start()
    run_to_end(eng, rid)
    dl = events("download_open_tabs")
    assert len(dl) == 1 and dl[0]["count"] == 2, MockDriver.calls
    assert events("open_marketplace"), MockDriver.calls
    product = products_mod.get_product(db, pid)
    assert "Tabbed Leather Sofa" in product["titles"], product["titles"]
    assert "Tabbed Oak Bed" in product["titles"], product["titles"]


def t_download_images_titles_cancelled_without_signal():
    # No signal + run stopped -> download is skipped, browser left open.
    db = Database()
    gid = seed_group(db, 1, "aldlc")
    eng = ListingEngine(db, driver_factory=MockDriver)
    rid = eng.start_run(start_cfg(db, gid, "download_images_titles"))
    import threading as _th
    _th.Timer(2.0, lambda: eng.stop_run(rid)).start()
    assert wait_until(
        lambda: eng.get_run(rid)["status"] in ("completed", "stopped"), 60), \
        eng.get_run(rid)
    assert not events("download_open_tabs"), MockDriver.calls


def t_browser_launch():
    db = Database()
    gid = seed_group(db, 1, "albl")
    eng = ListingEngine(db, driver_factory=MockDriver)
    rid = eng.start_run(start_cfg(db, gid, "browser_launch"))
    run_to_end(eng, rid)
    # Browser stays open: the engine must NOT call driver.close() at all.
    assert len(events("login")) == 1
    assert not events("close"), MockDriver.calls
    logs = eng.recent_logs(rid, 50)
    assert any("left open" in l["message"] for l in logs), logs


def t_change_location():
    db = Database()
    gid = seed_group(db, 1, "alcl")
    eng = ListingEngine(db, driver_factory=MockDriver)
    rid = eng.start_run(start_cfg(db, gid, "change_location",
                                  new_location="Manchester, UK"))
    run_to_end(eng, rid)
    assert len(events("set_location")) == 1
    assert events("set_location")[0]["location"] == "Manchester, UK"


def t_ai_behaviour_training_dropped():
    # Per the user (2026-10-15): not needed for now — not in the action
    # list, modes, or script blocks.
    assert "ai_behaviour_training" not in MODES, MODES
    assert all(key != "ai_behaviour_training" for key, _ in ACTIONS)
    assert "ai_behaviour_training" not in BLOCK_TYPES
    assert "ai_behaviour_training" not in scripts_mod.BLOCK_LABELS


# ------------------------------------------------------------------- (c) ---
def t_start_gap_respected():
    """2 accounts, batch=2, gap=1.0s -> second login lands ~1s after first."""
    db = Database()
    MockDriver.reset()
    MockDriver.activity_scale = 0.001
    MockDriver.post_delay = 0.05
    gid = seed_group(db, 2, "algap")
    eng = ListingEngine(db, driver_factory=MockDriver)
    rid = eng.start_run(start_cfg(db, gid, "login", batch_size=2,
                                  account_start_gap_s=1.0))
    run_to_end(eng, rid)
    logins = sorted(events("login"), key=lambda c: c["t"])
    assert len(logins) == 2, logins
    gap = logins[1]["t"] - logins[0]["t"]
    assert gap >= 0.9, f"start gap not respected: {gap:.2f}s"


def t_retry_then_lock():
    """retry_count=2 -> 3 login attempts, then the account is marked locked."""
    db = Database()
    MockDriver.reset()
    MockDriver.activity_scale = 0.001
    MockDriver.post_delay = 0.05
    gid = seed_group(db, 1, "alrt")
    label = accounts_mod.list_accounts(db, gid)[0]["label"]
    eng = ListingEngine(db, driver_factory=MockDriver)
    MockDriver.fail_labels = {label}
    rid = eng.start_run(start_cfg(db, gid, "login", retry_count=2))
    run_to_end(eng, rid)
    assert len(events("login", label)) == 3, MockDriver.calls
    acct = accounts_mod.list_accounts(db, gid)[0]
    assert acct["status"] == "locked", acct
    logs = eng.recent_logs(rid, 50)
    assert any("marked locked" in l["message"] for l in logs), logs


def t_parallel_cap():
    """batch=4 with cap=2 -> never more than 2 concurrent postings."""
    db = Database()
    MockDriver.reset()
    MockDriver.activity_scale = 0.001
    MockDriver.post_delay = 0.05
    gid = seed_group(db, 4, "alcap")
    pid = seed_product(db, "CapSofa")
    eng = ListingEngine(db, driver_factory=MockDriver)
    MockDriver.post_delay = 0.15
    rid = eng.start_run(start_cfg(db, gid, "complete", product_ids=[pid],
                                  batch_size=4, max_parallel_browsers=2,
                                  min_ads=1, max_ads=1,
                                  min_delay_s=0, max_delay_s=0,
                                  account_start_gap_s=0))
    run_to_end(eng, rid)
    assert MockDriver.max_concurrent <= 2, MockDriver.max_concurrent
    assert MockDriver.max_concurrent >= 1
    assert len(events("post_start")) == 4


def t_hide_from_friends_reaches_driver():
    db = Database()
    gid = seed_group(db, 1, "alhf")
    pid = seed_product(db, "HfSofa")
    eng = ListingEngine(db, driver_factory=MockDriver)
    rid = eng.start_run(start_cfg(db, gid, "complete", product_ids=[pid],
                                  min_ads=1, max_ads=1,
                                  hide_from_friends=True))
    run_to_end(eng, rid)
    starts = events("post_start")
    assert len(starts) == 1
    assert starts[0]["hide_from_friends"] is True, starts[0]


def t_exclude_labels():
    db = Database()
    gid = seed_group(db, 2, "alex")
    excluded = accounts_mod.list_accounts(db, gid)[0]["label"]
    eng = ListingEngine(db, driver_factory=MockDriver)
    rid = eng.start_run(start_cfg(db, gid, "login",
                                  exclude_labels=[excluded.upper()]))
    run_to_end(eng, rid)
    assert len(events("login")) == 1
    assert events("login")[0]["label"] != excluded


# ------------------------------------------------------------------- (d) ---
def _client():
    from fastapi.testclient import TestClient

    from fms.app import create_app

    MockDriver.reset()
    MockDriver.activity_scale = 0.001
    MockDriver.post_delay = 0.02
    return TestClient(create_app(driver_name="mock"))


def t_api_actions_order():
    c = _client()
    r = c.get("/api/actions")
    assert r.status_code == 200, r.text
    got = [(a["mode"], a["label"]) for a in r.json()]
    assert got == list(ACTIONS), got
    assert [a["label"] for a in r.json()] == EXPECTED_ORDER


def t_dashboard_pages():
    c = _client()
    r = c.get("/runs")
    assert r.status_code == 200, r.status_code
    html = r.text
    for label in EXPECTED_ORDER:
        # Jinja auto-escapes & as &amp; in the rendered dropdown.
        assert (label in html) or (label.replace("&", "&amp;") in html), \
            f"missing action label: {label}"
    assert "Behavior script" in html
    assert "Account start gap" in html
    assert "No of browsers parallely" in html
    assert "Retry count before quitting" in html
    assert "Tabs without limit" in html
    assert "Tabs draft and publish" in html
    assert "Hide from friends" in html
    assert "Schedule At:" in html
    assert "Exclude accounts" in html
    r = c.get("/accounts")
    assert r.status_code == 200, r.status_code
    assert "Add Bulk Accounts" in r.text


def t_api_run_accepts_new_settings():
    c = _client()
    groups = c.get("/api/groups").json()
    gid = groups[0]["id"] if groups else _mkgroup(c, "alweb")
    c.post("/api/accounts", json={"group_id": gid, "label": "alw@x.com",
                                  "password": "pw"})
    r = c.post("/api/runs", json={
        "group_id": gid, "mode": "login", "batch_size": 2,
        "hide_from_friends": True, "account_start_gap_s": 1.5,
        "max_parallel_browsers": 4, "retry_count": 1,
        "tabs_without_limit": 3, "tabs_draft_publish": 7,
        "action_urls": ["http://fb/item/1"], "new_location": "Leeds",
        "wait_minutes": 0,
        "scrap_include_description": False, "scrap_include_location": True,
        "download_folder": "/tmp/fms-dl",
        "exclude_labels": ["nobody@x.com"], "speed": 1.5,
        "schedule_at": None, "fresh": True,
    })
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    assert wait_until(lambda: c.get(f"/api/runs/{rid}").json()["status"]
                      == "completed", 60), c.get(f"/api/runs/{rid}").json()
    run = c.get(f"/api/runs/{rid}").json()
    settings = run.get("settings") or {}
    for key, val in [("hide_from_friends", True),
                     ("account_start_gap_s", 1.5),
                     ("max_parallel_browsers", 4), ("retry_count", 1),
                     ("tabs_without_limit", 3), ("tabs_draft_publish", 7),
                     ("new_location", "Leeds"),
                     ("scrap_include_description", False),
                     ("scrap_include_location", True),
                     ("download_folder", "/tmp/fms-dl"),
                     ("speed", 1.5)]:
        assert settings.get(key) == val, (key, settings.get(key))


# -------------------------------------------- (e) typing modes + IXBrowser ---
def t_typing_mode_recorded_per_mode():
    """Each typing_mode reaches the driver (recorded on every post)."""
    from fms import human as human_mod

    assert human_mod.TYPING_MODES == ("human", "fast_js", "clipboard")
    for mode in ("human", "fast_js", "clipboard"):
        MockDriver.reset()
        MockDriver.activity_scale = 0.001
        MockDriver.post_delay = 0.05
        db = Database()
        gid = seed_group(db, 1, f"altyp{mode}")
        pid = seed_product(db, f"TypSofa{mode}")
        eng = ListingEngine(db, driver_factory=MockDriver)
        rid = eng.start_run(start_cfg(db, gid, "complete", product_ids=[pid],
                                      batch_size=1, min_ads=1, max_ads=1,
                                      min_delay_s=0, max_delay_s=0,
                                      typing_mode=mode))
        run_to_end(eng, rid)
        posts = events("post_start")
        assert posts, f"no posts for mode {mode}"
        assert all(p["typing_mode"] == mode for p in posts), posts


def t_typing_config_validation():
    try:
        RunConfig(group_id=1, mode="login", typing_mode="telepathy")
    except ValueError:
        pass
    else:
        raise AssertionError("bad typing_mode accepted")
    try:
        RunConfig(group_id=1, mode="login", typing_preset="turbo")
    except ValueError:
        pass
    else:
        raise AssertionError("bad typing_preset accepted")
    try:
        RunConfig(group_id=1, mode="login", min_delay_ms=100, max_delay_ms=50)
    except ValueError:
        pass
    else:
        raise AssertionError("min>max delay accepted")
    try:
        RunConfig(group_id=1, mode="login", ix_local_api_port=99999)
    except ValueError:
        pass
    else:
        raise AssertionError("bad ix port accepted")


def t_human_delay_ranges_respected():
    """Mock clock: per-char/thinking/punct/newline delays stay in range."""
    from fms import human as human_mod

    recorded: list[tuple[float, float]] = []
    orig = human_mod.rand_delay
    human_mod.rand_delay = lambda a, b: recorded.append((a, b))
    try:
        cfg = human_mod.TypingConfig(
            mode="human", preset="normal",
            min_delay_ms=30, max_delay_ms=80,
            thinking_pause_min_ms=200, thinking_pause_max_ms=600,
            extra_delay_after_punctuation_ms=80,
            newline_pause_min_ms=300, newline_pause_max_ms=900,
        )

        class FakeLoc:
            def click(self): pass
            def press(self, ch): pass

        text = ("Hello world. This is a longer test sentence with many words in "
                "it!\nSecond line here, ok? Yes indeed.")
        human_mod.human_type_cfg(FakeLoc(), text, cfg)
    finally:
        human_mod.rand_delay = orig
    per_char = [b for a, b in recorded if a == 30.0]
    assert per_char, "no per-char delays recorded"
    assert all(a == 30.0 and b == 80.0 for a, b in recorded
               if a not in (80.0, 200.0, 300.0)), recorded
    assert any(a == 200.0 and b == 600.0 for a, b in recorded), \
        f"no thinking pause in range: {recorded}"
    assert any(a == 80.0 and b == 140.0 for a, b in recorded), \
        f"no punctuation pause: {recorded}"
    assert any(a == 300.0 and b == 900.0 for a, b in recorded), \
        f"no newline pause: {recorded}"


def t_human_preset_scales_delays():
    """Careful preset doubles the delay bounds vs normal."""
    from fms import human as human_mod

    recorded: list[tuple[float, float]] = []
    orig = human_mod.rand_delay
    human_mod.rand_delay = lambda a, b: recorded.append((a, b))
    try:
        cfg = human_mod.TypingConfig(preset="careful",
                                     min_delay_ms=30, max_delay_ms=80)

        class FakeLoc:
            def click(self): pass
            def press(self, ch): pass

        human_mod.human_type_cfg(FakeLoc(), "ab", cfg)
    finally:
        human_mod.rand_delay = orig
    assert recorded, "nothing recorded"
    assert all(a == 60.0 and b == 160.0 for a, b in recorded), recorded


def t_type_text_dispatch():
    """fast_js -> JS evaluate; clipboard -> paste path; human -> chars."""
    from fms import human as human_mod

    class FakeLoc:
        def __init__(self):
            self.calls = []

        def click(self): self.calls.append("click")
        def press(self, ch): self.calls.append(("press", ch))
        def evaluate(self, js, text):
            self.calls.append(("evaluate", text)); return True
        def fill(self, text): self.calls.append(("fill", text))

    cfg_js = human_mod.TypingConfig(mode="fast_js")
    loc = FakeLoc()
    assert human_mod.type_text(loc, "hello", cfg_js) == "fast_js"
    assert ("evaluate", "hello") in loc.calls, loc.calls

    cfg_cb = human_mod.TypingConfig(mode="clipboard")
    loc2 = FakeLoc()
    assert human_mod.type_text(loc2, "world", cfg_cb) == "clipboard"
    # clipboard path: Ctrl+V via page.keyboard, or graceful fill() fallback
    # when no OS clipboard is available in the test environment.
    assert ("fill", "world") in loc2.calls, loc2.calls

    cfg_h = human_mod.TypingConfig(mode="human")
    loc3 = FakeLoc()
    assert human_mod.type_text(loc3, "ab", cfg_h) == "human"
    assert ("press", "a") in loc3.calls and ("press", "b") in loc3.calls


def t_type_text_hybrid_long_text():
    """Human mode + text > threshold: type head, paste the rest."""
    from fms import human as human_mod

    class FakeLoc:
        def __init__(self):
            self.calls = []

        def click(self): self.calls.append("click")
        def press(self, ch): self.calls.append(("press", ch))
        def input_value(self): return "x" * human_mod.HYBRID_TYPE_CHARS
        def fill(self, text): self.calls.append(("fill", len(text)))

    cfg = human_mod.TypingConfig(mode="human", hybrid_threshold_chars=500)
    loc = FakeLoc()
    assert human_mod.type_text(loc, "y" * 600, cfg, hybrid_ok=True) == "human"
    presses = [c for c in loc.calls if c[0] == "press"]
    assert len(presses) == human_mod.HYBRID_TYPE_CHARS, len(presses)
    fills = [c for c in loc.calls if c[0] == "fill"]
    assert fills and fills[0][1] == 600, fills


def t_estimate_speed():
    from fms import human as human_mod

    cps, secs = human_mod.estimate_speed(
        human_mod.TypingConfig(min_delay_ms=30, max_delay_ms=80))
    assert 15.0 < cps < 21.0, cps      # dialog example: ~17.1 chars/sec
    assert 25.0 < secs < 45.0, secs    # dialog example: ~35s for 500 chars
    cps_fast, _ = human_mod.estimate_speed(
        human_mod.TypingConfig(preset="fast", min_delay_ms=30, max_delay_ms=80))
    assert cps_fast > cps, (cps_fast, cps)


def t_ixbrowser_enabled_mock():
    """IXBrowser on + profile id -> driver launch records ixbrowser params."""
    db = Database()
    gid = accounts_mod.create_group(db, f"alix-{time.time_ns()}")
    accounts_mod.add_account(db, gid, "ixu@x.com", "pw", ix_profile_id="IX-123")
    eng = ListingEngine(db, driver_factory=MockDriver)
    rid = eng.start_run(start_cfg(db, gid, "login", batch_size=1,
                                  ix_browser_enabled=True,
                                  ix_local_api_host="127.0.0.1",
                                  ix_local_api_port=53200))
    run_to_end(eng, rid)
    launches = events("launch")
    assert len(launches) == 1, launches
    lx = launches[0]
    assert lx["backend"] == "ixbrowser", lx
    assert lx["ix_profile_id"] == "IX-123", lx
    assert lx["ix_host"] == "127.0.0.1" and lx["ix_port"] == 53200, lx


def t_ixbrowser_disabled_uses_chrome():
    """IXBrowser off (default) -> chrome backend, unchanged behavior."""
    db = Database()
    gid = seed_group(db, 1, "alixoff")
    eng = ListingEngine(db, driver_factory=MockDriver)
    rid = eng.start_run(start_cfg(db, gid, "login", batch_size=1))
    run_to_end(eng, rid)
    launches = events("launch")
    assert len(launches) == 1, launches
    assert launches[0]["backend"] == "chrome", launches[0]
    assert launches[0]["ix_profile_id"] is None


def t_ixbrowser_unreachable_raises_gracefully():
    """PlaywrightDriver with IXBrowser on but unreachable -> clear error,
    no browser launched."""
    from fms.browser import PlaywrightDriver

    drv = PlaywrightDriver({
        "id": 424242, "label": "ixtest@x.com",
        "ixbrowser": {"enabled": True, "host": "127.0.0.1", "port": 1,
                      "profile_id": "IX-NOPE"},
    })
    try:
        drv._launch()
    except RuntimeError as exc:
        assert "IXBrowser" in str(exc), str(exc)
        assert "not reachable" in str(exc), str(exc)
    else:
        raise AssertionError("expected RuntimeError for unreachable IXBrowser")
    assert drv._page is None and drv._browser is None


def t_dashboard_typing_and_ix():
    c = _client()
    r = c.get("/runs")
    assert r.status_code == 200, r.status_code
    html = r.text
    assert "Human-like typing" in html
    assert "Types character by character. Most human-like, slowest, best against strict detection." in html
    assert "Fast fill (JS)" in html
    assert "Instantly sets values. Fastest, no clipboard use." in html
    assert "OS clipboard paste" in html
    assert "Copies each value and pastes with Ctrl+V." in html
    assert "Speed presets" in html
    assert "Min delay (ms)" in html and "Max delay (ms)" in html
    assert "Thinking pause min (ms)" in html
    assert "Extra delay after punctuation (ms)" in html
    assert "Newline pause min (ms)" in html
    assert "Estimated speed" in html
    assert "IXBrowser (anti-detect)" in html
    assert "Reset host" in html
    r = c.get("/accounts")
    assert r.status_code == 200, r.status_code
    assert "Anti-detect Profile ID" in r.text
    assert "Proxy Status" in r.text


def t_api_account_ix_profile_roundtrip():
    c = _client()
    groups = c.get("/api/groups").json()
    gid = groups[0]["id"] if groups else _mkgroup(c, "alixtrip")
    aid = c.post("/api/accounts", json={
        "group_id": gid, "label": "ixp@x.com", "password": "pw",
        "ix_profile_id": "IX-999"}).json()["id"]
    rows = c.get("/api/accounts", params={"group_id": gid}).json()
    row = [r for r in rows if r["id"] == aid][0]
    assert row["ix_profile_id"] == "IX-999", row
    c.put(f"/api/accounts/{aid}", json={"ix_profile_id": "IX-1000"})
    rows = c.get("/api/accounts", params={"group_id": gid}).json()
    row = [r for r in rows if r["id"] == aid][0]
    assert row["ix_profile_id"] == "IX-1000", row


def t_api_run_accepts_typing_and_ix_settings():
    c = _client()
    groups = c.get("/api/groups").json()
    gid = groups[0]["id"] if groups else _mkgroup(c, "altypix")
    c.post("/api/accounts", json={"group_id": gid, "label": "tix@x.com",
                                  "password": "pw"})
    r = c.post("/api/runs", json={
        "group_id": gid, "mode": "login", "batch_size": 1, "fresh": True,
        "typing_mode": "clipboard", "typing_preset": "careful",
        "min_delay_ms": 25, "max_delay_ms": 70,
        "thinking_pause_min_ms": 150, "thinking_pause_max_ms": 500,
        "extra_delay_after_punctuation_ms": 60,
        "newline_pause_min_ms": 250, "newline_pause_max_ms": 800,
        "hybrid_threshold_chars": 400,
        "ix_browser_enabled": True, "ix_local_api_host": "127.0.0.1",
        "ix_local_api_port": 53200,
    })
    assert r.status_code == 200, r.text
    rid = r.json()["run_id"]
    assert wait_until(lambda: c.get(f"/api/runs/{rid}").json()["status"]
                      == "completed", 60), c.get(f"/api/runs/{rid}").json()
    settings = c.get(f"/api/runs/{rid}").json().get("settings") or {}
    for key, val in [("typing_mode", "clipboard"),
                     ("typing_preset", "careful"),
                     ("min_delay_ms", 25), ("max_delay_ms", 70),
                     ("thinking_pause_min_ms", 150),
                     ("thinking_pause_max_ms", 500),
                     ("extra_delay_after_punctuation_ms", 60),
                     ("newline_pause_min_ms", 250),
                     ("newline_pause_max_ms", 800),
                     ("hybrid_threshold_chars", 400),
                     ("ix_browser_enabled", True),
                     ("ix_local_api_host", "127.0.0.1"),
                     ("ix_local_api_port", 53200)]:
        assert settings.get(key) == val, (key, settings.get(key))
    launches = [c_ for c_ in MockDriver.calls if c_["event"] == "launch"]
    assert launches and launches[0]["backend"] == "ixbrowser", launches


# ------------------------------------------------- pass #3: groups/accounts ---
def _mkgroup(c, prefix: str) -> int:
    """Create a uniquely-named group via the API (POST returns a redirect)."""
    name = f"{prefix}-{time.time_ns()}"
    r = c.post("/api/groups", data={"name": name})
    assert r.status_code in (200, 303), r.text
    groups = c.get("/api/groups").json()
    return [g["id"] for g in groups if g["name"] == name][0]


def t_bulk_parser_fbm_format():
    p = accounts_mod.parse_account_line
    # Exact FBM Robo v6.2.0.0 format: login;password;True|False;Browser
    d = p("+447542441738;London67890;True;Chrome")
    assert d["label"] == "+447542441738" and d["password"] == "London67890"
    assert d["active"] == 1 and d["browser"] == "Chrome", d
    d = p("Clarajaden256@gmail.com;Clara256@#;False;Chrome")
    assert d["active"] == 0 and d["browser"] == "Chrome", d
    d = p("el9@gmail.com;Hartley9@#;true;IXBrowser")
    assert d["active"] == 1 and d["browser"] == "IXBrowser", d
    # Browser empty -> default Chrome
    d = p("a@x.com;pw;True;")
    assert d["browser"] == "chrome", d
    # Classic formats still work
    d = p("a@x.com;pw")
    assert d["active"] == 1 and d["browser"] == "chrome" and "proxy" not in d
    d = p("a@x.com;pw;1.2.3.4:8080")
    assert d["proxy"] == "1.2.3.4:8080", d
    d = p("a@x.com;pw;True;1.2.3.4:8080:u:pass")
    assert d["proxy"] == "1.2.3.4:8080:u:pass", d
    d = p("a@x.com|pw|s3cr3t|ck")
    assert d["twofa_secret"] == "s3cr3t" and d["cookie"] == "ck", d
    # Bad lines raise (reported per line by the caller)
    for bad in ["justalogin", ";pw", "a@x.com;", ""]:
        try:
            r = p(bad)
        except ValueError:
            r = "err"
        assert r == "err" or r is None, (bad, r)
    assert p("# comment") is None


def t_bulk_add_false_stays_inactive():
    db = Database()
    gid = accounts_mod.create_group(db, f"bulk-{time.time_ns()}")
    text = ("+447542441738;pw1;True;Chrome\n"
            "a@gmail.com;pw2;False;Chrome\n"
            "b@gmail.com;pw3;false;\n"
            "badline-without-password\n"
            "# comment\n")
    added, errors = accounts_mod.bulk_add_accounts(db, gid, text,
                                                   validate_proxies=False)
    assert added == 3, (added, errors)
    assert len(errors) == 1 and errors[0].startswith("line 4"), errors
    rows = {r["label"]: r for r in accounts_mod.list_accounts(db, gid)}
    assert rows["+447542441738"]["active"] == 1
    assert rows["a@gmail.com"]["active"] == 0
    assert rows["b@gmail.com"]["active"] == 0
    assert rows["b@gmail.com"]["browser"] == "chrome"
    # Inactive accounts are never picked for runs.
    elig = accounts_mod.active_accounts_for_run(db, gid)
    labels = {a["label"] for a in elig}
    assert "+447542441738" in labels and "a@gmail.com" not in labels, labels


def t_import_from_file():
    c = _client()
    gid = _mkgroup(c, "imp")
    body = "x1@y.com;p1;True;Chrome\nx2@y.com;p2;False;Chrome\n"
    r = c.post("/api/accounts/import",
               data={"group_id": str(gid)},
               files={"file": ("group1.txt", body, "text/plain")})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["added"] == 2 and not j["errors"], j
    rows = c.get("/api/accounts", params={"group_id": gid}).json()
    by_label = {a["label"]: a for a in rows}
    assert by_label["x1@y.com"]["active"] == 1
    assert by_label["x2@y.com"]["active"] == 0


def t_clone_group():
    db = Database()
    gid = accounts_mod.create_group(db, f"src-{time.time_ns()}")
    accounts_mod.add_account(db, gid, "+447700900111", "pw1", browser="Chrome")
    aid2 = accounts_mod.add_account(db, gid, "e@y.com", "pw2", browser="IXBrowser")
    db.execute("UPDATE accounts SET active = 0 WHERE id = ?", (aid2,))
    pid = seed_product(db, f"cloneprod-{time.time_ns()}")
    accounts_mod.set_group_products(db, gid, [pid])
    new_id = accounts_mod.clone_group(db, gid)
    groups = {g["id"]: g for g in accounts_mod.list_groups(db)}
    assert groups[new_id]["name"].endswith("(copy)"), groups[new_id]["name"]
    assert groups[new_id]["account_count"] == 2, groups[new_id]
    assert groups[new_id]["product_count"] == 1, groups[new_id]
    src_accs = sorted(accounts_mod.list_accounts(db, gid),
                      key=lambda a: a["label"])
    new_accs = sorted(accounts_mod.list_accounts(db, new_id),
                      key=lambda a: a["label"])
    for s, n in zip(src_accs, new_accs):
        assert s["label"] == n["label"]
        assert s["active"] == n["active"]
        assert s["browser"] == n["browser"]
        assert s["password_enc"] == n["password_enc"]
        assert s["id"] != n["id"]
    prods = accounts_mod.list_group_products(db, new_id)
    assert [p["id"] for p in prods] == [pid], prods
    # Cloning again gets a unique name.
    again = accounts_mod.clone_group(db, gid)
    assert groups[new_id]["name"] != \
        {g["id"]: g for g in accounts_mod.list_groups(db)}[again]["name"]


def t_proxy_tools_selected():
    db = Database()
    gid = accounts_mod.create_group(db, f"px-{time.time_ns()}")
    ids = [accounts_mod.add_account(db, gid, f"px{i}@x.com", "pw") for i in range(3)]
    # Add Proxies to Selected (no validation here; cycling over 2 proxies).
    mapping = accounts_mod.assign_proxies_to_accounts(
        db, ids, ["1.1.1.1:8080", "2.2.2.2:8080:u:p"], validate=False)
    assert len(mapping) == 3 and len(set(mapping.values())) == 2, mapping
    rows = {r["id"]: r for r in accounts_mod.list_accounts(db, gid)}
    assert all(r["proxy_id"] for r in rows.values())
    # Check Proxy Status with a faked validator.
    import fms.proxies as pm
    real = pm.validate_proxy
    pm.validate_proxy = lambda *a, **k: (False, "refused") \
        if a[0] == "2.2.2.2" else (True, "OK")
    try:
        rep = accounts_mod.check_account_proxies(db, ids)
    finally:
        pm.validate_proxy = real
    by_label = {r["label"]: r for r in rep}
    assert by_label["px0@x.com"]["working"] is True
    assert by_label["px2@x.com"]["working"] is True
    assert by_label["px1@x.com"]["working"] is False, by_label
    # Remove Not Working Proxies: px1's proxy dies, others keep theirs.
    out = accounts_mod.remove_not_working_proxies(db, ids)
    assert out["proxies_deleted"] == 1 and out["accounts_cleared"] == 1, out
    rows = {r["id"]: r for r in accounts_mod.list_accounts(db, gid)}
    assert rows[ids[1]]["proxy_id"] is None
    assert rows[ids[0]]["proxy_id"] is not None
    # Remove Proxies from Selected.
    n = accounts_mod.remove_proxies_from_accounts(db, [ids[0], ids[2]])
    assert n == 2, n
    rows = {r["id"]: r for r in accounts_mod.list_accounts(db, gid)}
    assert all(r["proxy_id"] is None for r in rows.values())


def t_cookie_roundtrip_and_driver():
    c = _client()
    gid = _mkgroup(c, "ck")
    aid = c.post("/api/accounts", json={
        "group_id": gid, "label": f"ck{time.time_ns()}@x.com", "password": "pw",
        "cookie": "c_user=123; xs=abc"}).json()["id"]
    # Cookie is never exposed via the API (encrypted blobs stripped).
    rows = c.get("/api/accounts", params={"group_id": gid}).json()
    row = [r for r in rows if r["id"] == aid][0]
    assert "cookie_enc" not in row and "cookie" not in row, row.keys()
    # But the driver receives the decrypted cookie at login.
    db = Database()
    acc = accounts_mod.get_account(db, aid)
    creds = accounts_mod.account_credentials(db, acc)
    assert creds["cookie"] == "c_user=123; xs=abc", creds
    drv = MockDriver(creds)
    assert drv.login() is True
    logins = [e for e in MockDriver.calls if e["event"] == "login"]
    assert logins and logins[-1]["has_cookie"] is True, logins


def t_board_api():
    c = _client()
    gid = _mkgroup(c, "bd")
    c.post("/api/accounts", json={"group_id": gid, "label": "bd1@x.com",
                                  "password": "pw"})
    c.post("/api/accounts", json={"group_id": gid, "label": "bd2@x.com",
                                  "password": "pw"})
    r = c.get("/api/board", params={"group_id": gid})
    assert r.status_code == 200, r.text
    b = r.json()
    for key in ("accounts", "pending", "in_progress", "done_posting",
                "failed", "exclude", "logs"):
        assert key in b, key
    assert sorted(b["accounts"]) == ["bd1@x.com", "bd2@x.com"], b["accounts"]
    # After a login run both accounts land somewhere truthful (no postings).
    rr = c.post("/api/runs", json={"group_id": gid, "mode": "login",
                                   "batch_size": 2, "fresh": True})
    assert rr.status_code == 200, rr.text
    rid = rr.json()["run_id"]
    assert wait_until(lambda: c.get(f"/api/runs/{rid}").json()["status"]
                      == "completed", 60)
    b = c.get("/api/board", params={"group_id": gid}).json()
    assert b["run_id"] == rid
    seen = (set(b["pending"]) | set(b["in_progress"]) |
            set(b["done_posting"]) | set(b["failed"]))
    assert seen == {"bd1@x.com", "bd2@x.com"}, b


def t_dashboard_pass3_labels():
    c = _client()
    r = c.get("/runs")
    assert r.status_code == 200, r.status_code
    html = r.text
    for needle in ["— Select Script —", "Start:", "Pause/Continue:",
                   "Schedule At:", "⚡1x", "Filter accounts...",
                   "Accounts: (", "Pending: (", "In Progress: (",
                   "Done Posting: (", "Failed: (", "Exclude: (",
                   "Expand/Collapse", "Copy All", "Logs:",
                   "Settings ▾", "Help ▾", "stepBatch", "cycleSpeed",
                   "loadBoard"]:
        assert needle in html, f"missing: {needle}"
    r = c.get("/accounts")
    assert r.status_code == 200, r.status_code
    html = r.text
    for needle in ["Manage Groups", "Product Counts", "Manage Accounts",
                   "Clone", "Add Single Account", "Add Bulk Accounts",
                   "Import from File", "Remove Selected",
                   "Add Proxies to Selected", "Remove Proxies from Selected",
                   "Check Proxy Status", "Remove Not Working Proxies",
                   "Select All", "search...", "Anti-detect Profile ID",
                   "Active", "Proxy Status", "Cookie", "switch",
                   "Group products", "login;password;True|False;Browser"]:
        assert needle in html, f"missing: {needle}"


def t_api_proxy_and_group_product_endpoints():
    c = _client()
    gid = _mkgroup(c, "ep")
    a1 = c.post("/api/accounts", json={"group_id": gid, "label": "ep1@x.com",
                                       "password": "pw"}).json()["id"]
    a2 = c.post("/api/accounts", json={"group_id": gid, "label": "ep2@x.com",
                                       "password": "pw"}).json()["id"]
    r = c.post("/api/accounts/proxies/assign",
               json={"account_ids": [a1, a2],
                     "proxies_text": "9.9.9.9:1080\n8.8.8.8:1080"})
    assert r.status_code == 200 and len(r.json()["assigned"]) == 2, r.text
    r = c.post("/api/accounts/proxies/remove", json={"account_ids": [a1]})
    assert r.json()["cleared"] == 1, r.text
    r = c.post("/api/groups/999999/clone")
    assert r.status_code == 404, r.status_code
    r = c.post(f"/api/groups/{gid}/clone")
    assert r.status_code == 200, r.text
    new_id = r.json()["id"]
    assert new_id != gid
    db = Database()
    pid = seed_product(db, f"ep-prod-{time.time_ns()}")
    r = c.post(f"/api/groups/{gid}/products", json={"product_ids": [pid]})
    assert r.status_code == 200, r.text
    got = c.get(f"/api/groups/{gid}/products").json()
    assert [p["id"] for p in got] == [pid], got
    groups = {g["id"]: g for g in c.get("/api/groups").json()}
    assert groups[gid]["product_count"] == 1, groups[gid]


# ------------------------------------------------- pass #4: products ------
def _mk_img_dir(prefix: str, n: int) -> str:
    d = tempfile.mkdtemp(prefix=prefix)
    for i in range(n):
        Path(d, f"img{i:02d}.jpg").write_bytes(b"\xff\xd8fake")
    return d


def t_p4_product_crud():
    db = Database()
    pid = products_mod.create_product(
        db, "P4 Sofa", base_price=50.0, description="base desc",
        tags="sofa,comfy", category="Furniture > Sofas",
        listing_type="vehicle", condition="New", availability="In Stock",
        append_title_count=3, main_image_folder="/tmp/m", extra_image_folder="/tmp/e",
        delivery_options=["public_meetup", "door_dropoff", "bogus"],
        descriptions=["Desc one", "Desc two"])
    products_mod.set_titles(db, pid, ["T1", "T2"])
    products_mod.set_locations(db, pid, ["Loc1", "Loc2"])
    p = products_mod.get_product(db, pid)
    assert p["listing_type"] == "vehicle"
    assert p["condition"] == "New"
    assert p["availability"] == "In Stock"
    assert p["append_title_count"] == 3
    assert p["main_image_folder"] == "/tmp/m"
    assert p["extra_image_folder"] == "/tmp/e"
    assert p["delivery_options"] == ["public_meetup", "door_dropoff"], p["delivery_options"]
    assert p["descriptions"] == ["Desc one", "Desc two"]
    assert p["category"] == "Furniture||Sofas", p["category"]  # || separator
    assert p["titles"] == ["T1", "T2"] and p["locations"] == ["Loc1", "Loc2"]
    # update path
    products_mod.update_product(db, pid, condition="Used - good",
                                delivery_options=["door_pickup"], category="Cars")
    p2 = products_mod.get_product(db, pid)
    assert p2["condition"] == "Used - good"
    assert p2["delivery_options"] == ["door_pickup"]
    assert p2["category"] == "Cars"
    # bad listing type rejected
    try:
        products_mod.create_product(db, "Bad", listing_type="boat")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def t_p4_rotation_cycles():
    db = Database()
    gid = seed_group(db, 1, "p4rot")
    pid = products_mod.create_product(db, "Rot", base_price=10.0)
    products_mod.set_titles(db, pid, ["TA", "TB"])
    products_mod.set_descriptions(db, pid, ["DA", "DB"])
    products_mod.set_locations(db, pid, ["LA", "LB"])
    eng = ListingEngine(db, driver_factory=MockDriver)
    rid = eng.start_run(start_cfg(db, gid, "complete", product_ids=[pid],
                                  min_ads=4, max_ads=4))
    run_to_end(eng, rid)
    starts = events("post_start")
    assert len(starts) == 4, len(starts)
    assert [s["title"] for s in starts] == ["TA", "TB", "TA", "TB"]
    assert [s["description"] for s in starts] == ["DA", "DB", "DA", "DB"], \
        [s["description"] for s in starts]
    rows = db.fetchall("SELECT title_used FROM listings WHERE run_id=? ORDER BY id",
                       (rid,))
    assert [r["title_used"] for r in rows] == ["TA", "TB", "TA", "TB"]
    locs = db.fetchall("SELECT location_used FROM listings WHERE run_id=? ORDER BY id",
                       (rid,))
    assert [r["location_used"] for r in locs] == ["LA", "LB", "LA", "LB"], \
        [r["location_used"] for r in locs]


def t_p4_append_title_count():
    # Confirmed 2026-10-15: Append Title Count repeats the TITLE N times at
    # the END of the description (optional; 0 = off).
    db = Database()
    gid = seed_group(db, 1, "p4app")
    pid = products_mod.create_product(db, "App", base_price=5.0,
                                      append_title_count=2)
    products_mod.set_titles(db, pid, ["X"])
    products_mod.set_locations(db, pid, ["L"])
    eng = ListingEngine(db, driver_factory=MockDriver)
    rid = eng.start_run(start_cfg(db, gid, "complete", product_ids=[pid],
                                  min_ads=2, max_ads=2))
    run_to_end(eng, rid)
    posts = events("post_start")
    assert len(posts) == 2, MockDriver.calls
    for p in posts:
        assert p["title"] == "X", p
        assert p["description"].endswith("X\nX"), p["description"]
    # count 0 -> description untouched
    pid2 = products_mod.create_product(db, "App0", base_price=5.0,
                                       append_title_count=0)
    products_mod.set_titles(db, pid2, ["Y"])
    products_mod.set_locations(db, pid2, ["L"])
    eng2 = ListingEngine(db, driver_factory=MockDriver)
    rid2 = eng2.start_run(start_cfg(db, gid, "complete", product_ids=[pid2],
                                    min_ads=1, max_ads=1))
    run_to_end(eng2, rid2)
    p2 = events("post_start")[-1]
    assert p2["title"] == "Y" and not p2["description"].endswith("Y\nY"), p2


def t_p4_image_main_extra_and_tracking():
    db = Database()
    gid = seed_group(db, 1, "p4img")
    main_d = _mk_img_dir("p4main_", 2)
    extra_d = _mk_img_dir("p4extra_", 3)
    pid = products_mod.create_product(db, "Imgs", base_price=9.0,
                                      main_image_folder=main_d,
                                      extra_image_folder=extra_d)
    products_mod.set_titles(db, pid, ["T"])
    products_mod.set_locations(db, pid, ["L"])
    eng = ListingEngine(db, driver_factory=MockDriver)
    rid = eng.start_run(start_cfg(db, gid, "complete", product_ids=[pid],
                                  min_ads=2, max_ads=2))
    run_to_end(eng, rid)
    used, total = products_mod.image_stats(db, pid)
    assert total == 5, (used, total)
    assert used == 5, (used, total)  # all images rotated through across 2 listings
    prods = {p["id"]: p for p in products_mod.list_products(db)}
    assert prods[pid]["images_used"] == 5 and prods[pid]["image_count"] == 5
    # main images always first in the payload
    starts = events("post_start")
    first_paths = None
    import json as _json
    row = db.fetchone("SELECT images_used FROM listings WHERE run_id=? ORDER BY id",
                      (rid,))
    first_paths = _json.loads(row["images_used"])
    assert first_paths[0].startswith(main_d) and first_paths[1].startswith(main_d), \
        first_paths


def t_p4_delivery_options_reach_driver():
    db = Database()
    gid = seed_group(db, 1, "p4do")
    pid = seed_product(db, "DoSofa")
    products_mod.update_product(db, pid,
                                delivery_options=["public_meetup", "door_dropoff"])
    eng = ListingEngine(db, driver_factory=MockDriver)
    rid = eng.start_run(start_cfg(db, gid, "complete", product_ids=[pid],
                                  min_ads=1, max_ads=1))
    run_to_end(eng, rid)
    starts = events("post_start")
    assert starts[0]["delivery_options"] == ["public_meetup", "door_dropoff"], starts[0]
    assert starts[0]["listing_type"] == "item"


def t_p4_condition_availability_payload():
    db = Database()
    gid = seed_group(db, 1, "p4ca")
    pid = seed_product(db, "CaSofa")
    products_mod.update_product(db, pid, condition="Used - like new",
                                availability="Out of Stock")
    eng = ListingEngine(db, driver_factory=MockDriver)
    cfg = start_cfg(db, gid, "complete", product_ids=[pid], min_ads=1, max_ads=1)
    cfg.condition = "New"  # product-level wins over run-level
    rid = eng.start_run(cfg)
    run_to_end(eng, rid)
    starts = events("post_start")
    assert starts[0]["condition"] == "Used - like new", starts[0]
    assert starts[0]["availability"] == "Out of Stock", starts[0]
    # fallback to run config when product has none
    pid2 = seed_product(db, "CaSofa2")
    MockDriver.reset()
    eng2 = ListingEngine(db, driver_factory=MockDriver)
    cfg2 = start_cfg(db, gid, "complete", product_ids=[pid2], min_ads=1, max_ads=1)
    cfg2.condition = "New"
    rid2 = eng2.start_run(cfg2)
    run_to_end(eng2, rid2)
    starts2 = events("post_start")
    assert len(starts2) == 1, starts2
    assert starts2[0]["condition"] == "New", starts2[0]


def t_p4_clone_product():
    db = Database()
    pid = products_mod.create_product(
        db, "Orig", base_price=42.0, condition="New", listing_type="property",
        append_title_count=7, delivery_options=["door_pickup"],
        descriptions=["D1"], tags="t", category="Furniture||Sofas")
    products_mod.set_titles(db, pid, ["T1", "T2"])
    products_mod.set_locations(db, pid, ["L1"])
    new_id = products_mod.clone_product(db, pid)
    assert new_id != pid
    src = products_mod.get_product(db, pid)
    dst = products_mod.get_product(db, new_id)
    for k in ("base_price", "condition", "listing_type", "append_title_count",
              "tags", "category"):
        assert dst[k] == src[k], (k, dst[k], src[k])
    assert dst["delivery_options"] == ["door_pickup"]
    assert dst["descriptions"] == ["D1"]
    assert dst["titles"] == ["T1", "T2"] and dst["locations"] == ["L1"]
    assert dst["name"].startswith("Orig (copy")


def t_p4_products_page():
    c = _client()
    r = c.get("/products")
    assert r.status_code == 200, r.text
    for s in ("Listing Type", "Append Title Count", "Delivery Options",
              "Add Description", "Main Images", "Extra Images", "Clone"):
        assert s in r.text, s
    # API round-trip with the full dialog field set
    r2 = c.post("/api/products", json={
        "name": "PageProd", "listing_type": "property", "condition": "New",
        "availability": "In Stock", "append_title_count": 2, "tags": "x",
        "category": "Furniture > Sofas", "main_image_folder": "/tmp",
        "delivery_options": ["door_pickup"], "titles": ["T"],
        "locations": ["L"], "descriptions": ["D"]})
    assert r2.status_code == 200, r2.text
    pid = r2.json()["id"]
    got = c.get(f"/api/products/{pid}").json()
    assert got["category"] == "Furniture||Sofas"
    assert got["listing_type"] == "property"
    assert "Images" in c.get("/products").text


def main():
    print("== alignment tests (v6.1.2.0 + v6.2.0.0 groups/accounts) ==")
    for name, fn in sorted([(k, v) for k, v in list(globals().items())
                            if k.startswith("t_") and callable(v)]):
        check(name, fn)
    n_fail = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{len(results) - n_fail}/{len(results)} passed")
    if n_fail:
        raise SystemExit(1)


def main():
    print("== alignment tests (v6.2.0.0, pass #5: 20 actions + new modes) ==")
    for name, fn in sorted([(k, v) for k, v in list(globals().items())
                            if k.startswith("t_") and callable(v)]):
        check(name, fn)
    n_fail = sum(1 for s, _, _ in results if s == FAIL)
    print(f"\n{len(results) - n_fail}/{len(results)} passed")
    if n_fail:
        raise SystemExit(1)


# ------------------------------------------------- (e) pass #5 -----------
def t_p5_browser_launch_fast():
    db = Database()
    gid = seed_group(db, 1, "p5lf")
    eng = ListingEngine(db, driver_factory=MockDriver)
    rid = eng.start_run(start_cfg(db, gid, "browser_launch_fast"))
    run_to_end(eng, rid)
    # Identical to Browser Launch (confirmed 2026-10-15): logs in like the
    # regular Browser Launch, no special fast-launch call, browser left
    # open for manual work.
    assert len(events("login")) == 1, MockDriver.calls
    assert not events("launch_fast"), MockDriver.calls
    assert not [c for c in MockDriver.calls if c["event"] == "close"], \
        MockDriver.calls


def t_p5_delete_relist():
    db = Database()
    gid = seed_group(db, 1, "p5dr")
    pid = seed_product(db, "RelistSofa")
    eng = ListingEngine(db, driver_factory=MockDriver)
    MockDriver.fake_listings = [
        {"id": "rl1", "title": "Old Sofa A"},
        {"id": "rl2", "title": "Old Sofa B"},
    ]
    rid = eng.start_run(start_cfg(db, gid, "delete_relist",
                                  product_ids=[pid], min_ads=1, max_ads=1))
    run_to_end(eng, rid)
    deleted = [c for c in MockDriver.calls if c["event"] == "delete_listing"]
    assert len(deleted) == 2, MockDriver.calls
    assert {d["listing_id"] for d in deleted} == {"rl1", "rl2"}
    rows = db.fetchall("SELECT status FROM listings WHERE run_id = ?",
                       (rid,))
    statuses = [r["status"] for r in rows]
    assert statuses.count("deleted") == 2, statuses
    assert statuses.count("relisted") == 2, statuses


def t_p5_listing_number_verification_dropped():
    # Per the user (2026-10-15): Listing Number Verification is NOT added.
    assert "listing_number_verification" not in MODES, MODES
    assert all(key != "listing_number_verification" for key, _ in ACTIONS)
    assert "listing_number_verification" not in BLOCK_TYPES


def t_p5_wait_next_turn_script_only():
    c = _client()
    r = c.get("/api/actions")
    assert r.status_code == 200, r.text
    labels = [a["label"] for a in r.json()]
    assert "Wait For Next Turn" not in labels, labels
    assert len(labels) == 18
    assert "AI Behaviour Training" not in labels, labels
    # ...but it is still a valid script block (OTHER category)
    assert "wait_next_turn" in scripts_mod.BLOCK_TYPES
    cats = dict(scripts_mod.BUILDER_CATEGORIES)
    assert "wait_next_turn" in cats["OTHER"]
    assert scripts_mod.BLOCK_LABELS["wait_next_turn"] == "Wait For Next Turn"
    # old run-mode API still accepts it (back-compat)
    from fms.engine import MODES
    assert "wait_next_turn" in MODES


def t_p5_activity_params_roundtrip():
    c = _client()
    blocks = [scripts_mod.make_block(
        "scroll_feed", "b1", min_minutes=1, max_minutes=3,
        keep_scrolling_chance=55, like_a_post_chance=15),
        scripts_mod.make_block("scroll_marketplace", "b2", min_minutes=2,
                               max_minutes=5, keep_scrolling_chance=50,
                               hover_a_listing_chance=70,
                               open_a_listing_chance=6)]
    r = c.post("/api/scripts", json={"name": "p5params", "blocks": blocks})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    got = c.get(f"/api/scripts/{sid}").json()
    assert got["blocks"][0]["params"]["keep_scrolling_chance"] == 55
    assert got["blocks"][0]["params"]["min_minutes"] == 1
    assert got["blocks"][1]["params"]["open_a_listing_chance"] == 6
    assert c.delete(f"/api/scripts/{sid}").status_code == 200


def t_p5_activity_engine_honors_params():
    db = Database()
    gid = seed_group(db, 1, "p5ae")
    eng = ListingEngine(db, driver_factory=MockDriver)
    sid = scripts_mod.create_script(db, "p5-act", [
        scripts_mod.make_block("scroll_feed", "b1", min_minutes=0.02,
                               max_minutes=0.03, keep_scrolling_chance=55,
                               like_a_post_chance=15),
    ], shuffle=False)
    cfg = start_cfg(db, gid, "script", script_id=sid, activity_scale=0.001)
    rid = eng.start_run(cfg)
    run_to_end(eng, rid)
    acts = events("activity:scroll_feed")
    assert len(acts) == 1, MockDriver.calls
    rec = acts[0]
    # mock-clock friendly: recorded minutes within the configured range
    assert 0.02 * 0.001 <= rec["minutes"] <= 0.03 * 0.001, rec
    # chosen params recorded as 0-1 probabilities
    assert rec["probs"] == {"keep_scrolling": 0.55,
                            "like_post": 0.15}, rec
    assert rec["outcome"] == "completed"


def t_p5_activity_zero_minutes_skipped():
    db = Database()
    gid = seed_group(db, 1, "p5as")
    eng = ListingEngine(db, driver_factory=MockDriver)
    sid = scripts_mod.create_script(db, "p5-skip", [
        scripts_mod.make_block("watch_reels", "b1"),  # defaults: 0/0
    ], shuffle=False)
    cfg = start_cfg(db, gid, "script", script_id=sid, activity_scale=0.001)
    rid = eng.start_run(cfg)
    run_to_end(eng, rid)
    assert not events("activity:watch_reels"), MockDriver.calls
    msgs = [r["message"] for r in
            eng.db.fetchall("SELECT message FROM logs WHERE run_id = ?",
                            (rid,))]
    assert any("skipped (0 duration configured)" in m for m in msgs), msgs


def t_p5_builder_page():
    db = Database()
    sid = scripts_mod.create_script(
        db, "BuilderProbe",
        [scripts_mod.make_block("scroll_feed", "b1")], shuffle=False)
    from fastapi.testclient import TestClient
    from fms.app import create_app
    c = TestClient(create_app(driver_name="mock"))
    r = c.get(f"/scripts/{sid}")
    assert r.status_code == 200, r.text
    html = r.text
    for marker in ("AVAILABLE ACTIONS", "Click or drag to add",
                   "Search actions...", "SCRIPT ACTIONS", "actCount",
                   "Save Script", "Cancel", "SCRIPT NAME"):
        assert marker in html, marker
    for cat in ("LISTINGS", "DELETE", "ACTIVITY", "OTHER"):
        assert cat in html, cat
    # grouped actions with the v6.2.0.0 labels (tojson escapes & as \u0026)
    for label in ("Delete \\u0026 Relist", "Browser Launch Fast",
                  "Scroll Feed", "Wait For Next Turn"):
        assert label in html, label
    # HTML5 drag events + click-to-add fallback
    assert "dragstart" in html and "text/x-fms-block" in html
    assert "addEventListener('click'" in html
    # exact settings dialog labels from the screenshots
    for marker in ("Scroll Feed Settings", "Keep-scrolling chance (%)",
                   "Like-a-post chance (%)", "Watch Reels Settings",
                   "Skip chance (%)", "Scroll Marketplace Settings",
                   "Hover-a-listing chance (%)",
                   "Open-a-listing chance (%)"):
        assert marker in html, marker


def t_p5_relist_edit_and_delete_relist_blocks():
    db = Database()
    gid = seed_group(db, 1, "p5blk")
    pid = seed_product(db, "BlkSofa")
    eng = ListingEngine(db, driver_factory=MockDriver)
    MockDriver.fake_listings = [{"id": "e1", "title": "E1"},
                               {"id": "e2", "title": "E2"}]
    sid = scripts_mod.create_script(db, "p5-blocks", [
        scripts_mod.make_block("relist_edit", "b1", product_id=pid,
                               count_min=1, count_max=1),
        scripts_mod.make_block("delete_relist", "b2", product_id=pid,
                               max_relist=1),
    ], shuffle=False)
    cfg = start_cfg(db, gid, "script", script_id=sid, activity_scale=0.001)
    rid = eng.start_run(cfg)
    run_to_end(eng, rid)
    edited = [c for c in MockDriver.calls if c["event"] == "edit_listing"]
    assert len(edited) == 1, MockDriver.calls
    deleted = [c for c in MockDriver.calls if c["event"] == "delete_listing"]
    assert len(deleted) == 1, MockDriver.calls
    statuses = [r["status"] for r in
                db.fetchall("SELECT status FROM listings WHERE run_id = ?",
                            (rid,))]
    assert "relisted" in statuses, statuses
    assert "deleted" in statuses, statuses


if __name__ == "__main__":
    main()


def t_scrap_data_description_location_optional():
    # Confirmed 2026-10-15: scraped competitor listing -> own product.
    # Title always copied; description/location are optional toggles.
    from fms import scrap as scrap_mod
    db = Database()
    items = [{"title": "Competitor Sofa", "description": "Comp desc",
              "location": "Leeds", "photo_urls": [], "url": "http://fb/x"}]
    pid = scrap_mod.save_as_product(db, "ScrapOpt", items,
                                    include_description=False,
                                    include_location=False)
    p = products_mod.get_product(db, pid)
    assert p["titles"] == ["Competitor Sofa"], p
    assert not p["descriptions"], p["descriptions"]
    assert not p["locations"], p["locations"]
    pid2 = scrap_mod.save_as_product(db, "ScrapFull", items)
    p2 = products_mod.get_product(db, pid2)
    assert p2["descriptions"] == ["Comp desc"], p2["descriptions"]
    assert p2["locations"] == ["Leeds"], p2["locations"]

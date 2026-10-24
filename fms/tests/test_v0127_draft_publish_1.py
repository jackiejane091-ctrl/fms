"""v0.2.127 targeted test — new visible action "Draft & Publish 1".

Asserts, against a real in-memory SQLite DB through the real methods:
  1. Mode registry: the visible action list exposes draft_publish_1
     ("Draft & Publish 1") and no longer exposes draft_publish, while
     draft_publish stays a valid mode for older runs/API clients.
  2. draft_publish_1 end-to-end, quantity 1: one draft is created and
     the SAME draft is published in the same flow (row ends 'posted'
     with the saved fb_listing_id; total_posted bumped).
  3. draft_publish_1 publish failure preserves the draft (status
     stays 'draft', ID kept, 'publish pending' error) — the v0.2.126
     recovery logic inside the new publish method.
  4. The preserved old draft_publish flow still creates + publishes
     with the same stub driver (no regression for older runs).
  5. _rotation marks payloads as_draft only for the two draft_publish
     modes, not for other modes.
"""
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, ".")
import fms.engine as E  # noqa: E402


class ShimDB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE listings (
                id INTEGER PRIMARY KEY, run_id INTEGER, account_id INTEGER,
                product_id INTEGER, title_used TEXT, price_used REAL,
                location_used TEXT, status TEXT,
                fb_listing_id TEXT, images_used TEXT, error TEXT,
                posted_at TEXT);
            CREATE TABLE runs (id INTEGER PRIMARY KEY, total_posted INTEGER);
            CREATE TABLE account_image_usage (
                account_id INTEGER, image_path TEXT, last_used_at TEXT,
                PRIMARY KEY (account_id, image_path));
            INSERT INTO runs (id, total_posted) VALUES (1, 0);
            """)
        self.conn.commit()

    def execute(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def fetchall(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def fetchone(self, sql, params=()):
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def make_controller(db, mode):
    ctl = E.RunController.__new__(E.RunController)
    ctl.cfg = SimpleNamespace(mode=mode, tabs_draft_publish=2,
                              publish_mode="together", publish_gap_min=1,
                              speed=1.0)
    ctl.run_id = 1
    ctl.db = db
    ctl.logs = []
    ctl.log = lambda msg, level="info", account_id=None: ctl.logs.append(msg)
    ctl._wait_if_paused = lambda: None
    ctl._sleep_interruptible = lambda s: None
    ctl._draft_count = lambda aid: 0
    ctl._posted_count = lambda aid: 0
    ctl._next_product = lambda idx: (7, {"id": 7})
    ctl._record_failed = lambda account, pid, payload, why: None
    ctl._inter_post_delay = lambda: None
    ctl._rotator = SimpleNamespace(
        payload=lambda product, cfg, as_draft, account_id=None: {
            "as_draft": as_draft, "title": "T", "price": 5,
            "location": "L", "image_paths": []})
    return ctl


def main():
    # 1. Registry.
    keys = [k for k, _ in E.ACTIONS]
    assert "draft_publish_1" in keys, keys
    assert "draft_publish" not in keys, keys
    assert ("draft_publish_1", "Draft & Publish 1") in E.ACTIONS
    assert "draft_publish" in E.MODES and "draft_publish_1" in E.MODES
    assert "draft_publish_1" in E.POSTING_MODES
    print("1. registry: Draft & Publish 1 visible, old mode still valid: PASS")

    account = {"id": 9, "label": "Acct"}

    # 2. draft_publish_1, quantity 1, end to end.
    db = ShimDB()
    ctl = make_controller(db, "draft_publish_1")
    drv = SimpleNamespace(create_draft=lambda p: "777001",
                          publish_draft=lambda fid: True,
                          _last_error=None)
    ctl._draft_publish_1_flow(account, drv, quota=1, account_idx=0)
    rows = db.fetchall("SELECT * FROM listings")
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "posted", rows
    assert rows[0]["fb_listing_id"] == "777001", rows
    assert db.fetchone("SELECT * FROM runs WHERE id = 1")["total_posted"] == 1
    print("2. draft_publish_1 qty 1: draft saved, SAME draft published: PASS")

    # 3. draft_publish_1 publish failure -> preserved draft.
    db = ShimDB()
    ctl = make_controller(db, "draft_publish_1")
    drv = SimpleNamespace(create_draft=lambda p: "777002",
                          publish_draft=lambda fid: False,
                          _last_error="card not found")
    ctl._draft_publish_1_flow(account, drv, quota=1, account_idx=0)
    rows = db.fetchall("SELECT * FROM listings")
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "draft", rows
    assert rows[0]["fb_listing_id"] == "777002", rows
    assert rows[0]["error"].startswith("publish pending:"), rows
    assert any("PUBLISH PENDING" in m for m in ctl.logs), ctl.logs
    print("3. draft_publish_1 publish failure -> draft preserved: PASS")

    # 4. Old draft_publish flow still works unchanged.
    db = ShimDB()
    ctl = make_controller(db, "draft_publish")
    drv = SimpleNamespace(create_draft=lambda p: "888002",
                          publish_draft=lambda fid: True,
                          _last_error=None)
    ctl._draft_publish_flow(account, drv, quota=1, account_idx=0)
    rows = db.fetchall("SELECT * FROM listings")
    assert len(rows) == 1 and rows[0]["status"] == "posted", rows
    assert rows[0]["fb_listing_id"] == "888002", rows
    print("4. old draft_publish flow intact (create + publish): PASS")

    # 5. as_draft only for the two draft_publish modes.
    db = ShimDB()
    ctl = make_controller(db, "draft_publish_1")
    assert ctl._rotation({"id": 7}, 0)["as_draft"] is True
    ctl.cfg.mode = "draft_publish"
    assert ctl._rotation({"id": 7}, 0)["as_draft"] is True
    ctl.cfg.mode = "create_drafts"
    assert ctl._rotation({"id": 7}, 0)["as_draft"] is False
    print("5. _rotation as_draft gated to draft_publish modes only: PASS")

    print("ALL v0.2.127 DRAFT & PUBLISH 1 TESTS PASS")


if __name__ == "__main__":
    main()

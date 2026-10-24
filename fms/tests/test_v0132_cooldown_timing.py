"""v0.2.132 targeted test — image-cooldown timing changes, exactly as
supplied (no test-side patching of the shipped code).

Against a real in-memory SQLite DB through the real controller
methods, with stub drivers:
  1. Draft & Publish 1 batch: saving a draft records every selected
     image in account_image_usage IMMEDIATELY (before any publish).
  2. Shared _publish_draft_rows (old Draft & Publish + Publish
     Drafts path): publishing a draft NO LONGER records image usage
     (the .132 patch removed that block). Documented, not hidden:
     drafts published through this path now leave no cooldown record.
  3. _publish_draft_rows_1 (Draft & Publish 1 path): publishing
     STILL records image usage — so a _1 draft's images are recorded
     at save AND again at publish (upsert refreshes last_used_at to
     publish time).
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
            CREATE TABLE image_usage (
                product_id INTEGER, image_path TEXT, use_count INTEGER,
                PRIMARY KEY (product_id, image_path));
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


class StubDriver:
    def __init__(self, draft_id=None, publish_ok=True):
        self._draft_id = draft_id
        self._publish_ok = publish_ok
        self.publish_calls = []
    def create_draft(self, payload):
        return self._draft_id
    def publish_draft(self, draft_id):
        self.publish_calls.append(draft_id)
        return self._publish_ok


def make_controller(db):
    ctl = E.RunController.__new__(E.RunController)
    ctl.cfg = SimpleNamespace(mode="draft_publish_1", tabs_draft_publish=2,
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
    ctl._rotation = lambda product, idx, account_id=None: {
        "title": "Oak Chair", "price": 5, "location": "L",
        "image_paths": ["/img/a.jpg", "/img/b.jpg"]}
    return ctl


def usage(db, aid=21):
    return db.fetchall(
        "SELECT image_path FROM account_image_usage WHERE account_id = ?",
        (aid,))


def main():
    account = {"id": 21, "label": "A"}

    # 1. _1 batch records images at save time.
    db = ShimDB()
    ctl = make_controller(db)
    created = ctl._create_drafts_batch_1(account, StubDriver("draft-title:Oak Chair"), 1, 0)
    assert created == 1, created
    rows = usage(db)
    assert sorted(r["image_path"] for r in rows) == ["/img/a.jpg", "/img/b.jpg"], rows
    print("1. _1 batch: images recorded at save (before publish): PASS")

    # 2. Shared _publish_draft_rows no longer records at publish.
    db = ShimDB()
    ctl = make_controller(db)
    db.execute(
        "INSERT INTO listings (run_id, account_id, product_id, title_used,"
        " price_used, location_used, images_used, status, fb_listing_id)"
        " VALUES (1, 21, 7, 'Oak Chair', 5, 'L', ?, 'draft', '555')",
        ('["/img/a.jpg", "/img/b.jpg"]',))
    row = db.fetchone("SELECT * FROM listings WHERE id = 1")
    ctl._publish_draft_rows(account, StubDriver(publish_ok=True), [row])
    assert usage(db) == [], usage(db)
    assert db.fetchone("SELECT status FROM listings WHERE id = 1")["status"] == "posted"
    print("2. shared publish path: NO cooldown record at publish (patch behaviour): PASS")

    # 3. _publish_draft_rows_1 still records at publish.
    db = ShimDB()
    ctl = make_controller(db)
    db.execute(
        "INSERT INTO listings (run_id, account_id, product_id, title_used,"
        " price_used, location_used, images_used, status, fb_listing_id)"
        " VALUES (1, 21, 7, 'Oak Chair', 5, 'L', ?, 'draft', 'draft-title:Oak Chair')",
        ('["/img/a.jpg", "/img/b.jpg"]',))
    row = db.fetchone("SELECT * FROM listings WHERE id = 1")
    ctl._publish_draft_rows_1(account, StubDriver(publish_ok=True), [row])
    rows = usage(db)
    assert sorted(r["image_path"] for r in rows) == ["/img/a.jpg", "/img/b.jpg"], rows
    print("3. _1 publish path: cooldown record STILL written at publish: PASS")

    print("ALL v0.2.132 COOLDOWN TIMING TESTS PASS")


if __name__ == "__main__":
    main()

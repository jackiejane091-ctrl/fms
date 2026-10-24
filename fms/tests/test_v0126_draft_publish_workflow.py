"""v0.2.126 targeted test — Draft & Publish publish-failure preservation.

Asserts the corrected _publish_draft_rows behavior:
  1. A saved draft whose publish is NOT confirmed keeps status 'draft',
     keeps its exact fb_listing_id, and records the driver's real
     _last_error as a 'publish pending: ...' error (never 'failed').
  2. The preserved row is still returned by _draft_rows (so a later
     publish attempt retries the SAME draft instead of recreating it),
     and the publish log line names the draft and its ID.
  3. A confirmed publish still flips the row to 'posted' and bumps the
     run's total_posted (unchanged behavior).
Uses a real in-memory SQLite DB behind a minimal execute/fetchall shim
with the same call shape as the app DB.
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
                product_id INTEGER, title_used TEXT, status TEXT,
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


def make_controller(db):
    ctl = E.RunController.__new__(E.RunController)
    ctl.cfg = SimpleNamespace(publish_mode="together", publish_gap_min=1,
                              speed=1.0)
    ctl.run_id = 1
    ctl.db = db
    ctl.logs = []
    ctl.log = lambda msg, level="info", account_id=None: ctl.logs.append(msg)
    ctl._wait_if_paused = lambda: None
    ctl._sleep_interruptible = lambda s: None
    return ctl


def seed(db, row_id, fb_id, title):
    db.execute(
        "INSERT INTO listings (id, run_id, account_id, product_id, "
        "title_used, status, fb_listing_id, images_used) "
        "VALUES (?, 1, 9, 3, ?, 'draft', ?, '[]')",
        (row_id, title, fb_id))


def main():
    db = ShimDB()
    ctl = make_controller(db)
    account = {"id": 9, "label": "Acct"}

    # 1) Publish NOT confirmed -> row stays 'draft', ID preserved,
    #    error carries the driver's real reason.
    seed(db, 1, "555000111", "Oak Chair")
    drv_fail = SimpleNamespace(
        publish_draft=lambda fid: False,
        _last_error="Publish button not found in draft card for 555000111")
    rows = ctl._draft_rows(9)
    assert len(rows) == 1 and rows[0]["fb_listing_id"] == "555000111", rows
    ctl._publish_draft_rows(account, drv_fail, rows)
    row = db.fetchone("SELECT * FROM listings WHERE id = 1")
    assert row["status"] == "draft", row
    assert row["fb_listing_id"] == "555000111", row
    assert row["error"].startswith("publish pending:"), row
    assert "Publish button not found" in row["error"], row
    assert any("PUBLISH PENDING" in m and "555000111" in m
               for m in ctl.logs), ctl.logs
    print("1. unconfirmed publish -> status 'draft', ID + real reason kept: PASS")

    # 2) The SAME row is still selectable as a draft for a retry —
    #    nothing to recreate, and it is not 'failed'/'posted'.
    rows2 = ctl._draft_rows(9)
    assert len(rows2) == 1 and rows2[0]["id"] == 1, rows2
    rows_any = ctl._draft_rows(9, any_run=True)
    assert len(rows_any) == 1 and rows_any[0]["fb_listing_id"] == "555000111"
    print("2. preserved draft re-selectable for retry (same run + any run): PASS")

    # 3) Confirmed publish -> 'posted', total_posted bumped (unchanged).
    drv_ok = SimpleNamespace(publish_draft=lambda fid: True, _last_error=None)
    ctl._publish_draft_rows(account, drv_ok, rows2)
    row = db.fetchone("SELECT * FROM listings WHERE id = 1")
    assert row["status"] == "posted", row
    run = db.fetchone("SELECT * FROM runs WHERE id = 1")
    assert run["total_posted"] == 1, run
    # And a posted row is no longer returned as a draft (no republish).
    assert ctl._draft_rows(9, any_run=True) == []
    print("3. confirmed publish -> 'posted', counter bumped, not re-listed: PASS")

    print("ALL v0.2.126 WORKFLOW TESTS PASS")


if __name__ == "__main__":
    main()

"""SQLite storage for FMS (Phase 1 tables).

Thread-safe via a re-entrant lock; uses WAL mode. All secrets are stored
encrypted (see fms.crypto) — this module never sees plaintext secrets.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    locations TEXT NOT NULL DEFAULT '[]',
    apply_fingerprint INTEGER NOT NULL DEFAULT 1,
    loc_change_mode TEXT NOT NULL DEFAULT 'do_not_change',
    loc_change_location TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS proxies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    username TEXT,
    password_enc TEXT,
    country TEXT,
    working INTEGER,            -- 1 yes, 0 no, NULL unknown
    last_check TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER REFERENCES groups(id) ON DELETE CASCADE,
    label TEXT NOT NULL,         -- email or phone number
    password_enc TEXT,
    cookie_enc TEXT,             -- optional cookie login
    twofa_secret_enc TEXT,       -- optional TOTP secret
    proxy_id INTEGER REFERENCES proxies(id) ON DELETE SET NULL,
    browser TEXT NOT NULL DEFAULT 'chrome',
    active INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',  -- active|inactive|banned|locked
    status_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    base_price REAL,
    description TEXT,
    tags TEXT,
    category TEXT,
    attributes_json TEXT,        -- category-specific attributes, JSON object
    image_folder TEXT,
    video_path TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS titles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    value TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    value TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
    mode TEXT NOT NULL,          -- complete | draft_publish
    settings_json TEXT NOT NULL,
    config_hash TEXT,
    status TEXT NOT NULL,        -- pending|running|paused|interrupted|completed|failed|stopped
    total_planned INTEGER NOT NULL DEFAULT 0,
    total_posted INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    title_used TEXT,
    price_used REAL,
    location_used TEXT,
    images_used TEXT,            -- JSON list of paths
    status TEXT NOT NULL,        -- posted|failed|draft
    fb_listing_id TEXT,
    error TEXT,
    posted_at TEXT
);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    level TEXT NOT NULL,         -- info|warning|error
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scripts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    blocks_json TEXT NOT NULL,  -- JSON list of action blocks
    shuffle INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_tabs (    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    tab_ref TEXT NOT NULL,      -- driver tab handle
    title_used TEXT,
    status TEXT NOT NULL DEFAULT 'filled',  -- filled|published|skipped|failed
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS group_products (
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, product_id)
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- v0.2.46: FBM Robo style board — remembers when the user last pressed
-- Refresh per group, so a completed run's final Done/Failed board stays
-- visible until explicitly cleared.
CREATE TABLE IF NOT EXISTS board_refresh (
    group_id INTEGER PRIMARY KEY REFERENCES groups(id) ON DELETE CASCADE,
    refreshed_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Thin thread-safe wrapper around a single sqlite3 connection."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else config.db_path()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Additive migrations for DBs created by older FMS versions."""

        def _has_column(table: str, column: str) -> bool:
            rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            return any(r["name"] == column for r in rows)

        if not _has_column("products", "scraped_json"):
            self._conn.execute("ALTER TABLE products ADD COLUMN scraped_json TEXT")
        if not _has_column("runs", "script_id"):
            self._conn.execute(
                "ALTER TABLE runs ADD COLUMN script_id INTEGER REFERENCES scripts(id)"
            )
        # Alignment pass #2: per-account IXBrowser profile id (empty = auto).
        if not _has_column("accounts", "ix_profile_id"):
            self._conn.execute("ALTER TABLE accounts ADD COLUMN ix_profile_id TEXT")
        # Alignment pass #4: full product dialog (listing type, condition,
        # availability, append-title-count, main/extra image folders,
        # delivery options).
        for _col, _ddl in [
            ("listing_type", "ALTER TABLE products ADD COLUMN listing_type TEXT NOT NULL DEFAULT 'item'"),
            ("condition", "ALTER TABLE products ADD COLUMN condition TEXT"),
            ("availability", "ALTER TABLE products ADD COLUMN availability TEXT"),
            ("append_title_count", "ALTER TABLE products ADD COLUMN append_title_count INTEGER NOT NULL DEFAULT 0"),
            ("main_image_folder", "ALTER TABLE products ADD COLUMN main_image_folder TEXT"),
            ("extra_image_folder", "ALTER TABLE products ADD COLUMN extra_image_folder TEXT"),
            ("extra_image_count", "ALTER TABLE products ADD COLUMN extra_image_count INTEGER NOT NULL DEFAULT 0"),
            ("delivery_options_json", "ALTER TABLE products ADD COLUMN delivery_options_json TEXT"),
        ]:
            if not _has_column("products", _col):
                self._conn.execute(_ddl)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS descriptions (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                   value TEXT NOT NULL,
                   position INTEGER NOT NULL DEFAULT 0)"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS image_usage (
                   product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                   image_path TEXT NOT NULL,
                   use_count INTEGER NOT NULL DEFAULT 0,
                   PRIMARY KEY (product_id, image_path))"""
        )
        # v0.2.25: FBM Robo-style General Settings per group.
        for _col, _ddl in [
            ("locations", "ALTER TABLE groups ADD COLUMN locations TEXT NOT NULL DEFAULT '[]'"),
            ("apply_fingerprint", "ALTER TABLE groups ADD COLUMN apply_fingerprint INTEGER NOT NULL DEFAULT 1"),
            ("loc_change_mode", "ALTER TABLE groups ADD COLUMN loc_change_mode TEXT NOT NULL DEFAULT 'do_not_change'"),
            ("loc_change_location", "ALTER TABLE groups ADD COLUMN loc_change_location TEXT NOT NULL DEFAULT ''"),
        ]:
            if not _has_column("groups", _col):
                self._conn.execute(_ddl)
        # Alignment pass #5: per-account listing-number verification results.
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS listing_counts (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                   account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                   expected INTEGER NOT NULL DEFAULT 0,
                   found INTEGER NOT NULL DEFAULT 0,
                   checked_at TEXT NOT NULL)"""
        )

    # -- low-level ---------------------------------------------------------
    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def executemany(self, sql: str, seq: list[tuple]) -> None:
        with self._lock:
            self._conn.executemany(sql, seq)
            self._conn.commit()

    def fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- logs --------------------------------------------------------------
    def log(
        self,
        message: str,
        level: str = "info",
        run_id: int | None = None,
        account_id: int | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO logs (run_id, account_id, level, message, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (run_id, account_id, level, message, utcnow()),
        )

    # -- JSON helpers ------------------------------------------------------
    @staticmethod
    def to_json(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False)

    @staticmethod
    def from_json(text: str | None) -> Any:
        return json.loads(text) if text else None

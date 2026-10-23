"""Listing Engine: wave-batched, resumable, pausable listing runs.

WAVE SEMANTICS (user-confirmed): batch size N means N browser contexts open at
once, each on a DIFFERENT account; each context completes its account's FULL
assigned listing count; only when the whole wave finishes does the next wave
start with N new accounts.
"""
from __future__ import annotations

import collections
import hashlib
import heapq
import itertools
from pathlib import Path
import json
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from . import accounts as accounts_mod
from . import config
from . import human
from . import scripts as scripts_mod
from . import products as products_mod
from .browser import BaseDriver, MockDriver, PlaywrightDriver
from .db import Database, utcnow
from .human import TYPING_MODES, TYPING_PRESETS

# ------------------------------------------------------------------ config ---
# v6.2.0.0 exact action list (order matters — matches the competitor UI).
# "Wait For Next Turn" left the main dropdown in v6.2.0.0; it now lives only
# in the script builder's OTHER category, but stays a valid run mode so old
# runs/API calls keep working.
ACTIONS = [
    # The sole visible Post action now uses the existing Test Single Post flow.
    ("test_single_post", "Post"),
    ("relist_edit", "Relist & Edit"),
    ("draft_publish", "Draft & Publish"),
    ("create_drafts", "Create Drafts"),
    ("publish_drafts", "Publish Drafts"),
    ("browser_launch_fast", "Browser Launch Fast"),
    ("browser_launch", "Browser Launch"),
    ("delete_duplicate", "Delete Duplicate"),
    ("delete_previous", "Delete Previous"),
    ("delete_draft", "Delete Draft"),
    ("delete_take_action", "Delete Take Action Listings"),
    ("delete_all_invalid", "Delete All Invalid Listings"),
    ("renew_listing", "Renew Listing"),
    ("delete_relist", "Delete & Relist"),
    ("login", "Login"),
    ("download_images_titles", "Download Images & Titles"),
    ("change_location", "Change Marketplace Location"),
    ("scrap_data", "Scrap Data"),
]
# Keep the former complete mode valid for saved/older runs and API clients,
# but do not expose it as a separate visible Post action.
MODES = (tuple(key for key, _ in ACTIONS)
         + ("complete", "manual_publish", "script", "wait_next_turn"))
# Modes that create listings and therefore need product datasets.
POSTING_MODES = {"complete", "test_single_post", "draft_publish", "manual_publish",
                 "relist_edit", "create_drafts", "publish_drafts", "delete_relist"}
PUBLISH_MODES = ("all_together", "one_by_one")


class _LoginFailed(Exception):
    """Raised when an account's login fails (drives the retry loop)."""


# v0.2.28: real Playwright wordings for a dead/closed browser vary
# ("Browser closed", "Target closed", "Browser has been closed",
#  "Target crashed", ...). A closed browser must mark the account Failed
# immediately — never reopen+retry.
_BROWSER_CLOSED_HINTS = (
    "target closed",
    "browser closed",
    "browser has been closed",
    "context closed",
    "context has been closed",
    "target crashed",
    "browser crashed",
    "browser disconnected",
)


def _browser_closed_msg(msg: str | None) -> bool:
    m = (msg or "").lower()
    return any(h in m for h in _BROWSER_CLOSED_HINTS)


def _local_time(iso: str | None) -> str:
    """Render a UTC ISO timestamp in the laptop's local time (v0.2.30)."""
    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%d %b, %H:%M")
    except Exception:
        return iso


@dataclass
class RunConfig:
    group_id: int
    product_ids: list[int] = field(default_factory=list)
    mode: str = "complete"          # one of MODES (see ACTIONS for the 18)
    script_id: int | None = None    # required when mode == "script"
    batch_size: int = 2             # wave size: N parallel browser contexts
    min_ads: int = 1                # per-account listing quota range
    max_ads: int = 3
    renew_min_count: int = 1        # renew listing count range (user's
    renew_max_count: int = 5        # lower/upper limit)
    min_price: float | None = None   # None -> use product base_price
    max_price: float | None = None
    min_delay_s: float = 2.0         # delay between posts on one account
    max_delay_s: float = 5.0
    min_seconds_to_post: float = 25.0  # per-listing soft timing target
    max_seconds_to_post: float = 40.0  # per-listing soft timing target; never a deadline
    shuffle: bool = False           # shuffle account order each run
    split_locations: bool = False   # each account gets a different location slice
    condition: str = "New"
    availability: str = "In Stock"
    schedule_at: str | None = None  # ISO datetime; run waits until then
    publish_mode: str = "all_together"  # draft_publish/script: all_together | one_by_one
    publish_gap_min: float = 5.0    # gap between publishes (one_by_one only)
    activity_scale: float = 1.0     # test hook: scales activity durations
    # -- Alignment (v6.1.2.0) settings -------------------------------------
    hide_from_friends: bool = False  # listing visibility flag
    account_start_gap_s: float = 5.0  # delay between starting accounts in a wave
    max_parallel_browsers: int = 5   # hard cap on parallel browser contexts
    retry_count: int = 3             # retries per account before giving up
    tabs_without_limit: int = 2      # total reusable tabs; sequential publishing only
    tabs_draft_publish: int = 6      # draft batch size for draft & publish
    action_urls: list[str] = field(default_factory=list)  # scrap_data / download_images_titles
    scrap_include_description: bool = True   # scrap_data: also copy description
    scrap_include_location: bool = True      # scrap_data: also copy location
    download_folder: str | None = None       # download_images_titles target dir
    new_location: str | None = None  # change_location target
    wait_minutes: float = 0          # wait_next_turn mode: guaranteed rest
    # -- v0.2.25: FBM Robo General Settings per group ---------------------------
    group_locations: list[str] = field(default_factory=list)
    group_apply_fingerprint: bool = True
    group_loc_change_mode: str = "do_not_change"
    group_loc_change_location: str = ""
    exclude_labels: list[str] = field(default_factory=list)  # skip these accounts
    speed: float = 1.0               # 1x | 1.5x | 2x run speed multiplier
    # -- Alignment pass #2: typing mode + optional IXBrowser -------------------
    typing_mode: str = "human"       # human | fast_js | clipboard
    typing_preset: str = "normal"    # fast | normal | careful (delay scale)
    min_delay_ms: int = 30           # human-like typing: per-char delay range
    max_delay_ms: int = 80
    thinking_pause_min_ms: int = 200
    thinking_pause_max_ms: int = 600
    extra_delay_after_punctuation_ms: int = 80
    newline_pause_min_ms: int = 300
    newline_pause_max_ms: int = 900
    hybrid_threshold_chars: int = 500  # human mode: type head, paste rest above
    ix_browser_enabled: bool = False   # anti-detect via IXBrowser Local API
    ix_local_api_host: str = "127.0.0.1"
    ix_local_api_port: int = 53200

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"Unknown mode: {self.mode}")
        if not (1 <= self.batch_size <= 10):
            raise ValueError("batch_size must be 1..10")
        if not (1 <= self.max_parallel_browsers <= 10):
            raise ValueError("max_parallel_browsers must be 1..10")
        if self.retry_count < 0 or self.retry_count > 10:
            raise ValueError("retry_count must be 0..10")
        if self.min_ads < 0 or self.max_ads < self.min_ads:
            raise ValueError("bad min_ads/max_ads")
        if self.mode == "script":
            if not self.script_id:
                raise ValueError("script mode requires script_id")
        elif self.mode in POSTING_MODES and not self.product_ids:
            raise ValueError("at least one product is required")
        if self.publish_mode not in PUBLISH_MODES:
            raise ValueError(f"Unknown publish_mode: {self.publish_mode}")
        if self.speed <= 0:
            raise ValueError("speed must be positive")
        # -- Alignment pass #2 validation --------------------------------------
        if self.typing_mode not in TYPING_MODES:
            raise ValueError(f"Unknown typing_mode: {self.typing_mode}")
        if self.typing_preset not in TYPING_PRESETS:
            raise ValueError(f"Unknown typing_preset: {self.typing_preset}")
        for name in ("min_delay_ms", "max_delay_ms", "thinking_pause_min_ms",
                     "thinking_pause_max_ms", "extra_delay_after_punctuation_ms",
                     "newline_pause_min_ms", "newline_pause_max_ms"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.min_delay_ms > self.max_delay_ms:
            raise ValueError("min_delay_ms must be <= max_delay_ms")
        if self.hybrid_threshold_chars < 1:
            raise ValueError("hybrid_threshold_chars must be >= 1")
        if not (1 <= self.ix_local_api_port <= 65535):
            raise ValueError("ix_local_api_port must be 1..65535")

    def effective_parallelism(self) -> int:
        """Wave size honoring both batch size and the browser cap."""
        return max(1, min(self.batch_size, self.max_parallel_browsers))

    def to_dict(self) -> dict:
        return {
            "group_id": self.group_id, "product_ids": list(self.product_ids),
            "mode": self.mode, "script_id": self.script_id,
            "batch_size": self.batch_size,
            "min_ads": self.min_ads, "max_ads": self.max_ads,
            "renew_min_count": self.renew_min_count,
            "renew_max_count": self.renew_max_count,
            "min_price": self.min_price, "max_price": self.max_price,
            "min_delay_s": self.min_delay_s, "max_delay_s": self.max_delay_s,
            "shuffle": self.shuffle, "split_locations": self.split_locations,
            "condition": self.condition, "availability": self.availability,
            "schedule_at": self.schedule_at,
            "publish_mode": self.publish_mode,
            "publish_gap_min": self.publish_gap_min,
            "activity_scale": self.activity_scale,
            "hide_from_friends": self.hide_from_friends,
            "account_start_gap_s": self.account_start_gap_s,
            "max_parallel_browsers": self.max_parallel_browsers,
            "retry_count": self.retry_count,
            "tabs_without_limit": self.tabs_without_limit,
            "tabs_draft_publish": self.tabs_draft_publish,
            "action_urls": list(self.action_urls),
            "new_location": self.new_location,
            "wait_minutes": self.wait_minutes,
            "scrap_include_description": self.scrap_include_description,
            "scrap_include_location": self.scrap_include_location,
            "download_folder": self.download_folder,
            "exclude_labels": list(self.exclude_labels),
            "speed": self.speed,
            "typing_mode": self.typing_mode,
            "typing_preset": self.typing_preset,
            "min_delay_ms": self.min_delay_ms,
            "max_delay_ms": self.max_delay_ms,
            "thinking_pause_min_ms": self.thinking_pause_min_ms,
            "thinking_pause_max_ms": self.thinking_pause_max_ms,
            "extra_delay_after_punctuation_ms":
                self.extra_delay_after_punctuation_ms,
            "newline_pause_min_ms": self.newline_pause_min_ms,
            "newline_pause_max_ms": self.newline_pause_max_ms,
            "hybrid_threshold_chars": self.hybrid_threshold_chars,
            "ix_browser_enabled": self.ix_browser_enabled,
            "ix_local_api_host": self.ix_local_api_host,
            "ix_local_api_port": self.ix_local_api_port,
        }

    def config_hash(self) -> str:
        core = {
            "group_id": self.group_id,
            "product_ids": sorted(self.product_ids),
            "mode": self.mode,
            "script_id": self.script_id,
            "min_ads": self.min_ads, "max_ads": self.max_ads,
            "renew_min_count": self.renew_min_count,
            "renew_max_count": self.renew_max_count,
            "min_price": self.min_price, "max_price": self.max_price,
            "publish_mode": self.publish_mode,
        }
        return hashlib.sha256(
            json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]


class _StopRun(Exception):
    pass


def _activity_minutes(p: dict, scale: float) -> float:
    """Resolve an activity block's duration in (scaled) minutes.

    New schema (v6.2.0.0): random duration between min_minutes and
    max_minutes — or min_seconds/max_seconds for the seconds-based blocks
    (Read Notifications, Scroll Messages). A 0/0 range means the activity
    is skipped. Legacy params ("minutes", "seconds") are still honored
    when the new keys are unset.
    """
    lo = float(p.get("min_minutes") or 0)
    hi = float(p.get("max_minutes") or 0)
    if lo > 0 or hi > 0:
        lo, hi = min(lo, hi), max(lo, hi)
        return random.uniform(lo, hi) * scale
    slo = float(p.get("min_seconds") or 0)
    shi = float(p.get("max_seconds") or 0)
    if slo > 0 or shi > 0:
        slo, shi = min(slo, shi), max(slo, shi)
        return random.uniform(slo, shi) / 60.0 * scale
    if p.get("seconds"):
        return float(p["seconds"]) / 60.0 * scale
    return float(p.get("minutes") or 0) * scale


def _chance01(p: dict, pct_key: str, legacy_key: str | None) -> float | None:
    """Resolve a chance param to a 0-1 probability.

    New schema stores percentages (0-100, as shown in the settings dialogs);
    legacy params were already 0-1 probabilities.
    """
    v = p.get(pct_key)
    if v is not None:
        try:
            return max(0.0, min(1.0, float(v) / 100.0))
        except (TypeError, ValueError):
            return None
    if legacy_key:
        v = p.get(legacy_key)
        if v is not None:
            try:
                return max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                return None
    return None


def _insert_no_product_listing(db: Database, run_id: int, account: dict,
                               listing_id: str, status: str,
                               title: str = "") -> None:
    """Record a delete/renew record: no product or payload involved."""
    db.execute(
        """INSERT INTO listings
           (run_id, account_id, product_id, title_used, status,
            fb_listing_id, posted_at)
           VALUES (?, ?, NULL, ?, ?, ?, ?)""",
        (run_id, account["id"], title, status, listing_id, utcnow()),
    )


# -------------------------------------------------------------- controller ---
def _driver_creds(db: Database, cfg: "RunConfig", account: dict) -> dict:
    """Build driver credentials, injecting run-level typing + IXBrowser
    config (alignment pass #2). The driver reads these from the account
    dict — no driver signature changes needed."""
    creds = accounts_mod.account_credentials(db, account)
    creds["typing"] = {
        "mode": cfg.typing_mode,
        "preset": cfg.typing_preset,
        "min_delay_ms": cfg.min_delay_ms,
        "max_delay_ms": cfg.max_delay_ms,
        "thinking_pause_min_ms": cfg.thinking_pause_min_ms,
        "thinking_pause_max_ms": cfg.thinking_pause_max_ms,
        "extra_delay_after_punctuation_ms":
            cfg.extra_delay_after_punctuation_ms,
        "newline_pause_min_ms": cfg.newline_pause_min_ms,
        "newline_pause_max_ms": cfg.newline_pause_max_ms,
        "hybrid_threshold_chars": cfg.hybrid_threshold_chars,
    }
    creds["ixbrowser"] = {
        "enabled": cfg.ix_browser_enabled,
        "host": cfg.ix_local_api_host,
        "port": cfg.ix_local_api_port,
        "profile_id": creds.get("ix_profile_id"),
    }
    # v0.2.25: per-group "Apply Fingerprint" (FBM Robo General Settings).
    creds["apply_fingerprint"] = bool(getattr(cfg, "group_apply_fingerprint", True))
    return creds


def _persist_ix_profile(db: Database, account: dict, driver) -> None:
    """Save an auto-created IXBrowser profile id back to the account."""
    pid = getattr(driver, "account", {}).get("ix_profile_id_created")
    if pid:
        accounts_mod.set_ix_profile_id(db, account["id"], pid)


def _record_published_image_usage(db: Database, account_id: int, product_id: int | None,
                                  image_paths: list[str] | None) -> None:
    """Persist image cooldown only after Facebook confirms a listing publish.

    Main and extra photos are treated identically. Paths are canonicalized so
    the same file cannot bypass the cooldown through relative-path aliases.
    """
    from pathlib import Path as _Path
    images = list(dict.fromkeys(str(p) for p in (image_paths or []) if p))
    if not images:
        return
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for path in images:
        canonical = str(_Path(path).expanduser().resolve())
        db.execute(
            "INSERT INTO account_image_usage(account_id, image_path, last_used_at) VALUES (?, ?, ?) "
            "ON CONFLICT(account_id, image_path) DO UPDATE SET last_used_at=excluded.last_used_at",
            (int(account_id), canonical, now),
        )
        if product_id is not None:
            products_mod.record_image_usage(db, int(product_id), [path])


def _record_test_single_post_images_if_confirmed(
        db: Database, account_id: int, product_id: int | None,
        payload: dict, report: dict) -> bool:
    """Legacy helper for callers that explicitly require confirmed publication."""
    if report.get("publish_success_confirmed") is not True:
        return False
    _record_published_image_usage(
        db, account_id, product_id, payload.get("image_paths"))
    return True


class _Rotator:
    """Per-run rotation state: titles, descriptions, locations, images.

    Mirrors the real tool's product dialog: multi titles / descriptions /
    locations rotate per listing; Main Images always come first, Extra
    Images rotate with an offset; Append Title Count appends the product
    title N times at the END of the description (optional, 0 = off);
    image usage is tracked per product so the Manage Products table can
    show "used / total".
    """

    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.Lock()
        self._title_cycles: dict[int, Any] = {}
        self._desc_cycles: dict[int, Any] = {}
        self._loc_cycles: dict[int, Any] = {}
        self._seq = 0

    def _cooldown_days(self) -> int:
        row = self.db.fetchone("SELECT value FROM app_settings WHERE key = ?", ("skip_images_days",))
        try:
            return max(0, int((row["value"] if row else "8") or "8"))
        except (TypeError, ValueError):
            return 8

    def _eligible_images(self, paths: list[str], account_id: int, cooldown_days: int):
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=cooldown_days)).isoformat()
        eligible = []
        seen = set()
        for path in paths:
            if not path:
                continue
            canonical = str(Path(path).expanduser().resolve())
            if canonical in seen:
                continue
            seen.add(canonical)
            used = self.db.fetchone(
                "SELECT last_used_at FROM account_image_usage WHERE account_id = ? AND image_path = ?",
                (int(account_id), canonical),
            )
            if not used or not used["last_used_at"] or used["last_used_at"] < cutoff:
                eligible.append((path, canonical))
        return eligible

    def payload(self, product: dict, cfg: "RunConfig", as_draft: bool, account_id: int | None = None) -> dict:
        # Scraped products post from their scraped items (cycled), not from
        # the title/location lists.
        scraped = product.get("scraped_items")
        if scraped:
            with self._lock:
                seq = self._seq
                self._seq += 1
            item = scraped[seq % len(scraped)]
            raw_images = list(item.get("photo_paths") or [])[:10]
            image_paths = raw_images
            if account_id is not None and raw_images:
                try:
                    cooldown_days = self._cooldown_days()
                    eligible = self._eligible_images(raw_images, int(account_id), cooldown_days)
                    image_paths = [p for p, _canonical in eligible[:10]]
                except Exception as exc:
                    image_paths = []
                    print(f"[IMAGE COOLDOWN] Could not select scraped images for account {account_id}: {exc}", flush=True)
            return {
                "title": item.get("title"),
                "price": item.get("price") or product.get("base_price"),
                "description": item.get("description") or "",
                "category": item.get("category") or product.get("category"),
                "attributes": product.get("attributes") or {},
                "condition": product.get("condition") or cfg.condition,
                "availability": item.get("availability")
                or product.get("availability") or cfg.availability,
                "location": item.get("location"),
                "tags": product.get("tags"),
                "listing_type": product.get("listing_type") or "item",
                "delivery_options": product.get("delivery_options") or [],
                "image_paths": image_paths,
                "video_path": product.get("video_path"),
                "as_draft": as_draft,
                "hide_from_friends": cfg.hide_from_friends,
            }
        with self._lock:
            pid = product["id"]
            if pid not in self._title_cycles:
                titles = product["titles"] or [product["name"]]
                self._title_cycles[pid] = itertools.cycle(titles)
            if pid not in self._desc_cycles:
                descs = product["descriptions"] or [product.get("description") or ""]
                self._desc_cycles[pid] = itertools.cycle(descs)
            if pid not in self._loc_cycles:
                # v0.2.25: the group's General Settings locations take
                # precedence over the product's own locations.
                locs = (cfg.group_locations or product["locations"]) or [None]
                self._loc_cycles[pid] = itertools.cycle(locs)
            title = next(self._title_cycles[pid])
            description = next(self._desc_cycles[pid])
            location = next(self._loc_cycles[pid])
            seq = self._seq
            self._seq += 1

        if product.get("tags"):
            description = (description + "\n" + product["tags"]).strip()

        # Append Title Count: the title is repeated N times at the END of
        # the description (confirmed 2026-10-15). Optional — 0 = off.
        append_n = int(product.get("append_title_count") or 0)
        if append_n > 0:
            description = (description + "\n"
                           + "\n".join([title] * append_n)).strip()

        if cfg.min_price is not None and cfg.max_price is not None:
            price = round(random.uniform(cfg.min_price, cfg.max_price), 2)
        else:
            price = product.get("base_price")

        main_images = product.get("main_images") or []
        extra_images = product.get("extra_images") or []
        try:
            requested_extra_count = max(0, int(product.get("extra_image_count") or 0))
        except (TypeError, ValueError):
            requested_extra_count = 0
        image_paths: list[str] = []
        if account_id is not None:
            try:
                cooldown_days = self._cooldown_days()
                eligible_main = self._eligible_images(list(main_images), int(account_id), cooldown_days)
                eligible_extra = self._eligible_images(list(extra_images), int(account_id), cooldown_days)
                # Preserve the intended order: one main image first when available,
                # then eligible extra-folder images, then other eligible main images.
                # If all main photos are cooling down, use an eligible extra photo
                # rather than reusing a recent main photo or silently blocking publish.
                if eligible_main:
                    main_offset = seq % len(eligible_main)
                    rotated_main = eligible_main[main_offset:] + eligible_main[:main_offset]
                    first = rotated_main[:1]
                    remaining_main = rotated_main[1:]
                else:
                    first = eligible_extra[:1]
                    remaining_main = []
                    eligible_extra = eligible_extra[1:] if first else eligible_extra
                extra_capacity = min(requested_extra_count, 9, max(0, 10 - len(first)))
                selected_extras = eligible_extra[:extra_capacity]
                remaining_capacity = min(extra_capacity - len(selected_extras), max(0, 10 - len(first) - len(selected_extras)))
                selected_more_main = remaining_main[:remaining_capacity]
                chosen = first + selected_extras + selected_more_main
                image_paths = [item[0] for item in chosen]
                # IMPORTANT: selection does not write cooldown history. The image is
                # recorded only by the successful-publish paths below.
                if not image_paths and (main_images or extra_images):
                    print(f"[IMAGE COOLDOWN] No eligible photos for account {account_id}; continuing listing without photos rather than reusing a photo inside the cooldown.", flush=True)
            except Exception as exc:
                image_paths = []
                print(f"[IMAGE COOLDOWN] Could not select eligible images for account {account_id}: {exc}; continuing listing without photos.", flush=True)
        else:
            # Legacy non-account callers retain simple rotation, now including
            # the separate Extra Images folder.
            all_images = list(dict.fromkeys(list(main_images) + list(extra_images)))
            if all_images:
                offset = seq % len(all_images)
                all_images = all_images[offset:] + all_images[:offset]
                image_paths = all_images[:min(1 + requested_extra_count, 10, len(all_images))]

        if product.get("tags"):
            description = (description + "\n" + product["tags"]).strip()

        return {
            "title": title,
            "price": price,
            "description": description,
            "category": product.get("category"),
            "attributes": product.get("attributes") or {},
            "condition": product.get("condition") or cfg.condition,
            "availability": product.get("availability") or cfg.availability,
            "location": location,
            "tags": product.get("tags"),
            "listing_type": product.get("listing_type") or "item",
            "delivery_options": product.get("delivery_options") or [],
            "image_paths": image_paths,
            "video_path": product.get("video_path"),
            "as_draft": as_draft,
            "hide_from_friends": cfg.hide_from_friends,
            # Per-listing soft timing targets; settings are user-configurable.
            # Max is not a deadline and never stops a slow listing workflow.
            "min_seconds_to_post": max(0.0, float(getattr(
                cfg, "min_seconds_to_post", 25))),
            "max_seconds_to_post": max(0.0, float(getattr(
                cfg, "max_seconds_to_post", 40))),
        }


class RunController:
    """Owns one run's background thread, pause/stop signals and rotation state."""

    def __init__(
        self,
        db: Database,
        run_id: int,
        cfg: RunConfig,
        driver_factory: Callable[[dict], BaseDriver],
    ):
        self.db = db
        self.run_id = run_id
        self.cfg = cfg
        self.driver_factory = driver_factory
        self.pause_event = threading.Event()
        self.pause_event.set()          # set = running; cleared = paused
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._thread_main,
                                       name=f"fms-run-{run_id}", daemon=True)
        self._rot_lock = threading.Lock()
        self._rotator = _Rotator(db)
        self._product_cycle = itertools.cycle(cfg.product_ids)
        self._signals: dict[str, threading.Event] = {}
        # Slot-refill queue state (v0.2.23, FBM Robo style): batch_size
        # workers pull accounts from _queue; a freed slot immediately takes
        # the next pending account. _acct_state maps account_id ->
        # 'pending' | 'in_progress' | 'done_posting' | 'failed'.
        self._q_lock = threading.RLock()  # v0.2.43: _mark_failed nests
        self._queue: collections.deque = collections.deque()
        self._acct_state: dict[int, str] = {}
        self._acct_label: dict[int, str] = {}
        # v0.2.57: tracks which accounts' browsers FMS closed itself
        # (vs the user closing them) for clear log messages.
        self._closed_by_fms: dict[int, bool] = {}
        self._all_accounts: list = []

    def queue_state(self) -> dict:
        """Live board state for the dashboard (FBM Robo style)."""
        with self._q_lock:
            return {
                "pending": [self._acct_label[i] for i, s in self._acct_state.items()
                            if s == "pending"],
                "in_progress": [self._acct_label[i] for i, s in self._acct_state.items()
                                if s == "in_progress"],
                "done_posting": [self._acct_label[i] for i, s in self._acct_state.items()
                                 if s == "done_posting"],
                "failed": [self._acct_label[i] for i, s in self._acct_state.items()
                           if s == "failed"],
                "pending_ids": [i for i, s in self._acct_state.items() if s == "pending"],
                "in_progress_ids": [i for i, s in self._acct_state.items() if s == "in_progress"],
                "done_posting_ids": [i for i, s in self._acct_state.items() if s == "done_posting"],
                "failed_ids": [i for i, s in self._acct_state.items() if s == "failed"],
            }

    def restart_account(self, account_id: int) -> bool:
        """Right-click -> Restart: move a done/failed account back to pending."""
        with self._q_lock:
            if account_id not in self._acct_state:
                return False
            if self._acct_state[account_id] == "in_progress":
                return False  # already working; stop it first
            if self._acct_state[account_id] == "pending":
                return True
            self._acct_state[account_id] = "pending"
            # Rebuild queue preserving original order, restarted account last.
            acc = next((a for a in self._all_accounts if a["id"] == account_id), None)
            if acc is not None:
                self._queue.append(acc)
            # v0.2.43: clear the persisted failed flag so a later resume
            # doesn't push this manually-restarted account back to Failed.
            self._unmark_failed_persisted(account_id)
            return True

    def _failed_ids_persisted(self) -> list:
        try:
            row = self.db.fetchone("SELECT settings_json FROM runs WHERE id=?",
                                   (self.run_id,))
            settings = json.loads(row["settings_json"]) if row else {}
            return list(settings.get("failed_account_ids", []))
        except Exception:
            return []

    def _mark_failed_persisted(self, account_id: int) -> None:
        """Remember a failed account in the run's settings so pause/continue
        (or resume after restart) never silently re-queues it. v0.2.43:
        failed stays failed until the user manually restarts it."""
        try:
            row = self.db.fetchone("SELECT settings_json FROM runs WHERE id=?",
                                   (self.run_id,))
            settings = json.loads(row["settings_json"]) if row else {}
            failed_ids = settings.get("failed_account_ids", [])
            if account_id not in failed_ids:
                failed_ids.append(account_id)
                settings["failed_account_ids"] = failed_ids
                self.db.execute("UPDATE runs SET settings_json=? WHERE id=?",
                                (json.dumps(settings), self.run_id))
        except Exception:
            pass

    def _unmark_failed_persisted(self, account_id: int) -> None:
        try:
            row = self.db.fetchone("SELECT settings_json FROM runs WHERE id=?",
                                   (self.run_id,))
            settings = json.loads(row["settings_json"]) if row else {}
            failed_ids = settings.get("failed_account_ids", [])
            if account_id in failed_ids:
                failed_ids.remove(account_id)
                settings["failed_account_ids"] = failed_ids
                self.db.execute("UPDATE runs SET settings_json=? WHERE id=?",
                                (json.dumps(settings), self.run_id))
        except Exception:
            pass

    def _on_browser_closed_by_user(self, account_id: int, label: str) -> None:
        """The browser window was closed — flip the account to Failed RIGHT
        NOW (v0.2.49). Previously the worker only noticed on its next
        Playwright call, leaving the account stuck in In Progress for 8-10s.
        Only acts when the account is still in_progress (normal cleanup after
        a finished account is ignored). Idempotent with the worker's own
        exception handling.
        v0.2.57: distinguishes WHO closed it — the user (× button) vs FMS
        itself (auto-close). The driver sets _closed_by_fms=True right before
        FMS closes it; otherwise it's the user."""
        with self._q_lock:
            if self._acct_state.get(account_id) != "in_progress":
                return
            self._acct_state[account_id] = "failed"
        try:
            by_fms = getattr(self, "_closed_by_fms", {}).get(account_id, False)
            # Clear the flag so a later user-close isn't misattributed.
            try:
                self._closed_by_fms.pop(account_id, None)
            except Exception:
                pass
            if by_fms:
                self.log(f"[{label}] stopped by FMS (browser auto-closed)",
                         level="error", account_id=account_id)
            else:
                self.log(f"[{label}] stopped by user (browser closed manually)",
                         level="error", account_id=account_id)
        except Exception:
            pass

    def _mark_failed(self, account_id: int) -> None:
        """Set state=failed in memory AND persist it for resume."""
        with self._q_lock:
            self._acct_state[account_id] = "failed"
        self._mark_failed_persisted(account_id)

    def _save_final_board(self) -> None:
        """Snapshot the board when the run ends so the dashboard keeps
        showing Done/Failed (FBM Robo style) until the user presses
        Refresh — instead of everything silently flipping back to Pending."""
        try:
            with self._q_lock:
                final = {str(i): s for i, s in self._acct_state.items()}
                labels = {str(i): self._acct_label.get(i, "")
                          for i in self._acct_state}
            row = self.db.fetchone("SELECT settings_json FROM runs WHERE id=?",
                                   (self.run_id,))
            settings = json.loads(row["settings_json"]) if row else {}
            settings["final_board"] = final
            settings["final_board_labels"] = labels
            self.db.execute("UPDATE runs SET settings_json=? WHERE id=?",
                            (json.dumps(settings), self.run_id))
        except Exception:
            pass

    # -- public controls -------------------------------------------------------
    def signal(self, name: str) -> None:
        """Dashboard-initiated signal (e.g. 'download_start'): wakes a worker
        that is waiting in wait_for_signal()."""
        ev = self._signals.setdefault(name, threading.Event())
        ev.set()

    def wait_for_signal(self, name: str, poll_s: float = 2.0) -> bool:
        """Block until signal(name) arrives; False if the run was stopped."""
        ev = self._signals.setdefault(name, threading.Event())
        while not self.stop_event.is_set():
            if ev.wait(timeout=poll_s):
                return True
        return False

    # -- public controls -------------------------------------------------------
    def start(self) -> None:
        self.thread.start()

    def pause(self) -> None:
        self.pause_event.clear()
        self.db.execute("UPDATE runs SET status = 'paused' WHERE id = ?",
                        (self.run_id,))

    def resume(self) -> None:
        self.db.execute("UPDATE runs SET status = 'running' WHERE id = ?",
                        (self.run_id,))
        self.pause_event.set()

    def stop(self) -> None:
        self.stop_event.set()
        self.pause_event.set()  # unblock a paused worker so it can exit

    # -- internals --------------------------------------------------------------
    def log(self, message: str, level: str = "info", account_id: int | None = None) -> None:
        self.db.log(message, level=level, run_id=self.run_id, account_id=account_id)

    def _wait_if_paused(self) -> None:
        while not self.pause_event.is_set():
            if self.stop_event.is_set():
                raise _StopRun()
            time.sleep(0.2)
        if self.stop_event.is_set():
            raise _StopRun()

    def _sleep_interruptible(self, seconds: float) -> None:
        end = time.time() + max(0.0, seconds)
        while time.time() < end:
            self._wait_if_paused()
            time.sleep(0.2)

    def _rotation(self, product: dict, account_idx: int, account_id: int | None = None) -> dict:
        """Build one listing payload with rotated title/price/location/images."""
        _payload = self._rotator.payload(
            product, self.cfg, as_draft=self.cfg.mode == "draft_publish", account_id=account_id)
        # v0.2.70: LOG runtime config via self.log() so it appears in FMS Logs.
        # Sanitized: folder path truncated, no secrets.
        # Browser invokes this only between atomic UI actions; an in-flight
        # field action is allowed to finish before Pause All takes effect.
        _payload["_pause_checkpoint"] = self._wait_if_paused
        try:
            _pid = product.get("id", "?")
            _img_folder = str(product.get("image_folder") or "")[:50]
            _imgs = _payload.get("image_paths") or []
            self.log(f"[CONFIG] product_id={_pid} price={_payload.get('price')} "
                     f"condition={_payload.get('condition')} "
                     f"category={_payload.get('category')}")
            self.log(f"[CONFIG] image_folder={_img_folder} images={len(_imgs)}")
        except Exception:
            pass
        return _payload

    def _posted_count(self, account_id: int) -> int:
        row = self.db.fetchone(
            """SELECT COUNT(*) AS c FROM listings
               WHERE run_id = ? AND account_id = ? AND status = 'posted'""",
            (self.run_id, account_id),
        )
        return row["c"] if row else 0

    def _failed_count(self, account_id: int) -> int:
        row = self.db.fetchone(
            """SELECT COUNT(*) AS c FROM listings
               WHERE run_id = ? AND account_id = ? AND status = 'failed'""",
            (self.run_id, account_id),
        )
        return row["c"] if row else 0

    def _process_account(self, account: dict, quota: int, account_idx: int) -> str:
        """Process one account with retries (Retry Count Before Quitting).

        Returns 'done', 'failed', 'login_failed' or 'stopped' (v0.2.23).
        """
        label = account["label"]
        attempts = 1 + max(0, int(self.cfg.retry_count))
        for attempt in range(1, attempts + 1):
            try:
                driver = self.driver_factory(_driver_creds(self.db, self.cfg,
                                                           account))
                # Shared Pause All callback for browser tasks that contain their
                # own cooperative action loops (scrolling, notifications, etc.).
                try:
                    driver._pause_checkpoint_callback = self._wait_if_paused
                    binder = getattr(driver, "_bind_pause_checkpoint", None)
                    if callable(binder):
                        binder()
                except Exception:
                    pass
                # v0.2.49: instant Failed the moment the user closes the
                # browser window (no waiting for the worker's next call).
                try:
                    driver._on_close_callback = (
                        lambda: self._on_browser_closed_by_user(
                            account["id"], label))
                except Exception:
                    pass
            except Exception as exc:
                # v0.2.35: a broken stored credential (bad decrypt, corrupt
                # data) used to kill the whole worker thread silently — the
                # run "completed" and accounts sat looking Pending forever.
                # Now the account goes to Failed with a clear reason.
                self.log(f"[{label}] credential error: "
                         f"{type(exc).__name__}: {exc} — marked failed",
                         level="error", account_id=account["id"])
                return "failed"
            try:
                self._process_account_once(account, driver, quota, account_idx)
                _persist_ix_profile(self.db, account, driver)
                self._log_account_summary(account, quota)
                return "done"
            except _LoginFailed:
                reason = getattr(driver, "login_error", "") or "unknown reason"
                # v0.2.28: browser closed during login -> Failed at once,
                # no reopen+retry loop.
                # v0.2.57: say WHO closed it (user vs FMS).
                if _browser_closed_msg(reason):
                    _by_fms = self._closed_by_fms.pop(account["id"], False)
                    _who = "stopped by FMS" if _by_fms else "stopped by user"
                    self.log(f"[{label}] {_who} (browser closed during login) — "
                             f"marked failed (no retry)", level="error",
                             account_id=account["id"])
                    self._log_account_summary(account, quota)
                    # v0.2.69: mark failed BEFORE finally so browser stays open.
                    self._acct_state[account["id"]] = "failed"
                    return "failed"
                if attempt >= attempts:
                    # v0.2.39: no more permanent locking (user asked for it
                    # simple). The failure is logged and the account lands
                    # in Failed; the next Start tries it again.
                    self.log(f"[{label}] login FAILED after {attempts} "
                             f"attempt(s): {reason}",
                             level="error", account_id=account["id"])
                    self._log_account_summary(account, quota)
                    return "login_failed"
                self.log(f"[{label}] login failed "
                         f"(attempt {attempt}/{attempts}) — retrying",
                         level="warning", account_id=account["id"])
                self._sleep_interruptible(2.0)
            except _StopRun:
                self.log(f"[{label}] stopped by user", level="warning",
                         account_id=account["id"])
                self._log_account_summary(account, quota)
                return "stopped"
            except Exception as exc:  # never let one account kill the run
                msg = str(exc)
                # v0.2.24/v0.2.28: the user closed the browser window (or it
                # died). Don't reopen+retry — mark the account failed straight
                # away so it lands in Failed, not Pending.
                if _browser_closed_msg(msg):
                    self.log(f"[{label}] browser closed — marked failed "
                             f"(no retry)", level="error",
                             account_id=account["id"])
                    self._log_account_summary(account, quota)
                    # v0.2.69: mark failed BEFORE finally so browser stays open.
                    self._acct_state[account["id"]] = "failed"
                    return "failed"
                if attempt >= attempts:
                    self.log(f"[{label}] ERROR: {type(exc).__name__}: {exc}",
                             level="error", account_id=account["id"])
                    self._log_account_summary(account, quota)
                    # v0.2.69: mark failed BEFORE finally so browser stays open.
                    self._acct_state[account["id"]] = "failed"
                    return "failed"
                self.log(f"[{label}] error on attempt {attempt}/{attempts} "
                         f"({type(exc).__name__}) — retrying",
                         level="warning", account_id=account["id"])
                self._sleep_interruptible(2.0)
            finally:
                try:
                    # Browser Launch leaves the browser open for manual work.
                    # v0.2.57: also leave it open when the account FAILED —
                    # the user wants to see what went wrong, not find a
                    # closed browser. Only auto-close on success.
                    # The user's request: log WHO closed it — "stopped by user"
                    # vs "stopped by FMS".
                    _state = self._acct_state.get(account["id"])
                    _failed = _state in ("failed",)
                    if not getattr(driver, "keep_open", False) and not _failed:
                        # Mark it so the close-handler logs "stopped by FMS".
                        try:
                            self._closed_by_fms[account["id"]] = True
                        except Exception:
                            pass
                        driver.close()
                    elif _failed:
                        self.log(f"[{label}] browser left open (account failed — "
                                 f"close it yourself when done inspecting)",
                                 level="warning", account_id=account["id"])
                except Exception:
                    pass

    def _process_account_once(self, account: dict, driver, quota: int,
                              account_idx: int) -> None:
        label = account["label"]
        self.log(f"[{label}] logging in…", account_id=account["id"])
        if not driver.login():
            raise _LoginFailed(label)
        # v0.2.25: "Change Marketplace Location Before Listing — Change only
        # once": set the group's location once right after login.
        if (self.cfg.group_loc_change_mode == "change_once"
                and self.cfg.group_loc_change_location):
            target = self.cfg.group_loc_change_location
            ok = driver.set_marketplace_location(target)
            self.log(f"[{label}] marketplace location -> '{target}': "
                     f"{'OK' if ok else 'FAILED'}",
                     level="info" if ok else "error",
                     account_id=account["id"])
        mode = self.cfg.mode
        if mode == "draft_publish":
            self._draft_publish_flow(account, driver, quota, account_idx)
        elif mode == "manual_publish":
            self._manual_publish_flow(account, driver, quota, account_idx)
        elif mode == "relist_edit":
            self._relist_flow(account, driver, quota, account_idx)
        elif mode == "delete_relist":
            self._delete_relist_flow(account, driver, quota, account_idx)
        elif mode == "create_drafts":
            self._create_drafts_only_flow(account, driver, quota, account_idx)
        elif mode == "publish_drafts":
            self._publish_drafts_only_flow(account, driver)
        elif mode == "delete_duplicate":
            self._delete_duplicates_flow(account, driver)
        elif mode == "delete_previous":
            self._delete_previous_flow(account, driver)
        elif mode == "delete_draft":
            self._delete_draft_flow(account, driver)
        elif mode == "delete_take_action":
            self._delete_take_action_flow(account, driver)
        elif mode == "delete_all_invalid":
            self._delete_all_invalid_flow(account, driver)
        elif mode == "renew_listing":
            self._renew_flow(account, driver, quota)
        elif mode == "login":
            self.log(f"[{label}] login verified — no posting in Login mode",
                     account_id=account["id"])
        elif mode == "download_images_titles":
            self._download_assets_flow(account, driver)
        elif mode in ("browser_launch", "browser_launch_fast"):
            # Identical in the real tool (confirmed 2026-10-15): log in,
            # then leave the browser open for manual work.
            driver.keep_open = True
            self.log(f"[{label}] browser launched — left open for manual work",
                     account_id=account["id"])
        elif mode == "change_location":
            self._change_location_flow(account, driver)
        elif mode == "wait_next_turn":
            self._wait_mode_flow(account)
        elif mode == "scrap_data":
            self._scrap_flow(account, driver, account_idx)
        elif mode == "test_single_post":
            self._test_single_flow(account, driver, account_idx)
        else:
            self._complete_flow(account, driver, quota, account_idx)
        # v0.2.62: log the video recording path so the user can send it
        # for visual debugging (user's request — see the real issue).
        try:
            _vdir = None
            import fms.config as _cfg
            # v0.2.63: videos now in D:\FMS\videos (not AppData).
            _vdir = _cfg.debug_dir() / "videos" / f"account_{account['id']}"
            if _vdir.exists():
                _vids = sorted(_vdir.glob("*.webm"), key=lambda x: x.stat().st_mtime)
                if _vids:
                    self.log(f"[{label}] video: {_vids[-1]}",
                             account_id=account["id"])
        except Exception:
            pass
        self.log(f"[{label}] account finished", account_id=account["id"])

    # -- new modes (alignment pass #5, v6.2.0.0) ---------------------------------
    def _delete_relist_flow(self, account: dict, driver, quota: int,
                            account_idx: int) -> None:
        """Delete the account's live listings, then immediately relist each
        with a fresh rotated payload."""
        label = account["label"]
        targets = [l for l in driver.get_my_listings() if l.get("id")]
        self.log(f"[{label}] delete & relist: {len(targets)} live listing(s)",
                 account_id=account["id"])
        n = 0
        for l in targets:
            self._wait_if_paused()
            lid = l["id"]
            if not driver.delete_listing(lid):
                self.log(f"[{label}] could not delete {lid} — skipping relist",
                         level="warning", account_id=account["id"])
                continue
            self._record_no_product(account, lid, "deleted", l.get("title", ""))
            pid, product = self._next_product(account_idx)
            if product is None:
                self.log(f"[{label}] product vanished — listing stays deleted",
                         level="warning", account_id=account["id"])
                continue
            payload = self._rotation(product, account_idx, account_id=account["id"])
            new_id = driver.post_listing(payload)
            if new_id:
                self._record_posted(account, pid, payload, new_id,
                                    status="relisted")
                n += 1
            else:
                self._record_failed(account, pid, payload,
                                    "relist post failed")
            self._inter_post_delay()
        self.log(f"[{label}] delete & relist done: {n} relisted",
                 account_id=account["id"])


    # -- shared helpers ---------------------------------------------------------
    def _next_product(self, account_idx: int):
        self._wait_if_paused()
        with self._rot_lock:
            pid = next(self._product_cycle)
        product = products_mod.get_product(self.db, pid)
        return pid, product

    def _record_posted(self, account: dict, pid: int, payload: dict,
                       listing_id: str, status: str = "posted") -> None:
        label = account["label"]
        self.db.execute(
            """INSERT INTO listings
               (run_id, account_id, product_id, title_used, price_used,
                location_used, images_used, status, fb_listing_id, posted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (self.run_id, account["id"], pid, payload["title"],
             payload["price"], payload["location"],
             json.dumps(payload["image_paths"]),
             status, listing_id, utcnow()),
        )
        if status in ("posted", "relisted"):
            _record_published_image_usage(self.db, account["id"], pid, payload.get("image_paths"))
        if status == "posted":
            self.db.execute(
                "UPDATE runs SET total_posted = total_posted + 1 WHERE id = ?",
                (self.run_id,),
            )
        self.log(f"[{label}] {status} '{payload['title']}'"
                 + (f" @ {payload['location']}" if payload["location"] else ""),
                 account_id=account["id"])

    def _record_failed(self, account: dict, pid: int, payload: dict,
                       error: str) -> None:
        label = account["label"]
        self.db.execute(
            """INSERT INTO listings
               (run_id, account_id, product_id, title_used, price_used,
                location_used, images_used, status, error, posted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'failed', ?, ?)""",
            (self.run_id, account["id"], pid, payload["title"],
             payload["price"], payload["location"],
             json.dumps(payload["image_paths"]), error, utcnow()),
        )
        self.log(f"[{label}] FAILED '{payload['title']}': {error}",
                 level="error", account_id=account["id"])

    def _log_account_summary(self, account: dict, quota: int) -> None:
        """Per-account run summary in the Logs box (v0.2.30).

        The user asked: when an ID is clicked, the logs must show how many
        listings succeeded (ad title, location name, time posted), how many
        failed (with the error/reason), and how many remain.
        """
        label = account["label"]
        aid = account["id"]
        rows = self.db.fetchall(
            """SELECT title_used, location_used, fb_listing_id, error,
                      posted_at, status
               FROM listings WHERE run_id = ? AND account_id = ?
               ORDER BY id""",
            (self.run_id, aid),
        )
        posted = [r for r in rows if r["status"] == "posted"]
        failed = [r for r in rows if r["status"] == "failed"]
        self.log(f"[{label}] ===== SUMMARY =====", account_id=aid)
        self.log(f"[{label}] Successful: {len(posted)}", account_id=aid)
        for r in posted:
            self.log(f"[{label}]   + '{r['title_used']}' | location: "
                     f"{r['location_used'] or '-'} | listing: "
                     f"{r['fb_listing_id'] or '-'} | time: "
                     f"{_local_time(r['posted_at'])}", account_id=aid)
        self.log(f"[{label}] Failed: {len(failed)}",
                 level="error" if failed else "info", account_id=aid)
        for r in failed:
            self.log(f"[{label}]   x '{r['title_used']}' | wajah: "
                     f"{r['error'] or '?'}", level="error", account_id=aid)
        if self.cfg.mode == "complete":
            remaining = max(0, quota - len(posted) - len(failed))
            self.log(f"[{label}] Baqi (remaining): {remaining}", account_id=aid)
        self.log(f"[{label}] ===================", account_id=aid)

    def _inter_post_delay(self) -> None:
        # Run speed multiplies the pace: 2x halves every delay.
        speed = max(0.1, self.cfg.speed)
        delay = random.uniform(self.cfg.min_delay_s, self.cfg.max_delay_s) / speed
        self._sleep_interruptible(delay)

    def _record_no_product(self, account: dict, listing_id: str,
                           status: str, title: str = "") -> None:
        """Record a delete/renew record: no product or payload involved."""
        _insert_no_product_listing(self.db, self.run_id, account, listing_id,
                                   status, title)

    # -- mode: complete -----------------------------------------------------------
    def _complete_flow(self, account: dict, driver, quota: int,
                       account_idx: int) -> None:
        label = account["label"]
        remaining = quota - self._posted_count(account["id"])
        self.log(f"[{label}] logged in, {remaining} listing(s) to post",
                 account_id=account["id"])
        # v0.2.33: human warm-up — browse the feed briefly before the first
        # post, like a real person would (anti "suspected automated behavior").
        if remaining > 0:
            try:
                self.log(f"[{label}] warming up (browsing feed)…",
                         account_id=account["id"])
                driver.human_warmup()
            except Exception:
                pass
        # Tabs Without Limit is a finite tab pool for time-saving, sequential
        # work. A listing is processed completely before the next tab is used.
        try:
            tab_count = max(1, int(self.cfg.tabs_without_limit or 1))
        except (TypeError, ValueError):
            tab_count = 1
        tab_count = min(tab_count, 20)
        if hasattr(driver, "prepare_listing_tabs"):
            prepared = driver.prepare_listing_tabs(tab_count)
            tab_count = max(1, int(prepared or 1))
            self.log(f"[{label}] prepared {tab_count} listing tab(s); sequential mode",
                     account_id=account["id"])
        for i in range(remaining):
            if hasattr(driver, "activate_listing_tab"):
                driver.activate_listing_tab(i % tab_count)
            if remaining > tab_count and i % tab_count == 0:
                self.log(f"[{label}] tab cycle: listing {i + 1}/{remaining}, "
                         f"tab 1/{tab_count}", account_id=account["id"])
            self._wait_if_paused()
            pid, product = self._next_product(account_idx)
            if product is None:
                self.log(f"[{label}] product {pid} vanished — skipping",
                         level="warning", account_id=account["id"])
                continue
            payload = self._rotation(product, account_idx, account_id=account["id"])
            # v0.2.25: "Change Marketplace Location Before Listing — Change
            # Everytime": set the group's location before every post.
            if (self.cfg.group_loc_change_mode == "change_everytime"
                    and self.cfg.group_loc_change_location):
                driver.set_marketplace_location(
                    self.cfg.group_loc_change_location)
            listing_id = driver.post_listing(payload)
            if listing_id:
                self._record_posted(account, pid, payload, listing_id)
            else:
                # v0.2.29: surface the REAL failure reason (error + page URL)
                # so the per-account logs say where posting actually died.
                err = (getattr(driver, "_last_error", None)
                       or "driver returned no listing id")
                self._record_failed(account, pid, payload, err)
            self._inter_post_delay()
            # v0.2.24: notice Stop/Pause promptly after each listing instead
            # of only before the next one.
            self._wait_if_paused()

    # -- mode: test_single_post (multi-listing queue, v0.2.112) -----------------
    def _test_single_flow(self, account: dict, driver, account_idx: int) -> None:
        """Process each selected product independently using the configured
        Min/Max Listings target. Reuse the existing browser/tab and publishing
        routine; never mix product payloads or rotate into another product's
        details while fulfilling a product's own target.
        """
        from . import single_post_test as _spt
        label = account["label"]
        aid = account["id"]
        try:
            driver.keep_open = True
        except Exception:
            pass
        try:
            tab_count = max(1, int(self.cfg.tabs_without_limit or 1))
        except (TypeError, ValueError):
            tab_count = 1
        tab_count = min(tab_count, 20)
        if hasattr(driver, "prepare_listing_tabs"):
            prepared = driver.prepare_listing_tabs(tab_count)
            tab_count = max(1, int(prepared or 1))
            self.log(f"[{label}] prepared {tab_count} listing tab(s); sequential mode",
                     account_id=aid)

        product_ids = list(self.cfg.product_ids or [])
        if not product_ids:
            self.log(f"[{label}] Test Single Post: no product selected — select a product first",
                     level="error", account_id=aid)
            return

        total_targets = 0
        total_attempts = 0
        tab_sequence_index = 0
        for pid in product_ids:
            self._wait_if_paused()
            product = products_mod.get_product(self.db, pid)
            if product is None:
                self.log(f"[{label}] product {pid} not found — skipping this product",
                         level="error", account_id=aid)
                continue

            # The same configured range applies independently to every
            # selected product. Equal min/max produces a fixed target.
            target = random.randint(int(self.cfg.min_ads), int(self.cfg.max_ads))
            total_targets += target
            product_name = product.get("name") or f"product {pid}"
            self.log(f"[{label}] Product '{product_name}' (id={pid}): target={target} listing(s); using only this product's saved details",
                     account_id=aid)
            product_attempts = 0

            for listing_index in range(target):
                self._wait_if_paused()
                if hasattr(driver, "activate_listing_tab"):
                    # Round-robin tabs; one listing finishes before the next starts.
                    driver.activate_listing_tab(tab_sequence_index % tab_count)
                tab_sequence_index += 1
                # Re-fetch product for each listing so current saved settings
                # are used, then build its own payload (including price range).
                product = products_mod.get_product(self.db, pid)
                if product is None:
                    self.log(f"[{label}] product {pid} disappeared — stopping its queue",
                             level="error", account_id=aid)
                    break
                payload = self._rotation(product, account_idx, account_id=account["id"])
                self.log(f"[{label}] Product '{product_name}' listing {listing_index + 1}/{target}: publishing",
                         account_id=aid)
                # Record the selected images immediately when the Facebook Publish
                # button is clicked. The callback is invoked synchronously by
                # single_post_test.py at the click point, without waiting for a
                # Facebook success toast, URL, or listing-card confirmation.
                def _on_publish_clicked(_aid=int(aid), _pid=int(pid), _payload=payload, _label=label):
                    try:
                        _record_published_image_usage(
                            self.db, _aid, _pid, _payload.get("image_paths"))
                        self.log(
                            f"[{_label}] Publish clicked: recorded "
                            f"{len(_payload.get('image_paths') or [])} image(s) "
                            f"for the {self._rotator._cooldown_days()}-day account cooldown",
                            account_id=_aid)
                    except Exception as _record_exc:
                        self.log(
                            f"[{_label}] Publish was clicked but image cooldown recording failed: "
                            f"{type(_record_exc).__name__}: {_record_exc}",
                            level="error", account_id=_aid)
                        raise
                payload["_on_publish_clicked"] = _on_publish_clicked
                try:
                    report = _spt.run_single_post_test(driver, payload)
                except Exception as exc:
                    self.log(f"[{label}] Product '{product_name}' listing {listing_index + 1}/{target} crashed: {type(exc).__name__}: {exc}",
                             level="error", account_id=aid)
                    break

                # Cooldown is recorded synchronously at the Publish click via callback above.

                for row in report.get("fields", []):
                    status = row.get("status", "?")
                    lvl = "error" if status == "FAILED" else "info"
                    self.log(f"[{label}] product={pid} listing={listing_index + 1}/{target} | {row.get('field')}: {status} | {row.get('detail', '')}",
                             level=lvl, account_id=aid)

                publish_attempted = bool(report.get("publish_attempted"))
                publish_status = next((r.get("status") for r in report.get("fields", [])
                                       if r.get("field") == "publish_click"), "NOT_REPORTED")
                self.log(f"[{label}] product={pid} listing={listing_index + 1}/{target}: publish_attempted={publish_attempted}, publish_status={publish_status}, summary={report.get('summary', {})}",
                         account_id=aid)
                if not publish_attempted:
                    self.log(f"[{label}] product={pid}: stopping this product's queue because Publish was not reached",
                             level="error", account_id=aid)
                    break

                product_attempts += 1
                total_attempts += 1
                if listing_index + 1 < target:
                    self._inter_post_delay()

            self.log(f"[{label}] Product '{product_name}' finished: publish attempts {product_attempts}/{target}",
                     account_id=aid)

        self.log(f"[{label}] Multi-listing queue finished: attempts={total_attempts}, assigned_targets={total_targets}, selected_products={len(product_ids)}",
                 account_id=aid)

    # -- mode: draft_publish (full two-phase flow) ----------------------------------
    def _draft_count(self, account_id: int) -> int:
        row = self.db.fetchone(
            """SELECT COUNT(*) AS c FROM listings
               WHERE run_id = ? AND account_id = ? AND status = 'draft'""",
            (self.run_id, account_id),
        )
        return row["c"] if row else 0

    def _create_drafts_batch(self, account: dict, driver, quota: int,
                               account_idx: int, chunk: int | None = None) -> int:
        """Create drafts up to quota. Returns the number created.

        Drafts are created in tab-groups of `chunk` (Tabs Draft And Publish),
        mirroring the real tool opening N tabs at a time.
        """
        label = account["label"]
        # Drafts already made + already published both count toward the quota,
        # so resume-after-interruption never re-creates them.
        todo = max(0, quota - self._draft_count(account["id"])
                   - self._posted_count(account["id"]))
        if chunk is None:
            chunk = max(1, int(self.cfg.tabs_draft_publish))
        created = 0
        while created < todo:
            group = min(chunk, todo - created)
            self.log(f"[{label}] draft batch: {group} tab(s)",
                     account_id=account["id"])
            for _ in range(group):
                pid, product = self._next_product(account_idx)
                if product is None:
                    continue
                payload = self._rotation(product, account_idx, account_id=account["id"])
                draft_id = driver.create_draft(payload)
                if draft_id:
                    self.db.execute(
                        """INSERT INTO listings
                           (run_id, account_id, product_id, title_used, price_used,
                            location_used, images_used, status, fb_listing_id, posted_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
                        (self.run_id, account["id"], pid, payload["title"],
                         payload["price"], payload["location"],
                         json.dumps(payload["image_paths"]), draft_id, utcnow()),
                    )
                    self.log(f"[{label}] drafted '{payload['title']}'",
                             account_id=account["id"])
                    created += 1
                else:
                    self._record_failed(account, pid, payload,
                                        "draft creation failed")
                self._inter_post_delay()
        return created

    def _publish_draft_rows(self, account: dict, driver,
                            drafts: list[dict]) -> None:
        """Publish given draft rows (all-together or one-by-one with gap)."""
        label = account["label"]
        mode = self.cfg.publish_mode
        gap_s = self.cfg.publish_gap_min * 60.0 / max(0.1, self.cfg.speed)
        self.log(f"[{label}] publish phase: {len(drafts)} draft(s), mode={mode}",
                 account_id=account["id"])
        for i, d in enumerate(drafts):
            self._wait_if_paused()
            if mode == "one_by_one" and i > 0:
                self.log(f"[{label}] waiting {self.cfg.publish_gap_min} min "
                         f"before next publish", account_id=account["id"])
                self._sleep_interruptible(gap_s)
            ok = driver.publish_draft(d["fb_listing_id"])
            if ok:
                self.db.execute(
                    "UPDATE listings SET status = 'posted', posted_at = ? "
                    "WHERE id = ?",
                    (utcnow(), d["id"]),
                )
                self.db.execute(
                    "UPDATE runs SET total_posted = total_posted + 1 WHERE id = ?",
                    (self.run_id,),
                )
                try:
                    _draft_images = json.loads(d["images_used"] or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    _draft_images = []
                _record_published_image_usage(self.db, account["id"], d["product_id"], _draft_images)
                self.log(f"[{label}] published '{d['title_used']}'",
                         account_id=account["id"])
            else:
                self.db.execute(
                    "UPDATE listings SET status = 'failed', "
                    "error = 'publish failed' WHERE id = ?",
                    (d["id"],),
                )
                self.log(f"[{label}] FAILED to publish '{d['title_used']}'",
                         level="error", account_id=account["id"])

    def _draft_rows(self, account_id: int, any_run: bool = False) -> list[dict]:
        """Draft rows for an account: this run only, or any run (standalone)."""
        if any_run:
            return self.db.fetchall(
                """SELECT id, fb_listing_id, title_used, product_id, images_used FROM listings
                   WHERE account_id = ? AND status = 'draft' ORDER BY id""",
                (account_id,))
        return self.db.fetchall(
            """SELECT id, fb_listing_id, title_used, product_id, images_used FROM listings
               WHERE run_id = ? AND account_id = ? AND status = 'draft'
               ORDER BY id""",
            (self.run_id, account_id),
        )

    # -- mode: draft_publish (full two-phase flow) ----------------------------------
    def _draft_publish_flow(self, account: dict, driver, quota: int,
                            account_idx: int) -> None:
        """Phase 1: create drafts (tab closes as each draft completes).
        Phase 2: publish them — all together, or one-by-one with a gap."""
        self._create_drafts_batch(account, driver, quota, account_idx)
        self._publish_draft_rows(account, driver,
                                 self._draft_rows(account["id"]))

    # -- mode: create_drafts (drafts only, no publish) ------------------------------
    def _create_drafts_only_flow(self, account: dict, driver, quota: int,
                                 account_idx: int) -> None:
        label = account["label"]
        n = self._create_drafts_batch(account, driver, quota, account_idx)
        self.log(f"[{label}] created {n} draft(s) — publish them later",
                 account_id=account["id"])

    # -- mode: publish_drafts (publish drafts from any earlier run) -----------------
    def _publish_drafts_only_flow(self, account: dict, driver) -> None:
        drafts = self._draft_rows(account["id"], any_run=True)
        if not drafts:
            self.log(f"[{account['label']}] no drafts to publish",
                     account_id=account["id"])
            return
        self._publish_draft_rows(account, driver, drafts)

    # -- mode: delete actions ---------------------------------------------------------
    def _record_deleted(self, account: dict, listing_id: str,
                        title: str = "") -> None:
        self.db.execute(
            """INSERT INTO listings
               (run_id, account_id, product_id, title_used, status,
                fb_listing_id, posted_at)
               VALUES (?, ?, NULL, ?, 'deleted', ?, ?)""",
            (self.run_id, account["id"], title, listing_id, utcnow()),
        )

    def _delete_duplicates_flow(self, account: dict, driver) -> None:
        label = account["label"]
        deleted = driver.delete_duplicate_listings()
        for lid in deleted:
            self._record_deleted(account, lid)
        self.log(f"[{label}] deleted {len(deleted)} duplicate listing(s)",
                 account_id=account["id"])

    def _delete_previous_flow(self, account: dict, driver) -> None:
        """Delete listings posted by EARLIER runs (tracked in the DB)."""
        label = account["label"]
        rows = self.db.fetchall(
            """SELECT id, fb_listing_id, title_used FROM listings
               WHERE account_id = ? AND run_id != ? AND status = 'posted'
                 AND fb_listing_id IS NOT NULL
               ORDER BY id""",
            (account["id"], self.run_id),
        )
        n = 0
        for r in rows:
            self._wait_if_paused()
            if driver.delete_listing(r["fb_listing_id"]):
                self._record_deleted(account, r["fb_listing_id"],
                                     r["title_used"] or "")
                n += 1
            else:
                self.log(f"[{label}] could not delete {r['fb_listing_id']}",
                         level="warning", account_id=account["id"])
        self.log(f"[{label}] deleted {n}/{len(rows)} previous listing(s)",
                 account_id=account["id"])

    def _delete_draft_flow(self, account: dict, driver) -> None:
        label = account["label"]
        n = driver.delete_drafts()
        self.db.execute(
            "UPDATE listings SET status = 'deleted' "
            "WHERE account_id = ? AND status = 'draft'",
            (account["id"],),
        )
        self.log(f"[{label}] deleted {n} draft(s)",
                 account_id=account["id"])

    def _delete_take_action_flow(self, account: dict, driver) -> None:
        """Delete listings Facebook flagged with a 'take action'
        (identity/phone verification) requirement."""
        label = account["label"]
        flagged = driver.get_take_action_listings()
        n = 0
        for lst in flagged:
            self._wait_if_paused()
            lid = lst.get("id")
            if lid and driver.delete_listing(lid):
                self._record_deleted(account, lid, lst.get("title", ""))
                n += 1
        self.log(f"[{label}] deleted {n}/{len(flagged)} "
                 f"'take action' listing(s)", account_id=account["id"])

    def _delete_all_invalid_flow(self, account: dict, driver) -> None:
        """Delete every failed/invalid listing tracked for this account."""
        label = account["label"]
        rows = self.db.fetchall(
            """SELECT id, fb_listing_id, title_used FROM listings
               WHERE account_id = ? AND status = 'failed'
                 AND fb_listing_id IS NOT NULL
               ORDER BY id""",
            (account["id"],),
        )
        n = 0
        for r in rows:
            self._wait_if_paused()
            if driver.delete_listing(r["fb_listing_id"]):
                self.db.execute(
                    "UPDATE listings SET status = 'deleted' WHERE id = ?",
                    (r["id"],))
                n += 1
        self.log(f"[{label}] deleted {n}/{len(rows)} invalid listing(s)",
                 account_id=account["id"])

    # -- mode: renew_listing ------------------------------------------------------------
    def _renew_flow(self, account: dict, driver, quota: int) -> None:
        label = account["label"]
        existing = driver.get_my_listings()
        eligible = [l for l in existing if l.get("id")]
        lo = max(0, int(self.cfg.renew_min_count or 0))
        hi = max(lo, int(self.cfg.renew_max_count or 0))
        want = random.randint(lo, hi) if eligible else 0
        targets = eligible[:want]
        n = 0
        for lst in targets:
            self._wait_if_paused()
            if driver.renew_listing(lst["id"]):
                self.db.execute(
                    """INSERT INTO listings
                       (run_id, account_id, product_id, title_used, status,
                        fb_listing_id, posted_at)
                       VALUES (?, ?, NULL, ?, 'renewed', ?, ?)""",
                    (self.run_id, account["id"], lst.get("title", ""),
                     lst["id"], utcnow()),
                )
                n += 1
            else:
                self.log(f"[{label}] could not renew {lst['id']}",
                         level="warning", account_id=account["id"])
            self._inter_post_delay()
        self.log(f"[{label}] renewed {n}/{len(targets)} listing(s)",
                 account_id=account["id"])

    # -- mode: download_images_titles ---------------------------------------------------
    def _download_assets_flow(self, account: dict, driver) -> None:
        """Manual-assist flow (confirmed 2026-10-15): the browser opens with
        the account logged in on Marketplace; the user searches, opens the
        wanted products each in a new tab, then presses "Start Download" on
        the dashboard. FMS then downloads images + titles from ALL open tabs
        into a laptop folder (and appends the titles to the chosen product).
        """
        label = account["label"]
        driver.keep_open = True
        driver.open_marketplace()
        dest = self.cfg.download_folder or str(config.data_dir() / "downloads")
        self.log(f"[{label}] browser open on Marketplace — search products, "
                 f"open each wanted one in a NEW TAB, then press "
                 f"'Start Download' on the dashboard",
                 account_id=account["id"])
        if not self.wait_for_signal("download_start"):
            self.log(f"[{label}] download cancelled (run stopped)",
                     level="warning", account_id=account["id"])
            return
        self.log(f"[{label}] downloading images + titles from open tabs -> "
                 f"{dest}", account_id=account["id"])
        results = driver.download_open_tabs(dest) or []
        titles = [r["title"] for r in results if r.get("title")]
        self.log(f"[{label}] downloaded {len(results)} tab(s), "
                 f"{sum(len(r.get('photo_paths', [])) for r in results)} photo(s)",
                 account_id=account["id"])
        pid = (self.cfg.product_ids or [None])[0]
        if titles and pid:
            n = products_mod.append_titles(self.db, pid, titles)
            self.log(f"[{label}] added {n} title(s) to product #{pid}",
                     account_id=account["id"])

    # -- mode: change_location ------------------------------------------------------------
    def _change_location_flow(self, account: dict, driver) -> None:
        label = account["label"]
        target = self.cfg.new_location
        if not target:
            self.log(f"[{label}] no new location given",
                     level="warning", account_id=account["id"])
            return
        ok = driver.set_marketplace_location(target)
        self.log(f"[{label}] marketplace location -> '{target}': "
                 f"{'OK' if ok else 'FAILED'}",
                 level="info" if ok else "error", account_id=account["id"])


    # -- mode: wait_next_turn (standalone placeholder) --------------------------------------
    def _wait_mode_flow(self, account: dict) -> None:
        mins = max(0.0, float(self.cfg.wait_minutes))
        self.log(f"[{account['label']}] waiting {mins} min "
                 f"(Wait For Next Turn)", account_id=account["id"])
        self._sleep_interruptible(mins * 60.0)

    # -- mode: scrap_data ---------------------------------------------------------------------
    def _scrap_flow(self, account: dict, driver, account_idx: int) -> None:
        """Scrape the configured URLs with the FIRST account of the run only;
        the rest log a skip (scraping the same URLs on every account would
        just duplicate products)."""
        label = account["label"]
        if account_idx != 0:
            self.log(f"[{label}] scrap skipped — handled by first account",
                     account_id=account["id"])
            return
        urls = self.cfg.action_urls
        if not urls:
            self.log(f"[{label}] no URLs given for Scrap Data",
                     level="warning", account_id=account["id"])
            return
        items = scrap_mod.scrape_listings(
            driver, urls, category="Furniture", download_photos=False,
            log=lambda m: self.log(f"[{label}] {m}", account_id=account["id"]))
        if items:
            pid = scrap_mod.save_as_product(
                self.db, f"Scraped run-{self.run_id} {label}", items,
                include_description=self.cfg.scrap_include_description,
                include_location=self.cfg.scrap_include_location)
            self.log(f"[{label}] scraped {len(items)} listing(s) "
                     f"-> product #{pid}", account_id=account["id"])

    # -- mode: manual_publish (Listing Helper style) ----------------------------------
    def _manual_publish_flow(self, account: dict, driver, quota: int,
                             account_idx: int) -> None:
        """Fill N listing tabs WITHOUT publishing; the user publishes manually.
        Per-tab status is tracked in manual_tabs and shown on the dashboard."""
        label = account["label"]
        done = self.db.fetchone(
            "SELECT COUNT(*) AS c FROM manual_tabs WHERE run_id = ? AND account_id = ?",
            (self.run_id, account["id"]),
        )["c"]
        todo = max(0, quota - done)
        self.log(f"[{label}] manual-publish: filling {todo} tab(s) (no publishing)",
                 account_id=account["id"])
        for _ in range(todo):
            pid, product = self._next_product(account_idx)
            if product is None:
                continue
            payload = self._rotation(product, account_idx, account_id=account["id"])
            tab_ref = driver.fill_listing_form(payload)
            self.db.execute(
                """INSERT INTO manual_tabs
                   (run_id, account_id, tab_ref, title_used, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (self.run_id, account["id"], tab_ref or "failed",
                 payload["title"],
                 "filled" if tab_ref else "failed", utcnow()),
            )
            if tab_ref:
                self.log(f"[{label}] tab {tab_ref} filled: '{payload['title']}' "
                         f"— publish it manually in the browser",
                         account_id=account["id"])
            else:
                self.log(f"[{label}] FAILED to fill tab for '{payload['title']}'",
                         level="error", account_id=account["id"])
            self._inter_post_delay()

    # -- mode: relist_edit ------------------------------------------------------------
    def _relist_flow(self, account: dict, driver, quota: int,
                     account_idx: int) -> None:
        """For limit-hit accounts: rewrite existing listings instead of posting new."""
        label = account["label"]
        existing = driver.get_my_listings()
        already = self.db.fetchone(
            """SELECT COUNT(*) AS c FROM listings
               WHERE run_id = ? AND account_id = ? AND status = 'relisted'""",
            (self.run_id, account["id"]),
        )["c"]
        targets = [l for l in existing if l.get("id")][already:already + quota]
        self.log(f"[{label}] relist/edit: {len(targets)} existing listing(s) to rewrite",
                 account_id=account["id"])
        for l in targets:
            pid, product = self._next_product(account_idx)
            if product is None:
                continue
            payload = self._rotation(product, account_idx, account_id=account["id"])
            ok = driver.edit_listing(l["id"], payload)
            if ok:
                self._record_posted(account, pid, payload, l["id"],
                                    status="relisted")
            else:
                self._record_failed(account, pid, payload,
                                    f"edit failed for {l['id']}")
            self._inter_post_delay()


    def _thread_main(self) -> None:
        cfg = self.cfg
        try:
            # Optional scheduled start.
            if cfg.schedule_at:
                from datetime import datetime
                target = datetime.fromisoformat(cfg.schedule_at)
                self.log(f"scheduled start at {cfg.schedule_at}")
                while time.time() < target.timestamp():
                    if self.stop_event.is_set():
                        raise _StopRun()
                    time.sleep(1)

            accounts = [dict(r) for r in
                        accounts_mod.active_accounts_for_run(self.db, cfg.group_id)]
            if cfg.shuffle:
                random.shuffle(accounts)
            if cfg.exclude_labels:
                excluded = {str(l).strip().lower() for l in cfg.exclude_labels
                            if str(l).strip()}
                before = len(accounts)
                accounts = [a for a in accounts
                            if a["label"].strip().lower() not in excluded]
                if len(accounts) < before:
                    self.log(f"excluded {before - len(accounts)} account(s) "
                             f"from run", level="warning")
            if not accounts:
                self.log("no active accounts in group — nothing to do",
                         level="warning")
                self.db.execute(
                    "UPDATE runs SET status = 'completed', finished_at = ? WHERE id = ?",
                    (utcnow(), self.run_id))
                return

            # Quotas persist in settings_json so resume-after-restart keeps them.
            settings = json.loads(self.db.fetchone(
                "SELECT settings_json FROM runs WHERE id = ?",
                (self.run_id,))["settings_json"])
            quotas = {int(k): v for k, v in settings.get("quotas", {}).items()}
            for acc in accounts:
                quotas.setdefault(acc["id"],
                                  random.randint(cfg.min_ads, cfg.max_ads))
            settings["quotas"] = quotas
            total_planned = sum(quotas[a["id"]] for a in accounts)
            self.db.execute(
                "UPDATE runs SET settings_json = ?, total_planned = ?, "
                "status = 'running', started_at = COALESCE(started_at, ?) WHERE id = ?",
                (json.dumps(settings), total_planned, utcnow(), self.run_id))

            # ---- SLOT-REFILL QUEUE (v0.2.23, FBM Robo style): batch_size
            # workers; each freed slot immediately takes the next pending
            # account instead of waiting for the whole wave to finish.
            n_workers = cfg.effective_parallelism()
            gap_s = cfg.account_start_gap_s / max(0.1, cfg.speed)
            self._all_accounts = list(accounts)
            acct_index = {a["id"]: i for i, a in enumerate(accounts)}
            quotas_by_id = {a["id"]: quotas[a["id"]] for a in accounts}
            with self._q_lock:
                self._queue = collections.deque(accounts)
                self._acct_state = {a["id"]: "pending" for a in accounts}
                self._acct_label = {a["id"]: a["label"] for a in accounts}
                self._next_start_at = 0.0  # Account Start Gap bookkeeping
            # v0.2.43: pause/continue (or resume after restart) must NOT
            # re-queue accounts that already failed in this run. They stay
            # in Failed until the user manually restarts them (right-click).
            failed_ids = set(self._failed_ids_persisted())
            if failed_ids:
                with self._q_lock:
                    self._queue = collections.deque(
                        a for a in self._queue if a["id"] not in failed_ids)
                    for aid in failed_ids:
                        if aid in self._acct_state:
                            self._acct_state[aid] = "failed"
                self.log(f"{len(failed_ids)} previously-failed account(s) stay "
                         f"in Failed — right-click → Restart to retry them",
                         level="warning")
            self.log(f"run started: {len(accounts)} account(s), "
                     f"{n_workers} slot(s), "
                     f"{total_planned} listing(s) planned")

            def _worker(wi: int):
                while not self.stop_event.is_set():
                    self._wait_if_paused()
                    with self._q_lock:
                        acc = self._queue.popleft() if self._queue else None
                        if acc is not None:
                            self._acct_state[acc["id"]] = "in_progress"
                    if acc is None:
                        return
                    # Account Start Gap: space out account launches so
                    # logins never fire simultaneously (anti-detection).
                    try:
                        while not self.stop_event.is_set():
                            self._wait_if_paused()
                            with self._q_lock:
                                now = time.time()
                                wait = self._next_start_at - now
                                if wait <= 0:
                                    self._next_start_at = now + gap_s
                                    wait = 0
                            if wait <= 0:
                                break
                            time.sleep(min(wait, 0.25))
                    except _StopRun:
                        pass
                    if self.stop_event.is_set():
                        # Run stopped before this account started: Failed,
                        # not Pending (v0.2.24). Not persisted: a resume
                        # must retry these (they never genuinely failed).
                        with self._q_lock:
                            self._acct_state[acc["id"]] = "failed"
                        return
                    try:
                        outcome = self._process_account(
                            acc, quotas_by_id[acc["id"]], acct_index[acc["id"]])
                    except Exception as exc:
                        # v0.2.35: one bad account must NEVER kill the worker
                        # thread (that silently ended the run with accounts
                        # looking stuck in Pending). Mark it failed instead.
                        self._mark_failed(acc["id"])
                        self.log(f"[{self._acct_label[acc['id']]}] worker "
                                 f"error: {type(exc).__name__}: {exc} — "
                                 f"marked failed",
                                 level="error", account_id=acc["id"])
                        continue
                    with self._q_lock:
                        if outcome == "stopped":
                            # v0.2.24: a user-stopped account goes to Failed,
                            # never silently back to Pending. Not persisted:
                            # resume retries it (it never genuinely failed).
                            with self._q_lock:
                                self._acct_state[acc["id"]] = "failed"
                            self.log(f"[{self._acct_label[acc['id']]}] stopped "
                                     f"by user — marked failed",
                                     level="warning", account_id=acc["id"])
                        elif outcome == "done":
                            posted = self._posted_count(acc["id"])
                            failed = self._failed_count(acc["id"])
                            # Posted nothing but had failures -> failed panel.
                            if posted == 0 and failed > 0:
                                self._mark_failed(acc["id"])
                            else:
                                self._acct_state[acc["id"]] = "done_posting"
                        else:
                            self._mark_failed(acc["id"])

            workers = [
                threading.Thread(
                    target=_worker, args=(i,),
                    name=f"fms-run-{self.run_id}-w{i}", daemon=True)
                for i in range(n_workers)
            ]
            for t in workers:
                t.start()
            for t in workers:
                t.join()
            # A worker-level stop must propagate to the run: otherwise the
            # join returns "normally" and the run would be marked completed.
            if self.stop_event.is_set():
                raise _StopRun()

            # Run-level expected-vs-found summary for listing verification.
            if cfg.mode == "listing_number_verification":
                rows = self.db.fetchall(
                    "SELECT expected, found FROM listing_counts WHERE run_id = ?",
                    (self.run_id,))
                te = sum(r["expected"] for r in rows)
                tf = sum(r["found"] for r in rows)
                self.log(f"listing number verification summary: "
                         f"{len(rows)} account(s) — expected {te} tracked, "
                         f"found {tf} live"
                         + (" — all match" if te == tf else " — MISMATCHES present"),
                         level="info" if te == tf else "warning")

            self.db.execute(
                "UPDATE runs SET status = 'completed', finished_at = ? WHERE id = ?",
                (utcnow(), self.run_id))
            self._save_final_board()
            self.log("run completed")
        except _StopRun:
            self.db.execute(
                "UPDATE runs SET status = 'stopped', finished_at = ? WHERE id = ?",
                (utcnow(), self.run_id))
            self._save_final_board()
            self.log("run stopped by user", level="warning")
        except Exception as exc:
            self.db.execute(
                "UPDATE runs SET status = 'failed', finished_at = ? WHERE id = ?",
                (utcnow(), self.run_id))
            self._save_final_board()
            self.log(f"run FAILED: {type(exc).__name__}: {exc}", level="error")


# ------------------------------------------------------- script runs ---
class ScriptRunController:
    """Executes a named behavior script across accounts with queue semantics.

    Unlike the wave model, script runs use a shared account queue with
    `batch_size` workers: each worker takes an account, runs its resolved
    blocks, and on a `wait_next_turn` block the account yields back to the
    END of the queue (with an optional guaranteed gap) so another account's
    turn starts. This mirrors the analyzed tool's turn-rotation behavior.
    """

    def __init__(self, db: Database, run_id: int, cfg: RunConfig,
                 script: dict,
                 driver_factory: Callable[[dict], BaseDriver]):
        self.db = db
        self.run_id = run_id
        self.cfg = cfg
        self.script = script
        self.driver_factory = driver_factory
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._thread_main,
                                       name=f"fms-script-run-{run_id}",
                                       daemon=True)
        self._rot_lock = threading.Lock()
        self._rotator = _Rotator(db)
        # queue state
        self._q_lock = threading.Lock()
        self._pending: collections.deque = collections.deque()
        self._delayed: list = []  # heap of (not_before, seq, account, state)
        self._seq = itertools.count()
        self._accounts_total = 0
        self._accounts_done = 0
        self._signals: dict[str, threading.Event] = {}

    # -- public controls (same interface as RunController) -----------------------
    def signal(self, name: str) -> None:
        self._signals.setdefault(name, threading.Event()).set()

    def wait_for_signal(self, name: str, poll_s: float = 2.0) -> bool:
        ev = self._signals.setdefault(name, threading.Event())
        while not self.stop_event.is_set():
            if ev.wait(timeout=poll_s):
                return True
        return False

    # -- public controls (same interface as RunController) -----------------------
    def start(self) -> None:
        self.thread.start()

    def pause(self) -> None:
        self.pause_event.clear()
        self.db.execute("UPDATE runs SET status = 'paused' WHERE id = ?",
                        (self.run_id,))

    def resume(self) -> None:
        self.db.execute("UPDATE runs SET status = 'running' WHERE id = ?",
                        (self.run_id,))
        self.pause_event.set()

    def stop(self) -> None:
        self.stop_event.set()
        self.pause_event.set()

    # -- internals ----------------------------------------------------------------
    def log(self, message: str, level: str = "info",
            account_id: int | None = None) -> None:
        self.db.log(message, level=level, run_id=self.run_id,
                    account_id=account_id)

    def _wait_if_paused(self) -> None:
        while not self.pause_event.is_set():
            if self.stop_event.is_set():
                raise _StopRun()
            time.sleep(0.2)
        if self.stop_event.is_set():
            raise _StopRun()

    def _sleep_interruptible(self, seconds: float) -> None:
        end = time.time() + max(0.0, seconds)
        while time.time() < end:
            self._wait_if_paused()
            time.sleep(0.2)

    def _rotation(self, product: dict, account_id: int | None = None) -> dict:
        """One listing payload, rotated (mirrors RunController._rotation)."""
        payload = self._rotator.payload(product, self.cfg, as_draft=False, account_id=account_id)
        payload["_pause_checkpoint"] = self._wait_if_paused
        return payload

    def _record(self, account: dict, pid: int, payload: dict,
                listing_id: str | None, status: str) -> None:
        self.db.execute(
            """INSERT INTO listings
               (run_id, account_id, product_id, title_used, price_used,
                location_used, images_used, status, fb_listing_id, posted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (self.run_id, account["id"], pid, payload["title"],
             payload["price"], payload["location"],
             json.dumps(payload["image_paths"]), status,
             listing_id, utcnow()),
        )
        if status in ("posted", "relisted"):
            _record_published_image_usage(self.db, account["id"], pid, payload.get("image_paths"))
        if status == "posted":
            self.db.execute(
                "UPDATE runs SET total_posted = total_posted + 1 WHERE id = ?",
                (self.run_id,))

    def _record_no_product(self, account: dict, listing_id: str,
                           status: str, title: str = "") -> None:
        """Record a delete/renew record: no product or payload involved."""
        _insert_no_product_listing(self.db, self.run_id, account, listing_id,
                                   status, title)

    # -- queue ----------------------------------------------------------------------
    def _take_next(self):
        with self._q_lock:
            if self._pending:
                return self._pending.popleft()
            now = time.time()
            if self._delayed and self._delayed[0][0] <= now:
                _, _, account, state = heapq.heappop(self._delayed)
                return account, state
            return None

    def _others_waiting(self) -> bool:
        """True if another account is genuinely queued for a turn.

        Accounts in the delayed heap whose time has already come are
        eligible — not waiting — so they don't force pointless yielding.
        """
        with self._q_lock:
            if self._pending:
                return True
            now = time.time()
            return any(not_before > now
                       for not_before, _, _, _ in self._delayed)

    def _requeue(self, account: dict, state: dict, delay_s: float) -> None:
        with self._q_lock:
            heapq.heappush(self._delayed,
                           (time.time() + max(0.0, delay_s),
                            next(self._seq), account, state))

    def _mark_done(self, account: dict) -> None:
        with self._q_lock:
            self._accounts_done += 1

    def _all_done(self) -> bool:
        with self._q_lock:
            return self._accounts_done >= self._accounts_total

    def _persist_state(self, account: dict, state: dict) -> None:
        row = self.db.fetchone("SELECT settings_json FROM runs WHERE id = ?",
                               (self.run_id,))
        settings = json.loads(row["settings_json"]) if row else {}
        prog = settings.get("script_progress", {})
        prog[str(account["id"])] = {
            "done": sorted(state["done"]),
            "finished": state.get("finished", False),
        }
        settings["script_progress"] = prog
        self.db.execute("UPDATE runs SET settings_json = ? WHERE id = ?",
                        (json.dumps(settings), self.run_id))

    # -- block execution ---------------------------------------------------------------
    def _exec_block(self, account: dict, driver, block: dict,
                    state: dict, label: str):
        """Execute one block. Returns 'yielded' if the account yielded its turn."""
        btype = block["type"]
        p = block["params"]
        # Run speed multiplies the pace: 2x halves every duration.
        scale = self.cfg.activity_scale / max(0.1, self.cfg.speed)

        if btype == "post":
            product = products_mod.get_product(self.db, p["product_id"])
            if product is None:
                self.log(f"[{label}] block {block['id']}: product vanished",
                         level="warning", account_id=account["id"])
                return None
            n = random.randint(p.get("count_min", 1), p.get("count_max", 1))
            for _ in range(n):
                self._wait_if_paused()
                payload = self._rotation(product, account_id=account["id"])
                lid = driver.post_listing(payload)
                self._record(account, product["id"], payload, lid,
                             "posted" if lid else "failed")
            self.log(f"[{label}] block {block['id']}: posted {n}",
                     account_id=account["id"])
            return None

        if btype == "drafts":
            product = products_mod.get_product(self.db, p["product_id"])
            if product is None:
                return None
            n = random.randint(p.get("count_min", 1), p.get("count_max", 1))
            for _ in range(n):
                self._wait_if_paused()
                payload = self._rotation(product, account_id=account["id"])
                did = driver.create_draft(payload)
                self._record(account, product["id"], payload, did,
                             "draft" if did else "failed")
            self.log(f"[{label}] block {block['id']}: drafted {n}",
                     account_id=account["id"])
            return None

        if btype == "publish_drafts":
            drafts = self.db.fetchall(
                """SELECT id, fb_listing_id, title_used, product_id, images_used FROM listings
                   WHERE run_id = ? AND account_id = ? AND status = 'draft'
                   ORDER BY id""",
                (self.run_id, account["id"]),
            )
            mode = p.get("publish_mode") or self.cfg.publish_mode
            gap_s = (p.get("gap_minutes", self.cfg.publish_gap_min)) * 60.0
            for i, d in enumerate(drafts):
                self._wait_if_paused()
                if mode == "one_by_one" and i > 0:
                    self._sleep_interruptible(gap_s)
                if driver.publish_draft(d["fb_listing_id"]):
                    self.db.execute(
                        "UPDATE listings SET status='posted', posted_at=? WHERE id=?",
                        (utcnow(), d["id"]))
                    self.db.execute(
                        "UPDATE runs SET total_posted = total_posted + 1 WHERE id = ?",
                        (self.run_id,))
                    try:
                        _draft_images = json.loads(d["images_used"] or "[]")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        _draft_images = []
                    _record_published_image_usage(self.db, account["id"], d["product_id"], _draft_images)
                else:
                    self.db.execute(
                        "UPDATE listings SET status='failed' WHERE id=?", (d["id"],))
            self.log(f"[{label}] block {block['id']}: published {len(drafts)}",
                     account_id=account["id"])
            return None

        if btype == "wait_next_turn":
            mins = float(p.get("wait_minutes", 0) or 0)
            if self._others_waiting():
                self._requeue(account, state, mins * 60.0)
                self.log(f"[{label}] block {block['id']}: yielding turn "
                         f"(wait {mins} min)", account_id=account["id"])
                return "yielded"
            if mins > 0:
                self.log(f"[{label}] block {block['id']}: resting {mins} min",
                         account_id=account["id"])
                self._sleep_interruptible(mins * 60.0)
            return None

        # -- activity blocks (per-action settings, v6.2.0.0) ---------------------
        if btype in ("scroll_feed", "watch_reels", "watch_videos",
                     "scroll_marketplace", "read_notifications",
                     "scroll_messages"):
            mins = _activity_minutes(p, scale)
            if mins <= 0:
                self.log(f"[{label}] block {block['id']}: {btype} skipped "
                         f"(0 duration configured)", account_id=account["id"])
            elif btype == "scroll_feed":
                driver.scroll_feed(mins,
                                   {"keep_scrolling": _chance01(
                                       p, "keep_scrolling_chance",
                                       "p_keep_scrolling"),
                                    "like_post": _chance01(
                                       p, "like_a_post_chance",
                                       "p_like_post")})
            elif btype == "watch_reels":
                driver.watch_reels(mins,
                                   {"skip": _chance01(p, "skip_chance",
                                                      "p_skip"),
                                    "like": _chance01(p, "like_chance",
                                                      "p_like")})
            elif btype == "watch_videos":
                driver.watch_videos(mins,
                                    {"skip": _chance01(p, "skip_chance", None),
                                     "like": _chance01(p, "like_chance",
                                                       None)})
            elif btype == "scroll_marketplace":
                driver.scroll_marketplace(
                    mins,
                    {"keep_scrolling": _chance01(p, "keep_scrolling_chance",
                                                 "p_keep_scrolling"),
                     "hover": _chance01(p, "hover_a_listing_chance",
                                        "p_hover"),
                     "open": _chance01(p, "open_a_listing_chance", "p_open")})
            elif btype == "read_notifications":
                driver.read_notifications(mins * 60.0, {})
            elif btype == "scroll_messages":
                driver.scroll_messages(mins * 60.0, {})
        elif btype == "goto_messages":
            driver.goto_messages()

        # -- account management blocks ----------------------------------------
        elif btype == "login":
            self.log(f"[{label}] block {block['id']}: session verified",
                     account_id=account["id"])
            return None

        elif btype in ("browser_launch", "browser_launch_fast"):
            # Identical in the real tool (confirmed 2026-10-15).
            driver.keep_open = True
            self.log(f"[{label}] block {block['id']}: browser kept open "
                     f"for manual work", account_id=account["id"])
            return None

        elif btype == "delete_relist":
            product = (products_mod.get_product(self.db, p["product_id"])
                       if p.get("product_id") else None)
            if product is None:
                self.log(f"[{label}] block {block['id']}: product vanished — "
                         f"skipping", level="warning",
                         account_id=account["id"])
                return None
            max_r = int(p.get("max_relist", 0) or 0)
            targets = [l for l in driver.get_my_listings() if l.get("id")]
            if max_r > 0:
                targets = targets[:max_r]
            n = 0
            for l in targets:
                self._wait_if_paused()
                lid = l["id"]
                if not driver.delete_listing(lid):
                    continue
                self._record_no_product(account, lid, "deleted",
                                        l.get("title", ""))
                payload = self._rotation(product, account_id=account["id"])
                new_id = driver.post_listing(payload)
                self._record(account, product["id"], payload, new_id,
                             "relisted" if new_id else "failed")
                if new_id:
                    n += 1
            self.log(f"[{label}] block {block['id']}: delete & relist done "
                     f"({n} relisted)", account_id=account["id"])
            return None

        elif btype == "relist_edit":
            product = (products_mod.get_product(self.db, p["product_id"])
                       if p.get("product_id") else None)
            if product is None:
                self.log(f"[{label}] block {block['id']}: product vanished — "
                         f"skipping", level="warning",
                         account_id=account["id"])
                return None
            n = random.randint(int(p.get("count_min", 1) or 1),
                               int(p.get("count_max", 1) or 1))
            targets = [l for l in driver.get_my_listings()
                       if l.get("id")][:max(0, n)]
            for l in targets:
                self._wait_if_paused()
                payload = self._rotation(product, account_id=account["id"])
                ok = driver.edit_listing(l["id"], payload)
                self._record(account, product["id"], payload, l["id"],
                             "relisted" if ok else "failed")
            self.log(f"[{label}] block {block['id']}: relisted "
                     f"{len(targets)}", account_id=account["id"])
            return None

            return None

        elif btype == "renew_listing":
            min_r = int(p.get("min_renew", 1) or 0)
            max_r = int(p.get("max_renew", 5) or 0)
            if max_r < min_r:
                max_r = min_r
            eligible = [lst for lst in driver.get_my_listings()
                        if lst.get("id")]
            want = random.randint(min_r, max_r) if eligible else 0
            n = 0
            for lst in eligible[:max(0, want)]:
                self._wait_if_paused()
                if lst.get("id") and driver.renew_listing(lst["id"]):
                    self._record_no_product(account, lst["id"], "renewed",
                                            lst.get("title", ""))
                    n += 1
            self.log(f"[{label}] block {block['id']}: renewed {n}",
                     account_id=account["id"])
            return None

        elif btype == "delete_duplicate":
            deleted = driver.delete_duplicate_listings()
            for lid in deleted:
                self._record_no_product(account, lid, "deleted")
            self.log(f"[{label}] block {block['id']}: deleted {len(deleted)} "
                     f"duplicate(s)", account_id=account["id"])
            return None

        elif btype == "delete_previous":
            rows = self.db.fetchall(
                """SELECT id, fb_listing_id, title_used FROM listings
                   WHERE account_id = ? AND run_id != ? AND status = 'posted'
                     AND fb_listing_id IS NOT NULL ORDER BY id""",
                (account["id"], self.run_id))
            n = 0
            for r in rows:
                self._wait_if_paused()
                if driver.delete_listing(r["fb_listing_id"]):
                    self._record_no_product(account, r["fb_listing_id"],
                                            "deleted", r["title_used"] or "")
                    n += 1
            self.log(f"[{label}] block {block['id']}: deleted {n} "
                     f"previous listing(s)", account_id=account["id"])
            return None

        elif btype == "delete_draft":
            n = driver.delete_drafts()
            self.db.execute("UPDATE listings SET status = 'deleted' "
                            "WHERE account_id = ? AND status = 'draft'",
                            (account["id"],))
            self.log(f"[{label}] block {block['id']}: deleted {n} draft(s)",
                     account_id=account["id"])
            return None

        elif btype == "delete_take_action":
            n = 0
            for lst in driver.get_take_action_listings():
                self._wait_if_paused()
                lid = lst.get("id")
                if lid and driver.delete_listing(lid):
                    self._record_no_product(account, lid, "deleted",
                                            lst.get("title", ""))
                    n += 1
            self.log(f"[{label}] block {block['id']}: deleted {n} "
                     f"'take action' listing(s)", account_id=account["id"])
            return None

        elif btype == "delete_all_invalid":
            rows = self.db.fetchall(
                """SELECT id, fb_listing_id FROM listings
                   WHERE account_id = ? AND status = 'failed'
                     AND fb_listing_id IS NOT NULL ORDER BY id""",
                (account["id"],))
            n = 0
            for r in rows:
                self._wait_if_paused()
                if driver.delete_listing(r["fb_listing_id"]):
                    self.db.execute("UPDATE listings SET status='deleted' "
                                    "WHERE id = ?", (r["id"],))
                    n += 1
            self.log(f"[{label}] block {block['id']}: deleted {n} invalid",
                     account_id=account["id"])
            return None

        elif btype == "change_location":
            target = p.get("location") or ""
            ok = driver.set_marketplace_location(target) if target else False
            self.log(f"[{label}] block {block['id']}: location -> '{target}': "
                     f"{'OK' if ok else 'FAILED'}",
                     level="info" if ok else "warning", account_id=account["id"])
            return None

        elif btype == "download_images_titles":
            # Manual-assist (confirmed 2026-10-15): browser opens on
            # Marketplace, the user opens products in new tabs, then the
            # dashboard "Start Download" signal fires.
            driver.keep_open = True
            driver.open_marketplace()
            dest = p.get("download_folder") or str(
                config.data_dir() / "downloads")
            pid = p.get("product_id")
            self.log(f"[{label}] block {block['id']}: browser open on "
                     f"Marketplace — open wanted products in new tabs, then "
                     f"press 'Start Download' on the dashboard",
                     account_id=account["id"])
            if not self.wait_for_signal("download_start"):
                self.log(f"[{label}] block {block['id']}: download cancelled",
                         level="warning", account_id=account["id"])
                return None
            results = driver.download_open_tabs(dest) or []
            titles = [r["title"] for r in results if r.get("title")]
            if titles and pid:
                products_mod.append_titles(self.db, pid, titles)
            self.log(f"[{label}] block {block['id']}: downloaded "
                     f"{len(results)} tab(s), {len(titles)} title(s)",
                     account_id=account["id"])
            return None

        else:
            self.log(f"[{label}] block {block['id']}: unknown type '{btype}'",
                     level="warning", account_id=account["id"])
            return None
        self.log(f"[{label}] block {block['id']}: {btype} done",
                 account_id=account["id"])
        return None

    def _run_blocks(self, account: dict, state: dict) -> None:
        label = account["label"]
        creds = _driver_creds(self.db, self.cfg, account)
        driver = self.driver_factory(creds)
        try:
            driver._pause_checkpoint_callback = self._wait_if_paused
            binder = getattr(driver, "_bind_pause_checkpoint", None)
            if callable(binder):
                binder()
        except Exception:
            pass
        try:
            if not driver.login():
                # v0.2.39: no permanent locking — just log and mark done.
                reason = getattr(driver, "login_error", "") or "unknown reason"
                self.log(f"[{label}] login FAILED: {reason}",
                         level="error", account_id=account["id"])
                self._mark_done(account)
                return
            rng = random.Random((self.run_id * 1000003) ^ account["id"])
            blocks = scripts_mod.resolve_blocks(self.script["blocks"], rng)
            if self.script.get("shuffle"):
                rng.shuffle(blocks)  # each account gets its own order
            done = state["done"]
            for b in blocks:
                if b["id"] in done:
                    continue
                self._wait_if_paused()
                try:
                    result = self._exec_block(account, driver, b, state, label)
                except _StopRun:
                    raise
                except Exception as exc:
                    self.log(f"[{label}] block {b['id']} ERROR: "
                             f"{type(exc).__name__}: {exc}",
                             level="error", account_id=account["id"])
                    done.add(b["id"])
                    self._persist_state(account, state)
                    continue
                if result == "yielded":
                    self._persist_state(account, state)
                    return  # worker moves on; account rejoins the queue later
                done.add(b["id"])
                self._persist_state(account, state)
            state["finished"] = True
            self._persist_state(account, state)
            self._mark_done(account)
            self.log(f"[{label}] script finished", account_id=account["id"])
        except _StopRun:
            raise
        except Exception as exc:
            self.log(f"[{label}] script ERROR: {type(exc).__name__}: {exc}",
                     level="error", account_id=account["id"])
            self._mark_done(account)  # avoid a poisoned account stalling the run
        finally:
            try:
                driver.close()
            except Exception:
                pass

    def _worker(self) -> None:
        while True:
            try:
                self._wait_if_paused()
            except _StopRun:
                return
            nxt = self._take_next()
            if nxt is None:
                if self._all_done() or self.stop_event.is_set():
                    return
                time.sleep(0.5)
                continue
            account, state = nxt
            try:
                self._run_blocks(account, state)
            except _StopRun:
                return

    def _thread_main(self) -> None:
        cfg = self.cfg
        try:
            if cfg.schedule_at:
                from datetime import datetime
                target = datetime.fromisoformat(cfg.schedule_at)
                self.log(f"scheduled start at {cfg.schedule_at}")
                while time.time() < target.timestamp():
                    if self.stop_event.is_set():
                        raise _StopRun()
                    time.sleep(1)

            accounts = [dict(r) for r in
                        accounts_mod.active_accounts_for_run(self.db, cfg.group_id)]
            if cfg.shuffle:
                random.shuffle(accounts)
            if cfg.exclude_labels:
                excluded = {str(l).strip().lower() for l in cfg.exclude_labels
                            if str(l).strip()}
                before = len(accounts)
                accounts = [a for a in accounts
                            if a["label"].strip().lower() not in excluded]
                if len(accounts) < before:
                    self.log(f"excluded {before - len(accounts)} account(s) "
                             f"from run", level="warning")
            if not accounts:
                self.log("no active accounts in group — nothing to do",
                         level="warning")
                self.db.execute(
                    "UPDATE runs SET status='completed', finished_at=? WHERE id=?",
                    (utcnow(), self.run_id))
                return

            # Restore per-account progress for resume-after-interruption.
            row = self.db.fetchone("SELECT settings_json FROM runs WHERE id=?",
                                   (self.run_id,))
            settings = json.loads(row["settings_json"]) if row else {}
            saved_prog = settings.get("script_progress", {})
            settings["script_id"] = cfg.script_id
            for acc in accounts:
                sp = saved_prog.get(str(acc["id"]), {})
                state = {"done": set(sp.get("done", [])),
                         "finished": bool(sp.get("finished", False))}
                if state["finished"]:
                    with self._q_lock:
                        self._accounts_done += 1
                else:
                    with self._q_lock:
                        self._pending.append((acc, state))
            self._accounts_total = len(accounts)
            self.db.execute(
                "UPDATE runs SET settings_json=?, status='running', "
                "started_at=COALESCE(started_at, ?) WHERE id=?",
                (json.dumps(settings), utcnow(), self.run_id))
            n_blocks = len(self.script["blocks"])
            self.log(f"script run started: {len(accounts)} account(s), "
                     f"'{self.script['name']}' ({n_blocks} blocks), "
                     f"{cfg.effective_parallelism()} worker(s)")

            n_workers = cfg.effective_parallelism()
            workers = [threading.Thread(target=self._worker, daemon=True,
                                        name=f"fms-script-{self.run_id}-w{i}")
                       for i in range(n_workers)]
            gap_s = cfg.account_start_gap_s / max(0.1, cfg.speed)
            for i, w in enumerate(workers):
                if i > 0:
                    time.sleep(gap_s)
                w.start()
            for w in workers:
                w.join()
            if self.stop_event.is_set():
                raise _StopRun()
            self.db.execute(
                "UPDATE runs SET status='completed', finished_at=? WHERE id=?",
                (utcnow(), self.run_id))
            self.log("script run completed")
        except _StopRun:
            self.db.execute(
                "UPDATE runs SET status='stopped', finished_at=? WHERE id=?",
                (utcnow(), self.run_id))
            self.log("script run stopped by user", level="warning")
        except Exception as exc:
            self.db.execute(
                "UPDATE runs SET status='failed', finished_at=? WHERE id=?",
                (utcnow(), self.run_id))
            self.log(f"script run FAILED: {type(exc).__name__}: {exc}",
                     level="error")


# ------------------------------------------------------------------ engine ---
class ListingEngine:
    """Creates and controls runs. Owns the live RunController registry."""

    def __init__(
        self,
        db: Database,
        driver_factory: Callable[[dict], BaseDriver] | None = None,
    ):
        self.db = db
        self.driver_factory = driver_factory or PlaywrightDriver
        self._controllers: dict[int, RunController] = {}
        self._lock = threading.Lock()
        self.mark_stale_runs()

    # -- lifecycle --------------------------------------------------------------
    def mark_stale_runs(self) -> int:
        """Runs left 'running'/'paused' by a crash become resumable 'interrupted'."""
        cur = self.db.execute(
            "UPDATE runs SET status = 'interrupted' "
            "WHERE status IN ('running', 'paused')"
        )
        return cur.rowcount

    def _find_resumable(self, cfg: RunConfig) -> int | None:
        row = self.db.fetchone(
            """SELECT id, settings_json FROM runs
               WHERE group_id = ? AND mode = ? AND config_hash = ?
                 AND status IN ('interrupted', 'paused', 'stopped', 'failed')
               ORDER BY id DESC LIMIT 1""",
            (cfg.group_id, cfg.mode, cfg.config_hash()),
        )
        return row["id"] if row else None

    def _make_controller(self, run_id: int, cfg: RunConfig):
        """Build the right controller for the run mode."""
        if cfg.mode == "script":
            script = scripts_mod.get_script(self.db, cfg.script_id or 0)
            if not script:
                raise ValueError(f"script #{cfg.script_id} not found")
            if not script["blocks"]:
                raise ValueError(f"script '{script['name']}' has no blocks")
            return ScriptRunController(self.db, run_id, cfg, script,
                                       self.driver_factory)
        return RunController(self.db, run_id, cfg, self.driver_factory)

    def start_run(self, cfg: RunConfig, fresh: bool = True) -> int:
        """Start a run. fresh=False reuses the latest interrupted run
        ("continue where you left off"); falls back to a new run if none exists."""
        run_id: int | None = None
        if not fresh:
            run_id = self._find_resumable(cfg)
        if run_id is None:
            if not fresh:
                self.db.log("no interrupted run found — starting fresh")
            cur = self.db.execute(
                """INSERT INTO runs
                   (group_id, mode, script_id, settings_json, config_hash, status)
                   VALUES (?, ?, ?, ?, ?, 'pending')""",
                (cfg.group_id, cfg.mode, cfg.script_id,
                 json.dumps(cfg.to_dict()), cfg.config_hash()),
            )
            run_id = cur.lastrowid
        else:
            self.db.execute(
                "UPDATE runs SET status = 'pending', finished_at = NULL WHERE id = ?",
                (run_id,))
            self.db.log(f"resuming run #{run_id} — already-posted listings are skipped",
                        run_id=run_id)
        controller = self._make_controller(run_id, cfg)
        with self._lock:
            self._controllers[run_id] = controller
        controller.start()
        return run_id

    def _controller(self, run_id: int) -> RunController | None:
        with self._lock:
            return self._controllers.get(run_id)

    def pause_run(self, run_id: int) -> bool:
        c = self._controller(run_id)
        if c and c.thread.is_alive():
            c.pause()
            return True
        return False

    def resume_run(self, run_id: int) -> bool:
        c = self._controller(run_id)
        if c and c.thread.is_alive():
            c.resume()
            return True
        # Paused run whose controller is gone (e.g. after restart): relaunch it.
        row = self.db.fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))
        if row and row["status"] in ("paused", "interrupted", "stopped"):
            settings = json.loads(row["settings_json"])
            cfg = RunConfig(**{k: v for k, v in settings.items()
                               if k in RunConfig.__dataclass_fields__})
            controller = self._make_controller(run_id, cfg)
            with self._lock:
                self._controllers[run_id] = controller
            self.db.execute("UPDATE runs SET status = 'pending' WHERE id = ?",
                            (run_id,))
            controller.start()
            return True
        return False

    def stop_run(self, run_id: int) -> bool:
        c = self._controller(run_id)
        if c and c.thread.is_alive():
            c.stop()
            return True
        return False

    def signal_run(self, run_id: int, name: str) -> bool:
        c = self._controller(run_id)
        if c and c.thread.is_alive():
            c.signal(name)
            return True
        return False

    # -- queries -----------------------------------------------------------------
    def get_run(self, run_id: int) -> dict | None:
        row = self.db.fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))
        if not row:
            return None
        d = dict(row)
        d["settings"] = json.loads(d["settings_json"])
        # per-account progress
        per_account = []
        acc_rows = self.db.fetchall(
            "SELECT id, label, status FROM accounts WHERE group_id = ?",
            (d["group_id"],))
        quotas = {int(k): v for k, v in d["settings"].get("quotas", {}).items()}
        for acc in acc_rows:
            posted = self.db.fetchone(
                """SELECT COUNT(*) AS c FROM listings
                   WHERE run_id = ? AND account_id = ? AND status = 'posted'""",
                (run_id, acc["id"]))["c"]
            failed = self.db.fetchone(
                """SELECT COUNT(*) AS c FROM listings
                   WHERE run_id = ? AND account_id = ? AND status = 'failed'""",
                (run_id, acc["id"]))["c"]
            per_account.append({
                "id": acc["id"], "label": acc["label"], "status": acc["status"],
                "quota": quotas.get(acc["id"]), "posted": posted, "failed": failed,
            })
        d["per_account"] = per_account
        c = self._controller(run_id)
        d["live"] = bool(c and c.thread.is_alive())
        d["paused"] = bool(c and c.thread.is_alive() and not c.pause_event.is_set())
        # manual_publish: per-tab status for the dashboard
        if d["mode"] == "manual_publish":
            d["tabs"] = [
                dict(r) for r in self.db.fetchall(
                    """SELECT mt.*, a.label AS account_label FROM manual_tabs mt
                       LEFT JOIN accounts a ON a.id = mt.account_id
                       WHERE mt.run_id = ? ORDER BY mt.id""", (run_id,))
            ]
        else:
            d["tabs"] = []
        # script mode: per-account block progress
        if d["mode"] == "script":
            prog = d["settings"].get("script_progress", {})
            script = scripts_mod.get_script(self.db, d.get("script_id") or 0)
            total_blocks = len(script["blocks"]) if script else 0
            sp = []
            for acc in acc_rows:
                ap = prog.get(str(acc["id"]), {})
                done = ap.get("done", [])
                sp.append({
                    "id": acc["id"], "label": acc["label"],
                    "done_blocks": done, "done_count": len(done),
                    "total_blocks": total_blocks,
                    "finished": ap.get("finished", False),
                })
            d["script_progress"] = sp
            d["script_name"] = script["name"] if script else "—"
        else:
            d["script_progress"] = []
            d["script_name"] = None
        return d

    def set_tab_status(self, run_id: int, tab_id: int, status: str) -> bool:
        """Mark a manual-publish tab published/skipped by the user."""
        if status not in ("published", "skipped", "filled", "failed"):
            return False
        cur = self.db.execute(
            "UPDATE manual_tabs SET status = ? WHERE id = ? AND run_id = ?",
            (status, tab_id, run_id),
        )
        return cur.rowcount > 0

    def list_runs(self, limit: int = 50) -> list[dict]:
        rows = self.db.fetchall(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            grp = self.db.fetchone("SELECT name FROM groups WHERE id = ?",
                                   (d["group_id"],))
            d["group_name"] = grp["name"] if grp else "—"
            out.append(d)
        return out

    def recent_logs(self, run_id: int | None = None, limit: int = 200) -> list[dict]:
        if run_id:
            rows = self.db.fetchall(
                "SELECT * FROM logs WHERE run_id = ? ORDER BY id DESC LIMIT ?",
                (run_id, limit))
        else:
            rows = self.db.fetchall(
                "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

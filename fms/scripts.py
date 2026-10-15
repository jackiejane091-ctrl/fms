"""Script Engine: named behavior scripts built from action blocks.

A script is an ordered list of blocks; each account executes its own resolved
copy (shuffle + K-of-N grouping make every account's session look different).
Block ids are stable ("b1", "b2", ...) so interrupted runs can skip blocks
that already completed.

Block types:
  post               N listings from a bound dataset (product)
  drafts             N drafts from a bound dataset
  publish_drafts     publish this account's drafts from this run
  wait_next_turn     yield the account back to the queue (see semantics below)
  scroll_feed        human-like home-feed scrolling (minutes, probabilities)
  watch_reels        watch reels (minutes, probabilities)
  watch_videos       watch videos (minutes)
  scroll_marketplace scroll the Marketplace feed (minutes, probabilities)
  read_notifications read notifications (seconds)
  scroll_messages    scroll the messages list (seconds)
  goto_messages      open the messages page
  group              run K of N member blocks (members reference block ids)

"Wait For Next Turn" semantics (from the analyzed tool):
  wait_minutes = 0 -> if another account is waiting, yield the turn; if no
                      other account is queued, continue immediately (no gap).
  wait_minutes = N -> guaranteed gap of at least N minutes before this
                      account's next turn, even with nobody waiting.
"""
from __future__ import annotations

import random
from typing import Any

from .db import Database, utcnow

# Legacy activity param names (pre-v6.2.0.0 schema). Still accepted so
# scripts saved earlier keep working; the new min/max-minute + chance-%
# keys take precedence when set. The dashboard settings dialogs hide these.
LEGACY_ACTIVITY_PARAMS = {"minutes", "seconds", "p_keep_scrolling",
                          "p_like_post", "p_skip", "p_like", "p_hover",
                          "p_open"}

# ------------------------------------------------------------------ model ---
# param schema: name -> (default, description). Used by the dashboard editor.
BLOCK_TYPES: dict[str, dict[str, tuple[Any, str]]] = {
    "post": {
        "product_id": (None, "Dataset: product to post"),
        "count_min": (1, "Min listings"),
        "count_max": (3, "Max listings"),
    },
    "drafts": {
        "product_id": (None, "Dataset: product to draft"),
        "count_min": (1, "Min drafts"),
        "count_max": (3, "Max drafts"),
    },
    "publish_drafts": {
        "publish_mode": ("all_together", "all_together | one_by_one"),
        "gap_minutes": (5.0, "Gap between publishes (one_by_one only)"),
    },
    "relist_edit": {
        "product_id": (None, "Dataset: product to rewrite listings with"),
        "count_min": (1, "Min listings"),
        "count_max": (3, "Max listings"),
    },
    "delete_relist": {
        "product_id": (None, "Dataset: product to relist with"),
        "max_relist": (0, "Max listings to delete & relist (0 = all)"),
    },
    "wait_next_turn": {
        "wait_minutes": (0, "0 = yield only if someone waits; N = guaranteed N-min gap"),
    },
    # -- activity blocks: per-action settings (v6.2.0.0). A 0/0 minute
    # range means the activity is skipped (logged).
    "scroll_feed": {
        "min_minutes": (0, "Min minutes"),
        "max_minutes": (0, "Max minutes"),
        "keep_scrolling_chance": (55, "Keep-scrolling chance (%)"),
        "like_a_post_chance": (15, "Like-a-post chance (%)"),
        "minutes": (0, "Legacy: duration in minutes"),
        "p_keep_scrolling": (None, "Legacy: keep-scrolling probability 0-1"),
        "p_like_post": (None, "Legacy: like-post probability 0-1"),
    },
    "watch_reels": {
        "min_minutes": (0, "Min minutes"),
        "max_minutes": (0, "Max minutes"),
        "skip_chance": (40, "Skip chance (%)"),
        "like_chance": (45, "Like chance (%)"),
        "minutes": (0, "Legacy: duration in minutes"),
        "p_skip": (None, "Legacy: skip probability 0-1"),
        "p_like": (None, "Legacy: like probability 0-1"),
    },
    "watch_videos": {
        "min_minutes": (0, "Min minutes"),
        "max_minutes": (0, "Max minutes"),
        "skip_chance": (20, "Skip chance (%)"),
        "like_chance": (30, "Like chance (%)"),
        "minutes": (0, "Legacy: duration in minutes"),
    },
    "scroll_marketplace": {
        "min_minutes": (0, "Min minutes"),
        "max_minutes": (0, "Max minutes"),
        "keep_scrolling_chance": (50, "Keep-scrolling chance (%)"),
        "hover_a_listing_chance": (70, "Hover-a-listing chance (%)"),
        "open_a_listing_chance": (6, "Open-a-listing chance (%)"),
        "minutes": (0, "Legacy: duration in minutes"),
        "p_keep_scrolling": (None, "Legacy: keep-scrolling probability 0-1"),
        "p_hover": (None, "Legacy: hover probability 0-1"),
        "p_open": (None, "Legacy: open probability 0-1"),
    },
    "read_notifications": {
        "min_seconds": (0, "Min seconds"),
        "max_seconds": (0, "Max seconds"),
    },
    "scroll_messages": {
        "min_seconds": (0, "Min seconds"),
        "max_seconds": (0, "Max seconds"),
    },
    "goto_messages": {},
    # -- account management blocks (alignment, v6.1.2.0) ---------------------
    "login": {},
    "browser_launch": {},
    "browser_launch_fast": {},
    "renew_listing": {
        "min_renew": (1, "Min listings to renew"),
        "max_renew": (5, "Max listings to renew"),
    },
    "delete_duplicate": {},
    "delete_previous": {},
    "delete_draft": {},
    "delete_take_action": {},
    "delete_all_invalid": {},
    "change_location": {
        "location": ("", "New marketplace location"),
    },
    "download_images_titles": {
        "product_id": (None, "Product to add titles to"),
        "download_folder": ("", "Download folder (empty = default)"),
    },
    "group": {
        "members": ([], "Block ids to choose from"),
        "min_select": (1, "Min blocks to run"),
        "max_select": (1, "Max blocks to run"),
    },
}

ACTIVITY_BLOCKS = {"scroll_feed", "watch_reels", "watch_videos",
                   "scroll_marketplace", "read_notifications",
                   "scroll_messages", "goto_messages"}


def default_params(block_type: str) -> dict:
    return {k: v[0] for k, v in BLOCK_TYPES[block_type].items()}


def make_block(block_type: str, block_id: str, **params) -> dict:
    if block_type not in BLOCK_TYPES:
        raise ValueError(f"Unknown block type: {block_type}")
    merged = default_params(block_type)
    for k, v in params.items():
        if k not in merged:
            raise ValueError(f"Unknown param '{k}' for block '{block_type}'")
        merged[k] = v
    return {"id": block_id, "type": block_type, "params": merged}


def validate_blocks(blocks: list[dict]) -> list[str]:
    """Return a list of human-readable problems (empty = valid)."""
    errors: list[str] = []
    seen: set[str] = set()
    by_id = {}
    for b in blocks:
        bid = b.get("id")
        btype = b.get("type")
        if not bid or not isinstance(bid, str):
            errors.append("every block needs a string id")
            continue
        if bid in seen:
            errors.append(f"duplicate block id: {bid}")
        seen.add(bid)
        by_id[bid] = b
        if btype not in BLOCK_TYPES:
            errors.append(f"block {bid}: unknown type '{btype}'")
            continue
        params = b.get("params") or {}
        for p in params:
            if p not in BLOCK_TYPES[btype]:
                errors.append(f"block {bid}: unknown param '{p}'")
        if btype in ("post", "drafts", "relist_edit", "delete_relist") \
                and not params.get("product_id"):
            errors.append(f"block {bid} ({btype}): needs a product_id dataset")
        if btype == "group":
            members = params.get("members") or []
            for m in members:
                if m == bid:
                    errors.append(f"block {bid}: group cannot contain itself")
                elif m not in by_id and m not in [x.get("id") for x in blocks]:
                    errors.append(f"block {bid}: unknown member '{m}'")
            lo, hi = params.get("min_select", 1), params.get("max_select", 1)
            if not members:
                errors.append(f"block {bid}: group has no members")
            if hi < lo:
                errors.append(f"block {bid}: max_select < min_select")
            if lo < 0:
                errors.append(f"block {bid}: min_select < 0")
    # A script of pure waits could never make progress (accounts would yield
    # to each other forever), so require at least one productive block.
    productive = {"post", "drafts", "publish_drafts", "relist_edit",
                  "delete_relist", "scroll_feed",
                  "watch_reels", "watch_videos", "scroll_marketplace",
                  "read_notifications", "scroll_messages", "goto_messages",
                  "login", "browser_launch", "browser_launch_fast",
                  "renew_listing", "delete_duplicate", "delete_previous",
                  "delete_draft", "delete_take_action", "delete_all_invalid",
                  "change_location", "download_images_titles"}
    if blocks and not any(b.get("type") in productive for b in blocks):
        errors.append("script needs at least one action block "
                      "(post / drafts / publish / activity) besides waits")
    return errors


def resolve_blocks(blocks: list[dict], rng: random.Random) -> list[dict]:
    """Resolve one account's executable block list.

    Applies K-of-N grouping (each account picks its own random subset) and
    returns blocks in execution order (shuffle is applied by the caller per
    account when the script has shuffle enabled).
    """
    by_id = {b["id"]: b for b in blocks}
    out = list(blocks)  # keep every block; the pass below replaces groups
    # and drops member blocks that run only via their group.
    member_ids: set[str] = set()
    for b in blocks:
        if b["type"] == "group":
            member_ids.update(b["params"].get("members") or [])
    final: list[dict] = []
    for b in out:
        if b["type"] == "group":
            members = [by_id[mid] for mid in b["params"].get("members", [])
                       if mid in by_id]
            lo = max(0, b["params"].get("min_select", 1))
            hi = max(lo, b["params"].get("max_select", lo))
            hi = min(hi, len(members))
            k = rng.randint(lo, hi) if members else 0
            chosen = rng.sample(members, k) if k else []
            # chosen members run in script order
            order = {mid: i for i, mid in enumerate(b["params"].get("members", []))}
            chosen.sort(key=lambda m: order.get(m["id"], 0))
            final.extend(chosen)
        elif b["id"] not in member_ids:
            final.append(b)
        # else: pure group member -> runs only via the group
    return final


# ------------------------------------------------------------------ CRUD ---
def create_script(db: Database, name: str,
                  blocks: list[dict] | None = None,
                  shuffle: bool = False) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("Script name cannot be empty.")
    blocks = blocks or []
    errors = validate_blocks(blocks)
    if errors:
        raise ValueError("; ".join(errors))
    cur = db.execute(
        "INSERT INTO scripts (name, blocks_json, shuffle, created_at)"
        " VALUES (?, ?, ?, ?)",
        (name, Database.to_json(blocks), 1 if shuffle else 0, utcnow()),
    )
    return cur.lastrowid


def list_scripts(db: Database) -> list[dict]:
    rows = db.fetchall("SELECT * FROM scripts ORDER BY name")
    out = []
    for r in rows:
        d = dict(r)
        d["blocks"] = Database.from_json(d["blocks_json"]) or []
        d["shuffle"] = bool(d["shuffle"])
        out.append(d)
    return out


def get_script(db: Database, script_id: int) -> dict | None:
    row = db.fetchone("SELECT * FROM scripts WHERE id = ?", (script_id,))
    if not row:
        return None
    d = dict(row)
    d["blocks"] = Database.from_json(d["blocks_json"]) or []
    d["shuffle"] = bool(d["shuffle"])
    return d


def update_script(db: Database, script_id: int, name: str | None = None,
                  blocks: list[dict] | None = None,
                  shuffle: bool | None = None) -> None:
    sets, params = [], []
    if name is not None:
        if not name.strip():
            raise ValueError("Script name cannot be empty.")
        sets.append("name = ?")
        params.append(name.strip())
    if blocks is not None:
        errors = validate_blocks(blocks)
        if errors:
            raise ValueError("; ".join(errors))
        sets.append("blocks_json = ?")
        params.append(Database.to_json(blocks))
    if shuffle is not None:
        sets.append("shuffle = ?")
        params.append(1 if shuffle else 0)
    if not sets:
        return
    params.append(script_id)
    db.execute(f"UPDATE scripts SET {', '.join(sets)} WHERE id = ?", tuple(params))


def delete_script(db: Database, script_id: int) -> None:
    db.execute("DELETE FROM scripts WHERE id = ?", (script_id,))


def next_block_id(blocks: list[dict]) -> str:
    nums = []
    for b in blocks:
        bid = b.get("id", "")
        if bid.startswith("b") and bid[1:].isdigit():
            nums.append(int(bid[1:]))
    return f"b{(max(nums) + 1) if nums else 1}"


def describe_block(b: dict) -> str:
    p = b.get("params", {})
    t = b.get("type")
    if t == "post":
        return f"Post {p.get('count_min')}-{p.get('count_max')} (product #{p.get('product_id')})"
    if t == "drafts":
        return f"Draft {p.get('count_min')}-{p.get('count_max')} (product #{p.get('product_id')})"
    if t == "publish_drafts":
        return f"Publish drafts ({p.get('publish_mode')})"
    if t == "relist_edit":
        return (f"Relist & edit {p.get('count_min')}-{p.get('count_max')} "
                f"(product #{p.get('product_id')})")
    if t == "delete_relist":
        return f"Delete & relist (product #{p.get('product_id')})"
    if t == "wait_next_turn":
        return f"Wait for next turn ({p.get('wait_minutes')} min)"
    if t == "login":
        return "Login (verify session)"
    if t == "browser_launch":
        return "Browser launch (keep open)"
    if t == "browser_launch_fast":
        return "Browser launch (keep open)"
    if t == "renew_listing":
        return (f"Renew listings "
                f"({p.get('min_renew', 1)}–{p.get('max_renew', 5)})")
    if t == "delete_duplicate":
        return "Delete duplicate listings"
    if t == "delete_previous":
        return "Delete listings from previous runs"
    if t == "delete_draft":
        return "Delete drafts"
    if t == "delete_take_action":
        return "Delete 'take action' flagged listings"
    if t == "delete_all_invalid":
        return "Delete all invalid (failed) listings"
    if t == "change_location":
        return f"Change marketplace location to '{p.get('location')}'"
    if t == "download_images_titles":
        return (f"Download images & titles from open tabs "
                f"(product #{p.get('product_id')})")
    if t == "group":
        ms = p.get("members") or []
        return f"Group: run {p.get('min_select')}-{p.get('max_select')} of {len(ms)}"
    if "min_minutes" in p:
        return (f"{t.replace('_', ' ').title()} "
                f"({p.get('min_minutes')}-{p.get('max_minutes')} min)")
    mins = p.get("minutes", p.get("seconds", ""))
    unit = "min" if "minutes" in p else "s"
    return f"{t.replace('_', ' ').title()} ({mins}{unit})"


# ------------------------------------------------- dashboard builder meta ---
# Display labels for block types (mirrors the v6.2.0.0 script-builder labels).
BLOCK_LABELS: dict[str, str] = {
    "post": "Post",
    "drafts": "Create Drafts",
    "publish_drafts": "Publish Drafts",
    "relist_edit": "Relist & Edit",
    "delete_relist": "Delete & Relist",
    "renew_listing": "Renew Listing",
    "download_images_titles": "Download Images & Titles",
    "delete_duplicate": "Delete Duplicate",
    "delete_draft": "Delete Draft",
    "delete_previous": "Delete Previous",
    "delete_take_action": "Delete Take Action Listings",
    "delete_all_invalid": "Delete All Invalid Listings",
    "scroll_feed": "Scroll Feed",
    "watch_videos": "Watch Videos",
    "watch_reels": "Watch Reels",
    "scroll_marketplace": "Scroll Marketplace",
    "read_notifications": "Read Notifications",
    "scroll_messages": "Scroll Messages",
    "wait_next_turn": "Wait For Next Turn",
    "goto_messages": "Go to Messages",
    "login": "Login",
    "browser_launch": "Browser Launch",
    "browser_launch_fast": "Browser Launch Fast",
    "change_location": "Change Marketplace Location",
    "group": "Group (K of N)",
}

# AVAILABLE ACTIONS panel grouping (v6.2.0.0 script builder).
BUILDER_CATEGORIES: list[tuple[str, list[str]]] = [
    ("LISTINGS", ["post", "drafts", "publish_drafts", "relist_edit",
                  "delete_relist", "renew_listing"]),
    ("DELETE", ["delete_duplicate", "delete_draft", "delete_previous",
                "delete_take_action", "delete_all_invalid"]),
    ("ACTIVITY", ["scroll_feed", "watch_videos", "watch_reels",
                  "scroll_marketplace", "read_notifications",
                  "scroll_messages"]),
    ("OTHER", ["wait_next_turn", "goto_messages", "login", "browser_launch",
               "browser_launch_fast", "change_location",
               "download_images_titles", "group"]),
]

# Per-activity settings dialogs: exact field labels + helper texts from the
# v6.2.0.0 screenshots. Fields are (param_key, label, helper_text).
# watch_videos / read_notifications / scroll_messages follow the same pattern
# by analogy (best-effort: the real tool's exact texts were not captured).
BLOCK_SETTINGS_META: dict[str, dict] = {
    "scroll_feed": {
        "title": "Scroll Feed Settings",
        "fields": [
            ("min_minutes", "Min minutes", ""),
            ("max_minutes", "Max minutes", ""),
            ("keep_scrolling_chance", "Keep-scrolling chance (%)",
             "How often it just keeps scrolling instead of pausing to read. "
             "Realistic: 40–70%. Higher = breezes through the feed; "
             "lower = reads more posts."),
            ("like_a_post_chance", "Like-a-post chance (%)",
             "Chance to like a post you're looking at during a reading "
             "pause. Keep it low 5–20% so it doesn't look spammy (likes are "
             "also spaced ~15s apart)."),
        ],
    },
    "watch_reels": {
        "title": "Watch Reels Settings",
        "fields": [
            ("min_minutes", "Min minutes", ""),
            ("max_minutes", "Max minutes", ""),
            ("skip_chance", "Skip chance (%)",
             "Chance to skip a reel to the next one. Realistic: 30 to 50%."),
            ("like_chance", "Like chance (%)",
             "Chance to like a reel you watched. Keep it realistic, "
             "around 30 to 50%."),
        ],
    },
    "watch_videos": {
        "title": "Watch Videos Settings",
        "fields": [
            ("min_minutes", "Min minutes", ""),
            ("max_minutes", "Max minutes", ""),
            ("skip_chance", "Skip chance (%)",
             "Chance to skip a video early and move on. "
             "Realistic: 10 to 30%."),
            ("like_chance", "Like chance (%)",
             "Chance to like a video after watching it. "
             "Keep it realistic, around 20 to 40%."),
        ],
    },
    "scroll_marketplace": {
        "title": "Scroll Marketplace Settings",
        "fields": [
            ("min_minutes", "Min minutes", ""),
            ("max_minutes", "Max minutes", ""),
            ("keep_scrolling_chance", "Keep-scrolling chance (%)",
             "How often it just keeps scrolling instead of pausing to look. "
             "Realistic: 40–60%. Marketplace scrolling is slower since "
             "you're scanning items."),
            ("hover_a_listing_chance", "Hover-a-listing chance (%)",
             "During a pause, how often it moves the mouse over a listing "
             "or two. Realistic: 50–80%."),
            ("open_a_listing_chance", "Open-a-listing chance (%)",
             "Chance to open a listing, look for a few seconds, then go "
             "back. Keep it low 3–10% (opens are also spaced ~30s apart)."),
        ],
    },
    "read_notifications": {
        "title": "Read Notifications Settings",
        "fields": [
            ("min_seconds", "Min seconds", ""),
            ("max_seconds", "Max seconds", ""),
        ],
    },
    "scroll_messages": {
        "title": "Scroll Messages Settings",
        "fields": [
            ("min_seconds", "Min seconds", ""),
            ("max_seconds", "Max seconds", ""),
        ],
    },
}

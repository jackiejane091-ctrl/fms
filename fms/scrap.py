"""Scrap Module: turn competitor Marketplace listings into your product data.

Workflow (from the analyzed tool):
  1. scrape_listings(driver, urls, ...) extracts public data per listing URL.
  2. Global overrides replace scraped values; per-listing `customize` wins.
  3. "Always edit description" hook lets you swap in your own contact info
     (5-minute timeout per listing, else the listing is skipped).
  4. save_as_product() stores the set as a Product so the normal Listing
     Engine can post it.

Field priority: customized > global override > original scraped value.

The tool is for the user's own competitive research. Scraped text/photos must
be rewritten/paraphrased (titles/descriptions in your own words, your own
contact) — never repost someone else's content verbatim.
"""
from __future__ import annotations

import concurrent.futures
import itertools
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import config
from . import products as products_mod
from .db import Database, utcnow

# Fallback when a scraped listing has no description (Facebook requires one).
DEFAULT_FALLBACK_DESCRIPTION = "Contact for more details."


def _download_photos(photo_urls: list[str], dest: Path,
                     limit: int = 10) -> list[str]:
    """Download photo URLs into dest. Returns local paths (stdlib only)."""
    dest.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for i, url in enumerate(photo_urls[:limit]):
        try:
            ext = Path(url.split("?")[0]).suffix or ".jpg"
            if len(ext) > 5:
                ext = ".jpg"
            target = dest / f"scraped_{i}{ext}"
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp, \
                    open(target, "wb") as fh:
                fh.write(resp.read(8 * 1024 * 1024))  # cap 8 MiB per photo
            saved.append(str(target))
        except Exception:
            continue
    return saved


def _run_hook_with_timeout(hook: Callable[[dict], Any], raw: dict,
                           timeout_s: float) -> tuple[bool, Any]:
    """Run the edit-description hook; (timed_out, result)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(hook, dict(raw))
        try:
            return False, fut.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            return True, None
        except Exception:
            return False, None


def scrape_listings(
    driver,
    urls: list[str],
    *,
    category: str,
    overrides: dict | None = None,
    customize: dict[str, dict] | None = None,
    edit_description: Callable[[dict], str | None] | None = None,
    always_edit_description: bool = False,
    fallback_description: str = DEFAULT_FALLBACK_DESCRIPTION,
    country_append: str = "",
    backup_location: str = "",
    timeout_s: float = 300.0,
    download_photos: bool = True,
    log: Callable[[str], None] | None = None,
) -> list[dict]:
    """Scrape listing URLs into normalized item dicts.

    overrides: {"titles": [...], "locations": [...], "descriptions": [...],
                "price": "123" | 123} — applied in rotation across urls.
    customize: {url: {"title": ..., "location": ..., "price": ...,
                      "description": ..., "availability": ...}} — wins per url.
    edit_description: hook(raw) -> new description (or None to keep/skip).
    Returns items: {title, price, description, location, availability,
                    category, photo_paths, source_url}.
    """
    overrides = overrides or {}
    customize = customize or {}
    log = log or (lambda m: None)

    title_cycle = itertools.cycle(overrides["titles"]) \
        if overrides.get("titles") else None
    loc_cycle = itertools.cycle(overrides["locations"]) \
        if overrides.get("locations") else None
    desc_cycle = itertools.cycle(overrides["descriptions"]) \
        if overrides.get("descriptions") else None

    items: list[dict] = []
    for url in urls:
        custom = customize.get(url, {})
        raw = driver.scrape_listing(url)
        if not raw:
            log(f"scrap: skipped {url} (no data)")
            continue

        # -- description: hook (with timeout) -> fallback -> raw -------------
        description = raw.get("description") or ""
        if always_edit_description and edit_description:
            timed_out, new_desc = _run_hook_with_timeout(
                edit_description, raw, timeout_s)
            if timed_out:
                log(f"scrap: skipped {url} (description edit timed out)")
                continue
            if new_desc:
                description = new_desc
        if not description and fallback_description:
            description = fallback_description

        # -- merge with priority: customized > override > original ------------
        title = custom.get("title") or \
            (next(title_cycle) if title_cycle else None) or raw.get("title")
        location = custom.get("location") or \
            (next(loc_cycle) if loc_cycle else None) or raw.get("location") or ""
        if not custom.get("location") and not (loc_cycle and overrides.get("locations")):
            # original location: country-append rule applies only here
            if location and country_append and country_append.lower() not in location.lower():
                location = f"{location} {country_append}".strip()
        if not location and backup_location:
            location = backup_location
        if "description" in custom:
            description = custom["description"]
        elif desc_cycle:
            description = next(desc_cycle)
        price = custom.get("price", overrides.get("price", raw.get("price")))
        availability = custom.get("availability") or raw.get("availability") or "In Stock"

        # -- photos -----------------------------------------------------------
        photo_paths: list[str] = []
        raw_photos = raw.get("photo_paths") or raw.get("photo_urls") or []
        if raw_photos and all(str(p).startswith("http") for p in raw_photos):
            if download_photos:
                dest = config.data_dir() / "scraped" / str(int(time.time()))
                photo_paths = _download_photos(raw_photos, dest)
            else:
                photo_paths = []
        elif raw_photos:
            photo_paths = [str(p) for p in raw_photos]  # already local (mock)

        items.append({
            "title": title,
            "price": price,
            "description": description,
            "location": location,
            "availability": availability,
            "category": category,
            "photo_paths": photo_paths,
            "source_url": url,
        })
        log(f"scrap: collected '{title}'")
    return items


def save_as_product(db: Database, name: str, items: list[dict],
                    include_description: bool = True,
                    include_location: bool = True) -> int:
    """Store scraped items as a Product the Listing Engine can post.

    Per the user (2026-10-15): a scraped competitor listing becomes your own
    listing — title is always copied; description and location are optional.
    """
    if not items:
        raise ValueError("nothing scraped — no product created")
    name = (name or "").strip() or f"Scraped {time.strftime('%Y-%m-%d %H:%M')}"
    pid = products_mod.create_product(db, name)
    products_mod.set_titles(db, pid, [i["title"] for i in items if i.get("title")])
    if include_description:
        products_mod.set_descriptions(
            db, pid, [i["description"] for i in items if i.get("description")])
    if include_location:
        products_mod.set_locations(
            db, pid, [i["location"] for i in items if i.get("location")])
    db.execute("UPDATE products SET scraped_json = ? WHERE id = ?",
               (Database.to_json(items), pid))
    # Photos live on disk; point the product's image folder at them if any.
    photo_dirs = {str(Path(p).parent) for i in items
                  for p in i.get("photo_paths", []) if p}
    if photo_dirs:
        db.execute("UPDATE products SET image_folder = ? WHERE id = ?",
                   (sorted(photo_dirs)[0], pid))
    db.log(f"scrap: saved {len(items)} item(s) as product '{name}' (#{pid})")
    return pid


def get_scraped_items(db: Database, product_id: int) -> list[dict]:
    prod = products_mod.get_product(db, product_id)
    if not prod:
        return []
    return prod.get("scraped_items") or []

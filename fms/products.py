"""Product Manager: products, rotating titles, locations, images, categories.

Categories are extensible without code changes: built-in defaults live below and
a user file (config.categories_path(), JSON) can add/override them.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config
from .db import Database, utcnow

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

# Built-in category -> attribute definitions. Each attribute: {"name": ..., "type":
# "text"|"select", "options": [...]}. Extend via categories.json in the data dir.
BUILTIN_CATEGORIES: dict[str, dict] = {
    "Furniture": {"attributes": []},
    "Furniture > Beds & bed frames": {
        "attributes": [
            {"name": "color", "type": "text"},
            {"name": "bed_size", "type": "select",
             "options": ["Single", "Double", "King", "Queen", "Super King"]},
        ]
    },
    "Furniture > Wardrobes": {
        "attributes": [
            {"name": "decor_style", "type": "text"},
            {"name": "finish", "type": "text"},
        ]
    },
    "Furniture > Sofas": {
        "attributes": [
            {"name": "color", "type": "text"},
            {"name": "seats", "type": "select",
             "options": ["1", "2", "3", "4", "5+"]},
        ]
    },
    "Furniture > Bar stools": {"attributes": [{"name": "color", "type": "text"}]},
}


def load_categories() -> dict[str, dict]:
    cats = dict(BUILTIN_CATEGORIES)
    path = config.categories_path()
    if path.exists():
        try:
            extra = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(extra, dict):
                cats.update(extra)
        except (OSError, json.JSONDecodeError):
            pass
    return cats


LISTING_TYPES = ("item", "vehicle", "property")

DELIVERY_OPTIONS = ("public_meetup", "door_pickup", "door_dropoff")
DELIVERY_LABELS = {
    "public_meetup": "Public Meetup",
    "door_pickup": "Door Pickup",
    "door_dropoff": "Door Dropoff",
}


def normalize_category(category: str | None) -> str | None:
    """Store category paths with the real tool's '||' separator."""
    if not category:
        return category
    return category.replace(" > ", "||").strip()


def category_attributes(category: str | None) -> list[dict]:
    if not category:
        return []
    cats = load_categories()
    hit = cats.get(category) or cats.get(category.replace("||", " > "))
    return (hit or {}).get("attributes", [])


# ------------------------------------------------------------------ CRUD ---
def create_product(
    db: Database,
    name: str,
    base_price: float | None = None,
    description: str | None = None,
    tags: str | None = None,
    category: str | None = None,
    attributes: dict | None = None,
    image_folder: str | None = None,
    video_path: str | None = None,
    # Alignment pass #4: full Add Product dialog fields.
    listing_type: str = "item",
    condition: str | None = None,
    availability: str | None = None,
    append_title_count: int = 0,
    main_image_folder: str | None = None,
    extra_image_folder: str | None = None,
    delivery_options: list[str] | None = None,
    descriptions: list[str] | None = None,
) -> int:
    name = name.strip()
    if not name:
        raise ValueError("Product name cannot be empty.")
    listing_type = (listing_type or "item").strip().lower()
    if listing_type not in LISTING_TYPES:
        raise ValueError(f"listing_type must be one of {LISTING_TYPES}")
    main_image_folder = main_image_folder or image_folder
    cur = db.execute(
        """INSERT INTO products
           (name, base_price, description, tags, category, attributes_json,
            image_folder, video_path, listing_type, condition, availability,
            append_title_count, main_image_folder, extra_image_folder,
            delivery_options_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            name,
            base_price,
            description,
            tags,
            normalize_category(category),
            json.dumps(attributes or {}, ensure_ascii=False),
            image_folder,
            video_path,
            listing_type,
            condition,
            availability,
            int(append_title_count or 0),
            main_image_folder,
            extra_image_folder,
            json.dumps([o for o in (delivery_options or []) if o in DELIVERY_OPTIONS]),
            utcnow(),
        ),
    )
    pid = cur.lastrowid
    if descriptions:
        set_descriptions(db, pid, descriptions)
    return pid


def clone_product(db: Database, product_id: int) -> int:
    """Duplicate a product with all fields, lists and image folders."""
    src = get_product(db, product_id)
    if not src:
        raise ValueError("product not found")
    base = src["name"]
    n = 1
    while db.fetchone("SELECT id FROM products WHERE name = ?",
                       (f"{base} (copy{'' if n == 1 else f' {n}'}",)):
        n += 1
    new_name = f"{base} (copy{'' if n == 1 else f' {n}'}"
    pid = create_product(
        db, new_name, base_price=src.get("base_price"),
        description=src.get("description"), tags=src.get("tags"),
        category=src.get("category"), attributes=src.get("attributes"),
        image_folder=src.get("image_folder"), video_path=src.get("video_path"),
        listing_type=src.get("listing_type") or "item",
        condition=src.get("condition"), availability=src.get("availability"),
        append_title_count=src.get("append_title_count") or 0,
        main_image_folder=src.get("main_image_folder"),
        extra_image_folder=src.get("extra_image_folder"),
        delivery_options=src.get("delivery_options"),
        descriptions=src.get("descriptions"),
    )
    set_titles(db, pid, src.get("titles") or [])
    set_locations(db, pid, src.get("locations") or [])
    return pid


def list_products(db: Database) -> list[dict]:
    rows = db.fetchall("SELECT * FROM products ORDER BY name")
    out = []
    for r in rows:
        d = dict(r)
        d["title_count"] = db.fetchone(
            "SELECT COUNT(*) AS c FROM titles WHERE product_id = ?", (r["id"],)
        )["c"]
        d["location_count"] = db.fetchone(
            "SELECT COUNT(*) AS c FROM locations WHERE product_id = ?", (r["id"],)
        )["c"]
        used, total = image_stats(db, r["id"])
        d["images_used"] = used
        d["image_count"] = total
        d["listing_type"] = d.get("listing_type") or "item"
        out.append(d)
    return out


def get_product(db: Database, product_id: int) -> dict | None:
    row = db.fetchone("SELECT * FROM products WHERE id = ?", (product_id,))
    if not row:
        return None
    d = dict(row)
    d["titles"] = [
        r["value"]
        for r in db.fetchall(
            "SELECT value FROM titles WHERE product_id = ? ORDER BY position, id",
            (product_id,),
        )
    ]
    d["locations"] = [
        r["value"]
        for r in db.fetchall(
            "SELECT value FROM locations WHERE product_id = ? ORDER BY position, id",
            (product_id,),
        )
    ]
    d["images"] = list_images(d.get("image_folder"))
    d["main_images"] = list_images(d.get("main_image_folder") or d.get("image_folder"))
    d["extra_images"] = list_images(d.get("extra_image_folder"))
    d["attributes"] = json.loads(d.get("attributes_json") or "{}")
    d["descriptions"] = [
        r["value"]
        for r in db.fetchall(
            "SELECT value FROM descriptions WHERE product_id = ? ORDER BY position, id",
            (product_id,),
        )
    ]
    try:
        d["delivery_options"] = json.loads(d.get("delivery_options_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["delivery_options"] = []
    d["listing_type"] = d.get("listing_type") or "item"
    d["append_title_count"] = d.get("append_title_count") or 0
    try:
        d["scraped_items"] = json.loads(d.get("scraped_json") or "null")
    except (json.JSONDecodeError, TypeError):
        d["scraped_items"] = None
    return d


def update_product(db: Database, product_id: int, **fields) -> None:
    allowed = {"name", "base_price", "description", "tags", "category",
               "image_folder", "video_path", "listing_type", "condition",
               "availability", "append_title_count", "main_image_folder",
               "extra_image_folder"}
    sets, params = [], []
    for key, value in fields.items():
        if key == "attributes":
            sets.append("attributes_json = ?")
            params.append(json.dumps(value or {}, ensure_ascii=False))
        elif key == "delivery_options":
            sets.append("delivery_options_json = ?")
            params.append(json.dumps(
                [o for o in (value or []) if o in DELIVERY_OPTIONS]))
        elif key == "category":
            sets.append("category = ?")
            params.append(normalize_category(value))
        elif key == "listing_type":
            if value not in LISTING_TYPES:
                raise ValueError(f"listing_type must be one of {LISTING_TYPES}")
            sets.append("listing_type = ?")
            params.append(value)
        elif key in allowed:
            sets.append(f"{key} = ?")
            params.append(value)
        else:
            raise ValueError(f"Unknown product field: {key}")
    if not sets:
        return
    params.append(product_id)
    db.execute(f"UPDATE products SET {', '.join(sets)} WHERE id = ?", tuple(params))


def delete_product(db: Database, product_id: int) -> None:
    db.execute("DELETE FROM products WHERE id = ?", (product_id,))


# ------------------------------------------------------- titles/locations ---
def set_titles(db: Database, product_id: int, titles: list[str]) -> int:
    db.execute("DELETE FROM titles WHERE product_id = ?", (product_id,))
    rows = [(product_id, t.strip(), i) for i, t in enumerate(titles) if t.strip()]
    db.executemany(
        "INSERT INTO titles (product_id, value, position) VALUES (?, ?, ?)", rows
    )
    return len(rows)


def append_titles(db: Database, product_id: int, titles: list[str]) -> int:
    """Append titles without wiping existing ones (Download Images & Titles)."""
    row = db.fetchone(
        "SELECT COALESCE(MAX(position), -1) AS m FROM titles WHERE product_id = ?",
        (product_id,),
    )
    start = (row["m"] if row else -1) + 1
    rows = [(product_id, t.strip(), start + i)
            for i, t in enumerate(titles) if t and t.strip()]
    db.executemany(
        "INSERT INTO titles (product_id, value, position) VALUES (?, ?, ?)", rows
    )
    return len(rows)


def set_locations(db: Database, product_id: int, locations: list[str]) -> int:
    db.execute("DELETE FROM locations WHERE product_id = ?", (product_id,))
    rows = [(product_id, loc.strip(), i) for i, loc in enumerate(locations) if loc.strip()]
    db.executemany(
        "INSERT INTO locations (product_id, value, position) VALUES (?, ?, ?)", rows
    )
    return len(rows)


def parse_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


# ------------------------------------------------------------------ images ---
def list_images(image_folder: str | None) -> list[str]:
    """Return image file paths in a folder (sorted). Empty list if unset/missing."""
    if not image_folder:
        return []
    folder = Path(image_folder)
    if not folder.is_dir():
        return []
    return sorted(
        str(p) for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS
    )


# --------------------------------------------------------------- descriptions
def set_descriptions(db: Database, product_id: int, descriptions: list[str]) -> int:
    """Multiple descriptions, rotated across listings (real tool's dialog)."""
    db.execute("DELETE FROM descriptions WHERE product_id = ?", (product_id,))
    rows = [(product_id, d.strip(), i) for i, d in enumerate(descriptions) if d and d.strip()]
    db.executemany(
        "INSERT INTO descriptions (product_id, value, position) VALUES (?, ?, ?)", rows
    )
    return len(rows)


# ------------------------------------------------------------ image tracking
def record_image_usage(db: Database, product_id: int, image_paths: list[str]) -> None:
    """Track per-image usage so the table can show 'used / total'."""
    for p in image_paths:
        db.execute(
            """INSERT INTO image_usage (product_id, image_path, use_count)
               VALUES (?, ?, 1)
               ON CONFLICT (product_id, image_path)
               DO UPDATE SET use_count = use_count + 1""",
            (product_id, str(p)),
        )


def image_stats(db: Database, product_id: int) -> tuple[int, int]:
    """Return (used_images, total_images) for the Manage Products table."""
    row = db.fetchone("SELECT * FROM products WHERE id = ?", (product_id,))
    total = 0
    if row:
        d = dict(row)
        total = len(list_images(d.get("main_image_folder") or d.get("image_folder"))) \
            + len(list_images(d.get("extra_image_folder")))
    used_row = db.fetchone(
        "SELECT COUNT(*) AS c FROM image_usage WHERE product_id = ? AND use_count > 0",
        (product_id,),
    )
    return (used_row["c"] if used_row else 0), total

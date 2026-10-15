"""App-wide settings — saved once, applied to every run (FBM Robo style).

Settings live in the `app_settings` key-value table. The Settings page edits
them; the run engine reads them as defaults.
"""
from __future__ import annotations

DEFAULTS: dict[str, str] = {
    # Posting behavior
    "batch_size": "3",
    "shuffle": "1",
    "hide_from_friends": "1",
    "use_human_behaviour": "1",
    "min_seconds_to_post": "25",
    "max_seconds_to_post": "40",
    # Browsers / accounts
    "max_parallel_browsers": "5",
    "account_start_gap_s": "5",
    "retry_count": "3",
    "skip_images_days": "8",
    "tabs_without_limit": "2",
    "tabs_draft_publish": "6",
    # Typing mode
    "typing_mode": "human",  # human | fast_js | clipboard
    "typing_preset": "normal",  # fast | normal | careful
    "min_delay_ms": "30",
    "max_delay_ms": "80",
    "thinking_pause_min_ms": "200",
    "thinking_pause_max_ms": "600",
    "extra_delay_after_punctuation_ms": "80",
    "newline_pause_min_ms": "300",
    "newline_pause_max_ms": "900",
    "hybrid_threshold_chars": "500",
    # IXBrowser (optional)
    "ix_browser_enabled": "0",
    "ix_local_api_host": "127.0.0.1",
    "ix_local_api_port": "53200",
}


def get_all(db) -> dict[str, str]:
    out = dict(DEFAULTS)
    try:
        for row in db.fetchall("SELECT key, value FROM app_settings"):
            out[row["key"]] = row["value"]
    except Exception:
        pass
    return out


def get(db, key: str) -> str:
    return get_all(db).get(key, DEFAULTS.get(key, ""))


def save_all(db, values: dict[str, str]) -> None:
    for key, value in values.items():
        if key not in DEFAULTS:
            continue
        db.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

"""Experimental one-product Marketplace form test.

This module intentionally does NOT publish, click Next, save a draft, or alter
product data. It accepts the already-built payload and an already-started
PlaywrightDriver instance from FMS. The GUI/integration layer should call
run_single_post_test(driver, payload) on a dedicated test action.

The existing production _fill_form() is not called. The established photo
upload mechanism is retained here without changing production photo code.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import human, selectors as S


def _record(results: list[dict[str, str]], field: str, expected: Any,
            actual: Any, status: str, detail: str = "") -> None:
    results.append({
        "field": field,
        "expected": "[configured]" if expected is not None else "[not configured]",
        "expected_value": str(expected) if expected is not None else "",
        "actual_value": str(actual) if actual is not None else "",
        "status": status,
        "detail": detail,
    })


def _visible_value(locator) -> str:
    """Read a control's current value or rendered selected text."""
    try:
        value = locator.input_value(timeout=1500)
        if value is not None:
            return str(value).strip()
    except Exception:
        pass
    try:
        value = locator.get_attribute("value", timeout=1500)
        if value is not None:
            return str(value).strip()
    except Exception:
        pass
    try:
        return (locator.inner_text(timeout=1500) or "").strip()
    except Exception:
        return ""


def _field_value(page, selector) -> str:
    try:
        return _visible_value(S.to_playwright(page, selector))
    except Exception:
        return ""


def _fill_text(page, selector, field: str, value: Any,
               results: list[dict[str, str]], timeout: int = 8000) -> None:
    if value is None or str(value).strip() == "":
        _record(results, field, None, "", "SKIPPED", "No saved value configured")
        return
    expected = str(value).strip()
    try:
        loc = S.to_playwright(page, selector)
        loc.wait_for(state="visible", timeout=timeout)
        try:
            loc.scroll_into_view_if_needed(timeout=2500)
        except Exception:
            pass
        loc.click(timeout=3000)
        loc.fill(expected, timeout=5000)
        actual = _visible_value(loc)
        if actual == expected:
            _record(results, field, expected, actual, "VERIFIED")
        else:
            _record(results, field, expected, actual, "FAILED",
                    "Control value differs after fill")
    except Exception as exc:
        _record(results, field, expected, "", "FAILED",
                f"{type(exc).__name__}: {str(exc)[:180]}")


def _select_option(page, field_selector, expected: Any, field: str,
                   results: list[dict[str, str]], exact: bool = True,
                   timeout: int = 8000) -> None:
    if expected is None or str(expected).strip() == "":
        _record(results, field, None, "", "SKIPPED", "No saved value configured")
        return
    wanted = str(expected).strip()
    try:
        field_loc = S.to_playwright(page, field_selector)
        field_loc.wait_for(state="visible", timeout=timeout)
        try:
            field_loc.scroll_into_view_if_needed(timeout=2500)
        except Exception:
            pass
        field_loc.click(timeout=4000)
        human.human_pause(250, 450)
        option = page.get_by_role("option", name=wanted, exact=exact).first
        try:
            option.wait_for(state="visible", timeout=5000)
        except Exception:
            # Fallback is constrained to the open listbox, never a global text click.
            option = page.locator("[role='listbox']").get_by_text(
                wanted, exact=exact).first
            option.wait_for(state="visible", timeout=3500)
        option.click(timeout=4000)
        human.human_pause(150, 300)
        actual = _visible_value(field_loc)
        # Facebook dropdowns vary in DOM semantics. Require the selected field
        # to visibly contain the exact saved value before declaring success.
        if actual == wanted or wanted in actual:
            _record(results, field, wanted, actual, "VERIFIED")
        else:
            _record(results, field, wanted, actual, "UNCERTAIN",
                    "Option clicked, but selected field text did not exactly confirm it")
    except Exception as exc:
        _record(results, field, wanted, "", "FAILED",
                f"{type(exc).__name__}: {str(exc)[:180]}")


def _upload_photos_unchanged(page, payload: dict,
                             results: list[dict[str, str]]) -> None:
    """Use the current established chooser/direct-input upload approach."""
    images = [str(p) for p in (payload.get("image_paths") or [])[:10]
              if p and Path(str(p)).is_file()]
    if not images:
        _record(results, "photos", None, "", "SKIPPED",
                "No existing image files in payload")
        return
    attempted = False
    last_error = ""
    try:
        with page.expect_file_chooser(timeout=8000) as chooser_info:
            add = page.get_by_text("Add photos", exact=False).first
            add.wait_for(state="visible", timeout=5000)
            human.human_click(page, add)
        chooser_info.value.set_files(images)
        attempted = True
    except Exception as exc:
        last_error = f"file chooser: {type(exc).__name__}: {str(exc)[:100]}"
    if not attempted:
        for selector in ("input[type='file']", "input[type='file'][accept*='image']"):
            try:
                page.set_input_files(selector, images, timeout=5000)
                attempted = True
                break
            except Exception as exc:
                last_error = f"direct input: {type(exc).__name__}: {str(exc)[:100]}"
    if attempted:
        human.human_pause(900, 1400)
        # Don't trust upload-attempt alone; count rendered image previews when possible.
        try:
            count = page.locator("img").count()
            _record(results, "photos", len(images),
                    f"upload supplied {len(images)} file(s); page img nodes={count}",
                    "ATTEMPTED",
                    "Existing upload method used; rendered img count is diagnostic only")
        except Exception:
            _record(results, "photos", len(images), f"upload supplied {len(images)} file(s)",
                    "ATTEMPTED", "Existing upload method used; visual confirmation needed")
    else:
        _record(results, "photos", len(images), "", "FAILED", last_error)


def run_single_post_test(driver, payload: dict) -> dict[str, Any]:
    """Fill one listing form and stop before Next/Publish.

    Args:
        driver: a logged-in FMS PlaywrightDriver instance. Its browser/session
                startup remains owned by the existing FMS architecture.
        payload: payload built from the selected product's latest DB data using
                 the existing product loader/payload builder.

    Returns a JSON-serializable report. This function never calls Next/Publish.
    """
    started = datetime.now(timezone.utc).isoformat()
    results: list[dict[str, str]] = []
    page = None
    try:
        driver._ensure()
        page = driver._page
        if page is None:
            raise RuntimeError("FMS driver did not provide an active page")
        ok, detail = driver._open_create_item_form(page)
        if not ok:
            _record(results, "form_open", True, detail, "FAILED", "Create form did not open")
            return _report(started, results, page)

        # Keep the existing working photo upload behavior isolated from the
        # experimental field logic.
        _upload_photos_unchanged(page, payload, results)

        _fill_text(page, S.F_TITLE, "title", payload.get("title"), results)
        price = payload.get("price")
        _fill_text(page, S.F_PRICE, "price",
                   ("{:.2f}".format(float(price)) if price not in (None, "") else None), results)
        _select_option(page, S.F_CATEGORY, payload.get("category"), "category", results,
                       exact=False)
        _select_option(page, S.F_CONDITION_FIELD, payload.get("condition"), "condition", results,
                       exact=True)

        # Reveal the optional/detail controls without changing any global click helper.
        expanded = False
        for label in ("More details", "Show more details", "More Details"):
            try:
                loc = page.get_by_text(label, exact=False).first
                if loc.is_visible(timeout=700):
                    loc.click(timeout=2500)
                    human.human_pause(200, 400)
                    expanded = True
                    break
            except Exception:
                continue
        _record(results, "more_details", True, expanded,
                "INFO" if expanded else "UNCERTAIN",
                "Detail section expansion status")

        _fill_text(page, S.F_DESCRIPTION, "description", payload.get("description"), results)

        availability_raw = (payload.get("availability") or "")
        availability_map = {
            "in_stock": "List as in stock",
            "list as in stock": "List as in stock",
            "stock": "List as in stock",
            "single": "List as single item",
            "list as single item": "List as single item",
        }
        availability_label = availability_map.get(str(availability_raw).strip().lower())
        if availability_label:
            _select_option(page, S.F_AVAILABILITY_FIELD, availability_label,
                           "availability", results, exact=False)
        elif availability_raw:
            _record(results, "availability", availability_raw, "", "SKIPPED",
                    "Saved availability value has no known exact Facebook label mapping")
        else:
            _record(results, "availability", None, "", "SKIPPED",
                    "No saved value configured")

        location = payload.get("location")
        if location and str(location).strip():
            try:
                driver._select_location(page, str(location).strip())
                actual = _field_value(page, S.F_LOCATION)
                status = "VERIFIED" if str(location).split(",")[0].strip().lower() in actual.lower() else "UNCERTAIN"
                _record(results, "location", location, actual, status,
                        "Uses existing location suggestion picker; verify selected suggestion")
            except Exception as exc:
                _record(results, "location", location, "", "FAILED",
                        f"{type(exc).__name__}: {str(exc)[:180]}")
        else:
            _record(results, "location", None, "", "SKIPPED", "No saved value configured")

        # Delivery values come from product configuration only. Never toggle an
        # unconfigured option, and inspect state before clicking to avoid deselection.
        delivery_selectors = {
            "public_meetup": S.F_DELIVERY_MEETUP,
            "door_pickup": S.F_DELIVERY_PICKUP,
            "door_dropoff": S.F_DELIVERY_DROPOFF,
        }
        delivery_values = payload.get("delivery_options") or []
        if isinstance(delivery_values, str):
            try:
                delivery_values = json.loads(delivery_values)
            except Exception:
                delivery_values = [delivery_values]
        if not delivery_values:
            _record(results, "delivery_options", None, "", "SKIPPED",
                    "No delivery options configured")
        for option_key in delivery_values:
            selector = delivery_selectors.get(option_key)
            if not selector:
                _record(results, f"delivery:{option_key}", option_key, "", "SKIPPED",
                        "Unrecognized configured delivery key")
                continue
            try:
                loc = S.to_playwright(page, selector)
                loc.wait_for(state="visible", timeout=6000)
                try:
                    loc.scroll_into_view_if_needed(timeout=2500)
                except Exception:
                    pass
                state = loc.evaluate("e => e.checked !== undefined ? e.checked : e.getAttribute('aria-checked')")
                if state not in (True, "true"):
                    loc.click(timeout=3000)
                state_after = loc.evaluate("e => e.checked !== undefined ? e.checked : e.getAttribute('aria-checked')")
                status = "VERIFIED" if state_after in (True, "true") else "UNCERTAIN"
                _record(results, f"delivery:{option_key}", option_key, state_after,
                        status, "Checkbox/toggle state after action")
            except Exception as exc:
                _record(results, f"delivery:{option_key}", option_key, "", "FAILED",
                        f"{type(exc).__name__}: {str(exc)[:180]}")

        # Hide-from-friends is a global run setting, not product data. Apply only
        # when explicitly true in the supplied payload, and never toggle OFF.
        if payload.get("hide_from_friends") is True:
            try:
                hff = S.to_playwright(page, S.F_HIDE_FROM_FRIENDS)
                hff.wait_for(state="visible", timeout=6000)
                state = hff.get_attribute("aria-checked")
                if state != "true":
                    hff.click(timeout=3000)
                after = hff.get_attribute("aria-checked")
                _record(results, "hide_from_friends", True, after,
                        "VERIFIED" if after == "true" else "UNCERTAIN")
            except Exception as exc:
                _record(results, "hide_from_friends", True, "", "FAILED",
                        f"{type(exc).__name__}: {str(exc)[:180]}")
        else:
            _record(results, "hide_from_friends", payload.get("hide_from_friends"), "",
                    "SKIPPED", "Setting is not explicitly enabled")

        # Deliberately DO NOT click Next, Publish, Save, or submit any form.
        try:
            next_button = S.to_playwright(page, S.F_NEXT_BUTTON)
            next_state = "enabled" if next_button.is_enabled(timeout=1500) else "disabled"
        except Exception:
            next_state = "not found"
        _record(results, "next_button", "not clicked", next_state, "INFO",
                "Safety guard: experimental test never clicks Next or Publish")
        return _report(started, results, page)
    except Exception as exc:
        _record(results, "fatal", "complete form test", "", "FAILED",
                f"{type(exc).__name__}: {str(exc)[:220]}")
        return _report(started, results, page)


def _report(started: str, results: list[dict[str, str]], page=None) -> dict[str, Any]:
    try:
        current_url = page.url if page is not None else ""
    except Exception:
        current_url = ""
    counts: dict[str, int] = {}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "test": "single_post_experimental",
        "started_at_utc": started,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "page_url": current_url,
        "publish_attempted": False,
        "next_clicked": False,
        "summary": counts,
        "fields": results,
    }

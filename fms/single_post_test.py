"""One-product Marketplace workflow: fill, publish once, open Selling.

This module fills the form, clicks Next once whenever Facebook enables it,
then clicks Facebook's final Publish button exactly once and navigates to
the Marketplace Selling page. It never saves a draft or alters product data.
It accepts the already-built payload and an already-started PlaywrightDriver
instance from FMS. The GUI/integration layer should call
run_single_post_test(driver, payload) on the Test Single Post action.

The existing production _fill_form() is not called. The established photo
upload mechanism is retained here without changing production photo code.
"""
from __future__ import annotations

import time
import random
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import human, selectors as S


def _pause_checkpoint(payload: dict) -> None:
    """Pause only at a safe boundary between Test Single Post UI phases.

    The callback is the same run-controller checkpoint used by production
    posting. Each field/action is allowed to finish; the next checkpoint
    blocks until Continue, or raises on Stop.
    """
    callback = payload.get("_pause_checkpoint")
    if callable(callback):
        callback()


def _parse_amount(text: Any) -> float | None:
    """Extract a numeric amount from currency-formatted text like "£5" or "5.00"."""
    cleaned = re.sub(r"[^\d.\-]", "", str(text or ""))
    parts = cleaned.split(".")
    if len(parts) > 2:
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(cleaned) if cleaned not in ("", "-", ".", "-.") else None
    except Exception:
        return None


def _amounts_equal(a: Any, b: Any, tol: float = 0.005) -> bool:
    pa, pb = _parse_amount(a), _parse_amount(b)
    if pa is None or pb is None:
        return False
    return abs(pa - pb) <= tol


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
               results: list[dict[str, str]], timeout: int = 8000,
               numeric_compare: bool = False) -> None:
    if value is None or str(value).strip() == "":
        _record(results, field, None, "", "SKIPPED", "No saved value configured")
        return
    expected = str(value).strip()
    try:
        loc = S.to_playwright(page, selector)
        # Fix 1: attached wait like production (browser.py), not strict visible.
        loc.wait_for(timeout=timeout)
        try:
            loc.scroll_into_view_if_needed(timeout=2500)
        except Exception:
            pass
        loc.click(timeout=3000)
        loc.fill(expected, timeout=5000)
        actual = _visible_value(loc)
        if numeric_compare:
            # Fix 3: normalize currency/format ("£5" vs "5.00"), compare numbers.
            if _amounts_equal(actual, expected):
                _record(results, field, expected, actual, "VERIFIED")
            else:
                _record(results, field, expected, actual, "FAILED",
                        "Numeric amount differs after fill")
        elif actual == expected:
            _record(results, field, expected, actual, "VERIFIED")
        else:
            _record(results, field, expected, actual, "FAILED",
                    "Control value differs after fill")
    except Exception as exc:
        _record(results, field, expected, "", "FAILED",
                f"{type(exc).__name__}: {str(exc)[:180]}")


def _select_option(page, field_selector, expected: Any, field: str,
                   results: list[dict[str, str]], exact: bool = True,
                   timeout: int = 8000,
                   production_sequence: bool = False) -> None:
    if expected is None or str(expected).strip() == "":
        _record(results, field, None, "", "SKIPPED", "No saved value configured")
        return
    wanted = str(expected).strip()
    try:
        field_loc = S.to_playwright(page, field_selector)
        # Fix 1 (dropdown fields): attached wait like production, not strict visible.
        field_loc.wait_for(timeout=timeout)
        if production_sequence:
            # Fix 2: mirror the proven production interaction sequence
            # (browser.py Category flow): human click, settle pause,
            # listbox option filter, human click.
            human.human_click(page, field_loc)
            human.human_pause(300, 600)
            option = page.locator(
                "[role='listbox'] [role='option'], "
                "[role='menu'] [role='menuitem']").filter(
                has_text=wanted).first
            try:
                option.wait_for(timeout=6000)
            except Exception:
                option = page.get_by_text(wanted, exact=False).first
                option.wait_for(timeout=6000)
            human.human_click(page, option)
            human.human_pause(100, 250)
        else:
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
            # Fix 7: attached wait exactly like production (browser.py uses
            # wait_for(timeout=5000)); strict "visible" stalled the chooser.
            add.wait_for(timeout=5000)
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
        # Verify via the "Photos • X/10" counter (same as production).
        _photo_count = 0
        try:
            _counter = page.get_by_text("Photos", exact=False).first
            _ctext = _counter.inner_text(timeout=3000) or ""
            _m = re.search(r"(\d+)\s*/\s*10", _ctext)
            if _m:
                _photo_count = int(_m.group(1))
        except Exception:
            _photo_count = 0
        if _photo_count > 0:
            _record(results, "photos", len(images),
                    f"{_photo_count}/10 photos visible",
                    "VERIFIED",
                    "Photos counter confirms uploaded previews")
        else:
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


def _more_details_state(page) -> tuple[str, Any]:
    """Locate the 'More details' toggle and read its aria-expanded state.

    Returns (state, button) with state in {'true','false','unknown'}.
    Read-only — never clicks.
    """
    for label in ("More details", "Show more details", "More Details"):
        try:
            raw = page.get_by_text(label, exact=False)
            if raw.count() == 0:
                continue
            text_loc = raw.first
            # The toggle is the role="button" ancestor carrying aria-expanded.
            btn = text_loc.locator(
                "xpath=ancestor::*[@role='button'][@aria-expanded][1]")
            if btn.count() > 0:
                state = btn.first.evaluate(
                    "e => e.getAttribute('aria-expanded')")
                return ("true" if state == "true"
                        else "false" if state == "false" else "unknown",
                        btn.first)
            # Fallback: the attribute may live on the text node itself.
            state = text_loc.evaluate("e => e.getAttribute('aria-expanded')")
            return ("true" if state == "true"
                    else "false" if state == "false" else "unknown",
                    text_loc)
        except Exception:
            continue
    return "unknown", None




def _snapshot_existing_listing_ids(driver) -> set[str] | None:
    """Best-effort baseline of listing IDs, using a temporary tab.

    This lets the post-publish check distinguish a newly created listing from
    an older listing that happens to have the same title. Failure to obtain a
    baseline is non-fatal; in that case only an explicit Facebook success
    signal or a new listing-item URL can confirm success.
    """
    context = getattr(driver, "_context", None)
    if context is None:
        return None
    probe = None
    try:
        probe = context.new_page()
        probe.goto("https://www.facebook.com/marketplace/you/selling/",
                   wait_until="domcontentloaded", timeout=12000)
        try:
            probe.wait_for_timeout(1400)
        except Exception:
            pass
        ids: set[str] = set()
        for link in probe.locator("a[href*='/marketplace/item/']").all()[:100]:
            try:
                href = link.get_attribute("href") or ""
                match = re.search(r"/marketplace/item/([^/?#]+)", href)
                if match:
                    ids.add(match.group(1))
            except Exception:
                continue
        # An empty set can mean Facebook did not render listing cards yet;
        # treat that as an unavailable baseline rather than risking a false
        # success match against an older same-title listing.
        return ids if ids else None
    except Exception:
        return None
    finally:
        if probe is not None:
            try:
                probe.close()
            except Exception:
                pass


def _explicit_publish_success(page) -> bool:
    """Return True only for an explicit Facebook success message/item URL."""
    try:
        url = str(page.url or "")
        match = re.search(r"/marketplace/item/([^/?#]+)", url)
        if match and match.group(1):
            return True
    except Exception:
        pass
    try:
        body = re.sub(r"\s+", " ", (page.locator("body").inner_text(timeout=1500) or "").lower())
    except Exception:
        body = ""
    patterns = (
        r"\byour listing (?:has been |was |is )?(?:now )?(?:published|live|active)\b",
        r"\blisting (?:has been |was )?published successfully\b",
        r"\byour item is now listed\b",
        r"\bpublished successfully\b",
    )
    return any(re.search(pattern, body) for pattern in patterns)


def _new_listing_link_matches(page, title: str, baseline_ids: set[str] | None) -> bool:
    """Check for the expected title on a listing ID not present before publish."""
    if baseline_ids is None or not str(title or "").strip():
        return False
    wanted = re.sub(r"\s+", " ", str(title)).strip().casefold()
    try:
        links = page.locator("a[href*='/marketplace/item/']").all()[:100]
    except Exception:
        return False
    for link in links:
        try:
            href = link.get_attribute("href") or ""
            match = re.search(r"/marketplace/item/([^/?#]+)", href)
            if not match or match.group(1) in baseline_ids:
                continue
            text = re.sub(r"\s+", " ", (link.inner_text(timeout=1000) or "")).strip().casefold()
            if text and (text == wanted or wanted in text):
                return True
        except Exception:
            continue
    return False


def run_single_post_test(driver, payload: dict) -> dict[str, Any]:
    """Fill one listing form, verify fields, click Next once, then stop before Publish.

    Args:
        driver: a logged-in FMS PlaywrightDriver instance. Its browser/session
                startup remains owned by the existing FMS architecture.
        payload: payload built from the selected product's latest DB data using
                 the existing product loader/payload builder.

    Returns a JSON-serializable report. This flow clicks Next and the final Publish button.
    """
    started = datetime.now(timezone.utc).isoformat()
    results: list[dict[str, str]] = []
    page = None
    # Match production per-listing soft timing. The first listing can consume
    # the browser-launch timestamp; subsequent listings get a fresh start.
    _pending_start = getattr(driver, "_pending_browser_start_time", None)
    _listing_t0 = _pending_start if _pending_start is not None else time.monotonic()
    if _pending_start is not None:
        driver._pending_browser_start_time = None
    try:
        _pause_checkpoint(payload)
        driver._ensure()
        _launched_at = getattr(driver, "_pending_browser_start_time", None)
        if _launched_at is not None:
            _listing_t0 = _launched_at
            driver._pending_browser_start_time = None
        page = driver._page
        if page is None:
            raise RuntimeError("FMS driver did not provide an active page")
        # Capture existing listing IDs in a temporary tab before creating the
        # new listing. This avoids treating an older same-title listing as proof
        # that this Publish attempt succeeded.
        _baseline_listing_ids = _snapshot_existing_listing_ids(driver)
        _pause_checkpoint(payload)
        ok, detail = driver._open_create_item_form(page)
        if not ok:
            _record(results, "form_open", True, detail, "FAILED", "Create form did not open")
            return _report(started, results, page)

        # Keep the existing working photo upload behavior isolated from the
        # experimental field logic.
        _pause_checkpoint(payload)
        _upload_photos_unchanged(page, payload, results)

        _pause_checkpoint(payload)
        _fill_text(page, S.F_TITLE, "title", payload.get("title"), results)
        _pause_checkpoint(payload)
        price = payload.get("price")
        _fill_text(page, S.F_PRICE, "price",
                   ("{:.2f}".format(float(price)) if price not in (None, "") else None), results,
                   numeric_compare=True)
        _pause_checkpoint(payload)
        _select_option(page, S.F_CATEGORY, payload.get("category"), "category", results,
                       exact=False, production_sequence=True)
        _pause_checkpoint(payload)
        _select_option(page, S.F_CONDITION_FIELD, payload.get("condition"), "condition", results,
                       exact=True)

        # --- More details: state-aware expansion (F-A / F-B / F-C) ---
        # Hoist the detail-field config BEFORE the gate so both branches
        # report honestly (configured vs missing).
        availability_raw = (payload.get("availability") or "")
        availability_map = {
            "in_stock": "List as in stock",
            "in stock": "List as in stock",
            "list as in stock": "List as in stock",
            "stock": "List as in stock",
            "single": "List as single item",
            "list as single item": "List as single item",
        }
        availability_label = availability_map.get(str(availability_raw).strip().lower())
        location = payload.get("location")
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

        # F-A: read the toggle's current aria-expanded BEFORE deciding.
        _pause_checkpoint(payload)
        _md_state, _md_btn = _more_details_state(page)
        if _md_state == "true":
            # Already open — clicking would COLLAPSE it, so do NOT click.
            pass
        elif _md_state == "false":
            # F-B: exactly one production-style human click.
            try:
                human.human_click(page, _md_btn)
                human.human_pause(200, 400)
            except Exception:
                pass
        else:
            # Requirement 2: no blind toggle on unknown/missing state.
            _record(results, "more_details", "expanded", _md_state, "UNCERTAIN",
                    "More details toggle state unreadable — no blind click performed")

        # F-C: click execution is NOT success. Verify the aria flip AND the
        # description control's actual DOM presence before trusting expansion.
        expanded_verified = False
        if _md_state in ("true", "false"):
            try:
                page.wait_for_function(
                    """() => {
                        const btns = [...document.querySelectorAll('[role="button"][aria-expanded]')];
                        const md = btns.find(e => (e.innerText || '').toLowerCase().includes('more details'));
                        return !!md && md.getAttribute('aria-expanded') === 'true';
                    }""", timeout=6000)
            except Exception:
                pass
            _md_state_after, _ = _more_details_state(page)
            try:
                S.to_playwright(page, S.F_DESCRIPTION).wait_for(timeout=8000)
                expanded_verified = True
            except Exception:
                expanded_verified = False
            _record(results, "more_details", True, expanded_verified,
                    "INFO" if expanded_verified else "UNCERTAIN",
                    (f"aria-expanded {_md_state}->{_md_state_after}; description control present"
                     if expanded_verified else "More details expansion not verified"))

        if expanded_verified:
            _pause_checkpoint(payload)
            _fill_text(page, S.F_DESCRIPTION, "description", payload.get("description"), results)

            if availability_label:
                _pause_checkpoint(payload)
                _select_option(page, S.F_AVAILABILITY_FIELD, availability_label,
                               "availability", results, exact=False,
                               production_sequence=True)
            elif availability_raw:
                _record(results, "availability", availability_raw, "", "SKIPPED",
                        "Saved availability value has no known exact Facebook label mapping")
            else:
                _record(results, "availability", None, "", "SKIPPED",
                        "No saved value configured")

            if location and str(location).strip():
                _pause_checkpoint(payload)
                try:
                    _loc_ok, _loc_detail = driver._select_location(
                        page, str(location).strip())
                    actual = _field_value(page, S.F_LOCATION)
                    _city = str(location).split(",")[0].strip().lower()
                    if _loc_ok and _city in actual.lower():
                        status = "VERIFIED"
                    elif _loc_ok:
                        # Picker verified internally, but this read-back could
                        # not confirm it — stay honest, don't claim success.
                        status = "UNCERTAIN"
                    else:
                        status = "FAILED"
                    _record(results, "location", location, actual, status,
                            _loc_detail or "Existing location suggestion picker result")
                except Exception as exc:
                    _record(results, "location", location, "", "FAILED",
                            f"{type(exc).__name__}: {str(exc)[:180]}")
            else:
                _record(results, "location", None, "", "SKIPPED", "No saved value configured")

            # Delivery values come from product configuration only. Never toggle an
            # unconfigured option, and inspect state before clicking to avoid deselection.
            if not delivery_values:
                _record(results, "delivery_options", None, "", "SKIPPED",
                        "No delivery options configured")
            for option_key in delivery_values:
                _pause_checkpoint(payload)
                selector = delivery_selectors.get(option_key)
                if not selector:
                    _record(results, f"delivery:{option_key}", option_key, "", "SKIPPED",
                            "Unrecognized configured delivery key")
                    continue
                try:
                    loc = S.to_playwright(page, selector)
                    loc.wait_for(timeout=6000)
                    try:
                        loc.scroll_into_view_if_needed(timeout=2500)
                    except Exception:
                        pass
                    # Fix 5: read state from the actual checkbox/toggle element,
                    # not the text label (a span has no .checked / aria-checked).
                    state_el = loc
                    for _xp in ("ancestor::*[@role='checkbox'][1]",
                                "ancestor::*[@role='switch'][1]",
                                "ancestor::label[1]//input[@type='checkbox']"):
                        try:
                            _cand = loc.locator(f"xpath={_xp}")
                            if _cand.count() > 0:
                                state_el = _cand.first
                                break
                        except Exception:
                            continue
                    _state_js = ("e => e.checked !== undefined ? e.checked : "
                                 "e.getAttribute('aria-checked')")
                    state = state_el.evaluate(_state_js)
                    if state not in (True, "true"):
                        loc.click(timeout=3000)
                    state_after = state_el.evaluate(_state_js)
                    status = "VERIFIED" if state_after in (True, "true") else "UNCERTAIN"
                    _record(results, f"delivery:{option_key}", option_key, state_after,
                            status, "Checkbox/toggle state after action")
                except Exception as exc:
                    _record(results, f"delivery:{option_key}", option_key, "", "FAILED",
                            f"{type(exc).__name__}: {str(exc)[:180]}")
        else:
            # F-C (requirement 4): shared diagnostic — never blame the
            # individual field selectors when the section itself did not open.
            _shared_err = "More details expansion not verified"
            if payload.get("description"):
                _record(results, "description", payload.get("description"), "",
                        "FAILED", _shared_err)
            if availability_label:
                _record(results, "availability", availability_raw, "", "FAILED",
                        _shared_err)
            elif availability_raw:
                _record(results, "availability", availability_raw, "", "SKIPPED",
                        "Saved availability value has no known exact Facebook label mapping")
            else:
                _record(results, "availability", None, "", "SKIPPED",
                        "No saved value configured")
            if location and str(location).strip():
                _record(results, "location", location, "", "FAILED", _shared_err)
            else:
                _record(results, "location", None, "", "SKIPPED", "No saved value configured")
            if delivery_values:
                _record(results, "delivery_options", delivery_values, "", "FAILED",
                        _shared_err)
            else:
                _record(results, "delivery_options", None, "", "SKIPPED",
                        "No delivery options configured")

        # Hide-from-friends is a global run setting, not product data. Apply only
        # when explicitly true in the supplied payload, and never toggle OFF.
        if payload.get("hide_from_friends") is True:
            _pause_checkpoint(payload)
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

        # Soft timing target for this listing. This is not a deadline: if form
        # fill/browser loading took longer than Max, proceed without extra wait.
        try:
            _min_s = max(0.0, float(payload.get("min_seconds_to_post", 25)))
        except (TypeError, ValueError):
            _min_s = 25.0
        try:
            _max_s = max(0.0, float(payload.get("max_seconds_to_post", 40)))
        except (TypeError, ValueError):
            _max_s = 40.0
        if _max_s < _min_s:
            _min_s, _max_s = _max_s, _min_s
        _target_s = random.uniform(_min_s, _max_s)
        _remaining = _target_s - (time.monotonic() - _listing_t0)
        if _remaining > 0:
            _pause_checkpoint(payload)
            time.sleep(_remaining)

        # Navigation policy: prioritize Facebook's actual Next button. A generic
        # button[type=submit] is NOT evidence that Publish is ready: Facebook may
        # render Next as a submit button. If Next is visible and enabled, click it
        # once, then locate the specifically labelled final Publish button.
        _pause_checkpoint(payload)
        try:
            next_button = page.get_by_role("button", name="Next", exact=True).first
            next_button.wait_for(state="visible", timeout=0)
            if next_button.is_enabled():
                next_button.click(timeout=5000)
                _record(results, "next_button", "click once when enabled", "clicked", "VERIFIED",
                        "Next was visible and enabled; clicked once before checking final Publish")
                # Facebook may transition as a SPA without changing the URL.
                # Wait briefly for the explicitly labelled Publish control.
                try:
                    S.to_playwright(page, S.F_FINAL_PUBLISH_BUTTON).wait_for(
                        state="visible", timeout=12000)
                except Exception:
                    pass  # final lookup below records the actual outcome
            else:
                final_now = S.to_playwright(page, S.F_FINAL_PUBLISH_BUTTON)
                if final_now.is_visible():
                    _record(results, "next_button", "not needed", "final Publish visible", "SKIPPED",
                            "Next was disabled and the actual final Publish button was already visible")
                else:
                    _record(results, "next_button", "click once", "disabled", "FAILED",
                            "Next is disabled and final Publish is not visible")
                    return _report(started, results, page)
        except Exception as next_exc:
            # Only the specifically labelled Publish button can establish that
            # the flow is already on the final screen; generic submit buttons do not.
            final_now = S.to_playwright(page, S.F_FINAL_PUBLISH_BUTTON)
            try:
                final_visible = final_now.is_visible()
            except Exception:
                final_visible = False
            if final_visible:
                _record(results, "next_button", "not needed", "final Publish visible", "SKIPPED",
                        "Next was not present; actual final Publish button is visible")
            else:
                _record(results, "next_button", "click once", "not found", "FAILED",
                        f"Next was not found and final Publish was not visible: {type(next_exc).__name__}: {str(next_exc)[:140]}")
                return _report(started, results, page)

        # Final step uses a dedicated Publish-labelled selector, never the
        # legacy generic submit selector that can match Next.
        try:
            publish_button = S.to_playwright(page, S.F_FINAL_PUBLISH_BUTTON)
            publish_button.wait_for(state="visible", timeout=12000)
            if not publish_button.is_enabled():
                _record(results, "publish_click", "one click", "disabled", "FAILED",
                        "Final Publish button is visible but disabled")
                return _report(started, results, page)
        except Exception as exc:
            _record(results, "publish_click", "one click", "not attempted", "FAILED",
                    f"Actual Publish button did not appear: {type(exc).__name__}: {str(exc)[:150]}")
            return _report(started, results, page)
        _pause_checkpoint(payload)
        try:
            human.human_click(page, publish_button)
            _record(results, "publish_click", "one click", "clicked", "ATTEMPTED",
                    "Clicked Facebook's final Publish button once")
            # User-required policy: a Publish click immediately consumes the
            # listing's photos for this account's 8-day cooldown. Do not wait
            # for Facebook's success wording, URL, or listing-card confirmation.
            _cooldown_callback = payload.get("_on_publish_clicked")
            if callable(_cooldown_callback):
                try:
                    _cooldown_callback()
                    _record(results, "image_cooldown", "record selected photos",
                            "recorded at Publish click", "VERIFIED",
                            "Image cooldown was written immediately after the Publish click")
                except Exception as _cooldown_exc:
                    _record(results, "image_cooldown", "record selected photos",
                            "record failed", "FAILED",
                            f"{type(_cooldown_exc).__name__}: {str(_cooldown_exc)[:180]}")
        except Exception as exc:
            _record(results, "publish_click", "one click", "click failed", "FAILED",
                    f"{type(exc).__name__}: {str(exc)[:180]}")
            return _report(started, results, page)
        human.human_pause(4000, 6000)
        _pause_checkpoint(payload)
        # Capture any explicit Facebook success toast or item-detail URL before
        # leaving the post-publish screen.
        publish_confirmed = _explicit_publish_success(page)
        selling_url = "https://www.facebook.com/marketplace/you/selling/"
        try:
            page.goto(selling_url, wait_until="domcontentloaded", timeout=20000)
            human.human_pause(1200, 2000)
            if not publish_confirmed:
                publish_confirmed = _new_listing_link_matches(
                    page, payload.get("title", ""), _baseline_listing_ids)
            _record(results, "selling_page", selling_url, page.url,
                    "VERIFIED" if "/marketplace/you/selling" in page.url else "UNCERTAIN",
                    "Opened the Marketplace Selling page after the Publish click")
        except Exception as exc:
            _record(results, "selling_page", selling_url, "", "UNCERTAIN",
                    f"Navigation after the Publish click failed: {type(exc).__name__}: {str(exc)[:150]}")
        _record(results, "publish_confirmation", True,
                "confirmed" if publish_confirmed else "not confirmed",
                "VERIFIED" if publish_confirmed else "NOT_CONFIRMED",
                ("Facebook showed a success signal or a newly created listing was found"
                 if publish_confirmed else
                 "Publish success was not confirmed; image cooldown was already recorded at the Publish click"))
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
        "publish_attempted": any(row["field"] == "publish_click" and row["status"] == "ATTEMPTED" for row in results),
        "publish_success_confirmed": any(row["field"] == "publish_confirmation" and row["status"] == "VERIFIED" for row in results),
        "next_clicked": any(row["field"] == "next_button" and row["expected_value"] == "click once when enabled" and row["status"] in {"VERIFIED", "UNCERTAIN", "FAILED"} for row in results),
        "summary": counts,
        "fields": results,
    }

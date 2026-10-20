"""Targeted tests for the v0.2.102 identity-guarded _select_location.

Runs the REAL PlaywrightDriver._select_location in a local headless
Chromium against HTML fixtures that mimic the Facebook form geometry
established from the saved DOM:

- A: THE BUG — the Location input has not rendered (only its label and
  an empty slot exist); Title/Price sit ABOVE it; an unrelated search
  input sits BELOW. The old fallback climbed to the panel and typed
  into Title. Now: safe 'field:' failure, Title byte-identical.
- B: Location input rendered inside its own section row (no <label>
  association, like Facebook) with a working suggestion listbox.
  Selection must succeed end-to-end, Title untouched.
- C: Location input reachable via the primary label lookup. Selection
  must succeed, Title untouched.

Run:  python -m fms.tests.location_select_test
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fms import human as human_mod          # noqa: E402
from fms.browser import PlaywrightDriver    # noqa: E402

PASS = 0
FAIL = 0

# Keep the human pauses tiny so the suite stays fast (clicks stay real).
human_mod.human_pause = lambda a, b: time.sleep(0.02)  # noqa: E731

TITLE_ROW = """
<div class="row"><label><div><span>Title</span>
<input type="text" id="title" value="Original Title"></div></label></div>
<div class="row"><label><div><span>Price</span>
<input type="text" id="price"></div></label></div>
"""

SUGGESTION_JS = """
<script>
(function(){
  var inp = document.getElementById('locinput');
  var sug = document.getElementById('sug');
  if (inp && sug) {
    inp.addEventListener('input', function(){ sug.style.display = 'block'; });
    sug.querySelector('[role="option"]').addEventListener('click', function(e){
      inp.value = e.target.textContent.trim();
      sug.style.display = 'none';
    });
  }
})();
</script>
"""

LISTBOX = """
<div role="listbox" id="sug" style="display:none">
  <div role="option">Coventry, United Kingdom</div>
</div>
"""

HTML_A = f"""<html><body><div id="panel">
{TITLE_ROW}
<div class="row"><div><span><span>Location</span></span></div>
<div class="slot">&nbsp;</div></div>
<div class="row"><div><span><span>Search</span></span>
<input type="text" id="search"></div></div>
</div></body></html>"""

HTML_B = f"""<html><body><div id="panel">
{TITLE_ROW}
<div class="row"><div><span><span>Location</span></span></div>
<div class="slot"><input type="text" id="locinput"></div>{LISTBOX}</div>
</div>{SUGGESTION_JS}</body></html>"""

HTML_C = f"""<html><body><div id="panel">
{TITLE_ROW}
<div class="row"><label>Location <input type="text" id="locinput"></label>{LISTBOX}</div>
</div>{SUGGESTION_JS}</body></html>"""

# v0.2.102's live failure mode: the input IS rendered inside its section
# but has no caption text of its own nearby (just a placeholder), and the
# section's text does not start with 'Location' — the caption-based
# identity check rejected the real input, so nothing was touched at all.
HTML_D = f"""<html><body><div id="panel">
{TITLE_ROW}
<div class="section">
  <div class="sec-note">Where is the item?</div>
  <div class="sec-head"><span><span>Location</span></span></div>
  <div class="sec-body"><div class="slot">
    <input type="text" id="locinput" placeholder="Enter a town or city">
  </div>{LISTBOX}</div>
</div>
</div>{SUGGESTION_JS}</body></html>"""

# Location input missing, but OTHER text inputs (tags, SKU) exist in
# sibling rows BEFORE the Location row inside the same details section.
# Nothing may be typed anywhere.
HTML_E = f"""<html><body><div id="panel">
{TITLE_ROW}
<div class="details">
  <div class="row"><div><span><span>Product tags</span></span>
  <input type="text" id="tags"></div></div>
  <div class="row"><div><span><span>SKU</span></span>
  <input type="text" id="sku"></div></div>
  <div class="row"><div><span><span>Location</span></span></div>
  <div class="slot">&nbsp;</div></div>
</div>
</div></body></html>"""


def check(name, fn):
    global PASS, FAIL
    try:
        fn()
    except Exception as exc:
        FAIL += 1
        print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    else:
        PASS += 1
        print(f"  ok    {name}")


def _driver():
    d = PlaywrightDriver.__new__(PlaywrightDriver)
    d.account = {}
    d._snap = lambda page, reason: None
    d._note_field_error = lambda page, step, msg: None
    return d


def _run(html):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content(html)
        result = _driver()._select_location(page, "Coventry, United kingdom")
        values = {}
        for el_id in ("title", "price", "tags", "sku", "search"):
            loc = page.locator(f"#{el_id}")
            if loc.count() > 0:
                values[el_id] = loc.input_value()
        browser.close()
        return result, values


def t_a_unrendered_location_never_touches_title():
    (ok, detail), values = _run(HTML_A)
    assert ok is False, (ok, detail)
    assert detail.startswith("field:"), detail
    assert values["title"] == "Original Title", values  # bug erased it
    assert values.get("search", "") == "", values


def t_b_section_input_selects():
    (ok, detail), values = _run(HTML_B)
    assert ok is True, (ok, detail)
    assert "selected" in detail and "tag=INPUT" in detail, detail
    assert values["title"] == "Original Title", values


def t_c_label_input_selects():
    (ok, detail), values = _run(HTML_C)
    assert ok is True, (ok, detail)
    assert values["title"] == "Original Title", values


def t_d_captionless_section_input_selects():
    (ok, detail), values = _run(HTML_D)
    assert ok is True, (ok, detail)
    assert "section match" in detail, detail
    assert values["title"] == "Original Title", values


def t_e_missing_input_types_nowhere():
    (ok, detail), values = _run(HTML_E)
    assert ok is False, (ok, detail)
    assert detail.startswith("field:"), detail
    assert values["title"] == "Original Title", values
    assert values.get("tags", "") == "" and values.get("sku", "") == "", values


def main() -> int:
    print("FMS _select_location identity-guard tests (local Chromium)\n")
    check("A: unrendered Location -> safe failure, Title intact",
          t_a_unrendered_location_never_touches_title)
    check("B: section input -> selected, Title intact", t_b_section_input_selects)
    check("C: label input -> selected, Title intact", t_c_label_input_selects)
    check("D: caption-less section input -> selected (v0.2.102 live mode)",
          t_d_captionless_section_input_selects)
    check("E: missing input + sibling inputs -> types nowhere",
          t_e_missing_input_types_nowhere)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

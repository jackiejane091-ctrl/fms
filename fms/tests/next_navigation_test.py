"""Targeted navigation tests for the Next-when-enabled flow (v0.2.106).

Runs the REAL fms.single_post_test.run_single_post_test (ChatGPT's file,
applied verbatim) in a local headless Chromium against a full-form
fixture with click counters on the Next and Publish buttons:

1. Happy path: every field verifies -> Next is clicked EXACTLY ONCE,
   the transition is verified, Publish is NEVER clicked.
2. UNCERTAIN field (location picked fine, but the report read-back
   cannot confirm it): Facebook's enabled Next is the only gate, so
   Next is STILL clicked once and the transition verified.
3. Next disabled by the page: no click is attempted, the row reports
   the disabled state clearly, Publish untouched.
4. Unconfirmed path: Next click produces no transition -> the row is
   UNCERTAIN (never claimed as success), Publish never clicked.

Run:  python -m fms.tests.next_navigation_test
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fms import human as human_mod          # noqa: E402
from fms.browser import PlaywrightDriver    # noqa: E402
from fms.single_post_test import run_single_post_test  # noqa: E402

PASS = 0
FAIL = 0

human_mod.human_pause = lambda a, b: time.sleep(0.02)  # noqa: E731

FORM_HTML = """<html><head><style>
label.combo{display:block;padding:10px;border:1px solid #999;margin:4px 0}
#mdtoggle{padding:10px;border:1px solid #999}
button{padding:10px 20px}
</style></head><body>
<div id="screen1">
  <label>Title <input type="text" id="title"></label>
  <label><span>Price</span><input type="text" id="price"></label>

  <label role="combobox" class="combo" id="catfield"><span>Category</span> <span id="catval"></span></label>
  <div role="listbox" id="catlist" style="display:none">
    <div role="option">Furniture</div><div role="option">Vehicles</div>
  </div>

  <label role="combobox" class="combo" id="condfield"><span>Condition</span> <span id="condval"></span></label>
  <div role="listbox" id="condlist" style="display:none">
    <div role="option">New</div><div role="option">Used - Good</div>
  </div>

  <div role="button" aria-expanded="false" id="mdtoggle"><span>More details</span></div>
  <div id="details" style="display:none">
    <label>Description <textarea id="desc"></textarea></label>
    <div id="avail">Availability</div>
    <div role="listbox" id="availlist" style="display:none">
      <div role="option">List as Single Item</div>
      <div role="option">List as in stock</div>
    </div>
    <label>Location <input type="text" id="locinput"></label>
    <div role="listbox" id="sug" style="display:none">
      <div role="option">Coventry, United Kingdom</div>
    </div>
    <label><input type="checkbox" id="dd"><span>Door drop-off</span></label>
  </div>

  <button type="button" id="nextbtn">Next</button>
</div>
<div id="screen2" style="display:none">
  <div>Publish your listing</div>
  <button type="button" id="pubbtn">Publish</button>
</div>
<script>
window.__nextClicks = 0; window.__pubClicks = 0;
function pick(listId, valId){
  var list = document.getElementById(listId);
  list.querySelectorAll('[role="option"]').forEach(function(o){
    o.addEventListener('click', function(){
      document.getElementById(valId).textContent = o.textContent.trim();
      list.remove();  // Facebook unmounts closed listboxes from the DOM
    });
  });
}
var catlist = document.getElementById('catlist');
document.getElementById('catfield').addEventListener('click', function(){ catlist.style.display = 'block'; });
pick('catlist', 'catval');
var condlist = document.getElementById('condlist');
document.getElementById('condfield').addEventListener('click', function(){ condlist.style.display = 'block'; });
pick('condlist', 'condval');
var avail = document.getElementById('avail');
var availlist = document.getElementById('availlist');
avail.addEventListener('click', function(){ availlist.style.display = 'block'; });
availlist.querySelectorAll('[role="option"]').forEach(function(o){
  o.addEventListener('click', function(){
    avail.textContent = 'Availability: ' + o.textContent.trim();
    availlist.remove();
  });
});
var mdt = document.getElementById('mdtoggle');
mdt.addEventListener('click', function(){
  var open = mdt.getAttribute('aria-expanded') === 'true';
  mdt.setAttribute('aria-expanded', open ? 'false' : 'true');
  document.getElementById('details').style.display = open ? 'none' : 'block';
});
var loc = document.getElementById('locinput');
var sug = document.getElementById('sug');
loc.addEventListener('input', function(){ sug.style.display = 'block'; });
sug.querySelector('[role="option"]').addEventListener('click', function(e){
  loc.value = e.target.textContent.trim();
  sug.style.display = 'none';
});
document.getElementById('pubbtn').addEventListener('click', function(){ window.__pubClicks++; });
</script>
</body></html>"""

NEXT_OK_JS = """<script>
document.getElementById('nextbtn').addEventListener('click', function(){
  window.__nextClicks++;
  document.getElementById('screen1').style.display = 'none';
  document.getElementById('screen2').style.display = 'block';
});
</script>"""

NEXT_DEAD_JS = """<script>
document.getElementById('nextbtn').addEventListener('click', function(){
  window.__nextClicks++;
});
</script>"""

PAYLOAD = {
    "title": "Test Sofa",
    "price": 5,
    "category": "Furniture",
    "condition": "New",
    "description": "INBOX ME PLEASE FOR PRICE",
    "availability": "In Stock",
    "location": "Coventry, United kingdom",
    "delivery_options": ["door_dropoff"],
    "hide_from_friends": False,
    "image_paths": [],
}


class _FakeDriver:
    """Minimal driver shell: the fixture page IS the opened form, and
    location selection delegates to the REAL PlaywrightDriver code."""

    def __init__(self, page):
        self._page = page
        real = PlaywrightDriver.__new__(PlaywrightDriver)
        real.account = {}
        real._snap = lambda p, r: None
        real._note_field_error = lambda p, s, m: None
        self._real = real

    def _ensure(self):
        return None

    def _open_create_item_form(self, page):
        return True, "fixture form"

    def _select_location(self, page, location):
        return self._real._select_location(page, location)


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


def _run(html, payload):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content(html)
        report = run_single_post_test(_FakeDriver(page), payload)
        clicks = page.evaluate(
            "() => ({next: window.__nextClicks, pub: window.__pubClicks})")
        browser.close()
        return report, clicks


def _rows(report, field):
    return [r for r in report["fields"] if r["field"] == field]


def t_happy_next_once_no_publish():
    report, clicks = _run(FORM_HTML + NEXT_OK_JS, dict(PAYLOAD))
    bad = [r for r in report["fields"]
           if r["status"] in ("FAILED", "UNCERTAIN")]
    assert not bad, bad
    nxt = _rows(report, "next_button")
    assert nxt and nxt[0]["status"] == "VERIFIED", nxt
    assert clicks == {"next": 1, "pub": 0}, clicks
    assert report["next_clicked"] is True, report["next_clicked"]
    assert report["publish_attempted"] is False, report["publish_attempted"]


def t_uncertain_field_does_not_block_next():
    # Location input WITHOUT a <label> association (section layout):
    # the real picker still selects it, but the report's label-based
    # read-back cannot confirm it -> location row is UNCERTAIN. The
    # page's Next button is enabled, and per v0.2.106 that is the only
    # gate: Next must still be clicked exactly once.
    html = FORM_HTML.replace(
        '<label>Location <input type="text" id="locinput"></label>',
        '<div class="sec-head"><span><span>Location</span></span></div>'
        '<div class="slot"><input type="text" id="locinput"></div>')
    assert html != FORM_HTML
    report, clicks = _run(html + NEXT_OK_JS, dict(PAYLOAD))
    loc = _rows(report, "location")
    assert loc and loc[0]["status"] == "UNCERTAIN", loc
    nxt = _rows(report, "next_button")
    assert nxt and nxt[0]["status"] == "VERIFIED", nxt
    assert clicks == {"next": 1, "pub": 0}, clicks
    assert report["publish_attempted"] is False, report["publish_attempted"]


def t_next_disabled_never_clicked():
    html = FORM_HTML.replace(
        '<button type="button" id="nextbtn">Next</button>',
        '<button type="button" id="nextbtn" disabled>Next</button>')
    assert html != FORM_HTML
    report, clicks = _run(html + NEXT_OK_JS, dict(PAYLOAD))
    nxt = _rows(report, "next_button")
    assert nxt and nxt[0]["status"] == "FAILED", nxt
    assert nxt[0]["actual_value"] == "disabled", nxt
    assert "disabled" in nxt[0]["detail"].lower(), nxt
    assert clicks == {"next": 0, "pub": 0}, clicks
    assert report["publish_attempted"] is False, report["publish_attempted"]


def t_unconfirmed_transition_is_uncertain():
    report, clicks = _run(FORM_HTML + NEXT_DEAD_JS, dict(PAYLOAD))
    nxt = _rows(report, "next_button")
    assert nxt and nxt[0]["status"] == "UNCERTAIN", nxt
    assert clicks == {"next": 1, "pub": 0}, clicks
    assert report["publish_attempted"] is False, report["publish_attempted"]


def main() -> int:
    print("FMS Next-only navigation tests (local Chromium)\n")
    check("happy: Next once, transition verified, Publish never clicked",
          t_happy_next_once_no_publish)
    check("uncertain field: does NOT block an enabled Next",
          t_uncertain_field_does_not_block_next)
    check("disabled Next: never clicked, reported clearly",
          t_next_disabled_never_clicked)
    check("unconfirmed: click without transition -> UNCERTAIN, no Publish",
          t_unconfirmed_transition_is_uncertain)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

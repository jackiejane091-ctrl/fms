"""Targeted tests for the v0.2.103 verified _select_category.

Runs the REAL single_post_test._select_category in a local headless
Chromium against a fixture that mimics Facebook's Category combobox:
clicking the combobox opens a listbox popup that has its OWN search box
and a long option list whose initially rendered chunk does NOT contain
the wanted category (virtualized list) — the v0.2.102 live failure mode
(dropdown opened, configured category never selected, flow moved on).

1. Configured category present after search -> clicked, read back from
   the control, VERIFIED, function returns True.
2. Configured category unavailable -> FAILED row with diagnostics,
   function returns False (the caller stops the form workflow).

Run:  python -m fms.tests.category_select_test
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fms import human as human_mod          # noqa: E402
from fms.single_post_test import _select_category  # noqa: E402

PASS = 0
FAIL = 0

human_mod.human_pause = lambda a, b: time.sleep(0.02)  # noqa: E731

HTML = """<html><body><div id="panel">
<div class="row"><label role="combobox" id="catfield"><span>Category</span><span id="catval"></span></label></div>
<div role="listbox" id="catpop" style="display:none">
  <input type="text" id="catsearch">
  <div id="catopts"></div>
</div>
</div>
<script>
(function(){
  var ALL = ['Vehicles','Property','Jobs','Furniture','Electronics'];
  var pop = document.getElementById('catpop');
  var opts = document.getElementById('catopts');
  function render(list){
    opts.innerHTML = '';
    list.forEach(function(t){
      var d = document.createElement('div');
      d.setAttribute('role','option');
      d.textContent = t;
      d.addEventListener('click', function(){
        document.getElementById('catval').textContent = t;
        pop.style.display = 'none';
      });
      opts.appendChild(d);
    });
  }
  render(ALL.slice(0, 2));  // 'Furniture' NOT in the initial chunk
  document.getElementById('catfield').addEventListener('click', function(){
    pop.style.display = 'block';
  });
  document.getElementById('catsearch').addEventListener('input', function(e){
    var q = e.target.value.toLowerCase();
    render(ALL.filter(function(t){ return t.toLowerCase().indexOf(q) >= 0; }));
  });
})();
</script>
</body></html>"""


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


def _run(expected):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_content(HTML)
        results: list[dict[str, str]] = []
        ok = _select_category(page, expected, results)
        shown = page.locator("#catval").inner_text()
        browser.close()
        return ok, results, shown


def t_category_search_select_verified():
    ok, results, shown = _run("Furniture")
    assert ok is True, (ok, results)
    row = [r for r in results if r["field"] == "category"]
    assert row and row[0]["status"] == "VERIFIED", results
    assert shown == "Furniture", shown


def t_category_missing_fails_and_stops():
    ok, results, shown = _run("Boats")
    assert ok is False, (ok, results)  # caller stops the workflow
    row = [r for r in results if r["field"] == "category"]
    assert row and row[0]["status"] == "FAILED", results
    assert "No matching category option" in row[0]["detail"], results
    assert shown == "", shown


def main() -> int:
    print("FMS _select_category verified-selection tests (local Chromium)\n")
    check("category: search-select + read-back VERIFIED",
          t_category_search_select_verified)
    check("category: unavailable option -> FAILED, workflow stops",
          t_category_missing_fails_and_stops)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

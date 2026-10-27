"""Focused source guard for the v0.2.146 Renew listing 1 refresh completion fix."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_refresh_wait_has_bounded_no_listing_completion_without_changing_click_path():
    source = (ROOT / "browser.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PlaywrightDriver")
    impl = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                and n.name == "_renew_listings_from_page_impl")
    text = ast.get_source_segment(source, impl)
    ready = text.split("def wait_for_ready_content()", 1)[1].split(
        "def listing_row_candidates()", 1)[0]
    assert "time.monotonic() - no_content_since >= 30" in ready
    assert 'return "empty"' in ready
    # Re-open the exact Renew Listings URL after each full-list scan; do not
    # rely on page.reload() for Facebook's SPA selling dialog.
    assert 'page.goto(renew_url, wait_until="domcontentloaded", timeout=0)' in text
    assert 'page.reload(wait_until="domcontentloaded", timeout=0)' not in text
    assert "processed.clear()" in text
    assert "attempted_this_pass.clear()" in text
    # Existing row-scoped button selection remains intact; no ID/link fallback added.
    row_branch = text.split("if row_scoped:", 1)[1].split("# Legacy action path", 1)[0]
    assert "a[href*='/marketplace/item/']" not in row_branch
    assert "re.search" not in row_branch

"""Focused tests for the separate Renew listing 1 action.

These tests cover action registration and the mock-driver contract. The real
Facebook DOM is intentionally not claimed as tested here.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_new_action_is_registered_without_replacing_old_action():
    source = (ROOT / "engine.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    actions = next(n.value for n in tree.body
                   if isinstance(n, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "ACTIONS" for t in n.targets))
    pairs = ast.literal_eval(actions)
    assert ("renew_listing", "Renew Listing") in pairs
    assert ("renew_listing_1", "Renew listing 1") in pairs


def test_new_browser_method_has_no_item_link_or_listing_id_extraction():
    source = (ROOT / "browser.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PlaywrightDriver")
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                  and n.name == "renew_listings_from_rows")
    assert "row_scoped=True" in ast.get_source_segment(source, method)
    impl = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                and n.name == "_renew_listings_from_page_impl")
    impl_source = ast.get_source_segment(source, impl)
    assert "if row_scoped:" in impl_source
    # The row-scoped branch is structurally based on clickable Renew buttons;
    # it does not extract IDs from listing links.
    row_branch = impl_source.split("if row_scoped:", 1)[1].split("# Legacy action path", 1)[0]
    assert "a[href*='/marketplace/item/']" not in row_branch
    assert "re.search" not in row_branch
    assert "parent.locator(" in row_branch
    assert "seen_row_positions" not in row_branch
    assert "for index in range(count)" in row_branch
    assert "parent_buttons.count() == 1" in row_branch


def test_new_action_script_builder_metadata_exists():
    source = (ROOT / "scripts.py").read_text(encoding="utf-8")
    assert '"renew_listing_1": {' in source
    assert '"renew_listing_1": "Renew listing 1"' in source


def test_row_scan_does_not_collapse_multiple_buttons_in_a_shared_wrapper():
    source = (ROOT / "browser.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PlaywrightDriver")
    impl = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                and n.name == "_renew_listings_from_page_impl")
    impl_source = ast.get_source_segment(source, impl)
    row_branch = impl_source.split("if row_scoped:", 1)[1].split(
        "# Legacy action path", 1)[0]
    # Each clickable button is yielded as a candidate. Ancestor wrapper text or
    # coordinates must not deduplicate separate rows/buttons into one attempt.
    assert "for index in range(count)" in row_branch
    assert "yield key, None, row, row_buttons" in row_branch
    assert "seen_row_positions" not in row_branch
    assert "__fmsRenewAttemptToken" in row_branch
    assert "key = f" + '"row:{signature}#{occurrence}"' not in row_branch


def test_renew_listing_1_empty_state_is_scoped_to_dialog_not_whole_page():
    source = (ROOT / "browser.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PlaywrightDriver")
    impl = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                and n.name == "_renew_listings_from_page_impl")
    impl_source = ast.get_source_segment(source, impl)
    ready = impl_source.split("def wait_for_ready_content()", 1)[1].split(
        "def listing_row_candidates()", 1)[0]
    assert "page.locator(\"[role='dialog']\")" in ready
    assert 'page.locator("body").inner_text' not in ready
    assert 'dialog.last.inner_text' in ready
    assert 'candidate.is_visible() and candidate.is_enabled()' in ready

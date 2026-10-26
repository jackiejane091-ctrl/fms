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
    assert "row.locator(" in row_branch


def test_new_action_script_builder_metadata_exists():
    source = (ROOT / "scripts.py").read_text(encoding="utf-8")
    assert '"renew_listing_1": {' in source
    assert '"renew_listing_1": "Renew listing 1"' in source

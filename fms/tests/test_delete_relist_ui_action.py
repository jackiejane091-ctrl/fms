"""Targeted source guards for the Delete & Relist UI workflow."""
from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]


def test_delete_relist_uses_dedicated_url_and_exact_empty_state():
    source = (ROOT / "browser.py").read_text(encoding="utf-8")
    assert "https://www.facebook.com/marketplace/selling/relist_items/?is_routable_dialog=true&show_only_delete_and_relist=true" in source
    assert "you have no more listings eligible to be deleted and relisted" in source
    assert "def delete_relist_from_rows(self, max_count: int)" in source
    assert "button:has-text('Delete & Relist')" in source


def test_engine_routes_existing_action_to_ui_workflow_without_product_repost():
    source = (ROOT / "engine.py").read_text(encoding="utf-8")
    assert 'elif mode == "delete_relist":\n            self._delete_relist_ui_flow(account, driver)' in source
    assert "def _delete_relist_ui_flow" in source
    assert '"delete_relist"' not in source.split("POSTING_MODES =", 1)[1].split("PUBLISH_MODES", 1)[0]


def test_dialog_has_delete_relist_min_max_and_no_price_fields():
    source = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert "Min Delete & Relist" in source
    assert "Max Delete & Relist" in source
    assert "else if(actVal === 'delete_relist')" in source
    assert "isDeleteRelist ? 'none'" in source


def test_locked_renew_action_implementation_still_exists():
    source = (ROOT / "browser.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PlaywrightDriver")
    names = [n.name for n in cls.body if isinstance(n, ast.FunctionDef)]
    assert "renew_listings_from_rows" in names
    assert "_renew_listings_from_page_impl" in names

"""Regression checks for the single visible Post action (v0.2.117 patch)."""
from pathlib import Path
import os, sys, tempfile

ROOT = Path(__file__).resolve().parents[2]
# Muse should copy this test to source/fms/tests before running it.
sys.path.insert(0, str(ROOT))
from fms.engine import ACTIONS, MODES, POSTING_MODES, RunConfig

def test_only_one_visible_post_and_it_uses_single_post_flow():
    posts = [(key, label) for key, label in ACTIONS if label == "Post"]
    assert posts == [("test_single_post", "Post")], posts
    assert not any(label == "Test Single Post" for _, label in ACTIONS)

def test_old_complete_mode_remains_internal_and_valid():
    assert "complete" in MODES
    RunConfig(group_id=1, mode="complete", product_ids=[1])

def test_new_post_mode_requires_product_and_is_valid():
    assert "test_single_post" in MODES
    assert "test_single_post" in POSTING_MODES
    RunConfig(group_id=1, mode="test_single_post", product_ids=[1])

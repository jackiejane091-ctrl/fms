"""Focused checks for configurable per-listing timing wiring (v0.2.121 patch)."""
from pathlib import Path
import ast

def test_run_config_has_independent_per_listing_fields():
    source = Path(__file__).resolve().parents[1] / "engine.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    run_config = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "RunConfig")
    names = {n.target.id for n in run_config.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
    assert {"min_seconds_to_post", "max_seconds_to_post"} <= names

def test_dashboard_does_not_map_post_timing_to_interlisting_delay():
    source = Path(__file__).resolve().parents[1] / "templates" / "index.html"
    text = source.read_text(encoding="utf-8")
    assert "min_seconds_to_post: Math.max" in text
    assert "max_seconds_to_post: Math.max" in text
    assert "min_delay_s: parseFloat(s.min_seconds_to_post)" not in text

def test_single_post_uses_soft_timing_target():
    source = Path(__file__).resolve().parents[1] / "single_post_test.py"
    text = source.read_text(encoding="utf-8")
    assert "_target_s = random.uniform(_min_s, _max_s)" in text
    assert "_remaining = _target_s - (time.monotonic() - _listing_t0)" in text

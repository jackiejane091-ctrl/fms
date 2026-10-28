"""Focused regression tests for the standalone Publish Drafts action only."""
from types import SimpleNamespace
from unittest.mock import patch

from fms.engine import RunController


class FakeDB:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))


class FakeDriver:
    def __init__(self, outcomes=None):
        self.calls = []
        self.outcomes = list(outcomes or [])
        self._last_error = "simulated publish failure"

    def publish_draft(self, token):
        self.calls.append(token)
        return self.outcomes.pop(0) if self.outcomes else True


def make_engine(drafts, lo=2, hi=4, outcomes=None):
    engine = RunController.__new__(RunController)
    engine.cfg = SimpleNamespace(
        publish_drafts_min_count=lo,
        publish_drafts_max_count=hi,
        publish_mode="all_together",
        publish_gap_min=0,
        speed=1.0,
    )
    engine.db = FakeDB()
    engine.run_id = 99
    engine.logs = []
    engine._draft_rows = lambda account_id, any_run=False: list(drafts)
    engine._wait_if_paused = lambda: None
    engine._sleep_interruptible = lambda seconds: None
    engine.log = lambda message, **kwargs: engine.logs.append((message, kwargs))
    return engine, FakeDriver(outcomes)


def test_publish_drafts_selects_first_n_and_uses_draft_link_route():
    drafts = [
        {"id": i, "fb_listing_id": f"legacy-{i}", "title_used": f"Title {i}",
         "product_id": i, "images_used": "[]"}
        for i in range(1, 6)
    ]
    engine, driver = make_engine(drafts, lo=2, hi=4)
    with patch("fms.engine.random.randint", return_value=3):
        engine._publish_drafts_only_flow({"id": 7, "label": "A"}, driver)
    assert driver.calls == ["draft-title:Title 1", "draft-title:Title 2", "draft-title:Title 3"]
    updates = [params for sql, params in engine.db.calls if "status = 'posted'" in sql]
    assert [params[1] for params in updates] == [1, 2, 3]


def test_publish_drafts_processes_available_when_fewer_than_min_and_continues_on_failure():
    drafts = [
        {"id": i, "fb_listing_id": f"legacy-{i}", "title_used": f"Title {i}",
         "product_id": i, "images_used": "[]"}
        for i in range(1, 3)
    ]
    engine, driver = make_engine(drafts, lo=4, hi=5, outcomes=[False, True])
    with patch("fms.engine.random.randint", return_value=4):
        engine._publish_drafts_only_flow({"id": 7, "label": "A"}, driver)
    assert driver.calls == ["draft-title:Title 1", "draft-title:Title 2"]
    assert any("status = 'draft'" in sql for sql, _ in engine.db.calls)
    assert any("status = 'posted'" in sql for sql, _ in engine.db.calls)


def test_publish_drafts_empty_list_does_not_open_browser():
    engine, driver = make_engine([], lo=1, hi=3)
    engine._publish_drafts_only_flow({"id": 7, "label": "A"}, driver)
    assert driver.calls == []
    assert any("no drafts to publish" in message for message, _ in engine.logs)

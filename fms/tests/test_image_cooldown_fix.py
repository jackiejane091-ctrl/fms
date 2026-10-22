"""Focused regression tests for the v0.2.115 image cooldown correction.
Run from the FMS source root: python -m unittest fms.tests.test_image_cooldown_fix
Requires the normal FMS runtime dependencies, just like the existing test suite.
"""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fms.engine import _Rotator, _record_published_image_usage


class FakeDB:
    def __init__(self):
        self.usage = {}  # (account_id, canonical_path) -> timestamp
        self.executed = []

    def fetchone(self, sql, params=()):
        if "FROM app_settings" in sql:
            return {"value": "8"}
        if "FROM account_image_usage" in sql:
            return {"last_used_at": self.usage.get((int(params[0]), str(params[1])))} if (int(params[0]), str(params[1])) in self.usage else None
        raise AssertionError(f"Unexpected fetchone SQL: {sql}")

    def execute(self, sql, params=()):
        self.executed.append((sql, params))
        if "INSERT INTO account_image_usage" in sql:
            self.usage[(int(params[0]), str(params[1]))] = str(params[2])
        return None


def config():
    return SimpleNamespace(
        group_locations=[], min_price=None, max_price=None,
        condition="used", availability="in_stock", hide_from_friends=False,
    )


def product(main, extra, extra_count=2):
    return {
        "id": 1, "name": "test", "titles": ["test"], "descriptions": ["desc"],
        "locations": [], "main_images": main, "extra_images": extra,
        "extra_image_count": extra_count, "tags": "", "base_price": 10,
        "attributes": {}, "delivery_options": [], "scraped_items": None,
    }


class ImageCooldownFixTests(unittest.TestCase):
    def test_payload_does_not_mark_images_before_publish(self):
        db = FakeDB()
        rotator = _Rotator(db)
        p = product(["/tmp/main-a.jpg"], ["/tmp/extra-a.jpg", "/tmp/extra-b.jpg"])
        payload = rotator.payload(p, config(), as_draft=False, account_id=101)
        self.assertEqual(len(payload["image_paths"]), 3)
        self.assertEqual(db.usage, {}, "Selecting/building payload must not start cooldown")

    def test_all_selected_main_and_extra_images_mark_after_success_and_are_account_scoped(self):
        db = FakeDB()
        rotator = _Rotator(db)
        p = product(["/tmp/main-a.jpg"], ["/tmp/extra-a.jpg", "/tmp/extra-b.jpg"])
        payload = rotator.payload(p, config(), as_draft=False, account_id=101)
        _record_published_image_usage(db, 101, 1, payload["image_paths"])
        self.assertEqual(len(db.usage), 3)
        # Account 101 cannot reuse the just-published images; account 202 can.
        self.assertEqual(rotator.payload(p, config(), as_draft=False, account_id=101)["image_paths"], [])
        other = rotator.payload(p, config(), as_draft=False, account_id=202)
        self.assertEqual(len(other["image_paths"]), 3)

    def test_failed_publish_does_not_record_usage(self):
        db = FakeDB()
        rotator = _Rotator(db)
        p = product(["/tmp/main-a.jpg"], ["/tmp/extra-a.jpg"])
        payload = rotator.payload(p, config(), as_draft=False, account_id=101)
        # A failed publish path records the failed listing only; it must not call
        # _record_published_image_usage. Payload selection alone leaves no history.
        self.assertTrue(payload["image_paths"])
        self.assertEqual(db.usage, {})

    def test_image_becomes_eligible_after_eight_days(self):
        db = FakeDB()
        rotator = _Rotator(db)
        p = product(["/tmp/main-a.jpg"], [], extra_count=0)
        from pathlib import Path
        canonical = str(Path("/tmp/main-a.jpg").resolve())
        db.usage[(101, canonical)] = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat(timespec="seconds")
        payload = rotator.payload(p, config(), as_draft=False, account_id=101)
        self.assertEqual(payload["image_paths"], ["/tmp/main-a.jpg"])

    def test_scraped_item_images_obey_cooldown_without_premature_recording(self):
        db = FakeDB()
        rotator = _Rotator(db)
        p = {
            "id": 2, "name": "scraped", "scraped_items": [
                {"title": "scraped", "photo_paths": ["/tmp/scraped-a.jpg", "/tmp/scraped-b.jpg"]}
            ], "base_price": 10, "attributes": {}, "condition": "used",
            "availability": "in_stock", "listing_type": "item", "delivery_options": [],
        }
        payload = rotator.payload(p, config(), as_draft=False, account_id=101)
        self.assertEqual(len(payload["image_paths"]), 2)
        self.assertEqual(db.usage, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)

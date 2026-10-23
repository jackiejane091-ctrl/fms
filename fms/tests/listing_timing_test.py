"""Focused regression tests for configurable, non-blocking listing timing."""
from unittest.mock import patch

from fms.browser import _listing_timing_target


def test_uses_configured_min_and_max():
    with patch("fms.browser.random.uniform", return_value=31.0) as uniform:
        minimum, maximum, target = _listing_timing_target({
            "min_seconds_to_post": 25, "max_seconds_to_post": 40
        })
    assert (minimum, maximum, target) == (25.0, 40.0, 31.0)
    uniform.assert_called_once_with(25.0, 40.0)


def test_custom_values_are_not_hardcoded():
    with patch("fms.browser.random.uniform", return_value=72.0):
        minimum, maximum, target = _listing_timing_target({
            "min_seconds_to_post": "60", "max_seconds_to_post": "90"
        })
    assert (minimum, maximum, target) == (60.0, 90.0, 72.0)


def test_reversed_range_is_normalized():
    with patch("fms.browser.random.uniform", return_value=20.0):
        minimum, maximum, target = _listing_timing_target({
            "min_seconds_to_post": 30, "max_seconds_to_post": 10
        })
    assert (minimum, maximum, target) == (10.0, 30.0, 20.0)


def test_zero_is_a_valid_configured_value():
    with patch("fms.browser.random.uniform", return_value=0.0):
        minimum, maximum, target = _listing_timing_target({
            "min_seconds_to_post": 0, "max_seconds_to_post": 0
        })
    assert (minimum, maximum, target) == (0.0, 0.0, 0.0)


if __name__ == "__main__":
    test_uses_configured_min_and_max()
    test_custom_values_are_not_hardcoded()
    test_reversed_range_is_normalized()
    test_zero_is_a_valid_configured_value()
    print("4 timing tests passed")

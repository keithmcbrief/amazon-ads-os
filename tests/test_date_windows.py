import pytest

import _reports as r


def test_valid_window_passes():
    r.validate_date_window("sp-campaigns", "2026-04-01", "2026-04-30",
                            today="2026-05-15")


def test_unknown_slug_raises():
    with pytest.raises(ValueError):
        r.validate_date_window("sp-fake", "2026-04-01", "2026-04-30",
                                today="2026-05-15")


def test_malformed_date_raises():
    with pytest.raises(ValueError):
        r.validate_date_window("sp-campaigns", "Apr 1 2026", "2026-04-30",
                                today="2026-05-15")


def test_end_before_start_raises():
    with pytest.raises(ValueError):
        r.validate_date_window("sp-campaigns", "2026-04-30", "2026-04-01",
                                today="2026-05-15")


def test_window_exceeding_max_days_raises():
    # sp-campaigns has max_days=31
    with pytest.raises(ValueError) as ei:
        r.validate_date_window("sp-campaigns", "2026-04-01", "2026-05-10",
                                today="2026-05-15")
    assert "max_days" in str(ei.value)


def test_end_in_future_raises():
    with pytest.raises(ValueError):
        r.validate_date_window("sp-campaigns", "2026-04-01", "2026-12-31",
                                today="2026-05-15")


def test_end_too_far_back_raises():
    # sp-search-terms has max_lookback_days=60; 100 days ago should fail
    with pytest.raises(ValueError) as ei:
        r.validate_date_window("sp-search-terms", "2026-01-01", "2026-01-31",
                                today="2026-05-15")
    assert "max_lookback_days" in str(ei.value)


def test_single_day_window_ok():
    r.validate_date_window("sp-campaigns", "2026-05-01", "2026-05-01",
                            today="2026-05-15")

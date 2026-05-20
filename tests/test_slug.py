import pytest

import _common as c


def test_simple_slug_passes():
    assert c.validate_slug("acme") == "acme"
    assert c.validate_slug("acme-us") == "acme-us"
    assert c.validate_slug("acme_us") == "acme_us"
    assert c.validate_slug("brand123") == "brand123"


def test_uppercase_is_normalized():
    assert c.validate_slug("Acme-US") == "acme-us"


def test_whitespace_is_stripped():
    assert c.validate_slug("  acme  ") == "acme"


def test_invalid_slugs():
    for bad in ["", "a", "1", "-acme", "acme!", "acme/us", "acme us",
                "acme..", "../../etc", "a" * 65]:
        with pytest.raises(ValueError):
            c.validate_slug(bad)


def test_non_string_rejected():
    with pytest.raises(ValueError):
        c.validate_slug(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        c.validate_slug(123)  # type: ignore[arg-type]

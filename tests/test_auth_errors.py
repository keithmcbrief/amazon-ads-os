import pytest

import _common as c


def test_invalid_grant_remediation_mentions_setup_profile():
    err = c.parse_lwa_error({"error": "invalid_grant", "error_description": "Token revoked"})
    assert isinstance(err, c.AuthError)
    assert err.code == "invalid_grant"
    assert "/amazon-setup-profile" in err.remediation


def test_invalid_client_remediation_mentions_client_secret():
    err = c.parse_lwa_error({"error": "invalid_client"})
    assert err.code == "invalid_client"
    assert "client_secret" in err.remediation.lower() or "client_id" in err.remediation.lower()


def test_unauthorized_client_mentions_ads_scope():
    err = c.parse_lwa_error({"error": "unauthorized_client"})
    assert err.code == "unauthorized_client"
    assert "ads" in err.remediation.lower() or "scope" in err.remediation.lower()


def test_unknown_error_falls_back_gracefully():
    err = c.parse_lwa_error({"error": "something_weird", "error_description": "?"})
    assert err.code == "something_weird"
    assert err.remediation  # non-empty fallback


def test_authoerror_str_includes_code_and_remediation():
    err = c.parse_lwa_error({"error": "invalid_grant", "error_description": "Token revoked"})
    s = str(err)
    assert "invalid_grant" in s
    assert "Token revoked" in s
    assert "→" in s


def test_authoerror_can_be_raised():
    with pytest.raises(c.AuthError):
        raise c.parse_lwa_error({"error": "invalid_grant"})

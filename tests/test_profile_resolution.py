import pytest

import _common as c
from conftest import seed_identity, seed_profile


def test_explicit_flag_wins(monkeypatch):
    seed_profile("brand-a")
    seed_profile("brand-b")
    c.atomic_write_text(c.active_profile_file(), "brand-a\n")
    monkeypatch.setenv("AMAZON_ADS_PROFILE", "brand-a")
    profile = c.resolve_profile("brand-b")
    assert profile.slug == "brand-b"
    assert profile.source == "--profile flag"


def test_env_beats_active_file(monkeypatch):
    seed_profile("brand-a")
    seed_profile("brand-b")
    c.atomic_write_text(c.active_profile_file(), "brand-a\n")
    monkeypatch.setenv("AMAZON_ADS_PROFILE", "brand-b")
    profile = c.resolve_profile(None)
    assert profile.slug == "brand-b"
    assert profile.source == "AMAZON_ADS_PROFILE env"


def test_active_file_used_when_nothing_else():
    seed_profile("brand-a")
    c.atomic_write_text(c.active_profile_file(), "brand-a\n")
    profile = c.resolve_profile(None)
    assert profile.slug == "brand-a"
    assert profile.source == "active_profile file"


def test_no_profile_raises_with_hint():
    with pytest.raises(RuntimeError) as ei:
        c.resolve_profile(None)
    assert "/amazon-setup-profile" in str(ei.value)


def test_no_profile_lists_existing():
    seed_profile("brand-a")
    seed_profile("brand-b")
    # active file not set
    with pytest.raises(RuntimeError) as ei:
        c.resolve_profile(None)
    msg = str(ei.value)
    assert "brand-a" in msg and "brand-b" in msg


def test_live_identity_profile_is_live_mode():
    seed_identity("agency-na", region="NA")
    seed_profile("brand-na", identity="agency-na")
    profile = c.load_profile("brand-na", source="--brand")
    assert profile.mode == "LIVE"
    assert profile.host == c.REGION_HOSTS["NA"]
    assert profile.region == "NA"


def test_sandbox_identity_profile_is_sandbox_mode():
    seed_identity("agency-sandbox", region="SANDBOX")
    seed_profile("brand-sandbox", identity="agency-sandbox")
    profile = c.load_profile("brand-sandbox", source="--brand")
    assert profile.mode == "SANDBOX"
    assert profile.host == c.SANDBOX_HOST


def test_profile_inherits_identity_region():
    seed_identity("eu-agency", region="EU")
    seed_profile("eu-brand", identity="eu-agency")
    profile = c.load_profile("eu-brand", source="--brand")
    assert profile.region == "EU"
    assert profile.host == c.REGION_HOSTS["EU"]


def test_multiple_profiles_can_share_identity():
    """The Manager Account use case: one identity, many profiles."""
    seed_identity("agency-main", region="NA")
    seed_profile("globex", identity="agency-main", profile_id="111")
    seed_profile("initech", identity="agency-main", profile_id="222")
    p1 = c.load_profile("globex", source="--brand")
    p2 = c.load_profile("initech", source="--brand")
    assert p1.identity_name == p2.identity_name == "agency-main"
    assert p1.profile_id == "111"
    assert p2.profile_id == "222"
    # And they share the same LWA credentials
    assert p1.client_id == p2.client_id
    assert p1.refresh_token == p2.refresh_token


def test_profile_pointing_at_missing_identity_raises():
    # Write the profile config directly so we don't auto-seed the identity
    c.write_env_file(c.profile_config_path("orphan"), {
        "IDENTITY": "nonexistent",
        "ADS_PROFILE_ID": "111",
    })
    with pytest.raises(RuntimeError) as ei:
        c.load_profile("orphan", source="--brand")
    assert "nonexistent" in str(ei.value)


def test_missing_required_field_raises():
    cfg_path = c.profile_config_path("brand-y")
    c.write_env_file(cfg_path, {"IDENTITY": "default"})  # missing ADS_PROFILE_ID
    seed_identity("default")
    with pytest.raises(RuntimeError) as ei:
        c.load_profile("brand-y", source="x")
    assert "missing required" in str(ei.value).lower()


def test_invalid_identity_region_raises():
    c.write_env_file(c.identity_env_path("bad-region"), {
        "LWA_CLIENT_ID": "x", "LWA_CLIENT_SECRET": "y", "LWA_REFRESH_TOKEN": "z",
        "ADS_REGION": "MARS",
    })
    with pytest.raises(RuntimeError) as ei:
        c.load_identity("bad-region")
    assert "invalid ADS_REGION" in str(ei.value)


def test_atomic_switch():
    seed_profile("brand-a")
    seed_profile("brand-b")
    from profile_switch import main as switch_main
    rc = switch_main(["--brand", "brand-a"])
    assert rc == 0
    assert c.active_profile_file().read_text().strip() == "brand-a"
    rc = switch_main(["--brand", "brand-b"])
    assert rc == 0
    assert c.active_profile_file().read_text().strip() == "brand-b"


def test_switch_to_unknown_brand_refuses():
    from profile_switch import main as switch_main
    with pytest.raises(SystemExit):
        switch_main(["--brand", "no-such-brand"])

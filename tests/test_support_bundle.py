"""Support bundle must not leak secrets, signed URLs, or auth headers."""
from __future__ import annotations

import io
import zipfile

import _common as c


_SECRETS = [
    "amzn1.application-oa2-client.deadbeef1234567890",
    "supersecretvalue9999",
    "Atzr|MEGA-SECRET-REFRESH-TOKEN-PAYLOAD",
]


def _seed_profile_with_secrets(slug: str, *, identity: str = "agency-x") -> None:
    """Seed an identity with sensitive creds + a profile pointing at it."""
    c.write_env_file(c.identity_env_path(identity), {
        "LWA_CLIENT_ID": _SECRETS[0],
        "LWA_CLIENT_SECRET": _SECRETS[1],
        "LWA_REFRESH_TOKEN": _SECRETS[2],
        "ADS_REGION": "NA",
    })
    c.write_env_file(c.profile_config_path(slug), {
        "IDENTITY": identity,
        "ADS_PROFILE_ID": "9999",
        "ADS_MARKETPLACE_ID": "ATVPDKIKX0DER",
        "ADS_ACCOUNT_NAME": "Test",
        "ADS_TIMEZONE": "UTC",
    })
    # Token cache lives at identity level now
    state_dir = c.identity_dir(identity) / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    c.atomic_write_json(state_dir / "access_token.json", {
        "access_token": "Atza|SHOULD-NEVER-APPEAR-IN-BUNDLE-1234567890",
        "expires_at_epoch": 9999999999,
    })


def _seed_log_with_signed_url() -> None:
    log_dir = c.logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "2026-05-19.log"
    log_file.write_text(
        "2026-05-19T10:00:00Z GET https://advertising-api.amazon.com/v2/profiles 200 123ms\n"
        # signed url query string — must be stripped already by the writer; this is a defense test
        "2026-05-19T10:01:00Z GET https://offline-report-storage.s3.amazonaws.com/report.gz 200 800ms\n"
    )


def test_bundle_contains_no_known_secrets(tmp_path, monkeypatch):
    from doctor import make_support_bundle

    _seed_profile_with_secrets("brand-a")
    _seed_log_with_signed_url()

    bundle = make_support_bundle(tmp_path / "support.zip")
    blob = bundle.read_bytes()

    for secret in _SECRETS:
        assert secret.encode() not in blob, f"{secret!r} leaked into bundle"
    # Access token cache content shouldn't appear in plaintext
    assert b"Atza|SHOULD-NEVER-APPEAR-IN-BUNDLE" not in blob


def test_bundle_redacts_inside_zip_entries(tmp_path):
    from doctor import make_support_bundle

    _seed_profile_with_secrets("brand-a")
    _seed_log_with_signed_url()

    bundle = make_support_bundle(tmp_path / "support.zip")
    with zipfile.ZipFile(bundle) as zf:
        names = zf.namelist()
        # The identity .env should be redacted; profile config has no secrets
        assert any("identities/" in n for n in names), f"identity files missing from bundle: {names}"
        for name in names:
            content = zf.read(name).decode("utf-8", errors="replace")
            for secret in _SECRETS:
                assert secret not in content, f"{secret!r} in zip entry {name}"


def test_bundle_includes_system_txt(tmp_path):
    from doctor import make_support_bundle

    bundle = make_support_bundle(tmp_path / "support.zip")
    with zipfile.ZipFile(bundle) as zf:
        assert "system.txt" in zf.namelist()
        sys_txt = zf.read("system.txt").decode()
        assert "python:" in sys_txt
        assert "platform:" in sys_txt

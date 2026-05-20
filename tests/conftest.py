"""Shared pytest setup: put scripts/ on sys.path and isolate AMAZON_ADS_OS_HOME."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugin" / "scripts"))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets a fresh AMAZON_ADS_OS_HOME under tmp_path."""
    monkeypatch.setenv("AMAZON_ADS_OS_HOME", str(tmp_path / "amazon-ads-os"))
    monkeypatch.delenv("AMAZON_ADS_PROFILE", raising=False)
    yield


# ---- Shared seeding helpers exposed to tests --------------------------
import _common as _c  # noqa: E402


def seed_identity(name: str = "default", *, region: str = "NA") -> None:
    """Write an identity .env with fake-but-valid credentials."""
    _c.write_env_file(_c.identity_env_path(name), {
        "LWA_CLIENT_ID": "amzn1.application-oa2-client.fake",
        "LWA_CLIENT_SECRET": "secret",
        "LWA_REFRESH_TOKEN": "Atzr|fake",
        "ADS_REGION": region,
    })


def seed_profile(
    slug: str,
    *,
    identity: str = "default",
    profile_id: str = "123456789",
    marketplace: str = "ATVPDKIKX0DER",
    account_name: str | None = None,
    timezone_name: str = "UTC",
    cache_access_token: bool = True,
) -> None:
    """Write a profile config that points at an identity. Idempotent."""
    if not _c.identity_env_path(identity).exists():
        seed_identity(identity)
    _c.write_env_file(_c.profile_config_path(slug), {
        "IDENTITY": identity,
        "ADS_PROFILE_ID": profile_id,
        "ADS_MARKETPLACE_ID": marketplace,
        "ADS_ACCOUNT_NAME": account_name or f"Test {slug}",
        "ADS_TIMEZONE": timezone_name,
    })
    if cache_access_token:
        _c.atomic_write_json(
            _c.identity_dir(identity) / "state" / "access_token.json",
            {"access_token": "Atza|cached", "expires_at_epoch": 9_999_999_999},
        )


@pytest.fixture
def seed_id_and_profile():
    """Fixture variant: returns a function the test can call to seed."""
    def _seed(slug: str, **kw):
        seed_profile(slug, **kw)
        return _c.load_profile(slug, source="--brand")
    return _seed

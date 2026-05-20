#!/usr/bin/env python3
"""Preflight checks for amazon-ads-os.

Default mode prints PASS/FAIL for each check. --support-bundle packages a
redacted zip the user can attach to a bug report.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import _common as c

OK = "[ok]"
WARN = "[warn]"
FAIL = "[fail]"


def _check_python_version() -> tuple[str, str]:
    if sys.version_info >= (3, 10):
        return OK, f"python {sys.version.split()[0]}"
    return FAIL, f"python {sys.version.split()[0]} (need 3.10+)"


def _check_requests_import() -> tuple[str, str]:
    """When run via `bin/amazon-python`, `requests` is in the plugin's private venv.
    If we got here at all, the venv bootstrap succeeded — but verify the import.
    """
    try:
        import requests  # noqa: F401
        # Note whether we're running inside the plugin's venv or the user's system python
        venv_hint = ""
        if "amazon-ads-os" in sys.executable or ".venv" in sys.executable:
            venv_hint = " (in plugin venv)"
        return OK, f"requests {requests.__version__}{venv_hint}"
    except ImportError as e:
        return FAIL, (
            f"requests not importable: {e}. "
            f"The plugin's venv may not have been bootstrapped — restart Claude Code "
            f"and let the SessionStart hook run, or run "
            f"`${{CLAUDE_PLUGIN_ROOT}}/scripts/bootstrap.py ${{CLAUDE_PLUGIN_ROOT}} ${{CLAUDE_PLUGIN_DATA}}` manually."
        )


def _check_data_dir() -> tuple[str, str]:
    path = c.home()
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".doctor_probe"
        probe.write_text("ok")
        probe.unlink()
        return OK, f"data dir writable: {path}"
    except OSError as e:
        return FAIL, f"data dir not writable ({path}): {e}"


def _check_identity_count() -> tuple[str, str]:
    names = c.list_identity_names()
    if not names:
        return WARN, "no identities configured — run /amazon-setup-profile"
    return OK, f"{len(names)} identity(ies) configured: {', '.join(names)}"


def _check_profile_count() -> tuple[str, str]:
    slugs = c.list_profile_slugs()
    if not slugs:
        return WARN, "no profiles configured — run /amazon-setup-profile"
    return OK, f"{len(slugs)} profile(s) configured: {', '.join(slugs)}"


def _check_active_profile() -> tuple[str, str]:
    apf = c.active_profile_file()
    if not apf.exists():
        return WARN, "no active profile pointer — first /amazon-switch-profile <brand>"
    slug = apf.read_text().strip()
    try:
        profile = c.load_profile(slug, source="active_profile file")
        return OK, (
            f"active profile: {slug} ({profile.account_name!r}, "
            f"identity={profile.identity_name}, region={profile.region}, mode={profile.mode})"
        )
    except Exception as e:
        return FAIL, f"active profile {slug!r} fails to load: {e}"


def _check_token_refresh_per_identity() -> tuple[str, str]:
    names = c.list_identity_names()
    if not names:
        return WARN, "skip (no identities)"
    failures = []
    successes = []
    for name in names:
        try:
            identity = c.load_identity(name)
            c.get_access_token(identity, force_refresh=True)
            successes.append(name)
        except c.AuthError as e:
            failures.append(f"{name}: {e.code}")
        except Exception as e:
            failures.append(f"{name}: {type(e).__name__}: {e}")
    if failures:
        return FAIL, f"LWA refresh failed for: {'; '.join(failures)}"
    return OK, f"LWA refresh works for: {', '.join(successes)}"


def _check_profiles_endpoint() -> tuple[str, str]:
    apf = c.active_profile_file()
    if not apf.exists():
        return WARN, "skip (no active profile)"
    slug = apf.read_text().strip()
    try:
        profile = c.load_profile(slug, source="active_profile file")
    except Exception as e:
        return FAIL, f"{slug} failed to load: {e}"
    try:
        resp = c.ads_request(profile, "GET", "/v2/profiles", content_type="application/json")
        if resp.status_code != 200:
            return FAIL, f"GET /v2/profiles → {resp.status_code} {resp.text[:200]}"
        profiles = resp.json() or []
        ids = {str(p.get("profileId")) for p in profiles}
        if profile.profile_id not in ids:
            return FAIL, (
                f"configured profile_id {profile.profile_id} not in /v2/profiles "
                f"({sorted(ids)}). region or refresh_token may be wrong."
            )
        return OK, f"GET /v2/profiles ok; profile_id {profile.profile_id} present"
    except Exception as e:
        return FAIL, f"GET /v2/profiles failed: {e}"


_CHECKS: list[tuple[str, Callable[[], tuple[str, str]]]] = [
    ("python_version", _check_python_version),
    ("requests", _check_requests_import),
    ("data_dir", _check_data_dir),
    ("identity_count", _check_identity_count),
    ("profile_count", _check_profile_count),
    ("active_profile", _check_active_profile),
    ("token_refresh", _check_token_refresh_per_identity),
    ("ads_profiles_endpoint", _check_profiles_endpoint),
]


def _run_checks(skip_network: bool) -> int:
    rc = 0
    for name, fn in _CHECKS:
        if skip_network and name in ("token_refresh", "ads_profiles_endpoint"):
            print(f"{WARN:<6} {name:<25} skipped (--no-network)")
            continue
        status, msg = fn()
        print(f"{status:<6} {name:<25} {msg}")
        if status == FAIL:
            rc = 1
    return rc


# ---- Support bundle ----------------------------------------------------
_SECRET_KEYS = ("LWA_CLIENT_ID", "LWA_CLIENT_SECRET", "LWA_REFRESH_TOKEN", "access_token")
_REDACT_RE = re.compile(
    r'("(?:' + "|".join(_SECRET_KEYS) + r')"\s*[:=]\s*")[^"]+(")',
    re.IGNORECASE,
)
_ENV_LINE_RE = re.compile(
    r'^(' + "|".join(_SECRET_KEYS) + r')=.*$',
    re.IGNORECASE | re.MULTILINE,
)


def _redact_text(text: str) -> str:
    text = _REDACT_RE.sub(r'\1[REDACTED]\2', text)
    text = _ENV_LINE_RE.sub(r'\1=[REDACTED]', text)
    return text


def _redact_bytes(data: bytes) -> bytes:
    try:
        return _redact_text(data.decode("utf-8")).encode("utf-8")
    except UnicodeDecodeError:
        return b"[BINARY OMITTED FROM SUPPORT BUNDLE]"


def make_support_bundle(out_path: Path) -> Path:
    """Build a zip of redacted logs + identity & profile metadata + pending states."""
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # Logs (redacted)
        if c.logs_dir().exists():
            for log in c.logs_dir().glob("*.log"):
                zf.writestr(f"logs/{log.name}", _redact_bytes(log.read_bytes()))
        # Identity .env files (heavily redacted — these hold the secrets)
        for name in c.list_identity_names():
            env_path = c.identity_env_path(name)
            zf.writestr(
                f"identities/{name}/env.redacted",
                _redact_bytes(env_path.read_bytes()),
            )
        # Profile config files (no secrets, but redact defensively in case
        # someone put something weird in there)
        for slug in c.list_profile_slugs():
            cfg_path = c.profile_config_path(slug)
            zf.writestr(
                f"profiles/{slug}/profile.env",
                _redact_bytes(cfg_path.read_bytes()),
            )
            # Pending report state files for each profile
            state_dir = c.profile_dir(slug) / "state"
            if state_dir.exists():
                for sf in state_dir.glob("pending_*.json"):
                    zf.writestr(
                        f"profiles/{slug}/state/{sf.name}",
                        _redact_bytes(sf.read_bytes()),
                    )
        # System info
        zf.writestr("system.txt",
                    f"python: {sys.version}\n"
                    f"platform: {sys.platform}\n"
                    f"ts: {datetime.now(timezone.utc).isoformat()}\n")
    return out_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="amazon-ads-os preflight diagnostics")
    ap.add_argument("--no-network", action="store_true",
                    help="skip checks that hit Amazon (token refresh + /v2/profiles)")
    ap.add_argument("--support-bundle", metavar="PATH",
                    help="build a redacted support bundle zip and exit")
    args = ap.parse_args(argv)

    if args.support_bundle:
        path = Path(args.support_bundle)
        out = make_support_bundle(path)
        print(f"wrote support bundle → {out}")
        print("verify it has no secrets with: python3 -m zipfile -e <bundle> /tmp/check && grep -ri 'amzn1\\|atzr|' /tmp/check")
        return 0

    return _run_checks(skip_network=args.no_network)


if __name__ == "__main__":
    sys.exit(main())

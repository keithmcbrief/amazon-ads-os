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


# ---- First-run wizard --------------------------------------------------
# Speaks user-friendly language ("Connection" / "Ad account") rather than
# the internal "Identity" / "Profile" terms. The wizard is the first thing
# `amazon-doctor --interactive` does when no credentials exist. Subsequent
# runs go straight to diagnostics.
import getpass

try:
    import requests as _rq
except ImportError:
    _rq = None


def _wizard_print(msg: str = "") -> None:
    """Wrapper so we can route output cleanly (stdout for the wizard text,
    stderr is reserved for noise from sub-calls)."""
    print(msg)


def _wizard_input(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val if val else (default or "")


def _wizard_confirm(prompt: str, *, default: bool = True) -> bool:
    yn = "[Y/n]" if default else "[y/N]"
    while True:
        val = input(f"{prompt} {yn}: ").strip().lower()
        if not val:
            return default
        if val in ("y", "yes"):
            return True
        if val in ("n", "no"):
            return False


def _wizard_secret(prompt: str) -> str:
    val = getpass.getpass(f"{prompt}: ").strip()
    if not val:
        raise SystemExit("aborted: empty value")
    return val


def _wizard_mint_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    if _rq is None:
        raise SystemExit("requests library not available — restart Claude Code so the venv bootstraps")
    resp = _rq.post(
        c.LWA_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    body = {}
    try:
        body = resp.json()
    except Exception:
        pass
    if resp.status_code != 200 or "access_token" not in body:
        raise c.parse_lwa_error(body or {"error": "unknown_error",
                                          "error_description": resp.text})
    return body["access_token"]


def _wizard_list_profiles_for(region: str, client_id: str, token: str) -> list[dict]:
    if _rq is None:
        raise SystemExit("requests not available")
    host = c.host_for(region)
    resp = _rq.get(
        host + "/v2/profiles",
        headers={
            "Authorization": f"Bearer {token}",
            "Amazon-Advertising-API-ClientId": client_id,
            "Accept": "application/json",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        return []
    return resp.json() or []


def _wizard_auto_slug(profile_payload: dict) -> str:
    info = profile_payload.get("accountInfo", {}) or {}
    name = (info.get("name") or "").strip().lower()
    cc = (info.get("countryCode") or info.get("marketplaceStringId") or "").strip().lower()
    pid = str(profile_payload.get("profileId", ""))
    base = re.sub(r"[^a-z0-9_-]+", "-", name).strip("-_") if name else f"acct-{pid}"
    if cc and len(cc) <= 4:
        base = f"{base}-{cc}"
    base = base[:64].rstrip("-_") or f"acct-{pid}"
    if not c.SLUG_RE.match(base):
        base = f"acct-{pid}"
    return base


def _wizard_already_setup() -> bool:
    return bool(c.list_identity_names()) and bool(c.list_profile_slugs())


def run_setup_wizard() -> int:
    """Linear interactive setup. Returns 0 on success, non-zero on user abort
    or hard failure. The caller should typically follow up by running diagnostics.
    """
    _wizard_print()
    _wizard_print("===== Amazon Ads OS — first-time setup =====")
    _wizard_print()
    _wizard_print("This will:")
    _wizard_print("  • take your Amazon Ads API credentials (client_id, client_secret, refresh_token)")
    _wizard_print("  • find which ad accounts they unlock")
    _wizard_print("  • save them locally on this machine so future skills can use them")
    _wizard_print()
    _wizard_print("Nothing on Amazon will be changed by this step.")
    _wizard_print()

    if not _wizard_confirm("Ready to connect?"):
        _wizard_print("Aborted. You can rerun /amazon-doctor any time.")
        return 1

    _wizard_print()
    _wizard_print("Don't have credentials yet? See:")
    _wizard_print("  https://advertising.amazon.com/API/docs/en-us/setting-up/overview")
    _wizard_print()
    _wizard_print("You'll need:")
    _wizard_print("  1. an LWA (Login with Amazon) app — client_id + client_secret")
    _wizard_print("  2. a refresh_token from completing the OAuth dance against that app")
    _wizard_print("  3. a region: NA (.com/.ca/.mx/.br) | EU (.co.uk/.de/...) | FE (.jp/.au/.sg)")
    _wizard_print()
    _wizard_print("Secrets are entered with hidden input. Nothing is logged.")
    _wizard_print()

    client_id = _wizard_secret("LWA client_id")
    client_secret = _wizard_secret("LWA client_secret")
    refresh_token = _wizard_secret("LWA refresh_token")
    region = (_wizard_input("Region", default="NA") or "NA").upper()
    if region not in c.REGION_HOSTS and region != c.SANDBOX_REGION:
        _wizard_print(f"  unknown region {region!r}; expected NA/EU/FE — aborting")
        return 1

    _wizard_print()
    _wizard_print("Verifying credentials with Amazon…")
    try:
        token = _wizard_mint_token(client_id, client_secret, refresh_token)
    except c.AuthError as e:
        _wizard_print()
        _wizard_print(f"  ✗ {e}")
        _wizard_print()
        _wizard_print("Common fixes:")
        _wizard_print("  invalid_grant     → refresh_token is wrong or expired; regenerate it")
        _wizard_print("  invalid_client    → client_id or client_secret is wrong")
        _wizard_print("  unauthorized_client → your LWA app is missing the Amazon Ads scope")
        return 1
    _wizard_print("  ✓ credentials accepted")

    _wizard_print()
    _wizard_print(f"Looking up ad accounts in {region}…")
    profiles = _wizard_list_profiles_for(region, client_id, token)
    if not profiles:
        _wizard_print(f"  no ad accounts found in {region}. Probing other regions…")
        for r in (x for x in ("NA", "EU", "FE") if x != region):
            alt = _wizard_list_profiles_for(r, client_id, token)
            if alt:
                _wizard_print(f"  ✓ found {len(alt)} account(s) in {r}; switching region")
                region = r
                profiles = alt
                break
    if not profiles:
        _wizard_print()
        _wizard_print("  ✗ no Amazon Ads ad accounts visible to these credentials.")
        _wizard_print("    Verify this Amazon account has Ads API access approved.")
        return 1

    _wizard_print()
    _wizard_print(f"Found {len(profiles)} ad account(s) in {region}:")
    for i, p in enumerate(profiles, 1):
        info = p.get("accountInfo", {}) or {}
        _wizard_print(
            f"  [{i}] {info.get('name', '?')!r}  "
            f"marketplace={info.get('marketplaceStringId', '?')}  "
            f"type={info.get('type', '?')}  "
            f"profile_id={p.get('profileId')}"
        )

    if len(profiles) == 1:
        chosen = profiles[0]
        _wizard_print()
        _wizard_print(f"Only one account — using it as your default.")
    else:
        _wizard_print()
        while True:
            sel = _wizard_input(f"Pick your default ad account [1-{len(profiles)}]", default="1")
            if sel.isdigit() and 1 <= int(sel) <= len(profiles):
                chosen = profiles[int(sel) - 1]
                break
            _wizard_print(f"  please enter a number between 1 and {len(profiles)}")

    suggested_slug = _wizard_auto_slug(chosen)
    _wizard_print()
    brand = _wizard_input("Short name for this ad account (used in commands)", default=suggested_slug)
    try:
        brand = c.validate_slug(brand)
    except ValueError as e:
        _wizard_print(f"  invalid name: {e} — using {suggested_slug!r}")
        brand = suggested_slug

    identity_name = "default"
    # If "default" identity already exists (unusual on cold start), bump the name
    if c.identity_env_path(identity_name).exists():
        idx = 2
        while c.identity_env_path(f"default-{idx}").exists():
            idx += 1
        identity_name = f"default-{idx}"

    _wizard_print()
    _wizard_print("Saving connection…")
    c.write_env_file(c.identity_env_path(identity_name), {
        "LWA_CLIENT_ID": client_id,
        "LWA_CLIENT_SECRET": client_secret,
        "LWA_REFRESH_TOKEN": refresh_token,
        "ADS_REGION": region,
    })

    info = chosen.get("accountInfo", {}) or {}
    c.write_env_file(c.profile_config_path(brand), {
        "IDENTITY": identity_name,
        "ADS_PROFILE_ID": str(chosen.get("profileId", "")),
        "ADS_MARKETPLACE_ID": str(info.get("marketplaceStringId", "")),
        "ADS_ACCOUNT_NAME": str(info.get("name", "")),
        "ADS_TIMEZONE": str(chosen.get("timezone", "UTC")),
    })

    if not c.active_profile_file().exists():
        c.atomic_write_text(c.active_profile_file(), brand + "\n")

    _wizard_print()
    _wizard_print("===== Ready =====")
    _wizard_print()
    _wizard_print(f"  Default ad account:  {info.get('name', '')!r}")
    _wizard_print(f"  Region / marketplace: {region} / {info.get('marketplaceStringId', '?')}")
    _wizard_print(f"  Short name:           {brand}")
    if len(profiles) > 1:
        _wizard_print(f"  Other accounts unlocked: {len(profiles) - 1}")
        _wizard_print(f"    (add later with /amazon-setup-profile or /amazon-switch-profile)")
    _wizard_print()
    _wizard_print("Next: try \"pull search terms for the last 30 days\"")
    _wizard_print()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="amazon-ads-os preflight diagnostics + first-run wizard")
    ap.add_argument("--no-network", action="store_true",
                    help="skip checks that hit Amazon (token refresh + /v2/profiles)")
    ap.add_argument("--support-bundle", metavar="PATH",
                    help="build a redacted support bundle zip and exit")
    ap.add_argument("--interactive", action="store_true",
                    help="auto-launch the first-run wizard if no connection is configured yet")
    ap.add_argument("--wizard-only", action="store_true",
                    help="run the interactive wizard unconditionally (skip diagnostics)")
    args = ap.parse_args(argv)

    if args.support_bundle:
        path = Path(args.support_bundle)
        out = make_support_bundle(path)
        print(f"wrote support bundle → {out}")
        print("verify it has no secrets with: python3 -m zipfile -e <bundle> /tmp/check && grep -ri 'amzn1\\|atzr|' /tmp/check")
        return 0

    if args.wizard_only:
        return run_setup_wizard()

    if args.interactive and not _wizard_already_setup():
        rc = run_setup_wizard()
        if rc != 0:
            return rc
        # Fall through to diagnostics after a successful setup

    return _run_checks(skip_network=args.no_network)


if __name__ == "__main__":
    sys.exit(main())

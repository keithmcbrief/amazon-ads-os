#!/usr/bin/env python3
"""Discover Amazon Ads advertiser profiles available under an existing identity.

The Manager Account killer feature: one OAuth grant exposes many advertiser
profiles via GET /v2/profiles. This script lists them and (optionally)
registers each one as a profiles/<slug>/profile.env entry — zero extra OAuth.

Usage:
    profile_discover.py --identity agency-main             # list only
    profile_discover.py --identity agency-main --register  # interactive register
    profile_discover.py --identity agency-main --register --all  # batch-register all
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone

import requests

import _common as c


def _mint_access_token(identity: c.Identity) -> str:
    """Mint a fresh access token directly so we don't touch the cache."""
    resp = requests.post(
        c.LWA_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": identity.refresh_token,
            "client_id": identity.client_id,
            "client_secret": identity.client_secret,
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


def _list_v2_profiles(identity: c.Identity, access_token: str) -> list[dict]:
    resp = requests.get(
        identity.host + "/v2/profiles",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Amazon-Advertising-API-ClientId": identity.client_id,
            "Accept": "application/json",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise SystemExit(
            f"GET /v2/profiles failed: {resp.status_code} {resp.text[:300]}"
        )
    return resp.json() or []


def _suggest_slug(info: dict, profile_id: str | int) -> str:
    name = (info.get("name") or "").strip().lower()
    cc = c.marketplace_country_code(
        info.get("marketplaceStringId"), info.get("countryCode"),
    ).lower()
    base = name or f"profile-{profile_id}"
    base = re.sub(r"[^a-z0-9_-]+", "-", base).strip("-_")
    if cc:
        base = f"{base}-{cc}"
    base = base[:64].rstrip("-_") or f"profile-{profile_id}"
    if not c.SLUG_RE.match(base):
        base = f"profile-{profile_id}"
    return base


def _register(identity: c.Identity, p: dict, *, brand: str) -> None:
    info = p.get("accountInfo", {}) or {}
    cfg_path = c.profile_config_path(brand)
    c.write_env_file(cfg_path, {
        "IDENTITY": identity.name,
        "ADS_PROFILE_ID": str(p.get("profileId", "")),
        "ADS_MARKETPLACE_ID": str(info.get("marketplaceStringId", "")),
        "ADS_ACCOUNT_NAME": str(info.get("name", "")),
        "ADS_TIMEZONE": str(p.get("timezone", "UTC")),
    })
    apf = c.active_profile_file()
    if not apf.exists():
        c.atomic_write_text(apf, brand + "\n")
    label = c.marketplace_label(info.get("marketplaceStringId"), info.get("countryCode"))
    print(
        f"  registered → {brand}  "
        f"({label}, profile_id={p.get('profileId')}, account={info.get('name')!r})"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Discover and register Amazon Ads profiles")
    ap.add_argument("--identity", required=True)
    ap.add_argument("--register", action="store_true",
                    help="register discovered profiles as brand slugs")
    ap.add_argument("--all", action="store_true",
                    help="(with --register) auto-register every visible profile with suggested slugs")
    args = ap.parse_args(argv)

    identity = c.load_identity(args.identity)
    print(f"identity: {identity.name}  region: {identity.region}", file=sys.stderr)
    token = _mint_access_token(identity)
    profiles = _list_v2_profiles(identity, token)

    if not profiles:
        print(f"no profiles returned for identity {identity.name!r} in {identity.region}",
              file=sys.stderr)
        return 1

    # Pair each Amazon profile with: (suggested-slug, existing-slug-if-already-registered)
    existing_by_pid = {
        c.parse_env_file(c.profile_config_path(s)).get("ADS_PROFILE_ID", ""): s
        for s in c.list_profile_slugs()
    }

    print(f"\n{len(profiles)} profile(s) visible:")
    for i, p in enumerate(profiles, 1):
        info = p.get("accountInfo", {}) or {}
        pid = str(p.get("profileId", ""))
        already = existing_by_pid.get(pid)
        suggested = _suggest_slug(info, pid)
        suffix = f"  [already registered as {already!r}]" if already else f"  [suggested slug: {suggested!r}]"
        label = c.marketplace_label(info.get("marketplaceStringId"), info.get("countryCode"))
        print(
            f"  [{i}] {label}  —  {info.get('name', '?')}  "
            f"(profile_id={pid}){suffix}"
        )

    if not args.register:
        return 0

    if args.all:
        for p in profiles:
            info = p.get("accountInfo", {}) or {}
            pid = str(p.get("profileId", ""))
            if pid in existing_by_pid:
                print(f"  skipped (already registered): profile_id={pid}")
                continue
            brand = _suggest_slug(info, pid)
            # Avoid collision with another brand slug
            if c.profile_config_path(brand).exists():
                brand = f"{brand}-{pid[-4:]}"
            _register(identity, p, brand=brand)
        return 0

    # Interactive register
    print("\nInteractive register. Press Enter to skip a profile.")
    for p in profiles:
        info = p.get("accountInfo", {}) or {}
        pid = str(p.get("profileId", ""))
        if pid in existing_by_pid:
            continue
        suggested = _suggest_slug(info, pid)
        brand = input(
            f"  profile_id={pid} ({info.get('name', '?')!r}): "
            f"register as [{suggested}] / Enter to skip: "
        ).strip()
        if not brand:
            brand = suggested
        if brand.lower() in ("skip", "n", "no"):
            continue
        try:
            brand = c.validate_slug(brand)
        except ValueError as e:
            print(f"  skipped (invalid slug): {e}")
            continue
        if c.profile_config_path(brand).exists():
            print(f"  skipped (slug {brand!r} already exists)")
            continue
        _register(identity, p, brand=brand)
    return 0


if __name__ == "__main__":
    sys.exit(main())

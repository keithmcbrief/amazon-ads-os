#!/usr/bin/env python3
"""Onboard an Amazon Ads brand profile.

Two-tier model:
  identities/<name>/.env  ← LWA app + refresh_token + region (the OAuth grant)
  profiles/<brand>/profile.env  ← profile_id + marketplace + identity reference

Flow:
  - If --identity NAME, reuse that identity (it must already exist)
  - If --new-identity NAME, create a fresh identity by prompting for LWA creds
  - Otherwise, list existing identities and prompt
  - Then call /v2/profiles using the chosen identity and pick one advertiser
  - Write profiles/<brand>/profile.env pointing at the identity

Secrets are collected via getpass — never via CLI flags (shell history + ps leak).
"""
from __future__ import annotations

import argparse
import getpass
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

import _common as c


# ---- Prompts -----------------------------------------------------------
def _prompt_secret(label: str) -> str:
    val = getpass.getpass(f"{label}: ").strip()
    if not val:
        raise SystemExit(f"refused: {label} cannot be empty")
    return val


def _prompt_text(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{label}{suffix}: ").strip()
    if not val and default is not None:
        return default
    if not val:
        raise SystemExit(f"refused: {label} cannot be empty")
    return val


# ---- LWA + Ads API helpers --------------------------------------------
def _mint_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    resp = requests.post(
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
    except json.JSONDecodeError:
        pass
    if resp.status_code != 200 or "access_token" not in body:
        raise c.parse_lwa_error(body or {"error": "unknown_error",
                                          "error_description": resp.text})
    return body["access_token"]


def _list_ads_profiles(region: str, client_id: str, access_token: str) -> list[dict]:
    host = c.host_for(region)
    resp = requests.get(
        host + "/v2/profiles",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Amazon-Advertising-API-ClientId": client_id,
            "Accept": "application/json",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise SystemExit(
            f"GET /v2/profiles failed ({region}): {resp.status_code} {resp.text}"
        )
    return resp.json() or []


# ---- Identity creation -------------------------------------------------
def _create_identity_interactive(name: str, *, sandbox: bool) -> tuple[c.Identity, str]:
    """Prompt for region + LWA creds, verify by minting a token, write the
    identity .env. Returns (identity, access_token) for the caller to reuse."""
    name = c.validate_slug(name)
    env_path = c.identity_env_path(name)
    if env_path.exists():
        raise SystemExit(
            f"identity {name!r} already exists at {env_path}. "
            f"Pass --identity {name} to reuse it, or pick a different name."
        )

    if sandbox:
        region = c.SANDBOX_REGION
        print(f"sandbox mode → region forced to {region}")
    else:
        region = _prompt_text("Region (NA/EU/FE)", default="NA").upper()
        if region not in c.REGION_HOSTS:
            raise SystemExit(f"refused: unknown region {region!r}; expected NA/EU/FE")

    print(f"\nCreating identity: {name}  (region={region})")
    print("Paste credentials at the prompts. Input is hidden.\n")
    client_id = _prompt_secret("LWA client_id")
    client_secret = _prompt_secret("LWA client_secret")
    refresh_token = _prompt_secret("LWA refresh_token")

    print("\nMinting an access token…")
    token = _mint_access_token(client_id, client_secret, refresh_token)
    print("ok")

    c.write_env_file(env_path, {
        "LWA_CLIENT_ID": client_id,
        "LWA_CLIENT_SECRET": client_secret,
        "LWA_REFRESH_TOKEN": refresh_token,
        "ADS_REGION": region,
    })
    print(f"wrote {env_path}")
    identity = c.load_identity(name)
    return identity, token


# ---- Profile registration ---------------------------------------------
def _confirm_profile_overwrite(slug: str, cfg_path: Path) -> None:
    print(f"\nProfile {slug!r} already exists at {cfg_path}.")
    typed = input(f"To overwrite, type the brand slug exactly: ").strip()
    if typed != slug:
        raise SystemExit("refused: confirmation slug did not match")
    backup = cfg_path.with_suffix(
        cfg_path.suffix + ".bak." + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    )
    shutil.copy2(cfg_path, backup)
    print(f"backup written → {backup}")


def _pick_ads_profile(profiles: list[dict]) -> dict:
    print()
    print("Profiles available for this identity:")
    for i, p in enumerate(profiles, 1):
        info = p.get("accountInfo", {}) or {}
        label = c.marketplace_label(
            info.get("marketplaceStringId"), info.get("countryCode"),
        )
        print(
            f"  [{i}] {label}  —  {info.get('name', '?')}  "
            f"({info.get('type', '?')}, profile_id={p.get('profileId')})"
        )
    if len(profiles) == 1:
        print("\nOnly one profile — selecting it.")
        return profiles[0]
    while True:
        choice = input(f"\nPick one [1-{len(profiles)}]: ").strip()
        if not choice.isdigit():
            continue
        idx = int(choice)
        if 1 <= idx <= len(profiles):
            return profiles[idx - 1]


def _register_profile(brand: str, identity: c.Identity, access_token: str,
                       *, overwrite: bool) -> None:
    cfg_path = c.profile_config_path(brand)
    if cfg_path.exists():
        if not overwrite:
            raise SystemExit(
                f"profile {brand!r} already exists. Pass --overwrite (you'll be asked to confirm)."
            )
        _confirm_profile_overwrite(brand, cfg_path)

    print(f"\nListing Amazon Ads profiles under identity {identity.name!r}…")
    profiles = _list_ads_profiles(identity.region, identity.client_id, access_token)

    # If region returned nothing and not sandbox, try probing other regions —
    # but this would require *new* creds; the user picked this identity, so
    # just surface the empty result. (Region auto-probe happens during
    # identity creation, not here.)
    if not profiles:
        raise SystemExit(
            f"identity {identity.name!r} returned no profiles in region "
            f"{identity.region}. Check that the refresh token belongs to an "
            f"Amazon Ads account in that region."
        )

    chosen = _pick_ads_profile(profiles)
    info = chosen.get("accountInfo", {}) or {}

    c.write_env_file(cfg_path, {
        "IDENTITY": identity.name,
        "ADS_PROFILE_ID": str(chosen.get("profileId", "")),
        "ADS_MARKETPLACE_ID": str(info.get("marketplaceStringId", "")),
        "ADS_ACCOUNT_NAME": str(info.get("name", "")),
        "ADS_TIMEZONE": str(chosen.get("timezone", "UTC")),
    })
    print(f"wrote {cfg_path}")

    apf = c.active_profile_file()
    if not apf.exists():
        c.atomic_write_text(apf, brand + "\n")
        print(f"set active profile → {brand}")

    print()
    print(f"Profile setup complete:")
    print(f"  brand:       {brand}")
    print(f"  identity:    {identity.name}")
    print(f"  account:     {info.get('name', '')!r}")
    print(f"  profile_id:  {chosen.get('profileId', '')}")
    print(f"  marketplace: {c.marketplace_label(info.get('marketplaceStringId'), info.get('countryCode'))}")
    print(f"  region:      {identity.region}")
    print(f"  timezone:    {chosen.get('timezone', 'UTC')}")
    print(f"  mode:        {'SANDBOX' if identity.is_sandbox else 'LIVE'}")
    print()
    print("next: run /amazon-doctor to verify network reachability")


# ---- Main --------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Add an Amazon Ads brand profile (identity + advertiser)"
    )
    ap.add_argument("--brand", required=True, help="brand slug, e.g. acme-us")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--identity", help="reuse an existing identity by name")
    group.add_argument("--new-identity", help="create a fresh identity with this name")
    ap.add_argument("--sandbox", action="store_true",
                    help="when creating a new identity, force region=SANDBOX")
    ap.add_argument("--overwrite", action="store_true",
                    help="overwrite an existing profile (requires typed-slug confirm)")
    args = ap.parse_args(argv)

    brand = c.validate_slug(args.brand)

    # Decide which identity to use
    existing_identities = c.list_identity_names()
    if args.identity:
        if args.identity not in existing_identities:
            raise SystemExit(
                f"identity {args.identity!r} not found. "
                f"Existing: {', '.join(existing_identities) or '(none)'}. "
                f"Use --new-identity to create a fresh one."
            )
        identity = c.load_identity(args.identity)
        # Mint a fresh token so we can call /v2/profiles
        token = _mint_access_token(identity.client_id, identity.client_secret,
                                     identity.refresh_token)
    elif args.new_identity:
        identity, token = _create_identity_interactive(args.new_identity,
                                                         sandbox=args.sandbox)
    elif not existing_identities:
        # Cold start: prompt user for identity name (default = brand slug)
        name = _prompt_text("Identity name", default=brand)
        identity, token = _create_identity_interactive(name, sandbox=args.sandbox)
    else:
        # Identities exist — let the user pick or create new
        print("Existing identities:")
        for i, name in enumerate(existing_identities, 1):
            print(f"  [{i}] {name}")
        print(f"  [n] create a new identity")
        choice = input(f"Pick [1-{len(existing_identities)}/n]: ").strip().lower()
        if choice == "n":
            new_name = _prompt_text("New identity name", default=brand)
            identity, token = _create_identity_interactive(new_name, sandbox=args.sandbox)
        elif choice.isdigit() and 1 <= int(choice) <= len(existing_identities):
            name = existing_identities[int(choice) - 1]
            identity = c.load_identity(name)
            token = _mint_access_token(identity.client_id, identity.client_secret,
                                         identity.refresh_token)
        else:
            raise SystemExit("refused: invalid identity selection")

    _register_profile(brand, identity, token, overwrite=args.overwrite)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""List configured Amazon Ads identities and the profiles registered under each."""
from __future__ import annotations

import argparse
import json
import sys

import _common as c


def _profile_summary(slug: str) -> dict:
    cfg = c.parse_env_file(c.profile_config_path(slug))
    identity_name = cfg.get("IDENTITY", "")
    reports_dir = c.profile_dir(slug) / "reports"
    last_report = None
    if reports_dir.exists():
        all_csvs = sorted(reports_dir.rglob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
        if all_csvs:
            last_report = all_csvs[0].name
    return {
        "slug": slug,
        "identity": identity_name,
        "account_name": cfg.get("ADS_ACCOUNT_NAME", ""),
        "profile_id": cfg.get("ADS_PROFILE_ID", ""),
        "marketplace": cfg.get("ADS_MARKETPLACE_ID", ""),
        "timezone": cfg.get("ADS_TIMEZONE", ""),
        "last_report": last_report,
    }


def _identity_summary(name: str) -> dict:
    env = c.parse_env_file(c.identity_env_path(name))
    token_path = c.identity_dir(name) / "state" / "access_token.json"
    token_cache = c.read_json_or(token_path, {})
    return {
        "name": name,
        "region": env.get("ADS_REGION", ""),
        "sandbox": env.get("ADS_REGION", "").upper() == c.SANDBOX_REGION,
        "last_auth_expiry_epoch": token_cache.get("expires_at_epoch"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="List Amazon Ads identities and profiles")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args(argv)

    identities = [_identity_summary(n) for n in c.list_identity_names()]
    profiles = [_profile_summary(s) for s in c.list_profile_slugs()]
    active = ""
    apf = c.active_profile_file()
    if apf.exists():
        active = apf.read_text().strip()

    if args.json:
        json.dump(
            {"active": active, "identities": identities, "profiles": profiles},
            sys.stdout, indent=2,
        )
        sys.stdout.write("\n")
        return 0

    if not identities:
        print("no identities or profiles configured. run /amazon-setup-profile to add one.")
        return 0

    print("IDENTITIES")
    print(f"  {'name':<20} {'region':<8} {'mode':<8}")
    for i in identities:
        mode = "SANDBOX" if i["sandbox"] else "LIVE"
        print(f"  {i['name']:<20} {i['region']:<8} {mode:<8}")

    if not profiles:
        print("\nno profiles registered yet.")
        return 0

    print("\nPROFILES")
    print(f"  {'active':<7} {'slug':<20} {'identity':<20} {'account':<28} "
          f"{'profile_id':<14} {'mkt':<14}")
    for p in profiles:
        active_marker = "  *" if p["slug"] == active else "   "
        print(
            f"  {active_marker:<7} {p['slug']:<20} {p['identity']:<20} "
            f"{p['account_name'][:28]:<28} {p['profile_id']:<14} {p['marketplace']:<14}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

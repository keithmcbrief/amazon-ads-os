#!/usr/bin/env python3
"""Switch the active Amazon Ads brand for this machine."""
from __future__ import annotations

import argparse
import sys

import _common as c


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Switch the active Amazon Ads profile")
    ap.add_argument("--brand", required=True, help="brand slug to make active")
    args = ap.parse_args(argv)

    slug = c.validate_slug(args.brand)
    if slug not in c.list_profile_slugs():
        existing = c.list_profile_slugs()
        msg = f"profile {slug!r} not found."
        if existing:
            msg += f" existing: {', '.join(existing)}"
        else:
            msg += " no profiles configured — run /amazon-setup-profile first."
        raise SystemExit(msg)

    # Load (and thus validate sandbox/host consistency) before committing the switch
    profile = c.load_profile(slug, source="--brand")
    c.atomic_write_text(c.active_profile_file(), slug + "\n")
    print(f"active profile → {slug} ({profile.account_name!r}  region={profile.region}  mode={profile.mode})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

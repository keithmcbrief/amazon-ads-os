#!/usr/bin/env python3
"""Print the active Amazon Ads profile (resolves --profile / env / active file)."""
from __future__ import annotations

import argparse
import sys

import _common as c


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Show active Amazon Ads profile")
    ap.add_argument("--profile", default=None, help="one-off override (does not change active)")
    args = ap.parse_args(argv)

    try:
        profile = c.resolve_profile(args.profile)
    except (FileNotFoundError, RuntimeError) as e:
        print(str(e), file=sys.stderr)
        return 1
    c.print_resolution_banner(profile, file=sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Pull an Amazon Ads report end-to-end (submit + poll + download).

Resumable: if a pending state file exists for the same {profile, slug, start,
end, columns, groupBy, specs_version, api_version} hash, this command resumes
instead of re-submitting. Use `--start --end` in YYYY-MM-DD format.

On exit:
- 0 → CSV written; path printed on stdout
- 1 → submit/download/FAILED — error printed on stderr
- 2 → still processing past the cap; rerun the same command to resume
"""
from __future__ import annotations

import argparse
import sys

import _common as c
import _reports as r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pull an Amazon Ads report")
    ap.add_argument("--report", required=True, choices=sorted(c.REPORT_SPECS),
                    help="report slug (see REPORT_SPECS)")
    ap.add_argument("--start", required=True, help="start date YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="end date YYYY-MM-DD")
    ap.add_argument("--profile", default=None,
                    help="one-off profile override (does not change active)")
    ap.add_argument("--poll-interval", type=float, default=30.0)
    ap.add_argument("--poll-timeout", type=float, default=1200.0)
    args = ap.parse_args(argv)

    try:
        profile = c.resolve_profile(args.profile)
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1

    try:
        _ = profile.mode  # raises if sandbox flag/region disagree
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1

    c.print_resolution_banner(profile)
    res = r.run_report(
        profile, args.report, args.start, args.end,
        poll_interval_s=args.poll_interval,
        poll_timeout_s=args.poll_timeout,
    )
    if res.csv_path:
        print(str(res.csv_path))
    if res.message:
        print(res.message, file=sys.stderr)
    return res.exit_code


if __name__ == "__main__":
    sys.exit(main())

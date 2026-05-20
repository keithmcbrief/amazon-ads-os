#!/usr/bin/env python3
"""Poll an in-flight Amazon Ads report. Resumable via the pending state file."""
from __future__ import annotations

import argparse
import sys

import _common as c
import _reports as r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Poll an in-flight Amazon Ads report")
    ap.add_argument("--report", required=True, choices=sorted(c.REPORT_SPECS))
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--profile", default=None)
    ap.add_argument("--poll-interval", type=float, default=30.0)
    ap.add_argument("--poll-timeout", type=float, default=1200.0)
    args = ap.parse_args(argv)

    profile = c.resolve_profile(args.profile)
    c.print_resolution_banner(profile)
    h = r.hash_for_request(profile, args.report, args.start, args.end)
    state_path = r.pending_state_path(profile, h)
    state = c.read_json_or(state_path, None)
    if not state:
        print(f"no pending state for hash {h}; nothing to poll", file=sys.stderr)
        return 1
    res = r.poll_until_terminal(
        profile, state,
        interval_s=args.poll_interval,
        timeout_s=args.poll_timeout,
    )
    c.atomic_write_json(state_path, res.state)
    if res.completed:
        print(f"COMPLETED url={res.download_url}")
        return 0
    if res.failed:
        print(f"FAILED: {res.error}", file=sys.stderr)
        return 1
    print("still processing — rerun to resume", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

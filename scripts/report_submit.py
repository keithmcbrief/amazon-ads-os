#!/usr/bin/env python3
"""Submit an Amazon Ads report without polling.

Persists the pending state file with the returned reportId so a later
`report_poll.py` or `report_run.py` invocation can resume.
"""
from __future__ import annotations

import argparse
import sys

import _common as c
import _reports as r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Submit an Amazon Ads report (no polling)")
    ap.add_argument("--report", required=True, choices=sorted(c.REPORT_SPECS))
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--profile", default=None)
    args = ap.parse_args(argv)

    profile = c.resolve_profile(args.profile)
    c.print_resolution_banner(profile)
    state = r.submit_report(profile, args.report, args.start, args.end)
    state_path = r.pending_state_path(profile, state["request_hash"])
    c.atomic_write_json(state_path, state)
    print(state["reportId"])
    print(f"saved → {state_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Download a COMPLETED Amazon Ads report to CSV."""
from __future__ import annotations

import argparse
import sys

import _common as c
import _reports as r


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Download a completed Amazon Ads report")
    ap.add_argument("--report", required=True, choices=sorted(c.REPORT_SPECS))
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--profile", default=None)
    args = ap.parse_args(argv)

    profile = c.resolve_profile(args.profile)
    c.print_resolution_banner(profile)
    h = r.hash_for_request(profile, args.report, args.start, args.end)
    state_path = r.pending_state_path(profile, h)
    state = c.read_json_or(state_path, None)
    if not state:
        print(f"no pending state for hash {h}", file=sys.stderr)
        return 1
    if state.get("status") != "COMPLETED":
        # Re-poll once in case the status moved after the poll script finished
        from _reports import poll_once
        latest = poll_once(profile, state)
        state["status"] = (latest.get("status") or "").upper()
        if state["status"] != "COMPLETED":
            print(f"not ready: status={state['status']}", file=sys.stderr)
            return 1
        download_url = latest.get("url")
    else:
        download_url = None  # download_report re-fetches if needed
    path = r.download_report(profile, state, download_url)
    print(str(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())

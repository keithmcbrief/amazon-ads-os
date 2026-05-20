#!/usr/bin/env python3
"""Verify + execute a negative-keyword proposal.

Without `--execute`, this is a dry run: verifies the proposal's hash and
metadata against the active profile and prints what would be sent, but
sends nothing. With `--execute`, chunks the API calls, dedupes against
existing negatives one more time, writes a per-row result CSV, and appends
JSONL entries to the per-profile mutation ledger.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import _common as c
import _negatives as n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Add negative keywords from a proposal")
    ap.add_argument("--proposal", required=True, help="path to proposal CSV")
    ap.add_argument("--proposal-hash", default=None, help="expected sha256 of proposal CSV")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--execute", action="store_true",
                    help="actually send mutations (otherwise: dry-run only)")
    ap.add_argument("--chunk-size", type=int, default=100)
    args = ap.parse_args(argv)

    profile = c.resolve_profile(args.profile)
    _ = profile.mode
    c.print_resolution_banner(profile)

    try:
        proposal = n.load_and_verify_proposal(
            Path(args.proposal), profile,
            expected_hash=args.proposal_hash,
        )
    except n.ProposalDrift as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1

    res = n.execute_proposal(profile, proposal, chunk_size=args.chunk_size,
                              dry_run=not args.execute)
    label = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"{label} {profile.mode}: proposed={res.proposed} "
          f"skipped_existing={res.skipped_existing} attempted={res.attempted} "
          f"created={res.created} failed={res.failed}", file=sys.stderr)
    if res.result_csv:
        print(str(res.result_csv))
    return 0 if res.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

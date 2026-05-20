#!/usr/bin/env python3
"""Build an immutable negative-keyword proposal from the latest sp-search-terms CSV.

Reads the newest sp-search-terms report for the active brand, applies threshold
filters, dedupes against existing negatives, flags conflicts with enabled
positive keywords, and writes a proposal CSV + .meta.json sidecar.

Prints (on stdout, one line each):
    <proposal_csv_path>
    <csv_sha256>
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import _common as c
import _negatives as n


def _newest_search_terms_csv(profile: c.Profile) -> Path | None:
    folder = c.profile_dir(profile.slug) / "reports" / "sp-search-terms"
    if not folder.exists():
        return None
    csvs = sorted(folder.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
    return csvs[0] if csvs else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build a negative-keyword proposal")
    ap.add_argument("--profile", default=None)
    ap.add_argument("--min-spend", type=float, default=10.0)
    ap.add_argument("--max-sales", type=float, default=0.0)
    ap.add_argument("--min-clicks", type=int, default=5)
    ap.add_argument("--lookback-days", type=int, default=30)
    ap.add_argument("--exclude-last-days", type=int, default=3)
    ap.add_argument("--match-type", default="negativeExact",
                    choices=["negativeExact", "negativePhrase"])
    ap.add_argument("--source-csv", default=None,
                    help="explicit search-terms CSV path; default is newest")
    ap.add_argument("--source-report-hash", default="",
                    help="hash of the source report (recorded in meta)")
    args = ap.parse_args(argv)

    profile = c.resolve_profile(args.profile)
    _ = profile.mode  # raises if sandbox/region disagree
    c.print_resolution_banner(profile)

    source_csv = Path(args.source_csv) if args.source_csv else _newest_search_terms_csv(profile)
    if not source_csv or not source_csv.exists():
        print("no sp-search-terms CSV found. run /amazon-pull-report first.", file=sys.stderr)
        return 1

    with source_csv.open() as f:
        rows = list(csv.DictReader(f))

    thresholds = n.Thresholds(
        min_spend=args.min_spend,
        max_sales=args.max_sales,
        min_clicks=args.min_clicks,
        lookback_days=args.lookback_days,
        exclude_last_days=args.exclude_last_days,
        match_type=args.match_type,
    )
    candidates = n.filter_search_term_rows(rows, thresholds)
    print(f"{len(candidates)} candidate(s) after threshold filter", file=sys.stderr)

    proposal = n.build_proposal(profile, candidates, thresholds=thresholds,
                                  source_report_hash=args.source_report_hash)
    n.write_proposal(proposal, profile)

    # Brief summary
    print(f"proposal: {len(proposal.rows)} negatives, "
          f"${proposal.meta['total_spend']:.2f} wasted spend, "
          f"{proposal.meta['campaigns_affected']} campaign(s)", file=sys.stderr)
    print(str(proposal.csv_path))
    print(proposal.csv_hash)
    return 0


if __name__ == "__main__":
    sys.exit(main())

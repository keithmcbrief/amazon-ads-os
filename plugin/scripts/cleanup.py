#!/usr/bin/env python3
"""Clean up stale amazon-ads-os data.

Removes old report CSVs, expired pending state files, archived/failed states,
old proposals + their result CSVs, and rotates logs. Dry-run by default.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import _common as c


def _is_older_than(path: Path, days: int) -> bool:
    cutoff = time.time() - (days * 86400)
    return path.stat().st_mtime < cutoff


def _candidates(profile_slug: str) -> list[tuple[str, Path]]:
    base = c.profile_dir(profile_slug)
    out: list[tuple[str, Path]] = []
    for sub in ("reports", "proposals"):
        d = base / sub
        if d.exists():
            for f in d.rglob("*.csv"):
                out.append((sub, f))
            for f in d.rglob("*.meta.json"):
                out.append((sub, f))
    state = base / "state"
    if state.exists():
        for f in state.glob("pending_*.json*"):
            out.append(("state", f))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Clean up old amazon-ads-os data")
    ap.add_argument("--older-than-days", type=int, default=30)
    ap.add_argument("--profile", default=None, help="only clean this brand")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default is dry-run)")
    args = ap.parse_args(argv)

    slugs = [args.profile] if args.profile else c.list_profile_slugs()
    total_to_remove = 0
    total_bytes = 0

    for slug in slugs:
        slug = c.validate_slug(slug)
        if not (c.profile_dir(slug) / ".env").exists():
            continue
        for kind, path in _candidates(slug):
            try:
                if not _is_older_than(path, args.older_than_days):
                    continue
                size = path.stat().st_size
                total_to_remove += 1
                total_bytes += size
                action = "DELETE" if args.apply else "would-delete"
                print(f"{action} [{kind:8}] {path}  ({size}b)")
                if args.apply:
                    path.unlink()
            except OSError as e:
                print(f"skip {path}: {e}", file=sys.stderr)

    # Logs
    if c.logs_dir().exists():
        for log in c.logs_dir().glob("*.log"):
            if not _is_older_than(log, args.older_than_days):
                continue
            size = log.stat().st_size
            total_to_remove += 1
            total_bytes += size
            action = "DELETE" if args.apply else "would-delete"
            print(f"{action} [logs    ] {log}  ({size}b)")
            if args.apply:
                log.unlink()

    suffix = "" if args.apply else " (dry-run; pass --apply to actually delete)"
    print(f"\n{total_to_remove} file(s), {total_bytes/1024/1024:.2f} MB{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

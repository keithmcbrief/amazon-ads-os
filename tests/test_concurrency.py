"""Concurrency: per-profile mutation lock and per-request report lock both
refuse to run when held by another process."""
from __future__ import annotations

import os
import time
from unittest.mock import patch

import _common as c
import _negatives as n
import _reports as r
from conftest import seed_profile


def _seed_profile(slug: str = "brand-a") -> c.Profile:
    seed_profile(slug)
    return c.load_profile(slug, source="--brand")


def test_mutation_lock_refuses_when_held():
    profile = _seed_profile()
    lock_path = c.profile_dir(profile.slug) / "state" / "mutation.lock"

    # Manually acquire the lock to simulate another process
    import fcntl
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    holder.write('{"pid": 99999, "started": "2026-05-19T10:00:00Z"}')
    holder.flush()

    try:
        # Build a minimal proposal so execute can reach the lock
        prop = n.write_proposal(
            n.build_proposal(profile, [], thresholds=n.Thresholds(),
                              existing_negatives=set(), enabled_positives=set()),
            profile,
        )
        import pytest
        with pytest.raises(RuntimeError) as ei:
            n.execute_proposal(profile, prop, dry_run=False)
        assert "another mutation in progress" in str(ei.value)
        assert "99999" in str(ei.value)
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_report_run_refuses_when_request_lock_held():
    profile = _seed_profile()
    h = r.hash_for_request(profile, "sp-campaigns", "2026-04-01", "2026-04-30")
    lock_path = r.report_lock_path(profile, h)

    import fcntl
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock_path, "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    holder.write('{"pid": 12345, "started": "2026-05-19T10:00:00Z"}')
    holder.flush()

    try:
        res = r.run_report(
            profile, "sp-campaigns", "2026-04-01", "2026-04-30",
            poll_interval_s=0, poll_timeout_s=1,
            sleep=lambda s: None, now=lambda: 0.0,
            progress=lambda m: None,
        )
        assert res.exit_code == 1
        assert "another report run already in progress" in res.message
        assert "12345" in res.message
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()

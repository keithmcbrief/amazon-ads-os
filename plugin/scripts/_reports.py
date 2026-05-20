"""Report flow library: submit, poll, download, and the orchestrator.

The CLI wrappers (report_submit.py / report_poll.py / report_download.py /
report_run.py) are intentionally thin. All real logic lives here so tests
can drive it directly with mocked HTTP.
"""
from __future__ import annotations

import gzip
import io
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests

import _common as c


# ---- Date validation ---------------------------------------------------
def validate_date_window(slug: str, start: str, end: str, *, today: str | None = None) -> None:
    """Raise ValueError if the date window violates REPORT_SPECS for `slug`."""
    if slug not in c.REPORT_SPECS:
        raise ValueError(f"unknown report slug {slug!r}; known: {sorted(c.REPORT_SPECS)}")
    spec = c.REPORT_SPECS[slug]
    try:
        d_start = datetime.strptime(start, "%Y-%m-%d").date()
        d_end = datetime.strptime(end, "%Y-%m-%d").date()
    except ValueError as e:
        raise ValueError(f"dates must be YYYY-MM-DD: {e}")
    if d_end < d_start:
        raise ValueError(f"end ({end}) is before start ({start})")
    span_days = (d_end - d_start).days + 1
    if span_days > spec["max_days"]:
        raise ValueError(
            f"window {span_days}d exceeds max_days={spec['max_days']} for {slug}"
        )
    if today is None:
        today = datetime.now(timezone.utc).date().isoformat()
    d_today = datetime.strptime(today, "%Y-%m-%d").date()
    if d_end > d_today:
        raise ValueError(f"end ({end}) is in the future relative to {today}")
    lookback_days = (d_today - d_end).days
    if lookback_days > spec["max_lookback_days"]:
        raise ValueError(
            f"end ({end}) is {lookback_days}d ago; exceeds max_lookback_days="
            f"{spec['max_lookback_days']} for {slug}"
        )


def hash_for_request(profile: c.Profile, slug: str, start: str, end: str) -> str:
    spec = c.REPORT_SPECS[slug]
    return c.request_hash(
        profile_id=profile.profile_id,
        region=profile.region,
        slug=slug,
        start=start,
        end=end,
        columns=spec["columns"],
        group_by=spec["groupBy"],
    )


def pending_state_path(profile: c.Profile, request_hash: str) -> Path:
    return c.profile_dir(profile.slug) / "state" / f"pending_{request_hash}.json"


def report_lock_path(profile: c.Profile, request_hash: str) -> Path:
    return c.profile_dir(profile.slug) / "state" / f"pending_{request_hash}.lock"


# ---- Status helpers ----------------------------------------------------
def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stale_pending(state: dict[str, Any], *, max_age_hours: int = 24) -> bool:
    created = state.get("created_at")
    if not created:
        return True
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return True
    age = datetime.now(timezone.utc) - dt
    return age.total_seconds() > max_age_hours * 3600


# ---- Submit ------------------------------------------------------------
def submit_report(
    profile: c.Profile,
    slug: str,
    start: str,
    end: str,
    *,
    name: str | None = None,
) -> dict[str, Any]:
    """Submit a v3 report request. Returns the parsed pending state we'll persist.

    Validates the date window first. Does NOT write to disk — the caller
    decides when to persist (run_report does it immediately).
    """
    validate_date_window(slug, start, end)
    spec = c.REPORT_SPECS[slug]
    # Amazon v3 reports with timeUnit=SUMMARY don't accept "date" as a column —
    # they require "startDate" + "endDate" instead. Our REPORT_SPECS keep "date"
    # because that's the canonical CSV column we write downstream; we translate
    # on the wire here and map it back when we decode the response.
    api_columns: list[str] = []
    for col in spec["columns"]:
        if col == "date":
            api_columns.extend(["startDate", "endDate"])
        else:
            api_columns.append(col)

    body: dict[str, Any] = {
        "name": name or f"amazon-ads-os {slug} {start}_{end}",
        "startDate": start,
        "endDate": end,
        "configuration": {
            "adProduct": spec["adProduct"],
            "reportTypeId": spec["reportTypeId"],
            "columns": api_columns,
            "timeUnit": "SUMMARY",
            "format": "GZIP_JSON",
            "groupBy": list(spec["groupBy"]),
        },
    }
    if "filter" in spec:
        body["configuration"]["filters"] = [
            {"field": "keywordType", "values": [spec["filter"]]},
        ]
    resp = c.ads_request(
        profile, "POST", "/reporting/reports",
        json_body=body,
        content_type="application/vnd.createasyncreportrequest.v3+json",
        accept="application/vnd.createasyncreportrequest.v3+json",
    )
    if resp.status_code not in (200, 202):
        raise RuntimeError(
            f"report submit failed ({resp.status_code}): {resp.text[:500]}"
        )
    payload = resp.json()
    report_id = payload.get("reportId")
    if not report_id:
        raise RuntimeError(f"submit response missing reportId: {payload}")

    h = hash_for_request(profile, slug, start, end)
    return {
        "reportId": report_id,
        "slug": slug,
        "start": start,
        "end": end,
        "request_hash": h,
        "specs_version": c.REPORT_SPECS_VERSION,
        "api_version": c.ADS_API_VERSION,
        "profile_id": profile.profile_id,
        "region": profile.region,
        "created_at": _utc_now_iso(),
        "last_polled_at": None,
        "status": "PENDING",
        "request_params": {
            "columns": spec["columns"],
            "groupBy": spec["groupBy"],
        },
    }


# ---- Poll --------------------------------------------------------------
@dataclass
class PollResult:
    state: dict[str, Any]
    completed: bool
    failed: bool
    timed_out: bool
    download_url: str | None = None
    error: str | None = None


def poll_once(profile: c.Profile, state: dict[str, Any]) -> dict[str, Any]:
    """Issue one status GET. Returns the latest Amazon payload."""
    report_id = state["reportId"]
    resp = c.ads_request(
        profile, "GET", f"/reporting/reports/{report_id}",
        accept="application/vnd.createasyncreportrequest.v3+json",
        content_type="application/vnd.createasyncreportrequest.v3+json",
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"poll failed ({resp.status_code}): {resp.text[:500]}"
        )
    return resp.json()


def poll_until_terminal(
    profile: c.Profile,
    state: dict[str, Any],
    *,
    interval_s: float = 30.0,
    timeout_s: float = 1200.0,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    progress: Callable[[str], None] = lambda m: print(m, file=sys.stderr),
) -> PollResult:
    """Loop poll_once() until COMPLETED, FAILED, CANCELLED, or timeout."""
    start = now()
    attempts = 0
    while True:
        attempts += 1
        payload = poll_once(profile, state)
        status = (payload.get("status") or "").upper()
        state["status"] = status
        state["last_polled_at"] = _utc_now_iso()
        elapsed = now() - start
        progress(f"status={status} attempts={attempts} elapsed={int(elapsed)}s")
        if status == "COMPLETED":
            return PollResult(
                state=state,
                completed=True,
                failed=False,
                timed_out=False,
                download_url=payload.get("url"),
            )
        if status in ("FAILED", "CANCELLED"):
            return PollResult(
                state=state,
                completed=False,
                failed=True,
                timed_out=False,
                error=str(payload.get("failureReason") or payload.get("statusDetails") or status),
            )
        if elapsed >= timeout_s:
            return PollResult(
                state=state,
                completed=False,
                failed=False,
                timed_out=True,
            )
        sleep(interval_s)


# ---- Download ----------------------------------------------------------
def download_report(
    profile: c.Profile,
    state: dict[str, Any],
    download_url: str | None,
) -> Path:
    """Download, gunzip, flatten JSON to CSV at canonical path. Re-fetches metadata
    if the signed URL has expired (403).
    """
    slug = state["slug"]
    if download_url is None:
        # Re-fetch metadata to get a fresh URL
        meta = poll_once(profile, state)
        download_url = meta.get("url")
    if not download_url:
        raise RuntimeError("no download URL available")

    rows = _fetch_and_decode(download_url, profile=profile, state=state)
    spec = c.REPORT_SPECS[slug]
    columns = spec["columns"]
    # Amazon returns startDate/endDate (because that's what we sent for SUMMARY)
    # but our canonical CSV columns include "date". Backfill it from startDate so
    # downstream consumers (search_terms_analyze, etc.) keep working.
    if "date" in columns:
        for row in rows:
            if "date" not in row and "startDate" in row:
                row["date"] = row["startDate"]
    out_path = canonical_csv_path(profile, slug, state["start"], state["end"], state["request_hash"])
    c.write_rows_csv(out_path, columns, rows)
    return out_path


def _fetch_and_decode(url: str, *, profile: c.Profile, state: dict[str, Any]) -> list[dict[str, Any]]:
    """GET signed URL, gunzip, parse JSON. Retries once with fresh URL on 403."""
    resp = requests.get(url, timeout=120)
    if resp.status_code == 403:
        meta = poll_once(profile, state)
        fresh = meta.get("url")
        if not fresh:
            raise RuntimeError("download URL expired and re-fetch returned no fresh URL")
        resp = requests.get(fresh, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"download failed ({resp.status_code}): {resp.text[:200]}")
    raw = resp.content
    # Reports may be gzipped JSON regardless of Content-Encoding header
    try:
        data = gzip.decompress(raw)
    except (OSError, gzip.BadGzipFile):
        data = raw
    payload = json.loads(data.decode("utf-8"))
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        # Some report types nest rows under a key — pick the first list value
        for v in payload.values():
            if isinstance(v, list):
                return v
        return []
    return []


def canonical_csv_path(
    profile: c.Profile, slug: str, start: str, end: str, request_hash: str,
) -> Path:
    short = request_hash[:8]
    name = f"{profile.slug}__{slug}__{start}_{end}__{short}.csv"
    return c.profile_dir(profile.slug) / "reports" / slug / name


# ---- Orchestrator ------------------------------------------------------
@dataclass
class RunResult:
    csv_path: Path | None
    exit_code: int
    message: str


def run_report(
    profile: c.Profile,
    slug: str,
    start: str,
    end: str,
    *,
    poll_interval_s: float = 30.0,
    poll_timeout_s: float = 1200.0,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    progress: Callable[[str], None] = lambda m: print(m, file=sys.stderr),
) -> RunResult:
    """Submit (or resume), poll, and download a report. Resumable across runs."""
    h = hash_for_request(profile, slug, start, end)
    state_path = pending_state_path(profile, h)
    lock_path = report_lock_path(profile, h)

    try:
        with c.file_lock(lock_path):
            return _run_locked(
                profile, slug, start, end, h, state_path,
                poll_interval_s=poll_interval_s,
                poll_timeout_s=poll_timeout_s,
                sleep=sleep, now=now, progress=progress,
            )
    except BlockingIOError:
        info = c.lock_holder_info(lock_path)
        return RunResult(
            csv_path=None,
            exit_code=1,
            message=f"another report run already in progress (pid={info.get('pid')}, started={info.get('started')})",
        )


def _run_locked(
    profile, slug, start, end, request_hash, state_path,
    *, poll_interval_s, poll_timeout_s, sleep, now, progress,
):
    state = c.read_json_or(state_path, None)
    if state and _stale_pending(state):
        progress(f"previous pending report >24h old — archiving and resubmitting")
        _archive_state(state_path, suffix="stale")
        state = None
    # Validate that an existing state's specs_version + api_version match
    if state and (state.get("specs_version") != c.REPORT_SPECS_VERSION
                  or state.get("api_version") != c.ADS_API_VERSION):
        progress("pending state from a different specs/api version — archiving and resubmitting")
        _archive_state(state_path, suffix="oldspecs")
        state = None

    if not state:
        progress("submitting new report…")
        state = submit_report(profile, slug, start, end)
        c.atomic_write_json(state_path, state)
        progress(f"reportId={state['reportId']} (saved → {state_path})")

    poll_res = poll_until_terminal(
        profile, state,
        interval_s=poll_interval_s,
        timeout_s=poll_timeout_s,
        sleep=sleep, now=now, progress=progress,
    )
    # Persist any status updates from polling
    c.atomic_write_json(state_path, poll_res.state)

    if poll_res.completed:
        csv_path = download_report(profile, poll_res.state, poll_res.download_url)
        # success — remove pending state; the CSV is the artifact
        _archive_state(state_path, suffix="done")
        return RunResult(csv_path=csv_path, exit_code=0, message=f"wrote {csv_path}")
    if poll_res.failed:
        _archive_state(state_path, suffix="failed")
        return RunResult(
            csv_path=None,
            exit_code=1,
            message=f"report {poll_res.state.get('status')}: {poll_res.error}",
        )
    if poll_res.timed_out:
        return RunResult(
            csv_path=None,
            exit_code=2,
            message=f"report still processing after {poll_timeout_s:.0f}s — rerun the same command to resume",
        )
    return RunResult(csv_path=None, exit_code=1, message="unknown poll outcome")


def _archive_state(state_path: Path, *, suffix: str) -> None:
    if not state_path.exists():
        return
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    target = state_path.with_suffix(state_path.suffix + f".{suffix}.{stamp}")
    state_path.rename(target)

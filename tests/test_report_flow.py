"""Mocked report-flow tests.

Patches `_common.requests` so the entire submit/poll/download path runs
without hitting Amazon. Covers 401-refresh-and-retry, 429-with-Retry-After,
FAILED status, empty result, and signed-URL 403 → re-fetch metadata.
"""
from __future__ import annotations

import gzip
import io
import json
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch, MagicMock

import pytest

import _common as c
import _reports as r
from conftest import seed_profile


def _seed_profile(slug: str = "brand-a") -> c.Profile:
    seed_profile(slug)
    return c.load_profile(slug, source="--brand")


@dataclass
class FakeResponse:
    status_code: int
    body: Any = None
    headers: dict = field(default_factory=dict)
    content_bytes: bytes = b""

    def json(self):
        if isinstance(self.body, (dict, list)):
            return self.body
        raise json.JSONDecodeError("not json", "", 0)

    @property
    def text(self) -> str:
        if isinstance(self.body, (dict, list)):
            return json.dumps(self.body)
        return str(self.body or "")

    @property
    def content(self) -> bytes:
        return self.content_bytes


def _gzip_json(rows: list[dict]) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(json.dumps(rows).encode("utf-8"))
    return buf.getvalue()


# ---- submit -----------------------------------------------------------

def test_submit_persists_state_with_hash():
    profile = _seed_profile()
    submit_resp = FakeResponse(202, {"reportId": "rpt-1"})
    with patch("_common.requests.request", return_value=submit_resp):
        state = r.submit_report(profile, "sp-campaigns", "2026-04-01", "2026-04-30")
    assert state["reportId"] == "rpt-1"
    assert state["specs_version"] == c.REPORT_SPECS_VERSION
    assert state["api_version"] == c.ADS_API_VERSION
    assert state["status"] == "PENDING"
    assert state["request_hash"] == r.hash_for_request(profile, "sp-campaigns", "2026-04-01", "2026-04-30")


def test_submit_invalid_window_raises_before_http():
    profile = _seed_profile()
    with patch("_common.requests.request") as m:
        with pytest.raises(ValueError):
            r.submit_report(profile, "sp-campaigns", "2026-05-01", "2026-04-30")
        m.assert_not_called()


def test_submit_translates_date_column_to_startDate_endDate():
    """Amazon v3 reports with timeUnit=SUMMARY reject 'date' as a column.
    We must send startDate + endDate on the wire even though our REPORT_SPECS
    keep 'date' as the canonical CSV column name."""
    profile = _seed_profile()
    submit_resp = FakeResponse(202, {"reportId": "rpt-translated"})
    with patch("_common.requests.request", return_value=submit_resp) as m:
        r.submit_report(profile, "sp-search-terms", "2026-04-01", "2026-04-30")
    # Inspect the body that was sent
    call = m.call_args
    body = call.kwargs.get("json")
    cols = body["configuration"]["columns"]
    assert "date" not in cols, "'date' column was sent to Amazon; SUMMARY rejects it"
    assert "startDate" in cols and "endDate" in cols, (
        f"startDate/endDate not in submitted columns: {cols}"
    )


def test_download_backfills_date_from_startDate():
    """API returns startDate/endDate but our CSV columns include 'date'.
    The downloader should populate 'date' from 'startDate' so downstream
    code (search_terms_analyze, etc.) keeps working."""
    profile = _seed_profile()
    rows = [{
        "startDate": "2026-04-01", "endDate": "2026-04-30",
        "campaignId": "111", "campaignName": "x", "searchTerm": "blue sneakers",
        "cost": 12.34, "sales7d": 0, "clicks": 5,
    }]
    dl_resp = MagicMock(status_code=200, content=_gzip_json(rows))
    state = {
        "reportId": "rpt-x",
        "slug": "sp-search-terms",
        "start": "2026-04-01",
        "end": "2026-04-30",
        "request_hash": "abc12345",
        "status": "COMPLETED",
    }
    with patch("_reports.requests.get", return_value=dl_resp):
        path = r.download_report(profile, state, "https://signed.example/x.gz")
    text = path.read_text()
    # 'date' column is in the header and populated from startDate
    header = text.splitlines()[0].split(",")
    assert "date" in header
    first_row = text.splitlines()[1].split(",")
    date_col = header.index("date")
    assert first_row[date_col] == "2026-04-01"


# ---- 401 refresh + retry ----------------------------------------------

def test_401_triggers_token_refresh_and_retry():
    profile = _seed_profile()
    seq = [
        FakeResponse(401, {"message": "expired"}),
        FakeResponse(202, {"reportId": "rpt-after-refresh"}),
    ]
    lwa_resp = FakeResponse(200, {"access_token": "Atza|fresh", "expires_in": 3600})
    with patch("_common.requests.request", side_effect=seq) as req_mock, \
         patch("_common.requests.post", return_value=lwa_resp) as lwa_mock:
        state = r.submit_report(profile, "sp-campaigns", "2026-04-01", "2026-04-30")
    assert state["reportId"] == "rpt-after-refresh"
    assert req_mock.call_count == 2
    assert lwa_mock.call_count == 1  # exactly one forced refresh


# ---- 429 retry with Retry-After ---------------------------------------

def test_429_respects_retry_after_with_backoff():
    profile = _seed_profile()
    seq = [
        FakeResponse(429, {"message": "slow down"}, headers={"Retry-After": "1"}),
        FakeResponse(202, {"reportId": "rpt-after-429"}),
    ]
    sleeps: list[float] = []
    with patch("_common.requests.request", side_effect=seq), \
         patch("_common.time.sleep", side_effect=lambda s: sleeps.append(s)):
        state = r.submit_report(profile, "sp-campaigns", "2026-04-01", "2026-04-30")
    assert state["reportId"] == "rpt-after-429"
    # We slept at least once between attempts; Retry-After=1 floor is 2 (base delay), so >=2
    assert sleeps and sleeps[0] >= 2


# ---- poll: FAILED / COMPLETED ----------------------------------------

def test_poll_completed_returns_download_url():
    profile = _seed_profile()
    submit_resp = FakeResponse(202, {"reportId": "rpt-1"})
    poll_resp = FakeResponse(200, {"status": "COMPLETED", "url": "https://signed.example/report.gz"})
    with patch("_common.requests.request", side_effect=[submit_resp, poll_resp]):
        state = r.submit_report(profile, "sp-campaigns", "2026-04-01", "2026-04-30")
        res = r.poll_until_terminal(
            profile, state, interval_s=0, timeout_s=10,
            sleep=lambda s: None, now=_clock([0, 1, 2]),
            progress=lambda m: None,
        )
    assert res.completed and not res.failed
    assert res.download_url == "https://signed.example/report.gz"


def test_poll_failed_returns_amazon_reason():
    profile = _seed_profile()
    submit_resp = FakeResponse(202, {"reportId": "rpt-1"})
    poll_resp = FakeResponse(200, {"status": "FAILED", "failureReason": "INVALID_DATE_RANGE"})
    with patch("_common.requests.request", side_effect=[submit_resp, poll_resp]):
        state = r.submit_report(profile, "sp-campaigns", "2026-04-01", "2026-04-30")
        res = r.poll_until_terminal(
            profile, state, interval_s=0, timeout_s=10,
            sleep=lambda s: None, now=_clock([0, 1]),
            progress=lambda m: None,
        )
    assert res.failed
    assert "INVALID_DATE_RANGE" in (res.error or "")


def test_poll_times_out_when_processing_too_long():
    profile = _seed_profile()
    submit_resp = FakeResponse(202, {"reportId": "rpt-1"})
    processing_resps = [FakeResponse(200, {"status": "PROCESSING"})] * 5
    with patch("_common.requests.request", side_effect=[submit_resp] + processing_resps):
        state = r.submit_report(profile, "sp-campaigns", "2026-04-01", "2026-04-30")
        res = r.poll_until_terminal(
            profile, state, interval_s=0.001, timeout_s=0.01,
            sleep=lambda s: None,
            now=_clock([0, 0.005, 0.02, 0.03, 0.04]),
            progress=lambda m: None,
        )
    assert res.timed_out and not res.completed and not res.failed


# ---- download: empty + signed-URL 403 --------------------------------

def test_download_empty_report_writes_header_only():
    profile = _seed_profile()
    submit_resp = FakeResponse(202, {"reportId": "rpt-1"})
    poll_resp = FakeResponse(200, {"status": "COMPLETED", "url": "https://signed.example/empty.gz"})
    dl_resp = MagicMock(status_code=200, content=_gzip_json([]))
    with patch("_common.requests.request", side_effect=[submit_resp, poll_resp]), \
         patch("_reports.requests.get", return_value=dl_resp):
        state = r.submit_report(profile, "sp-campaigns", "2026-04-01", "2026-04-30")
        res = r.poll_until_terminal(
            profile, state, interval_s=0, timeout_s=10,
            sleep=lambda s: None, now=_clock([0, 1]),
            progress=lambda m: None,
        )
        path = r.download_report(profile, res.state, res.download_url)
    text = path.read_text()
    lines = [ln for ln in text.splitlines() if ln]
    assert len(lines) == 1  # header only
    assert "campaignId" in lines[0]


def test_download_403_then_refreshed_url_succeeds():
    profile = _seed_profile()
    rows = [{"campaignId": "111111111111111", "campaignName": "x"}]
    expired = MagicMock(status_code=403, text="expired")
    ok = MagicMock(status_code=200, content=_gzip_json(rows))
    refreshed_meta = FakeResponse(200, {"status": "COMPLETED", "url": "https://signed.example/fresh.gz"})
    state = {
        "reportId": "rpt-1",
        "slug": "sp-campaigns",
        "start": "2026-04-01",
        "end": "2026-04-30",
        "request_hash": "abc12345",
        "status": "COMPLETED",
    }
    with patch("_reports.requests.get", side_effect=[expired, ok]), \
         patch("_common.requests.request", return_value=refreshed_meta):
        path = r.download_report(profile, state, "https://signed.example/expired.gz")
    # CSV was written; row appears
    assert "111111111111111" in path.read_text()


def test_download_writes_ids_as_strings_not_scientific():
    profile = _seed_profile()
    rows = [{"campaignId": "999999999999999", "campaignName": "z"}]
    ok = MagicMock(status_code=200, content=_gzip_json(rows))
    state = {
        "reportId": "rpt-1",
        "slug": "sp-campaigns",
        "start": "2026-04-01",
        "end": "2026-04-30",
        "request_hash": "abc12345",
        "status": "COMPLETED",
    }
    with patch("_reports.requests.get", return_value=ok):
        path = r.download_report(profile, state, "https://signed.example/x.gz")
    text = path.read_text()
    assert "999999999999999" in text
    # no scientific notation
    import re
    assert not re.search(r"9\.999\d+e\+", text, re.IGNORECASE)


# ---- run_report orchestrator: resumability + stale guard --------------

def test_run_resumes_from_saved_state(tmp_path):
    profile = _seed_profile()
    # Pre-seed a pending state file
    h = r.hash_for_request(profile, "sp-campaigns", "2026-04-01", "2026-04-30")
    state = {
        "reportId": "rpt-resume",
        "slug": "sp-campaigns",
        "start": "2026-04-01",
        "end": "2026-04-30",
        "request_hash": h,
        "specs_version": c.REPORT_SPECS_VERSION,
        "api_version": c.ADS_API_VERSION,
        "created_at": _recent_iso(),
        "status": "PROCESSING",
    }
    c.atomic_write_json(r.pending_state_path(profile, h), state)

    poll_resp = FakeResponse(200, {"status": "COMPLETED", "url": "https://signed.example/x.gz"})
    dl_resp = MagicMock(status_code=200, content=_gzip_json([]))

    with patch("_common.requests.request", return_value=poll_resp) as req_mock, \
         patch("_reports.requests.get", return_value=dl_resp):
        res = r.run_report(
            profile, "sp-campaigns", "2026-04-01", "2026-04-30",
            poll_interval_s=0, poll_timeout_s=10,
            sleep=lambda s: None, now=_clock([0, 1, 2]),
            progress=lambda m: None,
        )
    assert res.exit_code == 0
    # No submit call — only the poll
    submit_calls = [call for call in req_mock.call_args_list if call.args[0].upper() == "POST"]
    assert not submit_calls, "expected resume; saw a fresh submit"


def test_run_resubmits_when_state_is_stale(monkeypatch):
    profile = _seed_profile()
    h = r.hash_for_request(profile, "sp-campaigns", "2026-04-01", "2026-04-30")
    stale_state = {
        "reportId": "rpt-old",
        "slug": "sp-campaigns",
        "start": "2026-04-01",
        "end": "2026-04-30",
        "request_hash": h,
        "specs_version": c.REPORT_SPECS_VERSION,
        "api_version": c.ADS_API_VERSION,
        "created_at": "2024-01-01T00:00:00+00:00",  # ancient
        "status": "PROCESSING",
    }
    c.atomic_write_json(r.pending_state_path(profile, h), stale_state)

    submit_resp = FakeResponse(202, {"reportId": "rpt-fresh"})
    poll_resp = FakeResponse(200, {"status": "COMPLETED", "url": "https://signed.example/x.gz"})
    dl_resp = MagicMock(status_code=200, content=_gzip_json([]))
    with patch("_common.requests.request", side_effect=[submit_resp, poll_resp]), \
         patch("_reports.requests.get", return_value=dl_resp):
        res = r.run_report(
            profile, "sp-campaigns", "2026-04-01", "2026-04-30",
            poll_interval_s=0, poll_timeout_s=10,
            sleep=lambda s: None, now=_clock([0, 1, 2]),
            progress=lambda m: None,
        )
    assert res.exit_code == 0


def test_run_resubmits_when_specs_version_changed(monkeypatch):
    profile = _seed_profile()
    h = r.hash_for_request(profile, "sp-campaigns", "2026-04-01", "2026-04-30")
    state = {
        "reportId": "rpt-old",
        "slug": "sp-campaigns",
        "start": "2026-04-01",
        "end": "2026-04-30",
        "request_hash": h,
        "specs_version": c.REPORT_SPECS_VERSION - 1,  # outdated
        "api_version": c.ADS_API_VERSION,
        "created_at": _recent_iso(),
        "status": "PROCESSING",
    }
    c.atomic_write_json(r.pending_state_path(profile, h), state)

    submit_resp = FakeResponse(202, {"reportId": "rpt-new"})
    poll_resp = FakeResponse(200, {"status": "COMPLETED", "url": "https://signed.example/x.gz"})
    dl_resp = MagicMock(status_code=200, content=_gzip_json([]))
    with patch("_common.requests.request", side_effect=[submit_resp, poll_resp]), \
         patch("_reports.requests.get", return_value=dl_resp):
        res = r.run_report(
            profile, "sp-campaigns", "2026-04-01", "2026-04-30",
            poll_interval_s=0, poll_timeout_s=10,
            sleep=lambda s: None, now=_clock([0, 1, 2]),
            progress=lambda m: None,
        )
    assert res.exit_code == 0


# ---- helpers ----------------------------------------------------------

def _clock(values):
    """Returns a callable returning successive monotonic values."""
    it = iter(values)
    last = [0.0]

    def _now():
        try:
            v = next(it)
            last[0] = v
        except StopIteration:
            pass
        return last[0]

    return _now


def _recent_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()

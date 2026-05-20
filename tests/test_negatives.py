"""Negatives flow: filtering, dedupe, conflict flag, proposal hash, idempotency,
partial failure."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import _common as c
import _negatives as n
from conftest import seed_profile


def _seed_profile(slug: str = "brand-a") -> c.Profile:
    seed_profile(slug)
    return c.load_profile(slug, source="--brand")


def _candidate(**kw):
    base = {
        "campaignId": "111",
        "campaignName": "C1",
        "adGroupId": "222",
        "adGroupName": "AG1",
        "searchTerm": "blue sneakers",
        "spend": 15.0,
        "sales": 0.0,
        "clicks": 8,
        "acos": None,
    }
    base.update(kw)
    return base


# ---- normalize_term ----------------------------------------------------

def test_normalize_lowercases_and_collapses_whitespace():
    assert n.normalize_term("  Blue   Sneakers ") == "blue sneakers"
    assert n.normalize_term("RED") == "red"
    assert n.normalize_term("") == ""


def test_dedupe_key_is_match_type_aware():
    a = n.dedupe_key("1", "2", "Blue", "negativeExact")
    b = n.dedupe_key("1", "2", "blue", "negativeexact")
    assert a == b
    c2 = n.dedupe_key("1", "2", "blue", "negativePhrase")
    assert c2 != a


# ---- filter_search_term_rows ------------------------------------------

def test_filter_aggregates_across_days():
    rows = [
        {"date": "2026-04-01", "campaignId": "1", "adGroupId": "2",
         "campaignName": "C", "adGroupName": "AG", "searchTerm": "x",
         "cost": "5", "sales7d": "0", "clicks": "3"},
        {"date": "2026-04-02", "campaignId": "1", "adGroupId": "2",
         "campaignName": "C", "adGroupName": "AG", "searchTerm": "x",
         "cost": "6", "sales7d": "0", "clicks": "4"},
    ]
    out = n.filter_search_term_rows(rows, n.Thresholds(min_spend=10, max_sales=0,
                                                       min_clicks=5, exclude_last_days=0),
                                     today="2026-05-01")
    assert len(out) == 1
    assert out[0]["spend"] == 11
    assert out[0]["clicks"] == 7


def test_filter_drops_recent_days_when_exclude_set():
    rows = [
        {"date": "2026-05-15", "campaignId": "1", "adGroupId": "2",
         "searchTerm": "x", "campaignName": "C", "adGroupName": "AG",
         "cost": "100", "sales7d": "0", "clicks": "100"},
    ]
    # exclude_last_days=3 means anything within 3 days of today is dropped
    out = n.filter_search_term_rows(rows, n.Thresholds(exclude_last_days=3),
                                     today="2026-05-16")
    assert out == []


def test_filter_drops_high_sales():
    rows = [
        {"date": "2026-04-01", "campaignId": "1", "adGroupId": "2",
         "searchTerm": "x", "campaignName": "C", "adGroupName": "AG",
         "cost": "50", "sales7d": "200", "clicks": "20"},
    ]
    out = n.filter_search_term_rows(rows, n.Thresholds(max_sales=0, exclude_last_days=0),
                                     today="2026-05-01")
    assert out == []


def test_filter_drops_low_clicks():
    rows = [
        {"date": "2026-04-01", "campaignId": "1", "adGroupId": "2",
         "searchTerm": "x", "campaignName": "C", "adGroupName": "AG",
         "cost": "50", "sales7d": "0", "clicks": "1"},
    ]
    out = n.filter_search_term_rows(rows, n.Thresholds(min_clicks=5, exclude_last_days=0),
                                     today="2026-05-01")
    assert out == []


# ---- build_proposal: dedupe + conflict --------------------------------

def test_build_proposal_dedupes_against_existing_negatives():
    profile = _seed_profile()
    cands = [_candidate()]
    existing = {n.dedupe_key("111", "222", "blue sneakers", "negativeExact")}
    prop = n.build_proposal(profile, cands, thresholds=n.Thresholds(),
                             existing_negatives=existing,
                             enabled_positives=set())
    assert prop.rows == []


def test_build_proposal_flags_conflict_with_positive():
    profile = _seed_profile()
    cands = [_candidate(searchTerm="Blue Sneakers")]
    positives = {("111", "222", "blue sneakers")}
    prop = n.build_proposal(profile, cands, thresholds=n.Thresholds(),
                             existing_negatives=set(),
                             enabled_positives=positives)
    assert len(prop.rows) == 1
    assert prop.rows[0]["conflict_flag"] == "1"


def test_build_proposal_no_conflict_when_clean():
    profile = _seed_profile()
    cands = [_candidate()]
    prop = n.build_proposal(profile, cands, thresholds=n.Thresholds(),
                             existing_negatives=set(),
                             enabled_positives=set())
    assert prop.rows[0]["conflict_flag"] == "0"


# ---- write_proposal + load_and_verify_proposal -------------------------

def test_write_then_verify_round_trip():
    profile = _seed_profile()
    cands = [_candidate()]
    prop = n.build_proposal(profile, cands, thresholds=n.Thresholds(),
                             existing_negatives=set(), enabled_positives=set())
    prop = n.write_proposal(prop, profile)
    assert prop.csv_path and prop.csv_path.exists()
    assert prop.meta_path and prop.meta_path.exists()
    loaded = n.load_and_verify_proposal(prop.csv_path, profile)
    assert loaded.csv_hash == prop.csv_hash
    assert len(loaded.rows) == 1


def test_modified_csv_fails_hash_check():
    profile = _seed_profile()
    cands = [_candidate()]
    prop = n.write_proposal(
        n.build_proposal(profile, cands, thresholds=n.Thresholds(),
                          existing_negatives=set(), enabled_positives=set()),
        profile,
    )
    # Tamper with the CSV
    prop.csv_path.write_text(prop.csv_path.read_text() + "\nrowbomb,,,,,,,,,,,\n")
    with pytest.raises(n.ProposalDrift):
        n.load_and_verify_proposal(prop.csv_path, profile)


def test_proposal_from_different_profile_refuses():
    profile_a = _seed_profile("brand-a")
    cands = [_candidate()]
    prop = n.write_proposal(
        n.build_proposal(profile_a, cands, thresholds=n.Thresholds(),
                          existing_negatives=set(), enabled_positives=set()),
        profile_a,
    )
    seed_profile("brand-b", profile_id="222222222")
    profile_b = c.load_profile("brand-b", source="--brand")
    with pytest.raises(n.ProposalDrift):
        n.load_and_verify_proposal(prop.csv_path, profile_b)


def test_proposal_hash_flag_must_match():
    profile = _seed_profile()
    cands = [_candidate()]
    prop = n.write_proposal(
        n.build_proposal(profile, cands, thresholds=n.Thresholds(),
                          existing_negatives=set(), enabled_positives=set()),
        profile,
    )
    with pytest.raises(n.ProposalDrift):
        n.load_and_verify_proposal(prop.csv_path, profile, expected_hash="0" * 64)


# ---- execute_proposal --------------------------------------------------

class _Resp:
    def __init__(self, status, body=None, text=None):
        self.status_code = status
        self._body = body
        self._text = text

    def json(self):
        return self._body if self._body is not None else []

    @property
    def text(self):
        return self._text if self._text is not None else json.dumps(self._body or [])


def _make_proposal(profile, candidates):
    return n.write_proposal(
        n.build_proposal(profile, candidates, thresholds=n.Thresholds(),
                          existing_negatives=set(), enabled_positives=set()),
        profile,
    )


def test_execute_dry_run_makes_no_mutation_calls():
    """Dry-run still refreshes the existing-negatives cache (read), but
    must never POST to the mutation endpoint."""
    profile = _seed_profile()
    prop = _make_proposal(profile, [_candidate()])
    # The existing-negatives list call returns an empty list
    existing_list = _Resp(200, [])
    with patch("_common.requests.request", return_value=existing_list) as m:
        res = n.execute_proposal(profile, prop, dry_run=True)
    methods = [call.args[0].upper() for call in m.call_args_list]
    assert "POST" not in methods, "dry-run made a POST"
    assert res.proposed == 1
    assert res.created == 0


def test_execute_with_partial_failure_writes_per_row_status():
    profile = _seed_profile()
    cands = [_candidate(campaignId="1", adGroupId="1", searchTerm="a"),
             _candidate(campaignId="2", adGroupId="2", searchTerm="b")]
    prop = _make_proposal(profile, cands)
    api_body = [
        {"code": "SUCCESS", "keywordId": "kw-1"},
        {"code": "DUPLICATE_VALUE", "details": "exists"},
    ]
    existing_list = _Resp(200, [])  # no existing negatives
    post_resp = _Resp(207, api_body)
    with patch("_common.requests.request", side_effect=[existing_list, post_resp]):
        res = n.execute_proposal(profile, prop, dry_run=False, chunk_size=5)
    assert res.created == 1
    assert res.failed == 1
    text = res.result_csv.read_text()
    assert "kw-1" in text
    assert "DUPLICATE_VALUE" in text


def test_execute_idempotent_when_already_created():
    profile = _seed_profile()
    cands = [_candidate(campaignId="111", adGroupId="222", searchTerm="blue sneakers")]
    prop = _make_proposal(profile, cands)
    # API will report this negative already exists when we list
    existing_listing = _Resp(200, [
        {"campaignId": "111", "adGroupId": "222",
         "keywordText": "blue sneakers", "matchType": "negativeExact"},
    ])
    with patch("_common.requests.request", side_effect=[existing_listing]):
        res = n.execute_proposal(profile, prop, dry_run=False, chunk_size=5)
    assert res.skipped_existing == 1
    assert res.created == 0
    assert res.attempted == 0


def test_execute_refuses_rows_missing_ids():
    profile = _seed_profile()
    prop = _make_proposal(profile, [_candidate()])
    # Tamper in memory: clear campaignId
    prop.rows[0]["campaignId"] = ""
    existing_list = _Resp(200, [])
    with patch("_common.requests.request", return_value=existing_list) as m:
        res = n.execute_proposal(profile, prop, dry_run=False)
    # Existing-negatives list is read once; no POST to mutation endpoint
    methods_paths = [(call.args[0].upper(), call.args[1]) for call in m.call_args_list]
    assert not any(method == "POST" and "/negativeKeywords" in path
                   for method, path in methods_paths), "made a POST despite missing IDs"
    assert res.failed == 1
    assert res.created == 0


def test_execute_ledger_appended():
    profile = _seed_profile()
    cands = [_candidate(campaignId="111", adGroupId="222", searchTerm="x")]
    prop = _make_proposal(profile, cands)
    api_body = [{"code": "SUCCESS", "keywordId": "kw-x"}]
    with patch("_common.requests.request",
               side_effect=[_Resp(200, []), _Resp(207, api_body)]):
        n.execute_proposal(profile, prop, dry_run=False, chunk_size=5)
    ledger = c.ledger_path(profile)
    assert ledger.exists()
    lines = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(lines) == 1
    entry = lines[0]
    assert entry["result"] == "CREATED"
    assert entry["created_negative_id"] == "kw-x"
    assert entry["batch_correlation_id"]
    assert entry["proposal_hash"] == prop.csv_hash

"""Negatives flow library: analyze search-term CSVs, fetch existing
negatives/positives, build immutable proposals, and execute mutations with
idempotency + ledger.
"""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import _common as c


PROPOSAL_COLUMNS = [
    "campaignId", "campaignName", "adGroupId", "adGroupName",
    "searchTerm", "normalizedText", "spend", "sales", "clicks", "acos",
    "suggestedMatchType", "conflict_flag",
]


# ---- Normalization -----------------------------------------------------
def normalize_term(text: str) -> str:
    """Normalize a search term for duplicate detection.

    Lowercases, collapses internal whitespace, strips ends. Original text is
    preserved in the proposal — we only normalize for dedupe keys.
    """
    if not isinstance(text, str):
        return ""
    return " ".join(text.lower().split())


def dedupe_key(campaign_id: str, ad_group_id: str, text: str, match_type: str) -> tuple:
    return (str(campaign_id), str(ad_group_id), normalize_term(text), match_type.lower())


# ---- Thresholds + filtering -------------------------------------------
@dataclass
class Thresholds:
    min_spend: float = 10.0
    max_sales: float = 0.0
    min_clicks: int = 5
    lookback_days: int = 30
    exclude_last_days: int = 3
    match_type: str = "negativeExact"

    def as_meta(self) -> dict[str, Any]:
        return {
            "min_spend": self.min_spend,
            "max_sales": self.max_sales,
            "min_clicks": self.min_clicks,
            "lookback_days": self.lookback_days,
            "exclude_last_days": self.exclude_last_days,
            "match_type": self.match_type,
        }


def _to_float(v: Any) -> float:
    try:
        return float(v) if v not in ("", None) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _to_int(v: Any) -> int:
    try:
        return int(float(v)) if v not in ("", None) else 0
    except (TypeError, ValueError):
        return 0


def filter_search_term_rows(
    rows: Iterable[dict[str, Any]],
    thresholds: Thresholds,
    *,
    today: str | None = None,
) -> list[dict[str, Any]]:
    """Apply spend/sales/clicks/date filters to raw search-term rows.

    Aggregates per (campaignId, adGroupId, searchTerm) so a term that appears
    on multiple days is summed.
    """
    if today is None:
        today_d = datetime.now(timezone.utc).date()
    else:
        today_d = datetime.strptime(today, "%Y-%m-%d").date()
    cutoff = today_d  # rows with date > cutoff - exclude_last_days are dropped

    agg: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        date_str = row.get("date", "")
        if date_str and thresholds.exclude_last_days > 0:
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
                if (cutoff - d).days < thresholds.exclude_last_days:
                    continue
            except ValueError:
                pass
        key = (
            str(row.get("campaignId", "")),
            str(row.get("adGroupId", "")),
            row.get("searchTerm", ""),
        )
        if not key[0] or not key[1] or not key[2]:
            continue
        entry = agg.setdefault(key, {
            "campaignId": key[0],
            "campaignName": row.get("campaignName", ""),
            "adGroupId": key[1],
            "adGroupName": row.get("adGroupName", ""),
            "searchTerm": key[2],
            "spend": 0.0,
            "sales": 0.0,
            "clicks": 0,
        })
        entry["spend"] += _to_float(row.get("cost"))
        entry["sales"] += _to_float(row.get("sales7d"))
        entry["clicks"] += _to_int(row.get("clicks"))

    out = []
    for entry in agg.values():
        if entry["spend"] < thresholds.min_spend:
            continue
        if entry["sales"] > thresholds.max_sales:
            continue
        if entry["clicks"] < thresholds.min_clicks:
            continue
        entry["acos"] = (entry["spend"] / entry["sales"]) if entry["sales"] > 0 else None
        entry["normalizedText"] = normalize_term(entry["searchTerm"])
        out.append(entry)
    out.sort(key=lambda e: e["spend"], reverse=True)
    return out


# ---- Existing-negative + positive fetch --------------------------------
def _list_paged(profile: c.Profile, path: str, *, page_size: int = 5000) -> list[dict[str, Any]]:
    """GET a v2 SP list endpoint with startIndex pagination."""
    items: list[dict[str, Any]] = []
    start = 0
    while True:
        sep = "&" if "?" in path else "?"
        resp = c.ads_request(
            profile, "GET",
            f"{path}{sep}startIndex={start}&count={page_size}",
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"list {path} failed ({resp.status_code}): {resp.text[:300]}"
            )
        batch = resp.json() or []
        if not isinstance(batch, list):
            break
        items.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return items


def fetch_existing_negatives(profile: c.Profile) -> set[tuple]:
    """Return a set of dedupe_key() tuples for every enabled negative keyword."""
    items = _list_paged(profile, "/v2/sp/negativeKeywords?stateFilter=enabled")
    return {
        dedupe_key(
            it.get("campaignId", ""),
            it.get("adGroupId", ""),
            it.get("keywordText", ""),
            it.get("matchType", ""),
        )
        for it in items
    }


def fetch_enabled_positive_terms(profile: c.Profile) -> set[tuple]:
    """Return a set of (campaignId, adGroupId, normalized_text) for enabled positive keywords."""
    items = _list_paged(profile, "/v2/sp/keywords?stateFilter=enabled")
    return {
        (
            str(it.get("campaignId", "")),
            str(it.get("adGroupId", "")),
            normalize_term(it.get("keywordText", "")),
        )
        for it in items
    }


# ---- Cache layer -------------------------------------------------------
def _cache_path(profile: c.Profile, name: str) -> Path:
    return c.profile_dir(profile.slug) / "state" / f"{name}.json"


def _read_cache(path: Path, ttl_s: int) -> Any:
    obj = c.read_json_or(path, None)
    if not obj:
        return None
    if c.utc_now_epoch() - obj.get("epoch", 0) > ttl_s:
        return None
    return obj.get("value")


def _write_cache(path: Path, value: Any) -> None:
    c.atomic_write_json(path, {"epoch": c.utc_now_epoch(), "value": value})


def get_existing_negatives_cached(profile: c.Profile, *, ttl_s: int = 1800) -> set[tuple]:
    path = _cache_path(profile, "existing_negatives")
    cached = _read_cache(path, ttl_s)
    if cached is not None:
        return {tuple(t) for t in cached}
    items = fetch_existing_negatives(profile)
    _write_cache(path, [list(t) for t in items])
    return items


def get_enabled_positives_cached(profile: c.Profile, *, ttl_s: int = 1800) -> set[tuple]:
    path = _cache_path(profile, "enabled_positives")
    cached = _read_cache(path, ttl_s)
    if cached is not None:
        return {tuple(t) for t in cached}
    items = fetch_enabled_positive_terms(profile)
    _write_cache(path, [list(t) for t in items])
    return items


# ---- Proposal build + persist -----------------------------------------
@dataclass
class Proposal:
    rows: list[dict[str, Any]]
    csv_path: Path | None = None
    meta_path: Path | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    csv_hash: str | None = None


def build_proposal(
    profile: c.Profile,
    candidates: list[dict[str, Any]],
    *,
    thresholds: Thresholds,
    source_report_hash: str = "",
    existing_negatives: set[tuple] | None = None,
    enabled_positives: set[tuple] | None = None,
) -> Proposal:
    """Filter candidates against existing negatives, flag conflicts with positives,
    and return a Proposal ready to write."""
    existing_negatives = existing_negatives if existing_negatives is not None else get_existing_negatives_cached(profile)
    enabled_positives = enabled_positives if enabled_positives is not None else get_enabled_positives_cached(profile)

    rows: list[dict[str, Any]] = []
    for cand in candidates:
        key = dedupe_key(
            cand["campaignId"], cand["adGroupId"],
            cand["searchTerm"], thresholds.match_type,
        )
        if key in existing_negatives:
            continue
        conflict = (
            str(cand["campaignId"]),
            str(cand["adGroupId"]),
            normalize_term(cand["searchTerm"]),
        ) in enabled_positives
        rows.append({
            "campaignId": str(cand["campaignId"]),
            "campaignName": cand.get("campaignName", ""),
            "adGroupId": str(cand["adGroupId"]),
            "adGroupName": cand.get("adGroupName", ""),
            "searchTerm": cand["searchTerm"],
            "normalizedText": normalize_term(cand["searchTerm"]),
            "spend": round(float(cand.get("spend", 0)), 4),
            "sales": round(float(cand.get("sales", 0)), 4),
            "clicks": int(cand.get("clicks", 0)),
            "acos": (round(float(cand["acos"]), 4) if cand.get("acos") is not None else ""),
            "suggestedMatchType": thresholds.match_type,
            "conflict_flag": "1" if conflict else "0",
        })

    meta = {
        "profile_slug": profile.slug,
        "profile_id": profile.profile_id,
        "account_name": profile.account_name,
        "mode": profile.mode,
        "region": profile.region,
        "marketplace": profile.marketplace_id,
        "thresholds": thresholds.as_meta(),
        "source_report_hash": source_report_hash,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows": len(rows),
        "total_spend": round(sum(r["spend"] for r in rows), 4),
        "campaigns_affected": len({r["campaignId"] for r in rows}),
    }
    return Proposal(rows=rows, meta=meta)


def _csv_hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_proposal(proposal: Proposal, profile: c.Profile) -> Proposal:
    """Write proposal CSV + .meta.json sidecar to the profile's proposals dir.

    Returns the same Proposal with csv_path, meta_path, csv_hash filled in.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = c.profile_dir(profile.slug) / "proposals"
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_dir / f"{stamp}__negatives__pending.csv"
    c.write_rows_csv(csv_path, PROPOSAL_COLUMNS, proposal.rows)

    csv_bytes = csv_path.read_bytes()
    csv_hash = _csv_hash_bytes(csv_bytes)

    # Rename to include hash short
    final_csv = out_dir / f"{stamp}__negatives__{csv_hash[:8]}.csv"
    csv_path.rename(final_csv)

    meta = dict(proposal.meta)
    meta["csv_hash"] = csv_hash
    meta_path = final_csv.with_suffix(final_csv.suffix + ".meta.json")
    c.atomic_write_json(meta_path, meta)

    proposal.csv_path = final_csv
    proposal.meta_path = meta_path
    proposal.csv_hash = csv_hash
    proposal.meta = meta
    return proposal


# ---- Proposal verification --------------------------------------------
class ProposalDrift(RuntimeError):
    """Raised when a proposal's hash or metadata doesn't match the active profile."""


def load_and_verify_proposal(
    csv_path: Path, profile: c.Profile, expected_hash: str | None = None,
) -> Proposal:
    """Read proposal CSV + .meta.json sidecar; verify hash + metadata against `profile`.

    Raises ProposalDrift on any mismatch.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise ProposalDrift(f"proposal not found: {csv_path}")
    meta_path = csv_path.with_suffix(csv_path.suffix + ".meta.json")
    if not meta_path.exists():
        raise ProposalDrift(f"proposal metadata missing: {meta_path}")
    meta = json.loads(meta_path.read_text())

    csv_bytes = csv_path.read_bytes()
    actual_hash = _csv_hash_bytes(csv_bytes)
    if meta.get("csv_hash") != actual_hash:
        raise ProposalDrift(
            f"proposal CSV hash mismatch (expected {meta.get('csv_hash')}, actual {actual_hash})"
        )
    if expected_hash and expected_hash != actual_hash:
        raise ProposalDrift(
            f"proposal hash does not match --proposal-hash (expected {expected_hash}, actual {actual_hash})"
        )
    # Metadata drift checks
    if meta.get("profile_slug") != profile.slug:
        raise ProposalDrift(
            f"proposal was generated for {meta.get('profile_slug')!r} but active profile is {profile.slug!r}"
        )
    if meta.get("profile_id") != profile.profile_id:
        raise ProposalDrift(
            f"proposal profile_id {meta.get('profile_id')} does not match active {profile.profile_id}"
        )
    if meta.get("mode") != profile.mode:
        raise ProposalDrift(
            f"proposal mode {meta.get('mode')} does not match active {profile.mode}"
        )

    with csv_path.open() as f:
        rows = list(csv.DictReader(f))
    return Proposal(rows=rows, csv_path=csv_path, meta_path=meta_path,
                    meta=meta, csv_hash=actual_hash)


# ---- Execution ---------------------------------------------------------
@dataclass
class ExecuteResult:
    proposed: int = 0
    skipped_existing: int = 0
    attempted: int = 0
    created: int = 0
    failed: int = 0
    result_csv: Path | None = None


def execute_proposal(
    profile: c.Profile,
    proposal: Proposal,
    *,
    chunk_size: int = 100,
    dry_run: bool = True,
) -> ExecuteResult:
    """Execute (or dry-run) the proposal. Idempotent: refreshes existing-negatives
    immediately before each chunk and skips already-created rows. Writes a per-row
    result CSV and appends one JSONL line per row to the ledger.
    """
    lock_path = c.profile_dir(profile.slug) / "state" / "mutation.lock"
    try:
        with c.file_lock(lock_path):
            return _execute_locked(profile, proposal, chunk_size=chunk_size, dry_run=dry_run)
    except BlockingIOError:
        info = c.lock_holder_info(lock_path)
        raise RuntimeError(
            f"another mutation in progress on this profile "
            f"(pid={info.get('pid')}, started={info.get('started')})"
        )


def _execute_locked(
    profile: c.Profile,
    proposal: Proposal,
    *,
    chunk_size: int,
    dry_run: bool,
) -> ExecuteResult:
    result = ExecuteResult(proposed=len(proposal.rows))
    correlation = hashlib.sha256(
        f"{proposal.csv_hash}{datetime.now(timezone.utc).isoformat()}".encode()
    ).hexdigest()[:16]

    # Force a cache refresh — we want the freshest picture of what exists
    _cache_path(profile, "existing_negatives").unlink(missing_ok=True)

    result_rows: list[dict[str, Any]] = []
    rows_iter = list(proposal.rows)
    i = 0
    while i < len(rows_iter):
        chunk = rows_iter[i:i + chunk_size]
        i += chunk_size
        existing = get_existing_negatives_cached(profile, ttl_s=60)

        payload = []
        chunk_rows = []
        for row in chunk:
            # Refuse rows missing required IDs
            if not row.get("campaignId") or not row.get("adGroupId"):
                result_rows.append({
                    **row,
                    "status": "DROPPED",
                    "response_code": "",
                    "response_body": "missing campaignId or adGroupId",
                    "created_negative_id": "",
                    "batch_correlation_id": correlation,
                })
                result.failed += 1
                continue
            key = dedupe_key(
                row["campaignId"], row["adGroupId"],
                row["searchTerm"], row.get("suggestedMatchType", ""),
            )
            if key in existing:
                result_rows.append({
                    **row,
                    "status": "SKIPPED_EXISTS",
                    "response_code": "",
                    "response_body": "",
                    "created_negative_id": "",
                    "batch_correlation_id": correlation,
                })
                result.skipped_existing += 1
                continue
            payload.append({
                "campaignId": str(row["campaignId"]),
                "adGroupId": str(row["adGroupId"]),
                "keywordText": row["searchTerm"],
                "matchType": row.get("suggestedMatchType", "negativeExact"),
                "state": "enabled",
            })
            chunk_rows.append(row)

        if not payload:
            continue
        if dry_run:
            for row in chunk_rows:
                result_rows.append({
                    **row,
                    "status": "DRY_RUN",
                    "response_code": "",
                    "response_body": json.dumps({k: v for k, v in {
                        "campaignId": str(row["campaignId"]),
                        "adGroupId": str(row["adGroupId"]),
                        "keywordText": row["searchTerm"],
                        "matchType": row.get("suggestedMatchType"),
                        "state": "enabled",
                    }.items()}),
                    "created_negative_id": "",
                    "batch_correlation_id": correlation,
                })
            continue

        resp = c.ads_request(
            profile, "POST", "/v2/sp/negativeKeywords",
            json_body=payload,
        )
        if resp.status_code == 207 or resp.status_code == 200:
            try:
                body = resp.json()
            except Exception:
                body = []
            if not isinstance(body, list):
                body = []
            # Pair by index — Amazon returns same-order responses for v2 SP
            for row, item in zip(chunk_rows, body):
                code = str(item.get("code", "")).upper()
                ok = code in ("SUCCESS",) or item.get("keywordId")
                if ok:
                    ledger_status = "CREATED"
                    result.created += 1
                else:
                    ledger_status = "FAILED"
                    result.failed += 1
                result_rows.append({
                    **row,
                    "status": ledger_status,
                    "response_code": code or str(resp.status_code),
                    "response_body": json.dumps(item, separators=(",", ":")),
                    "created_negative_id": str(item.get("keywordId", "")),
                    "batch_correlation_id": correlation,
                })
                _ledger_row(profile, row, item, correlation, proposal, result_rows[-1])
            result.attempted += len(chunk_rows)
        else:
            # Whole-batch failure
            for row in chunk_rows:
                entry = {
                    **row,
                    "status": "FAILED",
                    "response_code": str(resp.status_code),
                    "response_body": resp.text[:500],
                    "created_negative_id": "",
                    "batch_correlation_id": correlation,
                }
                result_rows.append(entry)
                _ledger_row(profile, row, None, correlation, proposal, entry, http_status=resp.status_code, http_text=resp.text)
                result.failed += 1
            result.attempted += len(chunk_rows)

    # Write per-row result CSV next to the proposal
    if proposal.csv_path:
        result_path = proposal.csv_path.with_name(
            proposal.csv_path.stem + "__result.csv"
        )
        result.result_csv = result_path
        result_cols = PROPOSAL_COLUMNS + [
            "status", "response_code", "response_body",
            "created_negative_id", "batch_correlation_id",
        ]
        c.write_rows_csv(result_path, result_cols, result_rows)
    return result


def _ledger_row(
    profile: c.Profile,
    row: dict[str, Any],
    api_item: dict[str, Any] | None,
    correlation: str,
    proposal: Proposal,
    result_row: dict[str, Any],
    *, http_status: int | None = None, http_text: str | None = None,
) -> None:
    c.ledger_append(profile, {
        "command": "negative_keywords_add",
        "profile_slug": profile.slug,
        "profile_id": profile.profile_id,
        "account_name": profile.account_name,
        "mode": profile.mode,
        "proposal_path": str(proposal.csv_path or ""),
        "proposal_hash": proposal.csv_hash or "",
        "source_report_hash": (proposal.meta or {}).get("source_report_hash", ""),
        "row_id": f"{row.get('campaignId')}:{row.get('adGroupId')}:{normalize_term(row.get('searchTerm', ''))}",
        "payload": {
            "campaignId": str(row.get("campaignId", "")),
            "adGroupId": str(row.get("adGroupId", "")),
            "keywordText": row.get("searchTerm", ""),
            "matchType": row.get("suggestedMatchType", ""),
        },
        "response_status": http_status if api_item is None else (api_item.get("code") or 200),
        "response_body": (http_text[:500] if http_text else None) if api_item is None else api_item,
        "created_negative_id": result_row.get("created_negative_id", ""),
        "batch_correlation_id": correlation,
        "result": result_row.get("status", ""),
    })

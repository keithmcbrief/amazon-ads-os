"""Core helpers shared by every amazon-ads-os script.

Auth, region map, HTTP retries, profile loading + resolution, REPORT_SPECS,
slug validation, fcntl-based locks, CSV-escape, audit ledger writer, atomic
writes. Designed for stdlib + `requests` only.
"""
from __future__ import annotations

import csv
import fcntl
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import requests


# ---- Versioning ---------------------------------------------------------
# Bump REPORT_SPECS_VERSION whenever any spec in REPORT_SPECS changes — this
# invalidates resumable pending state from previous versions.
REPORT_SPECS_VERSION = 1
ADS_API_VERSION = "v3"


# ---- Paths --------------------------------------------------------------
def home() -> Path:
    """Root of all amazon-ads-os user data. Overridable via env for tests."""
    return Path(os.environ.get("AMAZON_ADS_OS_HOME", str(Path.home() / ".amazon-ads-os")))


def profiles_dir() -> Path:
    return home() / "profiles"


def profile_dir(brand: str) -> Path:
    return profiles_dir() / brand


def active_profile_file() -> Path:
    return home() / "active_profile"


def logs_dir() -> Path:
    return home() / "logs"


# ---- Slug validation ----------------------------------------------------
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")


def validate_slug(slug: str) -> str:
    """Return the normalized slug or raise ValueError."""
    if not isinstance(slug, str):
        raise ValueError("brand slug must be a string")
    normalized = slug.strip().lower()
    if not SLUG_RE.match(normalized):
        raise ValueError(
            f"invalid brand slug {slug!r}; must match {SLUG_RE.pattern}"
        )
    return normalized


# ---- Regions + hosts ----------------------------------------------------
REGION_HOSTS: dict[str, str] = {
    "NA": "https://advertising-api.amazon.com",
    "EU": "https://advertising-api-eu.amazon.com",
    "FE": "https://advertising-api-fe.amazon.com",
}
SANDBOX_REGION = "SANDBOX"
SANDBOX_HOST = "https://advertising-api-test.amazon.com"
LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

VALID_REGIONS = frozenset({*REGION_HOSTS.keys(), SANDBOX_REGION})


def host_for(region: str) -> str:
    """Return the host for a region. Sandbox is its own region ("SANDBOX")."""
    region = region.upper()
    if region == SANDBOX_REGION:
        return SANDBOX_HOST
    if region not in REGION_HOSTS:
        raise ValueError(f"unknown region {region!r}; expected NA/EU/FE/SANDBOX")
    return REGION_HOSTS[region]


# Amazon marketplace IDs → (country_code, country_name, region).
# Used to show "United States (US)" in the wizard instead of cryptic IDs.
MARKETPLACES: dict[str, tuple[str, str, str]] = {
    # North America
    "ATVPDKIKX0DER":  ("US", "United States",  "NA"),
    "A2EUQ1WTGCTBG2": ("CA", "Canada",         "NA"),
    "A1AM78C64UM0Y8": ("MX", "Mexico",         "NA"),
    "A2Q3Y263D00KWC": ("BR", "Brazil",         "NA"),
    # Europe / MENA / India
    "A1F83G8C2ARO7P": ("UK", "United Kingdom", "EU"),
    "A1PA6795UKMFR9": ("DE", "Germany",        "EU"),
    "A13V1IB3VIYZZH": ("FR", "France",         "EU"),
    "APJ6JRA9NG5V4":  ("IT", "Italy",          "EU"),
    "A1RKKUPIHCS9HS": ("ES", "Spain",          "EU"),
    "A1805IZSGTT6HS": ("NL", "Netherlands",    "EU"),
    "A2NODRKZP88ZB9": ("SE", "Sweden",         "EU"),
    "A1C3SOZRARQ6R3": ("PL", "Poland",         "EU"),
    "A1ZFFQZ3HTUKT9": ("BE", "Belgium",        "EU"),
    "AMEN7PMS3EDWL":  ("IE", "Ireland",        "EU"),
    "A2VIGQ35RCS4UG": ("AE", "United Arab Emirates", "EU"),
    "A21TJRUUN4KGV":  ("IN", "India",          "EU"),
    "A17E79C6D8DWNP": ("SA", "Saudi Arabia",   "EU"),
    "A33AVAJ2PDY3EV": ("TR", "Turkey",         "EU"),
    "ARBP9OOSHTCHU":  ("EG", "Egypt",          "EU"),
    # Far East
    "A1VC38T7YXB528": ("JP", "Japan",          "FE"),
    "A39IBJ37TRP1C6": ("AU", "Australia",      "FE"),
    "A19VAU5U5O7RUS": ("SG", "Singapore",      "FE"),
}


def marketplace_label(marketplace_id: str | None, country_code: str | None = None) -> str:
    """Human-readable label like 'United States (US)' for an Amazon marketplace.
    Prefers the API-provided countryCode if present; falls back to the
    marketplace-ID mapping; falls back to the raw ID if nothing matches."""
    if marketplace_id and marketplace_id in MARKETPLACES:
        cc, name, _ = MARKETPLACES[marketplace_id]
        return f"{name} ({cc})"
    if country_code:
        return country_code.strip().upper()
    return marketplace_id or "?"


def marketplace_country_code(marketplace_id: str | None, country_code: str | None = None) -> str:
    """Return the 2-letter country code, falling back to the marketplace map."""
    if country_code:
        return country_code.strip().upper()
    if marketplace_id and marketplace_id in MARKETPLACES:
        return MARKETPLACES[marketplace_id][0]
    return ""


# ---- REPORT_SPECS -------------------------------------------------------
# Typed report definitions for v1. Columns are the standard "all metrics"
# subset for each report type. Update REPORT_SPECS_VERSION when any spec
# changes so old pending state files don't resume against a new spec.
_SP_CAMPAIGN_COLS = [
    "date", "campaignId", "campaignName", "campaignStatus", "campaignBudgetAmount",
    "campaignBudgetType", "impressions", "clicks", "cost", "purchases7d", "sales7d",
    "acosClicks7d", "roasClicks7d", "unitsSoldClicks7d", "clickThroughRate",
    "costPerClick",
]
_SP_AD_GROUP_COLS = [
    "date", "campaignId", "campaignName", "adGroupId", "adGroupName", "adStatus",
    "impressions", "clicks", "cost", "purchases7d", "sales7d", "acosClicks7d",
    "roasClicks7d", "unitsSoldClicks7d", "clickThroughRate", "costPerClick",
]
_SP_KEYWORD_COLS = [
    "date", "campaignId", "campaignName", "adGroupId", "adGroupName", "keywordId",
    "keyword", "matchType", "keywordBid", "impressions", "clicks", "cost",
    "purchases7d", "sales7d", "acosClicks7d", "roasClicks7d", "unitsSoldClicks7d",
    "clickThroughRate", "costPerClick",
]
_SP_SEARCH_TERM_COLS = [
    "date", "campaignId", "campaignName", "adGroupId", "adGroupName", "keywordId",
    "keyword", "matchType", "searchTerm", "impressions", "clicks", "cost",
    "purchases7d", "sales7d", "acosClicks7d", "roasClicks7d", "unitsSoldClicks7d",
    "clickThroughRate", "costPerClick",
]
_SP_PRODUCT_TARGETING_COLS = [
    "date", "campaignId", "campaignName", "adGroupId", "adGroupName", "targetingId",
    "targetingExpression", "targetingText", "matchType", "impressions", "clicks",
    "cost", "purchases7d", "sales7d", "acosClicks7d", "roasClicks7d",
    "unitsSoldClicks7d", "clickThroughRate", "costPerClick",
]
_SP_ADVERTISED_PRODUCT_COLS = [
    "date", "campaignId", "campaignName", "adGroupId", "adGroupName", "advertisedAsin",
    "advertisedSku", "impressions", "clicks", "cost", "purchases7d", "sales7d",
    "acosClicks7d", "roasClicks7d", "unitsSoldClicks7d", "clickThroughRate",
    "costPerClick",
]

REPORT_SPECS: dict[str, dict[str, Any]] = {
    "sp-campaigns": {
        "adProduct": "SPONSORED_PRODUCTS",
        "reportTypeId": "spCampaigns",
        "groupBy": ["campaign"],
        "columns": _SP_CAMPAIGN_COLS,
        "max_days": 31,
        "max_lookback_days": 95,
    },
    "sp-ad-groups": {
        "adProduct": "SPONSORED_PRODUCTS",
        "reportTypeId": "spCampaigns",
        "groupBy": ["adGroup"],
        "columns": _SP_AD_GROUP_COLS,
        "max_days": 31,
        "max_lookback_days": 95,
    },
    "sp-keywords": {
        "adProduct": "SPONSORED_PRODUCTS",
        "reportTypeId": "spTargeting",
        "groupBy": ["targeting"],
        "columns": _SP_KEYWORD_COLS,
        "max_days": 31,
        "max_lookback_days": 95,
        "filter": "keyword",
    },
    "sp-search-terms": {
        "adProduct": "SPONSORED_PRODUCTS",
        "reportTypeId": "spSearchTerm",
        "groupBy": ["searchTerm"],
        "columns": _SP_SEARCH_TERM_COLS,
        "max_days": 31,
        "max_lookback_days": 60,
    },
    "sp-product-targeting": {
        "adProduct": "SPONSORED_PRODUCTS",
        "reportTypeId": "spTargeting",
        "groupBy": ["targeting"],
        "columns": _SP_PRODUCT_TARGETING_COLS,
        "max_days": 31,
        "max_lookback_days": 95,
        "filter": "product",
    },
    "sp-advertised-product": {
        "adProduct": "SPONSORED_PRODUCTS",
        "reportTypeId": "spAdvertisedProduct",
        "groupBy": ["advertiser"],
        "columns": _SP_ADVERTISED_PRODUCT_COLS,
        "max_days": 31,
        "max_lookback_days": 95,
    },
}

# Columns that are IDs across all report types — must be written as strings,
# never coerced to numeric (Excel would turn long IDs into scientific notation).
ID_COLUMNS = frozenset({
    "campaignId", "adGroupId", "keywordId", "targetingId", "profileId",
    "advertisedAsin", "advertisedSku", "reportId",
})


def request_hash(
    *,
    profile_id: str,
    region: str,
    slug: str,
    start: str,
    end: str,
    columns: list[str],
    group_by: list[str],
    time_unit: str = "SUMMARY",
    fmt: str = "GZIP_JSON",
) -> str:
    """Stable hash for resumable pending state.

    Includes REPORT_SPECS_VERSION and ADS_API_VERSION so old pending files
    never resume against a changed spec.
    """
    payload = json.dumps({
        "specs_version": REPORT_SPECS_VERSION,
        "api_version": ADS_API_VERSION,
        "profile_id": str(profile_id),
        "region": region.upper(),
        "slug": slug,
        "start": start,
        "end": end,
        "columns": sorted(columns),
        "group_by": sorted(group_by),
        "time_unit": time_unit,
        "format": fmt,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---- Atomic writes + JSON helpers --------------------------------------
def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, sort_keys=True, indent=2) + "\n")


def read_json_or(path: Path, default: Any) -> Any:
    """Return parsed JSON, or `default` if missing/corrupt. Never raises."""
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


# ---- CSV escape ---------------------------------------------------------
_DANGEROUS_LEAD = ("=", "+", "-", "@", "\t")


def escape_csv_text(value: str) -> str:
    """Escape free-text fields against CSV/spreadsheet formula injection.

    Prefixes a single apostrophe if the value starts with a dangerous lead.
    DO NOT call on ID columns — IDs should be plain strings.
    """
    if not isinstance(value, str) or not value:
        return value
    if value.startswith(_DANGEROUS_LEAD):
        return "'" + value
    return value


# ---- Locks --------------------------------------------------------------
@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Exclusive non-blocking flock on a lock file. Yields on acquire,
    raises BlockingIOError if already held.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            raise
        # write pid + start ts to the lock file for diagnostics
        f.seek(0)
        f.truncate()
        f.write(json.dumps({"pid": os.getpid(), "started": _utc_iso()}))
        f.flush()
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        f.close()


def lock_holder_info(path: Path) -> dict[str, Any]:
    """Read the JSON written by file_lock() so we can tell the user who has it."""
    return read_json_or(path, {})


# ---- Time helpers -------------------------------------------------------
def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def utc_now_epoch() -> int:
    return int(time.time())


# ---- .env parsing -------------------------------------------------------
def parse_env_file(path: Path) -> dict[str, str]:
    """Tiny parser for KEY=VALUE .env files. Quotes are stripped if balanced."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        out[k] = v
    return out


def write_env_file(path: Path, data: dict[str, str]) -> None:
    """Write a .env file atomically with strict perms (0600)."""
    lines = [f'{k}={v}' for k, v in data.items()]
    content = "\n".join(lines) + "\n"
    atomic_write_text(path, content)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ---- Identity model ----------------------------------------------------
# An Identity is a Login-with-Amazon OAuth grant (client_id + client_secret +
# refresh_token + region). One identity can give access to many advertiser
# profiles when the agency uses an Amazon Ads Manager Account, or a single
# brand authorized the agency's LWA app directly.
def identities_dir() -> Path:
    return home() / "identities"


def identity_dir(name: str) -> Path:
    return identities_dir() / name


def identity_env_path(name: str) -> Path:
    return identity_dir(name) / ".env"


@dataclass(frozen=True)
class Identity:
    name: str
    env: dict[str, str] = field(repr=False)

    @property
    def client_id(self) -> str:
        return self.env["LWA_CLIENT_ID"]

    @property
    def client_secret(self) -> str:
        return self.env["LWA_CLIENT_SECRET"]

    @property
    def refresh_token(self) -> str:
        return self.env["LWA_REFRESH_TOKEN"]

    @property
    def region(self) -> str:
        return self.env.get("ADS_REGION", "").upper()

    @property
    def host(self) -> str:
        return host_for(self.region)

    @property
    def is_sandbox(self) -> bool:
        return self.region == SANDBOX_REGION


def list_identity_names() -> list[str]:
    d = identities_dir()
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and (p / ".env").exists())


def load_identity(name: str) -> Identity:
    name = validate_slug(name)
    env_path = identity_env_path(name)
    if not env_path.exists():
        raise FileNotFoundError(
            f"identity {name!r} not found at {env_path}"
        )
    env = parse_env_file(env_path)
    required = ("LWA_CLIENT_ID", "LWA_CLIENT_SECRET", "LWA_REFRESH_TOKEN", "ADS_REGION")
    missing = [k for k in required if not env.get(k)]
    if missing:
        raise RuntimeError(
            f"identity {name!r} is missing required .env keys: {', '.join(missing)}"
        )
    region = env["ADS_REGION"].upper()
    if region not in VALID_REGIONS:
        raise RuntimeError(
            f"identity {name!r} has invalid ADS_REGION={region!r}; "
            f"expected one of {sorted(VALID_REGIONS)}"
        )
    return Identity(name=name, env=env)


# ---- Profile model + resolution ----------------------------------------
# A Profile is an Amazon Ads advertiser/marketplace combination. It points
# to ONE Identity (the OAuth grant used to reach it) plus the advertiser's
# profileId, marketplace, friendly name, and timezone. Multiple profiles
# can share an identity (Manager Account).
def profile_config_path(slug: str) -> Path:
    """Per-profile metadata file. Non-sensitive (no LWA secrets here)."""
    return profile_dir(slug) / "profile.env"


@dataclass(frozen=True)
class Profile:
    slug: str
    identity: Identity
    config: dict[str, str] = field(repr=False)
    source: str  # how this profile was resolved (e.g. "--profile flag", env, active file)

    # ---- profile-level data ----
    @property
    def profile_id(self) -> str:
        return self.config["ADS_PROFILE_ID"]

    @property
    def marketplace_id(self) -> str:
        return self.config.get("ADS_MARKETPLACE_ID", "")

    @property
    def account_name(self) -> str:
        return self.config.get("ADS_ACCOUNT_NAME", "(unknown account)")

    @property
    def timezone(self) -> str:
        return self.config.get("ADS_TIMEZONE", "UTC")

    @property
    def identity_name(self) -> str:
        return self.identity.name

    # ---- identity pass-throughs (so callers can stay terse) ----
    @property
    def client_id(self) -> str:
        return self.identity.client_id

    @property
    def client_secret(self) -> str:
        return self.identity.client_secret

    @property
    def refresh_token(self) -> str:
        return self.identity.refresh_token

    @property
    def region(self) -> str:
        return self.identity.region

    @property
    def host(self) -> str:
        return self.identity.host

    @property
    def is_sandbox(self) -> bool:
        return self.identity.is_sandbox

    @property
    def mode(self) -> str:
        """LIVE or SANDBOX, derived from the identity's region."""
        return "SANDBOX" if self.is_sandbox else "LIVE"


def list_profile_slugs() -> list[str]:
    pdir = profiles_dir()
    if not pdir.exists():
        return []
    return sorted(
        p.name for p in pdir.iterdir()
        if p.is_dir() and profile_config_path(p.name).exists()
    )


def load_profile(slug: str, source: str) -> Profile:
    slug = validate_slug(slug)
    cfg_path = profile_config_path(slug)
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"profile {slug!r} has no config at {cfg_path}; run /amazon-setup-profile"
        )
    cfg = parse_env_file(cfg_path)
    required = ("IDENTITY", "ADS_PROFILE_ID")
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise RuntimeError(
            f"profile {slug!r} is missing required config keys: {', '.join(missing)}"
        )
    identity_name = cfg["IDENTITY"]
    try:
        identity = load_identity(identity_name)
    except (FileNotFoundError, RuntimeError) as e:
        raise RuntimeError(
            f"profile {slug!r} references identity {identity_name!r} which failed to load: {e}"
        )
    return Profile(slug=slug, identity=identity, config=cfg, source=source)


def resolve_profile(cli_flag: str | None) -> Profile:
    """Resolution order: --profile flag → AMAZON_ADS_PROFILE env → active_profile file.

    Raises with a standardized hint if nothing resolves.
    """
    if cli_flag:
        return load_profile(cli_flag, source="--profile flag")
    env_val = os.environ.get("AMAZON_ADS_PROFILE", "").strip()
    if env_val:
        return load_profile(env_val, source="AMAZON_ADS_PROFILE env")
    apf = active_profile_file()
    if apf.exists():
        slug = apf.read_text().strip()
        if slug:
            return load_profile(slug, source="active_profile file")
    existing = list_profile_slugs()
    msg = (
        "No Amazon Ads profile selected.\n"
        "Run /amazon-setup-profile, or pass --profile <brand>."
    )
    if existing:
        msg += f"\nExisting profiles: {', '.join(existing)}"
    raise RuntimeError(msg)


def print_resolution_banner(profile: Profile, *, file=sys.stderr) -> None:
    """Print PROFILE / SOURCE / MODE banner before any other output.

    Every mutating script and every dry run MUST call this before doing work.
    """
    source = profile.source
    if source.startswith("--profile") and not _active_profile_matches(profile.slug):
        source += " (one-off; active profile unchanged)"
    print(
        f"PROFILE: {profile.slug}  "
        f'(account: "{profile.account_name}"  marketplace: {profile.marketplace_id or "-"}  '
        f"region: {profile.region}  identity: {profile.identity_name})",
        file=file,
    )
    print(f"SOURCE:  {source}         MODE: {profile.mode}", file=file)


def _active_profile_matches(slug: str) -> bool:
    apf = active_profile_file()
    if not apf.exists():
        return False
    return apf.read_text().strip() == slug


# ---- LWA token refresh --------------------------------------------------
class AuthError(RuntimeError):
    """Raised when LWA token refresh fails with a known error code."""

    def __init__(self, code: str, description: str, remediation: str):
        super().__init__(f"{code}: {description}\n→ {remediation}")
        self.code = code
        self.description = description
        self.remediation = remediation


_LWA_REMEDIATION = {
    "invalid_grant": (
        "Refresh token is revoked or expired. Re-authorize this brand: "
        "re-run /amazon-setup-profile to capture a fresh refresh_token."
    ),
    "invalid_client": (
        "client_id/client_secret pair is wrong. Double-check both values "
        "in the LWA developer console."
    ),
    "unauthorized_client": (
        "The LWA app is not authorized for Amazon Ads API. Add the Ads scope "
        "to the LWA app, or apply for Ads API access if you haven't yet."
    ),
    "invalid_scope": (
        "Requested scope is invalid. The LWA app needs the "
        "advertising::campaign_management scope."
    ),
}


def parse_lwa_error(payload: dict[str, Any]) -> AuthError:
    code = str(payload.get("error", "unknown_error"))
    desc = str(payload.get("error_description", ""))
    remediation = _LWA_REMEDIATION.get(
        code, "Inspect the LWA error_description and verify your credentials."
    )
    return AuthError(code, desc, remediation)


def _token_cache_path(identity: Identity) -> Path:
    """Token cache lives at the IDENTITY level — multiple profiles sharing
    an identity also share the cached access token."""
    return identity_dir(identity.name) / "state" / "access_token.json"


def _coerce_identity(obj: "Identity | Profile") -> Identity:
    """Accept either an Identity or a Profile for ergonomics."""
    if isinstance(obj, Profile):
        return obj.identity
    if isinstance(obj, Identity):
        return obj
    raise TypeError(f"expected Identity or Profile, got {type(obj).__name__}")


def get_access_token(target: "Identity | Profile", *, force_refresh: bool = False) -> str:
    """Return a valid access token for an identity, refreshing via LWA when needed.

    Caches in identities/<name>/state/access_token.json with expires_at_epoch.
    Refreshes when fewer than 10 minutes remain. Treats parse failure as cache miss.
    """
    identity = _coerce_identity(target)
    cache_path = _token_cache_path(identity)
    if not force_refresh:
        cached = read_json_or(cache_path, {})
        token = cached.get("access_token")
        exp = cached.get("expires_at_epoch", 0)
        if token and exp - utc_now_epoch() > 600:
            return token

    resp = requests.post(
        LWA_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": identity.refresh_token,
            "client_id": identity.client_id,
            "client_secret": identity.client_secret,
        },
        timeout=30,
    )
    body: dict[str, Any]
    try:
        body = resp.json()
    except json.JSONDecodeError:
        body = {}
    if resp.status_code != 200 or "access_token" not in body:
        raise parse_lwa_error(body if body else {"error": "unknown_error",
                                                  "error_description": resp.text})
    token = body["access_token"]
    expires_in = int(body.get("expires_in", 3600))
    atomic_write_json(cache_path, {
        "access_token": token,
        "expires_at_epoch": utc_now_epoch() + expires_in,
    })
    return token


# ---- HTTP wrapper -------------------------------------------------------
def _redact_url(url: str) -> str:
    """Strip the query string before logging (signed report URLs include creds)."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _log_line(method: str, url: str, status: int, ms: int) -> None:
    logs_dir().mkdir(parents=True, exist_ok=True)
    log_path = logs_dir() / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.log"
    line = f"{datetime.now(timezone.utc).isoformat()} {method} {_redact_url(url)} {status} {ms}ms\n"
    with log_path.open("a") as f:
        f.write(line)


_RETRY_DELAYS_S = (2, 4, 8, 16, 32)


def ads_request(
    profile: Profile,
    method: str,
    path: str,
    *,
    json_body: Any = None,
    accept: str = "application/json",
    content_type: str = "application/json",
    extra_headers: dict[str, str] | None = None,
    timeout: int = 60,
) -> requests.Response:
    """Call an Ads API endpoint with auth, retries, and request logging.

    Handles 401 with a single forced token refresh + retry, and 429/5xx with
    exponential backoff respecting Retry-After. Returns the final Response;
    callers decide how to handle non-2xx.
    """
    url = profile.host + path
    headers = {
        "Authorization": f"Bearer {get_access_token(profile)}",
        "Amazon-Advertising-API-ClientId": profile.client_id,
        "Amazon-Advertising-API-Scope": profile.profile_id,
        "Accept": accept,
        "Content-Type": content_type,
    }
    if extra_headers:
        headers.update(extra_headers)

    last_resp: requests.Response | None = None
    attempts = 0
    refreshed_once = False
    while True:
        attempts += 1
        t0 = time.time()
        resp = requests.request(method, url, headers=headers,
                                json=json_body if json_body is not None else None,
                                timeout=timeout)
        ms = int((time.time() - t0) * 1000)
        _log_line(method, url, resp.status_code, ms)
        last_resp = resp

        if resp.status_code == 401 and not refreshed_once:
            refreshed_once = True
            headers["Authorization"] = f"Bearer {get_access_token(profile, force_refresh=True)}"
            continue
        if resp.status_code in (429,) or 500 <= resp.status_code < 600:
            if attempts - 1 < len(_RETRY_DELAYS_S):
                delay = _RETRY_DELAYS_S[attempts - 1]
                ra = resp.headers.get("Retry-After")
                if ra is not None:
                    try:
                        delay = max(delay, int(float(ra)))
                    except ValueError:
                        pass
                time.sleep(delay)
                continue
        return resp


# ---- Audit ledger -------------------------------------------------------
def ledger_path(profile: Profile) -> Path:
    return profile_dir(profile.slug) / "ledger" / "mutations.jsonl"


def ledger_append(profile: Profile, entry: dict[str, Any]) -> None:
    """Append one JSON-line to the per-profile mutations ledger."""
    path = ledger_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now(timezone.utc).isoformat(), **entry}
    with path.open("a") as f:
        f.write(json.dumps(entry, separators=(",", ":"), sort_keys=True) + "\n")


# ---- CSV writer (ID-safe, text-escaping) --------------------------------
def write_rows_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    """Write CSV with ID columns as plain strings and text columns escaped.

    Empty result still gets a header row.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        w.writerow(columns)
        for row in rows:
            out = []
            for c in columns:
                v = row.get(c, "")
                if v is None:
                    out.append("")
                    continue
                if c in ID_COLUMNS:
                    out.append(str(v))
                elif isinstance(v, str):
                    out.append(escape_csv_text(v))
                else:
                    out.append(v)
            w.writerow(out)

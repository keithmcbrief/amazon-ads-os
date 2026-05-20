# amazon-ads-os

An operating system for Amazon Ads agencies, as a Claude Code plugin. Pull
reports, find wasteful search terms, and add negatives — all in plain English.

Built for agencies managing multiple brands. Multi-profile from day one.

## What it does

Five skills, fully driven by natural language:

| Skill | What it does |
|---|---|
| `/amazon-setup-profile` | Onboard a new brand (creates or reuses an identity, picks an advertiser profile) |
| `/amazon-switch-profile` | List identities + brands, switch the active brand |
| `/amazon-pull-report` | "pull search terms from the last 30 days" → CSV |
| `/amazon-add-negatives` | "find search terms with zero sales and >$10 spend" → confirm → mutate |
| `/amazon-doctor` | Preflight checks + redacted support bundle |

v1 covers Sponsored Products only. SB, SD, and DSP are intentionally out of scope.

## How credentials are organized

Amazon Ads agencies commonly use **Manager Accounts** — a single OAuth grant gives the agency
access to many advertiser profiles. amazon-ads-os mirrors that:

- **Identity** — one LWA app + refresh_token + region. The OAuth grant.
- **Profile** — one Amazon advertiser × marketplace, pointing at an identity. Many profiles can share one identity.

Three common shapes:

| Agency situation | Identities | Profiles |
|---|---|---|
| Solo seller, one brand on .com | 1 | 1 |
| Agency with Manager Account covering 20 brands | 1 | 20+ |
| Agency mixing MA brands with direct-grant brands | 2-3 | 20+ |

Solo sellers don't need to think about identities — `/amazon-setup-profile` auto-creates one.

## Requirements

- macOS or Linux (Windows not supported in v1)
- Python 3.10+
- Claude Code

## Install (2 commands)

In Claude Code:

```
/plugin marketplace add https://github.com/keithmcbrief/amazon-ads-os
/plugin install amazon-ads-os@amazon-ads-os
```

That's it. On the next Claude Code session start, the plugin auto-bootstraps
a private Python venv at `${CLAUDE_PLUGIN_DATA}/.venv` and installs the one
dependency (`requests`) — no `pip` commands required from you, no PEP 668
"externally managed environment" issues, no conflicts with your system Python.

Confirm everything is wired up:

```
/amazon-doctor
```

You should see warnings about no profile yet, but Python + requests + data dir
should all pass. If bootstrap fails (no Python, no `venv` module on some bare
Debian installs), doctor prints exactly what to fix.

## Get Amazon Ads API credentials

This is the slow step (~30 min the first time). You need four things:
`client_id`, `client_secret`, `refresh_token`, and `region` (NA / EU / FE).

1. **Apply for Amazon Ads API access.** Required once per Amazon account. Can
   take a day for approval. Follow:
   https://advertising.amazon.com/API/docs/en-us/setting-up/overview

2. **Create an LWA (Login with Amazon) app** at
   https://developer.amazon.com/loginwithamazon/console/site/lwa/overview.html.
   Save the `client_id` and `client_secret`. Add the
   `advertising::campaign_management` scope to the app.

3. **Generate a refresh_token** by completing the LWA OAuth flow once per
   Amazon Ads account you'll manage. Two ways:
   - Use Amazon's official "self-service" example tools in the API docs.
   - Use a community OAuth helper (e.g. `amzn-oauth`).

   Each brand you onboard = one `refresh_token`. Don't share across brands.

4. **Region** based on the marketplace:
   - **NA**: `.com`, `.ca`, `.mx`, `.br`
   - **EU**: `.co.uk`, `.de`, `.fr`, `.it`, `.es`, `.nl`, `.se`, `.pl`, `.ae`, `.in`, `.sa`, `.tr`, `.eg`
   - **FE**: `.jp`, `.au`, `.sg`

## First brand

```
/amazon-setup-profile
```

You'll be asked for a brand slug (`acme-us`), an identity name (defaults to the
brand slug), region, and the four credentials. Credentials are read via hidden
prompts — they're never put in shell history or visible in `ps`. The script
verifies the credentials, lists every advertiser profile visible to that
identity, and writes:

- `~/.amazon-ads-os/identities/<identity>/.env` (0600, holds the secrets)
- `~/.amazon-ads-os/profiles/<brand>/profile.env` (non-sensitive, points at the identity)

## Adding more brands under the same identity (Manager Account)

If your single identity has access to many brands:

```
/amazon-setup-profile        "add Globex under identity agency-main"
```

Or batch-register every visible advertiser at once:

```bash
python3 ~/.claude/plugins/amazon-ads-os/scripts/profile_discover.py \
  --identity agency-main --register --all
```

This generates a brand slug from each Amazon account's name and writes a
profile config for each — zero new OAuth.

## Daily use

```
/amazon-pull-report           "pull sponsored products campaigns last 30 days"
/amazon-pull-report           "pull search terms from the last 14 days"
/amazon-add-negatives         "find search terms with zero sales and >$10 spend"
/amazon-switch-profile        "switch to acme-eu"
/amazon-doctor                preflight or support bundle
```

For agencies: ask "pull Acme search terms last 30 days" and it'll target that
brand for the one report without changing your active profile.

## Where things live

```
~/.amazon-ads-os/
├── active_profile                       # single line: brand slug
├── identities/<identity>/
│   ├── .env                             # 0600; LWA app + refresh_token + region
│   └── state/access_token.json          # cached access token (shared by all profiles under this identity)
├── profiles/<brand>/
│   ├── profile.env                      # non-sensitive: identity name + profile_id + marketplace
│   ├── reports/sp-search-terms/         # timestamped CSVs
│   ├── proposals/                       # immutable; .meta.json sidecars
│   ├── ledger/mutations.jsonl           # every API mutation, audit-ready
│   └── state/                           # pending report state, mutation locks
└── logs/<date>.log                      # API calls (URLs redacted)
```

## Safety

- **Profile resolution banner** printed before every mutation: brand, account,
  region, mode (LIVE / SANDBOX), and how the profile was resolved.
- **Mutations are confirm-before-execute.** Live profiles require typing the
  brand slug to confirm; sandbox profiles can use `y`.
- **Proposals are immutable.** The CSV is hashed when written. Execute verifies
  the hash + the profile metadata in the `.meta.json` sidecar — refuses on any
  drift (different brand, different mode, tampered file).
- **Idempotent execute.** A second run of the same proposal will skip
  already-created negatives.
- **Per-profile mutation lock.** Two `--execute` runs can't race on one brand.
- **Audit ledger.** Every mutation appends one JSONL line with the full
  payload, response, and created-negative IDs — enough to manually revert.

## Distribution

This repo IS its own Claude Code marketplace. Anyone who runs
`/plugin marketplace add https://github.com/keithmcbrief/amazon-ads-os` + `/plugin install`
gets the latest version. Updates: push to `main`.

## Roadmap (post-v1)

- Bid adjustments (raise/lower by ACOS target)
- Daily/weekly performance digests
- Sponsored Brands + Sponsored Display
- Scheduled runs (cron-friendly)
- `revert` command driven from the ledger

## License

MIT.

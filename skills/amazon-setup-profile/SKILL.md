---
name: amazon-setup-profile
description: Onboard a new Amazon Ads brand profile. Use when the user says "set up amazon ads", "add a new brand", "configure amazon credentials", "first time setup", "onboard a new amazon account", "register a brand", or when there are no profiles configured yet.
---

# amazon-setup-profile

You are helping the user add a new Amazon Ads brand to amazon-ads-os.

## The two-tier model (read this once)

amazon-ads-os splits credentials into two layers:

- **Identity** — a single LWA app + refresh_token + region. One OAuth grant. The agency has one identity if they use Amazon Ads Manager Accounts, or one per directly-authorized brand.
- **Profile** — a specific Amazon advertiser × marketplace, pointing at an identity. Multiple profiles can share one identity (the Manager Account case).

So the question on every onboarding is: *do you want to reuse an existing identity, or create a new one for this brand?*

## Prerequisites the user must have

For a **new identity**, they need:
- Amazon Ads API access (one-time application, ~1 day approval)
- LWA `client_id` + `client_secret` from https://developer.amazon.com/loginwithamazon/console/site/lwa/overview.html
- LWA `refresh_token` from completing the OAuth dance against their LWA app
- Region (NA / EU / FE)

For **adding a brand under an existing identity** (Manager Account flow): nothing more — the existing identity has access already.

If they don't have any LWA credentials at all, point them at:
https://advertising.amazon.com/API/docs/en-us/setting-up/overview

## Flow

### Step 1: Check what already exists

```bash
${CLAUDE_PLUGIN_ROOT}/bin/amazon-python ${CLAUDE_PLUGIN_ROOT}/scripts/profile_list.py --json
```

Look at `identities` — empty means cold start, populated means there are existing identities to potentially reuse.

### Step 2: Decide identity strategy

- **Cold start (no identities)**: must create a new identity. Ask the user for an identity name (default: same as brand slug — `acme-us`).
- **Existing identities + user has Manager Account access to many brands**: reuse the existing identity. You won't need any new credentials. Run `profile_discover.py` to enumerate visible brands.
- **Existing identities + the new brand granted a separate OAuth**: create a new identity (separate OAuth grant).

If unsure, **ask the user** which case applies.

### Step 3a: Cold-start or new-identity flow

Ask for the brand slug. Then run:

```bash
${CLAUDE_PLUGIN_ROOT}/bin/amazon-python ${CLAUDE_PLUGIN_ROOT}/scripts/profile_add.py \
  --brand <slug> --new-identity <identity-name> [--sandbox]
```

The script will:
- prompt for region (or force `SANDBOX` if `--sandbox`)
- prompt via **hidden stdin** for client_id, client_secret, refresh_token (never via flags)
- mint a fresh LWA access token to verify the credentials
- call `GET /v2/profiles` and list every advertiser visible to this identity
- if multiple advertisers appear, ask the user to pick one
- write `~/.amazon-ads-os/identities/<name>/.env` (0600) AND `~/.amazon-ads-os/profiles/<brand>/profile.env`
- set `active_profile` if none was set

### Step 3b: Reuse-existing-identity flow

Ask for the brand slug. Then run:

```bash
${CLAUDE_PLUGIN_ROOT}/bin/amazon-python ${CLAUDE_PLUGIN_ROOT}/scripts/profile_add.py \
  --brand <slug> --identity <identity-name>
```

No credential prompts — the script uses the saved identity's refresh_token to call `/v2/profiles` and asks the user to pick.

### Step 3c: Manager Account batch flow (multiple brands at once)

For an agency that wants to register every brand under a Manager Account in one go:

```bash
${CLAUDE_PLUGIN_ROOT}/bin/amazon-python ${CLAUDE_PLUGIN_ROOT}/scripts/profile_discover.py \
  --identity <identity-name> --register --all
```

Generates slug suggestions from each Amazon account's name + marketplace and writes a profile config for each. The user can rename slugs afterwards with file renames.

## Never pass secrets via CLI flags

The script prompts via hidden stdin. Don't construct command lines with embedded `client_secret` or `refresh_token` — shell history and `ps` will leak them.

## After success

Confirm the printed summary (account, profile_id, marketplace, region, identity, mode) and recommend `/amazon-doctor` to verify the network checks.

## Common errors

- `invalid_grant` — refresh_token revoked. Need a fresh one.
- `invalid_client` — wrong client_id or client_secret pair.
- `unauthorized_client` — LWA app missing `advertising::campaign_management` scope.
- `identity X already exists` — pass `--identity X` (reuse) or pick a different name with `--new-identity Y`.
- `no profiles returned for identity ...` — the refresh token belongs to a different region than the identity claims, or the account isn't approved for Amazon Ads API yet.

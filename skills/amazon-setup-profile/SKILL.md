---
name: amazon-setup-profile
description: Add another Amazon Ads brand or ad account to amazon-ads-os. Use when the user already has a connection set up and wants to add a second brand, register more ad accounts under an existing connection, or onboard a new agency client. For first-time setup, use /amazon-doctor instead.
---

# amazon-setup-profile

> **First-time users should run `/amazon-doctor` instead.** That command has a guided wizard for the very first connection. This skill is for adding *additional* ad accounts after the first one is set up.

## When to use this skill

Use this when the user already has at least one ad account configured and wants to add more. Three common situations:

1. **Add a brand under an existing connection.** Common for Amazon Ads Manager Accounts — one OAuth grant gives access to multiple advertisers. No new credentials needed.
2. **Onboard a new agency client with their own credentials.** That client gave the agency a fresh refresh_token for their Amazon account. Need a new connection.
3. **Register every visible ad account at once.** Manager Account batch flow.

If the user hasn't set up any connection yet, redirect them: "Run `/amazon-doctor` to do first-time setup — it walks you through it."

## Step 1: See what already exists

```bash
${CLAUDE_PLUGIN_ROOT}/bin/amazon-python ${CLAUDE_PLUGIN_ROOT}/scripts/profile_list.py --json
```

`identities` empty → user is doing first-time setup, redirect to `/amazon-doctor`.

`identities` populated → ask the user which case they're in:
- A) "Add a brand using the existing `<name>` connection" → step 2A
- B) "This brand has separate credentials" → step 2B
- C) "Show all ad accounts and register multiple at once" → step 2C

## Step 2A: Add under an existing connection

```bash
${CLAUDE_PLUGIN_ROOT}/bin/amazon-python ${CLAUDE_PLUGIN_ROOT}/scripts/profile_add.py \
  --brand <slug> --identity <existing-connection-name>
```

The script reuses the saved refresh_token, calls `/v2/profiles`, asks the user to pick one. No new credential prompts.

## Step 2B: New credentials → new connection

```bash
${CLAUDE_PLUGIN_ROOT}/bin/amazon-python ${CLAUDE_PLUGIN_ROOT}/scripts/profile_add.py \
  --brand <slug> --new-identity <connection-name>
```

Prompts for client_id, client_secret, refresh_token via hidden stdin. Same flow as the wizard, just scoped to adding a specific brand.

## Step 2C: Batch-register every visible ad account

```bash
${CLAUDE_PLUGIN_ROOT}/bin/amazon-python ${CLAUDE_PLUGIN_ROOT}/scripts/profile_discover.py \
  --identity <connection-name> --register --all
```

Calls `/v2/profiles` once, generates a friendly slug for each account, and writes a profile config for each. User can rename slugs later by renaming the folders under `~/.amazon-ads-os/profiles/`.

## Never pass secrets via CLI flags

Scripts prompt via hidden stdin. Don't construct command lines with embedded `client_secret` or `refresh_token` — shell history and `ps` will leak them.

## After success

Confirm the printed summary (brand, account name, profile_id, marketplace, region, connection name). Recommend a verification step: `pull campaigns for the last 7 days` to verify the new brand works end-to-end.

---
name: amazon-doctor
description: First-run setup wizard and preflight diagnostics for amazon-ads-os. Use when the user says "amazon doctor", "set up amazon ads", "connect amazon", "first time setup", "preflight", "is amazon ads working", "diagnose", "amazon-ads-os not working", or when any other amazon-ads-os skill fails with a setup-related error.
---

# amazon-doctor

This is the **first command** a user runs after installing the plugin. It does two things:

1. **If no Amazon Ads connection is configured yet** → launches an interactive setup wizard that walks the user through credentials and ad-account selection.
2. **If already set up** → runs preflight diagnostics (PASS/FAIL checklist).

Run it with `--interactive`:

```bash
${CLAUDE_PLUGIN_ROOT}/bin/amazon-python ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py --interactive
```

## How the output flow works

The script auto-detects whether it has a TTY (a real terminal) or is being invoked from your Bash tool:

- **TTY available**: launches the interactive wizard, prompts for credentials with hidden input, completes setup end-to-end.
- **No TTY (your Bash tool)**: prints a short `setup needed` message that includes a one-line `!~/.amazon-ads-os/bin/wizard` command, then exits 0 (not an error — it just needs the user to take the next step in their own terminal).

**When you see the `setup needed` output, do this:**

1. Relay the `!~/.amazon-ads-os/bin/wizard` line to the user *verbatim*. That's a Claude Code shell escape — the leading `!` runs the wizard in their real terminal so they can enter credentials with hidden input.
2. Do NOT try to re-run the wizard via Bash — it'll fail the same way.
3. Do NOT ask the user for their credentials in chat — they'd leak into the conversation transcript.
4. Do NOT construct a longer/different command — `~/.amazon-ads-os/bin/wizard` is written by the bootstrap hook and always points at the current install.

After the user runs the wizard and confirms it succeeded, re-run doctor *without* `--interactive` to surface the diagnostic checks.

**Why the wizard can't run from your Bash tool:** the secrets (`client_secret`, `refresh_token`) are entered via `getpass`, which requires a TTY for hidden input. This is also intentional security: their refresh_token never enters the Claude conversation or transcript.

## What the wizard does (so you know what's happening)

1. Confirms the user wants to connect.
2. Prompts (with hidden input) for `client_id`, `client_secret`, `refresh_token`, and region.
3. Verifies credentials by minting an LWA access token. On `invalid_grant` / `invalid_client` / `unauthorized_client`, prints exact fix.
4. Calls `/v2/profiles` in the chosen region; auto-probes NA/EU/FE if empty.
5. If exactly one ad account is visible, auto-selects it. Otherwise prompts the user to pick the default.
6. Suggests a short name (slug) generated from the account name + marketplace; user can accept or override.
7. Writes the connection + ad-account config locally and runs diagnostics.

After the wizard succeeds, recommend the user try `pull search terms for the last 30 days` to verify end-to-end.

## Diagnostics-only mode

If the user just wants a health check (already set up):

```bash
${CLAUDE_PLUGIN_ROOT}/bin/amazon-python ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py
```

Checks: Python ≥ 3.10, `requests` importable (in plugin venv), data dir writable, identities/profiles configured, active profile loads, LWA refresh works per identity, `GET /v2/profiles` returns the configured `profile_id`.

Add `--no-network` to skip the Amazon-hitting checks.

## Common errors

| Error | Fix |
|---|---|
| `LWA refresh failed for <identity>: invalid_grant` | refresh_token revoked; delete `~/.amazon-ads-os/identities/<name>/` and re-run `/amazon-doctor` |
| `LWA refresh failed for <identity>: invalid_client` | wrong client_id/secret in the identity's `.env` |
| `configured profile_id not in /v2/profiles` | brand may have revoked access, or the identity is in the wrong region |
| `requests not importable` | plugin venv didn't bootstrap; restart Claude Code so the SessionStart hook runs |

## Support bundle

If the user reports a bug:

```bash
${CLAUDE_PLUGIN_ROOT}/bin/amazon-python ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py --support-bundle /tmp/amazon-ads-os-support.zip
```

Writes a **redacted** zip — logs with signed URLs stripped, identity/profile metadata with secrets removed, pending state files. The user can attach it to a GitHub issue.

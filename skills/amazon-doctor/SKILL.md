---
name: amazon-doctor
description: Preflight health check for amazon-ads-os. Use when the user says "amazon doctor", "preflight", "is amazon ads working", "diagnose", "amazon-ads-os not working", or when any other amazon-ads-os skill fails with a setup-related error.
---

# amazon-doctor

You are running preflight diagnostics on the user's amazon-ads-os setup.

## Default: PASS/FAIL checklist

```bash
${CLAUDE_PLUGIN_ROOT}/bin/amazon-python ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py
```

Checks:
1. Python ≥ 3.10
2. `requests` library importable
3. Data directory (`~/.amazon-ads-os/`) writable
4. At least one identity configured
5. At least one profile configured
6. Active profile loads without errors (identity exists, valid region)
7. LWA token refresh succeeds for **every** identity
8. `GET /v2/profiles` returns the configured `profile_id` for the active profile

If checks 6 or 7 should be skipped (offline / quick check), pass `--no-network`.

A non-zero exit means one or more checks failed. Surface the specific FAIL lines to the user — each one has a hint.

## Common fixes

| FAIL | Fix |
|---|---|
| `python 3.X.Y (need 3.10+)` | Install a newer Python |
| `requests not importable` | `python3 -m pip install --user -r requirements.txt` (the plugin's `requirements.txt`) |
| `data dir not writable` | check `~/.amazon-ads-os` permissions |
| `no identities configured` | run `/amazon-setup-profile` |
| `no profiles configured` | run `/amazon-setup-profile` (or `profile_discover.py --register --all` to batch-register under an existing identity) |
| `LWA refresh failed for <identity>: invalid_grant` | refresh_token for that identity was revoked; delete `~/.amazon-ads-os/identities/<name>/` and rerun setup |
| `LWA refresh failed for <identity>: invalid_client` | wrong client_id/secret in the identity's `.env` |
| `configured profile_id not in /v2/profiles` | the active profile's identity returns a different set of profile IDs than expected; the brand may have revoked access, or the identity may be in the wrong region |
| `profile X references identity Y which failed to load` | edit `profiles/X/profile.env` to point at a valid identity name, or recreate the identity |

## Support bundle

If the user is reporting a bug they want help with:

```bash
${CLAUDE_PLUGIN_ROOT}/bin/amazon-python ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py --support-bundle /tmp/amazon-ads-os-support.zip
```

This writes a **redacted** zip with: logs (signed URLs and auth headers stripped), profile metadata (secrets removed), and pending state files. The user can attach this to a GitHub issue.

The bundle is verified by tests to never contain `client_secret`, `refresh_token`, `client_id`, or cached access tokens — but if the user wants to double-check, suggest:
```bash
python3 -m zipfile -e /tmp/amazon-ads-os-support.zip /tmp/check && grep -ri 'amzn1\|Atza\|Atzr' /tmp/check
```
(Should print nothing.)

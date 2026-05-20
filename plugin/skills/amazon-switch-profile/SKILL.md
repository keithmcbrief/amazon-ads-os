---
name: amazon-switch-profile
description: Switch the active Amazon Ads brand for this machine, or list configured brands. Use when the user says "switch to <brand>", "use <brand>", "change profile", "which brands do I have", "list amazon profiles", or any phrase about choosing/listing brand accounts.
---

# amazon-switch-profile

You are helping the user list configured Amazon Ads brand profiles or switch the active one.

## List mode

If the user asks "which brands do I have", "list profiles", "show my brands", etc.:

```bash
${CLAUDE_PLUGIN_ROOT}/bin/amazon-python ${CLAUDE_PLUGIN_ROOT}/scripts/profile_list.py
```

Or for structured output:
```bash
${CLAUDE_PLUGIN_ROOT}/bin/amazon-python ${CLAUDE_PLUGIN_ROOT}/scripts/profile_list.py --json
```

The output has two sections:
- **IDENTITIES** — the OAuth grants. Each identity has a region and is LIVE or SANDBOX.
- **PROFILES** — the registered brands, each pointing at an identity. The active profile is marked with `*`.

Multiple profiles sharing one identity is the Manager Account pattern — common for agencies. If the user expected each brand to have its own identity but sees them sharing one, that's normal.

## Switch mode

If the user says "switch to Acme" or similar:

1. Run `${CLAUDE_PLUGIN_ROOT}/bin/amazon-python ${CLAUDE_PLUGIN_ROOT}/scripts/profile_list.py --json` to get the list.
2. **Fuzzy-match the requested name against the slugs**, but treat fuzzy matches as *suggestions only*:
   - If exactly one slug matches → use it.
   - If multiple match (e.g. "acme" matches `acme-us`, `acme-ca`, `acme-eu`) → **refuse to switch**, print the candidates, and ask the user to specify the exact slug. **Never auto-pick** — agency money is on the line.
   - If zero match → list configured profiles and ask the user to pick.
3. Once you have a single unambiguous slug, run:
   ```bash
   ${CLAUDE_PLUGIN_ROOT}/bin/amazon-python ${CLAUDE_PLUGIN_ROOT}/scripts/profile_switch.py --brand <slug>
   ```
4. Print the confirmation line back to the user.

## One-off targeting (don't change the active profile)

If the user wants to do something for a non-active brand without switching globally ("pull Acme search terms"), do **not** run this skill. Instead, pass `--profile <slug>` to the other scripts (`amazon-pull-report`, `amazon-add-negatives`, etc.). The resolution banner those scripts print will say `SOURCE: --profile flag (one-off; active profile unchanged)`.

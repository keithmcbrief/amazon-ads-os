---
name: amazon-add-negatives
description: Find wasteful Amazon search terms and add them as negative keywords. Use when the user says "find bad search terms", "add negative keywords", "kill non-converting search terms", "negate wasteful keywords", "pause search terms", or any phrase about cutting wasted ad spend on Amazon.
---

# amazon-add-negatives

You are finding wasteful Amazon search terms and proposing them as negative keywords. Confirm-before-execute is mandatory.

> Naming note: search terms can't be "paused" directly — the action is **adding negative keywords**. If the user says "pause search terms", do this skill but use the correct terminology in your reply.

## Step 1: Parse thresholds

Defaults (use unless user overrides):
- `--min-spend 10` — minimum spend in window
- `--max-sales 0` — only terms with zero sales qualify
- `--min-clicks 5` — ignore terms with too few clicks (statistical noise)
- `--lookback-days 30`
- `--exclude-last-days 3` — drop the most recent N days (attribution lag)
- `--match-type negativeExact` — alternative: `negativePhrase`

Parse the user's phrasing for overrides ("zero sales and >$15 spend" → `--min-spend 15`, etc.).

## Step 2: Ensure a fresh search-terms report exists

If the newest `sp-search-terms` CSV under `~/.amazon-ads-os/profiles/<brand>/reports/sp-search-terms/` is more than 24h old, pull a fresh one first via `/amazon-pull-report`. Use the same lookback window.

## Step 3: Build the proposal (dry-run)

```bash
${CLAUDE_PLUGIN_ROOT}/bin/amazon-python ${CLAUDE_PLUGIN_ROOT}/scripts/search_terms_analyze.py \
  --min-spend 10 --max-sales 0 --min-clicks 5 \
  --lookback-days 30 --exclude-last-days 3 \
  --match-type negativeExact \
  [--profile <slug>]
```

Stdout prints two lines: the proposal CSV path and its sha256 hash. **Remember both** — execute needs them.

Read the CSV (it's small) and present a markdown table grouped by campaign with campaign + ad-group names (not just IDs), columns `searchTerm | spend | sales | clicks | acos | conflict_flag`, and totals (rows / wasted spend / campaigns affected). Use the `conflict_flag` column to surface any search terms that match an enabled positive keyword in the same ad group — those need an extra moment of thought.

## Step 4: Spend-burn guardrail

If the proposal exceeds **any** of:
- 50+ negatives
- $500+ total wasted spend
- 10+ campaigns affected

…surface an explicit warning and ask the user to confirm they want to proceed at this scale.

## Step 5: Strong confirmation

The mutation is irreversible from this skill (no undo command in v1). Confirm by **typing the brand slug** for LIVE profiles. SANDBOX profiles can accept `y`. Read the resolution banner from step 3 to know which mode you're in.

If the user types anything other than the exact slug (LIVE) or `y` (SANDBOX), abort.

## Step 6: Execute

```bash
${CLAUDE_PLUGIN_ROOT}/bin/amazon-python ${CLAUDE_PLUGIN_ROOT}/scripts/negative_keywords_add.py \
  --proposal <path-from-step-3> \
  --proposal-hash <hash-from-step-3> \
  --execute \
  [--profile <slug>]
```

The script:
- verifies the CSV hash and `.meta.json` metadata against the active profile (refuses on drift: different brand, mode flipped, file tampered)
- acquires a per-profile mutation lock
- refreshes the existing-negatives cache (so idempotent reruns skip already-created rows)
- chunks the API calls
- writes a per-row result CSV (`__result.csv` next to the proposal)
- appends one JSONL entry per row to `~/.amazon-ads-os/profiles/<brand>/ledger/mutations.jsonl` — enough data to manually revert later

Present the summary line (`proposed / skipped_existing / attempted / created / failed`) and the result CSV path.

## Recovery

If some rows fail, the result CSV has per-row reasons. Common ones:
- `DUPLICATE_VALUE` — negative already exists (idempotency caught it after the fact). Safe.
- `INVALID_*` — Amazon rejected the payload. Inspect the offending row.
- HTTP 4xx whole-batch fail → likely a transient network or auth issue; rerun.

The ledger has the full payload + response — share that JSONL line if reporting a bug.

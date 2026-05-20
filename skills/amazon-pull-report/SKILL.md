---
name: amazon-pull-report
description: Pull an Amazon Ads report for the active brand (or a one-off brand). Use when the user says "pull <X> report", "get sponsored products data", "download search terms", "campaign performance last 30 days", "ad group report", "advertised products report", or any phrase about getting Amazon Ads data for a date range.
---

# amazon-pull-report

You are pulling a Sponsored Products report from Amazon Ads.

## Step 1: Parse intent → `{report_slug, start, end, profile?}`

Map the user's phrasing to a slug:

| Phrase | Slug |
|---|---|
| "campaigns" / "campaign performance" | `sp-campaigns` |
| "ad groups" / "adgroup" | `sp-ad-groups` |
| "keywords" / "keyword performance" | `sp-keywords` |
| "search terms" / "customer search terms" | `sp-search-terms` |
| "product targeting" / "ASINs targeted" | `sp-product-targeting` |
| "advertised products" / "advertised ASINs" | `sp-advertised-product` |

If the slug is ambiguous, ask the user.

## Step 2: Date window defaults + lag warning

Default window is **last 30 days ending two days ago** in the marketplace timezone (Amazon attribution lags ~24h, especially for sales). Format dates as `YYYY-MM-DD`.

If the user asks for an end date that is today or yesterday, warn:
> Recent days may have incomplete sales attribution. Want to proceed anyway, or end 2 days ago?

Don't proceed against today/yesterday unless they confirm.

## Step 3: One-off vs active profile

If the user mentions a specific brand name in the request ("pull Acme search terms"), pass `--profile <slug>` so the active profile is **not** changed. Otherwise, the active profile is used.

Use `/amazon-switch-profile` (list mode) if the user's brand mention is ambiguous against multiple slugs — never auto-pick.

## Step 4: Run the report

```bash
${CLAUDE_PLUGIN_ROOT}/bin/amazon-python ${CLAUDE_PLUGIN_ROOT}/scripts/report_run.py \
  --report <slug> --start YYYY-MM-DD --end YYYY-MM-DD [--profile <slug>]
```

Stream stderr to the user so they see polling progress.

## Step 5: Interpret the result

- **Exit 0**: CSV path is printed on stdout. Read the first ~20 rows. Present a summary table:
  - row count
  - date range
  - top 5 rows by `cost` (or whatever spend column is in the report)
  - full CSV path
- **Exit 1**: an Amazon failure reason was printed on stderr. Surface it verbatim. Common reasons:
  - invalid date range → suggest a smaller window
  - account / profile issue → suggest `/amazon-doctor`
- **Exit 2**: report still PROCESSING after 20-min cap. Tell the user to rerun the same command to resume — the pending state is saved.

## Notes

- Resumability is automatic. If the user kills Claude mid-poll, rerunning the exact same command (same report + same date range) will pick up the reportId from `~/.amazon-ads-os/profiles/<brand>/state/pending_<hash>.json`.
- The CSV filename embeds brand + slug + dates + a short request-hash so multiple windows don't collide.
- IDs in CSVs are written as plain strings — don't worry about Excel mangling them to scientific notation.

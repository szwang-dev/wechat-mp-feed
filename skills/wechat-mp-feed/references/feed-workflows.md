# Feed Workflows

## Offline Agent Validation

Run:

```bash
mpfeed run agent-smoke --work-dir ./work/agent-smoke
```

Inside a virtualenv-based workspace, the fallback command is:

```bash
.venv/bin/mpfeed run agent-smoke --work-dir ./work/agent-smoke
```

Expected outputs:

```text
agent-smoke.sqlite
feed-items.csv
feed-summary.json
feed-failures.csv
article-llm-jobs.json
agent-smoke-report.md
```

Agent checklist:

1. Read `agent-smoke-report.md`.
2. Confirm counts in `feed-summary.json`.
3. Explain each row in `feed-failures.csv`.
4. Confirm `article-llm-jobs.json` is suitable for article-level semantic analysis.
5. Tell the user this is synthetic data and real deployment needs a private reviewed source registry.

## Real Feed Run

Before a real downloader-backed run, check local readiness:

```bash
mpfeed doctor --base-url http://127.0.0.1:5001
```

If the service is running but login has expired, inspect the adapter directly:

```bash
mpfeed adapter wechat-download-api --base-url http://127.0.0.1:5001 auth-status
mpfeed adapter wechat-download-api --base-url http://127.0.0.1:5001 login-url
```

Use a config file:

```bash
mpfeed run feed --config ./work/feed-config.json
```

Virtualenv fallback:

```bash
.venv/bin/mpfeed run feed --config ./work/feed-config.json
```

The config should point to the user's private SQLite database and downloader base URL. Keep generated feed outputs under `work/` or another ignored/private directory.

For scheduled refreshes, prefer the configured incremental feed mode when available. It pages article metadata until the latest known article is reached instead of relying only on a fixed one-page window.

Expected outputs:

```text
feed-items.csv/json
feed-summary.json/csv
feed-failures.csv/json
```

If the command reports downloader auth failure:

1. Show the login URL if present.
2. Ask the user to scan the QR code.
3. Retry the same command after login.

After content-stage LLM scoring, archive images only for high-value `full_archive` articles:

```bash
mpfeed archive assets --output-dir ./work/archive/assets
```

This preserves image files locally and keeps their order through `article_assets.block_index` and `content_ref`.

## Onboarding

For first-time account setup:

```bash
mpfeed import csv accounts.csv --name-column name
mpfeed resolve imports --source-type csv --limit 100
mpfeed export candidates --format csv
mpfeed review accept <candidate_id> --tier core
```

For larger runs with recordings or names:

```bash
mpfeed run onboarding --work-dir ./work --source-type onboarding
```

After the user-reviewed table has been applied and active sources are stable, run a bounded recent-history metadata backfill. This is the final onboarding stage for a fresh database or a large account-list import:

```bash
mpfeed collect history \
  --tier all \
  --days 90 \
  --count 100 \
  --max-sources 20 \
  --max-pages-per-source 20 \
  --delay-min 3 \
  --delay-max 8 \
  --page-delay-min 2 \
  --page-delay-max 5
```

Continue additional batches by increasing `--source-offset`, or use `--max-sources 0` for a controlled full run when the downloader login state is stable. Historical backfill is an onboarding/repair task, not a high-frequency cron task.

Identity rule:

- The final matched account in the user-reviewed table is the source of truth.
- OCR names are audit evidence only.
- Keep distinct account names separate unless the user explicitly requests an alias merge.

## Single Source Intake And Status

For one-off account or article-link intake, first check source status, then use the same source LLM job/import path as batch classification. Do not invent formal categories, source attributes, or tags outside the taxonomy; return unsupported labels as suggestions for user review.

```bash
mpfeed source status --name <公众号名称>
mpfeed source taxonomy --taxonomy finance
mpfeed llm export-jobs --entity-type source --source-id <source_id> --source-article-limit 8 --output ./work/source-intake-job.json
mpfeed llm import-results ./work/source-intake-result.json --model llm:agent
mpfeed source confirm-classification --source-id <source_id> --category <category> --source-attribute <attribute> --tags <tag_ids>
mpfeed source refresh-latest --source-id <source_id> --count 5 --base-url http://127.0.0.1:5001
```

For unsubscribe/reactivation, use the safe source status interface instead of editing SQLite directly:

```bash
mpfeed source set-status --name <公众号名称> --status inactive
mpfeed source set-status --name <公众号名称> --status active
```

Use `inactive` when the user says not to follow an account anymore. Use `active` only when the user explicitly asks to follow/reactivate it again. After reactivation, run the intake classification confirmation path if the source has no confirmed classification.

For article URLs, prefer the adapter article endpoint first when the user asks to read a single WeChat article:

```bash
mpfeed adapter wechat-download-api --base-url http://127.0.0.1:5001 article <article-url>
```

After reading the article, check whether the source is already tracked. If the source is missing and the user asked to follow it, run the intake flow. If the user only asked to read or summarize the article, ask before adding the source.

## Article LLM Jobs

Export article jobs:

```bash
mpfeed llm export-jobs \
  --entity-type article \
  --taxonomy finance \
  --output ./work/article-llm-jobs.json
```

Import results:

```bash
mpfeed llm import-results ./work/article-llm-results.json --model llm:agent
```

Agent results should include classification, digest, `score_breakdown`, reference holistic importance score, reason, and `analysis_stage`. Metadata-stage jobs use `article_metadata_score_rubric` and should favor recall for content fetching; do not return a free-form triage decision because the system computes fetch action from the formula score plus deterministic low-signal overrides. Content-stage jobs use `article_score_rubric` and should also return `digest.application_targets` so downstream workflows can route articles to daily digest, weekly report, strategy backlog, market view, industry tracking, risk monitoring, source-quality review, or ignore. Do not assign a free-form score without sub-scores. Imports store the formula score computed from `score_breakdown`; the holistic score is kept as `model_importance_score` for review. Low-signal articles such as recruiting, events, product marketing, and boilerplate market wrap text should stay below normal digest thresholds unless they contain reusable research logic.

Recommended staged workflow:

1. Refresh metadata with `mpfeed run feed`.
2. Export metadata-stage jobs with `mpfeed llm export-jobs --entity-type article --article-content-scope metadata --article-analysis-stage metadata --article-published-after <iso> --article-published-before <iso>`.
3. Ask the agent/LLM to complete the JSON jobs, then import with `mpfeed llm import-results`.
4. Fetch retained content with `mpfeed collect content --tier all --limit 0 --passes 3 --timeout 45`, using conservative delays when the downloader reports rate limits.
5. Export content-stage jobs with `mpfeed llm export-jobs --entity-type article --article-content-scope content --article-analysis-stage content --article-retention content_or_archive`.
6. Import content-stage results.
7. Export digest packs with `mpfeed export digests --application-target weekly_report --analysis-stage content --min-score 0.6 --format markdown`.
8. For final application-layer writing, export source contexts with `mpfeed export digest-context --application-target weekly_report --analysis-stage content --min-score 0.6 --format json`. Use these rows when the final digest must re-read the original article text, structured content, and image assets instead of relying only on prior summaries.

For user-facing digest work, treat `application_targets` as routing labels. For example, `strategy_backlog` feeds strategy reproduction, `weekly_report` feeds recurring weekly reports, `market_view` feeds market timing/style summaries, and `ignore` keeps low-signal content out of final outputs.

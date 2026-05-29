---
name: wechat-mp-feed
description: Operate the wechat-mp-feed agent-first WeChat Official Account source registry, semantic feed, and domain-pack workflow toolkit. Use when an agent needs to import/review sources, run feed refreshes, inspect health/failures, execute two-stage article LLM workflows, manage source intake/subscription state, export digest context, or build application inbox/digest workflows from mpfeed outputs.
---

# WeChat MP Feed

Use this skill to operate the `wechat-mp-feed` project through its CLI. This is the primary agent-facing interface for the project.

The agent's job is to onboard large account lists, run the feed layer, inspect outputs, explain failures, prepare and import LLM jobs, manage single-source intake, and build application workflows while keeping private runtime data in the user's configured local paths.

## Core Rules

- Treat `mpfeed` as the operational interface. Edit SQLite directly only when the user explicitly asks for database repair.
- Store account lists, recordings, screenshots, downloader credentials, raw article archives, and personal digests only in user-controlled local paths.
- Describe this as a local feed toolkit that works with user-operated downloader services.
- Use conservative delays for real downloader runs. Reserve `--no-delay` for small local tests.
- For identity matching, only accepted/reviewed sources should enter feed collection. Keep distinct accounts separate unless the user explicitly asks for a merge.
- If downloader health/auth fails, report the login URL or required user action instead of aggressive retries.
- Do not confuse downloader WeChat login QR codes with agent-platform device-pairing QR codes. A downloader login QR decodes to `mp.weixin.qq.com/mp/scanlogin`; device-pairing QR codes may contain `ws://` or `wss://` URLs. If a WeChat scan shows an error, regenerate the downloader QR first and verify the QR type before changing any agent-platform pairing or network-exposure settings.

## Quick Decision

Use `mpfeed` when it is on `PATH`. In virtualenv-based workspaces, `.venv/bin/mpfeed` is the fallback command.

1. **Agent integration test**: run `mpfeed run agent-smoke --work-dir ./work/agent-smoke`.
2. **Environment check**: run `mpfeed doctor --base-url <url>` when a real downloader service is involved.
3. **First-time source onboarding**: use `run onboarding` for large account lists, recordings, screenshots, or article URL batches.
4. **Scheduled daily feed run**: prefer `mpfeed run daily` for cron/agent-supervised refreshes because it writes manifest and progress files.
5. **Manual feed run**: run `mpfeed run feed --config ./work/feed-config.json`; use incremental mode for ad hoc refreshes when configured.
6. **Article semantic analysis**: use `run daily`, `run llm-feed`, or explicit `llm export-jobs` / `llm import-results` steps.
7. **Asset archival**: use `archive assets` after content-stage scoring when `full_archive` articles need local images; the command filters low-value images before saving by default.
8. **Application digest context**: use `export digest-context` when final reports need original article text, structure, or image assets.
9. **Application output**: read the feed outputs first, then build an inbox, digest, report, or other downstream result above the feed layer.

## Required Agent Flow

1. Run `mpfeed run agent-smoke --work-dir ./work/agent-smoke` for first validation.
2. For first-run account setup, run staged onboarding and export the review table.
3. Ask the user to review only unresolved identity rows, uncertain classifications, or manual corrections.
4. For real downloader-backed work, run `doctor` or adapter health/auth checks before a long operation.
5. For scheduled real feed runs, use `mpfeed run daily`. For manual refresh/export, `mpfeed run feed --config ./work/feed-config.json` is still valid.
6. Read `run-manifest.json` and `progress.ndjson` before reporting health. Do not treat an advancing `RUNNING` manifest as a failure.
7. Read `feed-summary.json` before reporting article/source counts.
8. Read `feed-failures.csv` before recommending retries or login refresh.
9. Use two-stage article LLM jobs before article-level semantic analysis.
10. Use `digest-context` for final application-layer reports that require source-level evidence.
11. Use `archive assets` only for high-value `full_archive` articles that should preserve local image files. Keep the default asset filter on unless the user explicitly asks to save every image.
12. Use the relevant taxonomy references when building inbox or digest output; use the finance references for finance research outputs.
13. LLM jobs support batch or chunked processing. Prefer batching when context allows to avoid repeating taxonomy and scoring rubrics. Single-item processing is acceptable for retries, high-value article review, user-directed analysis, or context-size limits.

## Asset Archival Rules

Use `mpfeed archive assets` as the asset archival interface. Do not write image files directly from agent code.

- Default command: `mpfeed archive assets --output-dir ./work/archive/assets`.
- Default behavior filters low-value images before saving local files, including QR codes, small icons/logos, divider bars, and low-detail decorative images.
- Skipped images remain in `article_assets` with `download_status=skipped`; the skip reason is stored in `metadata.archive_decision`.
- Keep the default filter on for routine feed and digest work.
- Use `--no-filter` only when the user explicitly asks to save every image.
- Use `--asset-filter-ocr paddle` only when OCR extras are installed and the task needs image text signals; OCR is local image-to-text processing and does not call an LLM.
- Treat `cached + skipped` as archive-complete for an article. A skipped image is a deliberate archival decision, not a failed download.

## Essential Commands

```bash
mpfeed --help
mpfeed doctor --base-url http://127.0.0.1:5001
mpfeed run agent-smoke --work-dir ./work/agent-smoke
mpfeed run onboarding --work-dir ./work/onboarding --source-type onboarding
mpfeed run daily --work-dir ./work/daily-feed --base-url http://127.0.0.1:5001
mpfeed run feed --config ./work/feed-config.json
mpfeed run llm-feed --work-dir ./work/feed-llm
mpfeed archive assets --output-dir ./work/archive/assets
mpfeed archive assets --output-dir ./work/archive/assets --asset-filter-ocr paddle
mpfeed archive assets --output-dir ./work/archive/assets --no-filter
mpfeed export feed-summary --format json
mpfeed export feed --format json --limit 100
mpfeed llm export-jobs --entity-type article --output ./work/article-llm-jobs.json
mpfeed export digest-context --application-target weekly_report --analysis-stage content --format json
mpfeed source status --name <account-name>
mpfeed source taxonomy --taxonomy finance
mpfeed source refresh-latest --name <account-name> --count 5 --base-url http://127.0.0.1:5001
```

Virtualenv fallback command:

```bash
.venv/bin/mpfeed run agent-smoke --work-dir ./work/agent-smoke
```

Offline validation output:

```text
feed-items.csv
feed-summary.json
feed-failures.csv
article-llm-jobs.json
agent-smoke-report.md
```

When reporting results to the user, mention:

- source/article/digest counts from `feed-summary`;
- failed content rows and `fetch_error`;
- whether article LLM jobs are ready;
- concrete next step, such as login refresh, retry later, or run application-layer analysis.

## References

Read only the reference needed for the task:

- Platform installation and compatibility: `references/platforms.md`
- Feed/onboarding operations: `references/feed-workflows.md`
- Finance research inbox and digest layer: `references/finance-applications.md`

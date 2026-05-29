# Changelog

## Unreleased

No changes yet.

## v0.3.0 - 2026-05-29

`v0.3.0` makes the feed layer easier to run as a scheduled, agent-supervised workflow and strengthens evidence-preserving digest exports.

- Add `mpfeed run daily`, a resumable daily runner for scheduled jobs. It writes manifest and progress files, exports metadata-stage and content-stage LLM jobs, imports staged LLM results, fetches retained content, archives selected assets, and exports digest packs.
- Add explicit daily runner states such as `RUNNING`, `WAITING_FOR_METADATA_LLM`, `WAITING_FOR_CONTENT_LLM`, `DONE`, `LOGIN_REQUIRED`, `DOWNLOADER_UNREACHABLE`, `REFRESH_CIRCUIT_BREAKER`, and `FAILED`.
- Add dated daily output structure with `run-manifest.json`, `run-manifest-latest.json`, `progress.ndjson`, feed exports, LLM job files, asset cache files, and `digest-packs/`.
- Improve agent handling guidance so schedulers and agents inspect manifest/progress files before declaring a blocked run.
- Make asset archival selective by default. `mpfeed archive assets` now filters low-value images before saving local files, including QR codes, icons, divider bars, small logos, low-detail decorative images, and promotional/template images.
- Add `download_status=skipped` for intentionally skipped image assets and store skip reasons under `article_assets.metadata.archive_decision`.
- Add optional local OCR signal support for asset filtering with `--asset-filter-ocr paddle`.
- Treat cached and skipped assets together when deciding whether a `full_archive` article's image archival is complete.
- Update the agent skill package with `run daily`, manifest/progress handling, asset archival rules, LLM batching guidance, and downloader login QR handling.
- Move the Chinese README entry point to the repository root as `README.zh-CN.md` and update public documentation links.
- Extend tests for the daily runner, manifest waiting states, asset filtering, skipped asset status, and archive completion behavior.

## v0.2.0 - 2026-05-20

`v0.2.0` upgrades the reviewable feed layer into an agent-oriented semantic feed system with domain-pack workflows and a built-in finance research pack.

- Add `run feed --progress-every` progress output for article metadata refresh and retained-content fetching, so scheduled jobs can distinguish long-running work from a stalled process.
- Add `run feed --refresh-mode incremental`, which pages each source until the latest known local article is reached instead of relying only on a fixed latest-page count.
- Add `mpfeed source ...` agent-safe helpers for source status, taxonomy allowlists, confirmed source classification with readback, and single-source latest metadata refresh.
- Improve `mpfeed llm ...` jobs for agent workflows: source classification jobs now include recent article evidence, article jobs can target metadata-only preliminary scoring, and imported classifications are validated against the selected taxonomy.
- Add article scoring rubric support with persisted `digest.score_breakdown`, and allow article LLM jobs to be filtered by `--article-updated-after` for scheduled refresh batches.
- Allow `mpfeed llm export-jobs --entity-type source --source-id ...` for single-source intake workflows that use the same JSON job/import path as batch source classification.
- Add pending source classification reviews for first-time LLM source suggestions and staged article LLM scores (`metadata`, `content`, `rules`) for cleaner retention priority.
- Add `mpfeed archive assets` to cache image files for `full_archive` articles while preserving image position through existing content structure references.
- Add `run llm-feed` and related filters for the two-stage semantic feed workflow: publish-window metadata jobs, retained-content fetch, content-stage jobs, and target-specific digest packs.
- Add `mpfeed export digest-context` and automatic `.context.json` files for digest packs, so final application-layer digests can re-read source article text, structured content, and image assets.
- Add `mpfeed source set-status` as the agent-safe write path for unsubscribe, reactivation, archive, and review-needed source states.

## v0.1.0 - 2026-05-16

Initial public release of `wechat-mp-feed`.

Highlights:

- Reviewable first-run onboarding for WeChat Official Account sources.
- Batch import from names, URLs, screenshots, and recordings.
- Multi-round account search and compact review-table export.
- Local SQLite store for sources, candidates, articles, classifications, digests, and content assets.
- Article metadata refresh, importance scoring, retained-content fetching, and feed export.
- Optional local OCR extras for screenshot/video onboarding.
- Agent-oriented skill and CLI workflows for local-first feed generation.

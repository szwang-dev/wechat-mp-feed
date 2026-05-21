# Changelog

## Unreleased

No changes yet.

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

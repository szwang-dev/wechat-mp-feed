# CLI Reference

Command name: `mpfeed`.

## Principles

- Commands are composable and safe by default.
- Import/resolve/collect are separate steps so users can review ambiguous matches.
- Any backend adapter requiring session credentials is opt-in and rate-limited.
- Output defaults to local files/SQLite; pushing to external channels is explicit.

## Global options

```bash
mpfeed --db ./data/mpfeed.sqlite <command>
```

Some commands also accept command-specific config files. For example, `run feed --config` reads a JSON config file for first-layer feed runs.

## `init`

Create config and local database.

```bash
mpfeed init --db ./data/mpfeed.sqlite
mpfeed init --config config.yaml --taxonomy examples/taxonomy.finance.yaml
```

## `demo`

Create a small synthetic demo database. This is meant for first-time users and CI validation, and runs fully offline.

```bash
mpfeed --db ./work/demo-feed.sqlite demo seed-feed --work-dir ./work/demo-feed
mpfeed run feed --config ./work/demo-feed/feed-config.demo.json
```

The seeded demo contains core finance, finance-related, recruiting, and failed-content examples. It can export `feed-items`, `feed-summary`, and `feed-failures` entirely offline.

## `import`

### CSV/JSON

```bash
mpfeed import csv accounts.csv --name-column name --category-column category_hint
mpfeed import json accounts.json --name-field name
```

Output:

- writes rows to `source_imports`
- prints import batch id

### Article URLs

```bash
mpfeed import urls article_urls.txt
mpfeed import url 'https://mp.weixin.qq.com/s?...'
```

Behavior:

- extracts `__biz` when present
- stores raw URL
- can later resolve to source record

### Screenshot/video OCR

```bash
mpfeed import images screenshots/*.png --ocr paddle
mpfeed import video wechat_accounts.mp4 --fps 2 --ocr paddle --min-occurrences 2 --names-output accounts.txt --raw-output ocr.json
mpfeed import video following.mp4 --fps 0.5 --ocr paddle --crop 220,0,900,2556 --scale-width 480 --names-output accounts.txt --raw-output ocr.json
```

Local OCR is optional and should be installed only where needed:

```bash
python3 -m pip install -e "packages/wechat_mp_feed[ocr]"
```

Behavior:

- extracts frames if video
- crops optional region if configured
- OCRs account names
- dedupes similar names
- writes candidate raw names into `source_imports`

Dependency policy:

- core install keeps OCR dependencies optional
- `ffmpeg` is treated as a binary dependency; local OCR can use a system `ffmpeg` or a Python-managed ffmpeg binary
- PaddleOCR/OpenCV/Pillow are lazy optional imports
- Docker one-shot OCR remains available for users who prefer an isolated OCR environment

Useful options:

```bash
--crop x,y,w,h
--scale-width 480
--lang zh
--dedupe-threshold 0.88
--min-occurrences 2
--save-frames ./work/frames
--names-output ./work/accounts.txt
--raw-output ./work/ocr.json
```

Recommended first-run recording settings:

- `--fps 1` for a low-cost preview run.
- `--fps 2 --min-occurrences 2` for a slower but higher-recall onboarding pass.
- Use `--crop` to keep only the account-name area, then `--scale-width 480` or `--scale-width 720` to make PaddleOCR practical in an agent/container runtime.
- Use `--names-output` / `--raw-output` for large lists; terminal output stays summarized.

## `resolve`

Resolve imported names/URLs into canonical source candidates.

```bash
mpfeed resolve --batch <batch_id> --adapter wechat-backend
mpfeed resolve --all-pending --limit 100 --rate 10/m
```

Behavior:

- calls adapter search, e.g. `searchbiz`-style backend adapter
- writes `source_candidates`
- auto-accepts high-confidence matches
- leaves ambiguous matches for review

## `review`

Review ambiguous matches.

```bash
mpfeed review list --batch <batch_id>
mpfeed review accept <candidate_id>
mpfeed review reject <candidate_id>
mpfeed review export review.csv
mpfeed review import reviewed.csv
```

Current MVP commands:

```bash
mpfeed resolve search '第一财经'
mpfeed resolve imports --source-type recording --limit 1000
mpfeed resolve imports --source-type recording --status searched --retry-empty --limit 1000
mpfeed resolve imports --source-type recording --status searched --retry-empty --query-variants --limit 1000
mpfeed resolve imports --source-type recording --status searched --replace-pending-candidates --limit 1000
mpfeed review list
mpfeed review accept <candidate_id> --tier core
mpfeed review reject <candidate_id>
mpfeed review apply reviewed.csv
mpfeed review auto-exact --source-type recording --finance-only --taxonomy finance
mpfeed export sources --format json
```

`review auto-exact --finance-only` promotes exact high-score candidate matches that also look finance-related under the active taxonomy. Other candidates remain pending for LLM or manual review.

Use `--query-variants` when OCR names include spaces, common traditional/simplified variants, or punctuation. It searches the original name plus normalized variants and merges candidates.

Use `--replace-pending-candidates` when re-running resolve with a richer downloader, for example a patched `searchbiz` that returns `signature`. It replaces only pending candidates for each import and preserves accepted/rejected decisions.

For first MVP, CSV-based review plus `export onboarding` is enough. A TUI/web UI can come later.

## `export onboarding`

Export a first-run source onboarding table.

```bash
mpfeed export onboarding --source-type recording --taxonomy finance --format csv > work/onboarding.csv
mpfeed export onboarding --source-type recording --taxonomy finance --format json > work/onboarding.json
```

The table combines:

- OCR/list name.
- best resolved candidate.
- candidate score and intro.
- fakeid / biz.
- whether it is already active in `sources`.
- latest known article title, digest, URL, and publish time.
- latest probe status for candidate/source evidence.
- rule/LLM classification fields.
- whether the row needs manual review.
- recommended action.

This table is the expected handoff point for LLM-assisted first-run onboarding. The LLM should use account name, candidate intro, and latest article evidence before asking the user to confirm.

## `collect`

Collect article metadata and content.

```bash
mpfeed collect latest --tier core --count 10 --delay-min 3 --delay-max 8
mpfeed collect history --tier all --days 90 --count 100 --max-sources 20 --delay-min 3 --delay-max 8
mpfeed collect content --tier core --limit 20 --delay-min 3 --delay-max 8
```

`collect history` is the final stage after first-run onboarding or a large account-list import. It pages the downloader article-list API by `fakeid + begin/count`, saves metadata newer than the lookback window, and stops per source when it reaches the cutoff, downloader `total_count`, a short page, or the safety page cap. Use `--source-offset` and `--max-sources` for resumable batches.

Compact review exports use a strict user-facing match policy: only `exact` and `normalized` names are shown as matched accounts. `similar`, `different`, and `unresolved` rows remain candidate accounts and require manual confirmation.

Manual onboarding review should edit only the manual columns:

- `manual_account_name` / `人工确认账号名`
- `manual_article_url` / `人工确认文章链接`
- `manual_account_category` / `人工确认分类`
- `manual_decision` / `人工决策`
- `notes` / `备注`

Use article URLs only for rows where matching is uncertain: no candidate, wrong candidate, likely rename, or generic account names. Rows with strict matched accounts usually need no edit.

Apply edited review rows back into the database:

```bash
mpfeed review apply-onboarding --source-type recording work/onboarding-review.xlsx
mpfeed resolve manual-names --source-type recording --base-url http://127.0.0.1:5001
```

Supported manual decisions are `确认候选`, `忽略`, `无效`, and `稍后` (`confirm_candidate`, `ignore`, `invalid`, `needs_review` also work). `确认候选` promotes the current best candidate into `sources`; rows with only a manual name or article URL are recorded as `needs_review` for a later resolver step.

After users type a manual account name, run `resolve manual-names`. It searches only those manual names, keeps existing system candidates intact unless `--replace-pending-candidates` is used, and ranks candidates against the manual name instead of the original OCR text. Strict manual-name matches then join the identity-confirmed pool for LLM/source classification.

If the downloader returns HTTP 200 with body-level errors such as `success:false` / `invalid session`, the adapter treats the call as failed. Re-scan the downloader login QR code and rerun the affected step.

## `llm`

Export agent-agnostic LLM jobs and import results.

```bash
mpfeed llm export-onboarding-jobs \
  --source-type recording \
  --taxonomy finance \
  --candidate-limit 5 \
  --article-limit 3 \
  --strict-match-only \
  --output work/onboarding-jobs.json

mpfeed llm import-results work/onboarding-results.json
```

`export-onboarding-jobs` is the first-run account triage handoff. Each job contains the imported OCR/list name, ranked search candidates, match quality, candidate intro when available, and latest article probes gathered by `collect candidate-latest`.

After manual identity review, pass `--strict-match-only` so the LLM/agent classifies only identity-confirmed accounts. Non-strict matches should remain in the review table until the user supplies a corrected account name or article link.

For source onboarding/classification, the expected account-level shape is now:

- `inclusion_tier`: `core_finance`, `finance_related`, `optional_extension`, `exclude`, or `needs_review`.
- `classification.category`: primary research domain, such as `macro_policy`, `strategy`, `quant`, `fixed_income`, `industry_research`, or `company_research`.
- `source_attribute`: source type/facet, such as `sell_side`, `buy_side`, `media`, `kol`, or `recruiting`.

Buy-side/sell-side are source attributes. Primary categories describe research domain.

LLM results can:

- `accept_source`: promote one selected candidate into `sources`, save classification, and set status/tier.
- `ignore_non_finance`: reject candidates for an imported account, mark the import ignored, and archive any source that had previously been accepted from those candidates.
- `reject_all`: reject false/noisy imports.
- `needs_manual_review`: keep the imported account for user review.

## `crawl`

Collect article metadata and optionally content.

```bash
mpfeed crawl latest --tier core --pages 1
mpfeed crawl latest --source <source_id> --pages 2
mpfeed crawl batch --tier normal --metadata-only
mpfeed crawl content --article <article_id>
```

Current MVP command name:

```bash
mpfeed collect latest --tier core --count 10
mpfeed export articles --format json
mpfeed collect content --limit 20
mpfeed collect candidate-latest --source-type recording --decision pending --limit 100 --count 1
mpfeed collect candidate-latest --source-type recording --best-per-import --limit 1000 --count 3
mpfeed collect candidate-latest --source-type recording --best-per-import --strict-match-only --limit 1000 --count 3
mpfeed export contents --format json
mpfeed collect latest --tier core --delay-min 3 --delay-max 8 --retries 2
```

Safety defaults:

- metadata-only unless `--with-content`
- no more than one page per source unless explicitly set
- conservative per-account and global delay

`collect candidate-latest` is for first-run onboarding. It probes latest article metadata for unresolved candidates without promoting them into `sources`. The article evidence is saved into the candidate `raw_payload` and then shown by `export onboarding`.

Use `--best-per-import` during large first-run onboarding. It probes only the current best candidate for each imported account, which avoids spending requests on every search candidate.

Use `--strict-match-only` with `--best-per-import` once the identity matching phase is done. This keeps article evidence and downstream classification limited to accounts whose candidate name strictly matches the OCR/list name or the manually corrected account name.

Useful options:

```bash
--with-content
--with-assets
--max-sources 50
--delay-min 3
--delay-max 15
--stop-on-rate-limit
```

## `run feed`

Generate the first-layer article feed from accepted sources.

Config-driven run:

```bash
mpfeed run feed --config examples/feed-config.example.json
```

Config reference:

```text
docs/config.md
docs/zh-CN/config.md
```

Equivalent command-style run:

```bash
mpfeed --db ./data/mpfeed.sqlite run feed \
  --base-url http://127.0.0.1:5001 \
  --full \
  --max-sources 0 \
  --count 5 \
  --refresh-mode incremental \
  --incremental-max-pages 4 \
  --content-limit 0 \
  --work-dir ./work/feed
```

Outputs:

```text
feed-items.csv/json      article-level feed rows
feed-summary.json/csv    aggregate source/article/content status
feed-failures.csv/json   rows with crawl_status=content_failed
```

Important behavior:

- `--full` refreshes article metadata, runs initial scoring, fetches retained content, then exports files.
- `--skip-refresh` exports from existing SQLite rows without requiring downloader login.
- `--refresh-mode latest` fetches one latest page per source. `--refresh-mode incremental` pages forward until the latest known local article is reached, or until `--incremental-max-pages` is hit.
- `--count` is the downloader page size. For WeChat Official Accounts it can represent recent publish batches, and a batch can contain multiple article rows after normalization.
- retained content fetches use a fixed queue and multiple internal passes.
- `--progress-every N` emits progress lines during article metadata refresh and retained-content fetches. This is useful for scheduled jobs where long quiet periods can look like a hang.
- body-level retry hints such as `请 N 秒后重试` are honored.
- failed rows keep `fetch_error` so users can distinguish retryable limits from deleted or restricted articles.

## `run daily`

Run the stable daily feed runner for scheduled jobs and agent supervision.

```bash
mpfeed --db ./work/feed.sqlite run daily \
  --date 2026-05-25 \
  --base-url http://127.0.0.1:5001 \
  --work-dir ./work/daily-feed
```

The command writes a dated run directory:

```text
work/daily-feed/YYYY-MM-DD/
  run-manifest.json
  run-manifest-latest.json
  progress.ndjson
  feed-items.csv
  feed-summary.json
  feed-failures.csv
  article-metadata-llm-jobs.json
  article-llm-jobs.json
  digest-packs/
```

Status values are explicit:

- `RUNNING`: code is still working and progress should be checked before reporting failure.
- `WAITING_FOR_METADATA_LLM`: metadata refresh and metadata jobs are complete; an agent should fill `article-metadata-llm-jobs.json` and rerun with `--metadata-results`.
- `WAITING_FOR_CONTENT_LLM`: retained content has been fetched and content jobs are complete; an agent should fill `article-llm-jobs.json` and rerun with `--content-results`.
- `DONE`: digest packs and final context exports are complete.
- `LOGIN_REQUIRED`, `DOWNLOADER_UNREACHABLE`, `REFRESH_CIRCUIT_BREAKER`, `FAILED`: actionable failure states for agents or schedulers.

Resume pattern:

```bash
mpfeed --db ./work/feed.sqlite run daily \
  --date 2026-05-25 \
  --base-url http://127.0.0.1:5001 \
  --skip-refresh \
  --metadata-results ./work/daily-feed/2026-05-25/article-metadata-llm-results.json

mpfeed --db ./work/feed.sqlite run daily \
  --date 2026-05-25 \
  --base-url http://127.0.0.1:5001 \
  --skip-refresh \
  --metadata-results ./work/daily-feed/2026-05-25/article-metadata-llm-results.json \
  --content-results ./work/daily-feed/2026-05-25/article-llm-results.json
```

Operational notes:

- The runner belongs to `wechat-mp-feed`, not to the downloader service. The downloader remains a replaceable HTTP adapter.
- Local, Docker, and agent usage all call the same CLI. Docker should mount a persistent `work/` directory.
- Asset OCR is off by default. Expensive image OCR should run as a separate review task, not in the daily critical path.
- Agents should read `run-manifest.json` and `progress.ndjson`; they should not treat an advancing `RUNNING` manifest as a failed run.

## `run agent-smoke`

Run offline agent validation with synthetic feed data:

```bash
mpfeed run agent-smoke --work-dir ./work/agent-smoke
```

Outputs:

```text
agent-smoke.sqlite
feed-items.csv
feed-summary.json
feed-failures.csv
article-llm-jobs.json
agent-smoke-report.md
```

This command is intended for agent integration tests. It verifies that an agent can run the feed layer, read feed health, inspect failures, and prepare article-level LLM jobs without WeChat login or private data.

## `classify`

Classify sources or articles.

```bash
mpfeed classify sources --taxonomy default
mpfeed classify sources --taxonomy examples/taxonomy.finance.yaml
mpfeed classify articles --since 24h
```

Output:

- writes `classifications`
- supports rules first, LLM optional

## `source`

Agent-safe helpers for single-source status, taxonomy, confirmed classification, and latest-article refresh.

```bash
mpfeed source status --name "Example Research"
mpfeed source set-status --name "Example Research" --status inactive
mpfeed source taxonomy --taxonomy finance
mpfeed source confirm-classification \
  --name "Example Research" \
  --category industrial \
  --source-attribute kol \
  --tags ai \
  --confidence 0.78
mpfeed source refresh-latest --name "Example Research" --count 5 --base-url http://127.0.0.1:5001
```

Behavior:

- `source status` and `source taxonomy` are read-only.
- `source set-status` is the agent-safe write path for subscription state. Use `inactive` for unsubscribe, `active` for explicit re-follow, `archived` for long-term removal, and `needs_review` when user confirmation is required.
- `source confirm-classification` validates ids against the taxonomy, writes a confirmed source classification, and returns a readback payload.
- `source refresh-latest` refreshes metadata for one active source.
- Invalid taxonomy ids are rejected and should be returned to the user as suggestions, not written as formal classifications.

## `digest`

Generate summaries and importance scores.

```bash
mpfeed digest articles --since 24h --min-score 0.65
mpfeed digest source <source_id> --limit 20
```

Output:

- writes `digests`
- can export markdown/json

## `archive`

Cache retained assets for high-value articles:

```bash
mpfeed archive assets --output-dir work/archive/assets --limit 100
```

Behavior:

- selects image assets from articles whose `retention_level=full_archive`
- downloads kept images to a local directory grouped by article id
- filters low-value images before writing files, including QR codes, tiny icons, divider bars, small logos, low-detail decorative images, and promotional/template images
- updates `article_assets.local_path` and `download_status`
- marks filtered images as `download_status=skipped` and stores the reason in `article_assets.metadata.archive_decision`
- updates `articles.archive_status` to `cached`, `pending`, or `failed`
- preserves image order through `article_assets.block_index` and `content_ref`

Filtering is enabled by default. Use `--no-filter` to cache every image, or `--asset-filter-ocr paddle` to add optional OCR text signals when OCR extras are installed.

## `llm`

Export agent-readable jobs and import LLM results.

```bash
mpfeed llm export-jobs --entity-type all --output work/llm-jobs.json
mpfeed llm export-jobs --entity-type article --limit 50 --content-chars 8000
mpfeed llm export-jobs --entity-type article --article-content-scope metadata --output work/article-score-jobs.json
mpfeed llm export-jobs --entity-type article --article-updated-after 2026-05-20T00:00:00+00:00 --output work/article-score-jobs.json
mpfeed llm export-jobs --entity-type article --article-content-scope metadata --article-analysis-stage metadata --article-published-after 2026-05-11T00:00:00+08:00 --article-published-before 2026-05-17T23:59:59+08:00 --output work/article-metadata-jobs.json
mpfeed llm export-jobs --entity-type article --article-content-scope content --article-analysis-stage content --article-retention content_or_archive --output work/article-content-jobs.json
mpfeed llm export-jobs --entity-type source --source-article-limit 8 --output work/source-classification-jobs.json
mpfeed llm export-jobs --entity-type source --source-id mp_xxx --source-article-limit 8 --output work/source-intake-job.json
mpfeed llm import-results work/llm-results.json --model llm:codex
mpfeed export digests --application-target weekly_report --analysis-stage content --min-score 0.6 --format markdown
mpfeed export digest-context --application-target weekly_report --analysis-stage content --min-score 0.6 --format json
```

Behavior:

- exports sources/articles with the active taxonomy and a JSON result schema
- exports source jobs with recent article evidence for semantic account classification; `--source-id` limits the job to one account for single-source intake
- exports article jobs for either metadata-only preliminary scoring or content-based semantic scoring
- exports article jobs with an explicit `article_score_rubric`; agents should return `digest.score_breakdown` before the reference holistic `importance_score`
- can limit article jobs with `--article-updated-after` so scheduled runs score only the current refresh batch instead of all historical articles
- can limit article jobs by publish-time window and retention level with `--article-published-after`, `--article-published-before`, and `--article-retention`
- can export digest packs by `application_targets`, score, and analysis stage using `mpfeed export digests`
- can export final application context with `mpfeed export digest-context`, including the digest row, source article text, structured content, and image assets; final weekly reports or deep digests should read this context instead of relying only on second-level summaries
- lets any agent decide finance relevance, source category, source attribute, source tier/status, article category, tags, digest, and semantic importance
- validates imported category and tag ids against the selected taxonomy; unknown ids are skipped instead of written
- saves first-time source LLM results as pending `source_classification_reviews` when `requires_user_confirmation=true`
- imports classifications into `classifications`
- imports source status/tier updates into `sources`
- imports article summaries into `digests`

`rules_v1` remains the local fallback for scoring and retention. For production agent workflows, prefer LLM-imported scores for article retention and digest priority. Article scoring uses the exported rubric fields: `research_depth`, `decision_value`, `strategy_reproducibility`, `timeliness`, `source_relevance`, `originality`, `evidence_quality`, and `noise_penalty`. Imports store the formula score computed from `digest.score_breakdown`; `digest.importance_score` is kept inside the breakdown as the agent's reference holistic score. The rubric is intentionally explicit so agents can be audited and the scoring policy can be revised later. Source attributes such as `sell_side`, `buy_side`, `media`, or `kol` can be returned either in `source_attribute` or in `classification.tags`; imports normalize them into tags. First-time source classification should set `requires_user_confirmation=true`; imports then save the suggestion as a pending source classification review. The user confirms it through the review workflow or `mpfeed source confirm-classification`, optionally with `--review-id` to mark the suggestion confirmed. If an agent needs a new source category, source attribute, or tag, it should use the closest current taxonomy id and return structured `taxonomy_suggestions` for user review.

Article LLM scores are staged. `analysis_stage=metadata` is a preliminary score for content-fetch priority. `analysis_stage=content` is the final score for digest and retention. When both exist, content-stage scores take precedence over metadata-stage scores; `rules` scores are the fallback.

## `run llm-feed`

Advance the two-stage semantic feed workflow without hiding the LLM review boundary.

```bash
mpfeed run llm-feed \
  --work-dir work/feed-llm \
  --published-after 2026-05-11T00:00:00+08:00 \
  --published-before 2026-05-17T23:59:59+08:00

mpfeed run llm-feed \
  --work-dir work/feed-llm \
  --metadata-results work/feed-llm/article-metadata-results.json \
  --fetch-content \
  --base-url http://127.0.0.1:5001

mpfeed run llm-feed \
  --work-dir work/feed-llm \
  --content-results work/feed-llm/article-content-results.json \
  --digest-min-score 0.6
```

Behavior:

- always writes `article-metadata-jobs.json`
- after `--metadata-results`, imports metadata scores, optionally fetches retained content, and writes `article-content-jobs.json`
- after `--content-results`, imports final content scores and writes digest packs under `digest-packs/`
- keeps LLM execution outside the package so Codex, Claude Code, or another agent can complete the JSON jobs

For first-run source onboarding, LLM jobs/results should decide:

- finance relevance.
- source category and tags.
- `source_update.status`.
- `source_update.tier`.
- whether manual confirmation is still required.

Current first-run source onboarding behavior:

- `accept_source` promotes the selected candidate into `sources`, saves source classification, and can set tier/status.
- `ignore_non_finance` rejects candidates for the import, marks the import ignored, and archives any source previously accepted from those candidates.
- `reject_all` rejects noisy/invalid imports and also archives any linked previously accepted source.
- `needs_manual_review` leaves the row for user review.

Recommended large-list sequence:

```bash
mpfeed run onboarding \
  --base-url http://127.0.0.1:5001 \
  --video-file ./following.mp4 \
  --crop 220,0,900,2556 \
  --scale-width 480 \
  --source-type recording \
  --work-dir ./work
```

Internally, `run onboarding` hides the slow first-run mechanics:

```text
doctor/login check
-> optional import video or names
-> search original OCR/list names
-> retry only zero-candidate rows with the original name
-> retry only remaining zero-candidate rows with normalized/OCR query variants
-> collect candidate-latest --best-per-import
-> export LLM onboarding jobs
-> optionally import LLM results
-> export compact review table
```

Search remains a name-matching phase. It only treats `exact` and normalized/OCR-equivalent names as strict matches. Intro/signature and latest articles are gathered after search as classification evidence for the LLM/review table, so the original downloader can still be used when it lacks intro fields.

The older explicit sequence is still available for debugging:

```bash
mpfeed import video ./following.mp4 --fps 2 --min-occurrences 2 --names-output ./work/accounts.txt --raw-output ./work/ocr.json
mpfeed resolve imports --source-type recording --limit 1000
mpfeed resolve imports --source-type recording --status all --retry-empty --limit 1000
mpfeed resolve imports --source-type recording --status all --retry-empty --query-variants --limit 1000
mpfeed collect candidate-latest --source-type recording --decision all --best-per-import --limit 1000 --count 1
mpfeed llm export-onboarding-jobs --source-type recording --taxonomy finance --candidate-limit 5 --article-limit 3 --output ./work/onboarding-jobs.json
mpfeed llm import-results ./work/onboarding-results.json --model llm:agent
mpfeed export onboarding --source-type recording --view compact --taxonomy finance --format csv > ./work/onboarding-review.csv
```

`run onboarding` writes review CSV files with a UTF-8 BOM so Excel opens Chinese text correctly.

## `export`

```bash
mpfeed export sources --format csv > sources.csv
mpfeed export articles --since 24h --format json > articles.json
mpfeed export rss --tier core --output feed.xml
mpfeed export markdown --since 24h --output digest.md
mpfeed export onboarding --source-type recording --format csv > onboarding.csv
```

## `deliver`

External push is explicit.

```bash
mpfeed deliver webhook --since 24h --target https://...
mpfeed deliver discord --since 24h --channel <channel-name>
```

For agent systems, use the skill wrapper to call package commands and use the agent platform's message/file tools for channel delivery.

## Python API sketch

```python
from wechat_mp_feed import Pipeline

pipe = Pipeline.from_config('config.yaml')
batch = pipe.import_csv('accounts.csv')
pipe.resolve(batch_id=batch.id, adapter='wechat-backend')
pipe.crawl_latest(tier='core', with_content=True)
pipe.classify_articles(taxonomy='finance')
pipe.digest_articles(since='24h')
```

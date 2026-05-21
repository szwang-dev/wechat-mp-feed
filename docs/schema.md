# Storage Schema

`wechat-mp-feed` uses local SQLite storage by default. DuckDB can be added later for analytics-heavy workflows.

## Design goals

- Preserve raw imported data and resolved canonical data separately.
- Make ambiguous account matches reviewable.
- Keep article metadata separate from extracted full content/assets.
- Support generic use cases; finance-specific fields live in optional taxonomies or enrichment tables.

## Tables

### `sources`

Canonical WeChat Official Account records after resolution.

| column | type | notes |
|---|---:|---|
| `id` | text pk | internal stable id, e.g. `mp_<hash>` |
| `platform` | text | default `wechat_mp` |
| `name` | text | canonical account name |
| `wechat_fakeid` | text nullable | fakeid/base64 id used by backend adapters |
| `biz` | text nullable | `__biz` from article URLs when available |
| `avatar_url` | text nullable | remote avatar URL |
| `intro` | text nullable | account description |
| `status` | text | `active`, `inactive`, `archived`, `needs_review` |
| `tier` | text | `core`, `normal`, `long_tail` |
| `source_type` | text | `ocr`, `csv`, `json`, `article_url`, `manual`, `api` |
| `created_at` | timestamp | |
| `updated_at` | timestamp | |

### `source_imports`

Raw imported rows before resolution.

| column | type | notes |
|---|---:|---|
| `id` | text pk | import item id |
| `batch_id` | text | import batch id |
| `raw_name` | text nullable | OCR/CSV detected name |
| `raw_url` | text nullable | article/account URL if any |
| `raw_payload` | json | original row/OCR metadata |
| `source_type` | text | `ocr`, `csv`, `json`, `article_url`, `manual` |
| `status` | text | `pending`, `resolved`, `ignored`, `error` |
| `created_at` | timestamp | |

### `source_candidates`

Resolution candidates returned by search/adapters.

| column | type | notes |
|---|---:|---|
| `id` | text pk | candidate id |
| `import_id` | text fk | source_imports.id |
| `candidate_name` | text | |
| `wechat_fakeid` | text nullable | |
| `biz` | text nullable | |
| `avatar_url` | text nullable | |
| `intro` | text nullable | |
| `score` | real | 0-1 confidence |
| `decision` | text | `auto_accept`, `manual_accept`, `reject`, `pending` |
| `raw_payload` | json nullable | original adapter candidate payload |
| `created_at` | timestamp | |

### `articles`

Article metadata, normally collected from list APIs.

| column | type | notes |
|---|---:|---|
| `id` | text pk | stable article id; prefer aid/mid+idx hash |
| `source_id` | text fk | sources.id |
| `title` | text | |
| `url` | text unique | canonical article URL |
| `digest` | text nullable | list摘要 |
| `cover_url` | text nullable | |
| `publish_time` | timestamp nullable | |
| `crawl_status` | text | `metadata_only`, `content_ok`, `content_failed`, `deleted` |
| `retention_level` | text | `metadata`, `content`, `full_archive` |
| `archive_status` | text | `not_requested`, `pending`, `cached`, `failed` |
| `retention_reason` | text nullable | policy or LLM reason for current retention level |
| `raw_payload` | json nullable | original list item |
| `created_at` | timestamp | |
| `updated_at` | timestamp | |

### `article_contents`

Full text/HTML extraction result.

| column | type | notes |
|---|---:|---|
| `article_id` | text pk/fk | articles.id |
| `content_html` | text nullable | cleaned HTML |
| `content_text` | text nullable | plain text |
| `content_markdown` | text nullable | optional markdown |
| `content_structure` | json nullable | ordered text/image/video blocks for reconstructing article narrative |
| `fetch_error` | text nullable | environment check/deleted/etc. |
| `extracted_at` | timestamp | |

### `article_assets`

Images, video metadata, and other embedded assets.

| column | type | notes |
|---|---:|---|
| `id` | text pk | asset id |
| `article_id` | text fk | articles.id |
| `asset_type` | text | `image`, `video`, `audio`, `iframe`, `file` |
| `url` | text | remote URL |
| `block_index` | integer nullable | index in `article_contents.content_structure` |
| `content_ref` | text nullable | stable reference such as `block:12` |
| `local_path` | text nullable | cached file path |
| `metadata` | json nullable | cover, dimensions, platform, etc. |
| `download_status` | text | `url_only`, `cached`, `failed`, `unsupported` |
| `created_at` | timestamp | |

### `classifications`

Source/article category and tags.

| column | type | notes |
|---|---:|---|
| `id` | text pk | |
| `entity_type` | text | `source` or `article` |
| `entity_id` | text | source/article id |
| `taxonomy` | text | e.g. `default`, `finance` |
| `category` | text | primary category |
| `tags` | json | list of tags |
| `confidence` | real | 0-1 |
| `method` | text | `rules_v1`, `llm:<agent-or-model>`, `manual` |
| `created_at` | timestamp | |

### `source_classification_reviews`

Pending LLM/account classification suggestions that require user confirmation before becoming formal source classifications.

| column | type | notes |
|---|---:|---|
| `id` | text pk | review id |
| `source_id` | text fk | sources.id |
| `taxonomy` | text | selected taxonomy |
| `category` | text | closest existing source category id |
| `source_attribute` | text nullable | closest existing source attribute id |
| `tags` | json | existing tag ids only |
| `confidence` | real | 0-1 |
| `method` | text | `llm:<agent-or-model>` |
| `reason` | text nullable | user-facing rationale |
| `taxonomy_suggestions` | json | proposed new category/attribute/tag items |
| `status` | text | `pending`, `confirmed`, `rejected` |
| `raw_payload` | json nullable | original LLM result |
| `created_at` | timestamp | |
| `updated_at` | timestamp | |

### `digests`

Summaries and scoring results.

| column | type | notes |
|---|---:|---|
| `id` | text pk | |
| `article_id` | text fk | articles.id |
| `summary` | text | |
| `key_points` | json nullable | bullet points |
| `importance_score` | real | 0-1 |
| `score_breakdown` | json nullable | rubric sub-scores used to derive the importance score |
| `application_targets` | json nullable | downstream routing targets such as daily digest, weekly report, strategy backlog, or risk monitoring |
| `reason` | text nullable | why it matters |
| `model` | text nullable | model/provider label |
| `analysis_stage` | text | `metadata`, `content`, or `rules` |
| `created_at` | timestamp | |

`importance_score` drives article retention. Metadata-stage scores prioritize content fetches and should favor recall. The system computes metadata-stage fetch actions from the formula score plus deterministic low-signal overrides, so agents should not return free-form triage decisions. Content-stage scores take precedence for final digest, routing, and retention. Rules scores are a fallback. For LLM imports, the stored score is computed from `score_breakdown` rather than trusting a free-form holistic score. `score_breakdown` keeps the rubric evidence, the computed score, and the agent's reference holistic score so scoring rules can be reviewed and adjusted later. `application_targets` route content-stage articles into daily digest, weekly report, strategy backlog, market view, industry tracking, risk monitoring, source-quality review, or ignore workflows. Default thresholds are expected to evolve; monitor digest/archive false positives, missed useful articles, storage cost, and user feedback before changing them.

### `delivery_logs`

Push/export history.

| column | type | notes |
|---|---:|---|
| `id` | text pk | |
| `target` | text | discord/rss/webhook/file/etc. |
| `entity_type` | text | `article`, `digest`, `batch` |
| `entity_id` | text | |
| `status` | text | `sent`, `skipped`, `failed` |
| `payload` | json nullable | delivery metadata |
| `created_at` | timestamp | |

## Indexes

- `sources(wechat_fakeid)` lookup index where not null
- `sources(biz)` lookup index where not null
- `articles(source_id, publish_time)`
- `articles(url)` unique
- `source_candidates(import_id, score)`
- `classifications(entity_type, entity_id, taxonomy)`
- `source_classification_reviews(source_id, taxonomy, status)`

## Review policy

- Imported names first land in `source_imports`.
- Adapter search results land in `source_candidates`.
- Only accepted rows are promoted into `sources`.
- Exact high-score matches may be auto-accepted only when the active workflow allows it.
- For finance onboarding, `review auto-exact --finance-only` requires both exact name match and finance taxonomy evidence.
- Non-exact, non-finance, low-confidence, or unresolved rows stay pending for LLM/manual review.
- LLM onboarding results can supersede earlier decisions. If a later `ignore_non_finance` or `reject_all` result applies to an import whose candidate had already been accepted, the linked source is archived so it no longer participates in collection.

## Onboarding export

`export onboarding` is a joined review view over existing tables:

- `source_imports` provides OCR/list names and batch identity.
- `source_candidates` provides best candidate, fakeid/biz, score, intro, and decision.
- For reviewed onboarding imports, the final `matched_account` / `匹配账号` column is the identity source of truth. That final table already includes user confirmations and corrections. `ocr_account` is audit-only.
- A reviewed row may only import a `fakeid` when that `fakeid` came from the final matched-account candidate. If the final matched account and latest candidate name differ, re-run search or regenerate the final table before importing it as collectable.
- `sources` provides accepted active/inactive state and tier.
- `articles` provides latest article title, digest, URL, and publish time when available.
- candidate `raw_payload.article_probe` provides latest article evidence for candidates waiting for acceptance.
- `classifications` provides rule/LLM/manual category, confidence, tags, and method.

Recommended columns:

| column | notes |
|---|---|
| `ocr_name` | raw OCR/list name |
| `best_candidate_name` | best resolved candidate |
| `candidate_intro` | account intro returned by adapter, if available |
| `exact_match` | normalized account-name match |
| `display_exact_match` | raw display text equality |
| `match_type` | `exact`, `normalized`, `similar`, `different`, or `unresolved` |
| `name_similarity` | normalized name similarity score |
| `source_status` | active/inactive/archived/needs_review when accepted |
| `latest_publish_time` | newest collected article time |
| `latest_article_title` | newest collected article title |
| `latest_probe_status` | `candidate_latest_ok`, `candidate_latest_empty`, `candidate_latest_failed`, `source_latest_ok`, or `missing` |
| `latest_probe_refreshed` | whether latest evidence exists for the row |
| `classification_category` | source category under taxonomy |
| `classification_method` | `rules_v1`, `llm:*`, or `manual` |
| `llm_review_category` | coarse LLM onboarding category shown to users |
| `llm_review_action` | LLM onboarding action, e.g. `accept_source`, `ignore_non_finance`, `needs_manual_review`, `reject_all` |
| `llm_requires_user_confirmation` | LLM's manual-confirmation flag |
| `is_finance_candidate` | current finance relevance decision |
| `needs_manual_review` | whether the system still wants user confirmation |
| `recommended_action` | `accepted_finance`, `accept_finance_candidate`, `review_candidate`, `review_unresolved`, `ignore_non_finance`, etc. |

Compact review exports use a stricter user-facing match policy:

- only `exact` and `normalized` rows populate `matched_account`;
- `similar`, `different`, and `unresolved` rows populate `candidate_account` and require manual confirmation;
- when LLM review exists, the compact `requires_manual_confirmation` field follows the LLM flag unless the name match is non-strict.

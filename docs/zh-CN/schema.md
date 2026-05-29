# 存储 Schema

`wechat-mp-feed` 默认使用本地 SQLite。后续如需更重的分析工作流，可以再增加 DuckDB 等分析型存储。

## 设计目标

- 原始导入数据和确认后的规范数据分开保存。
- 不确定账号匹配保留审核记录。
- 文章元数据和正文/图片资产分开保存。
- 通用 feed 字段和金融增强字段分层保存。

## 核心表

### `sources`

确认后的微信公众号来源。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | text pk | 内部稳定 id |
| `platform` | text | 默认 `wechat_mp` |
| `name` | text | 规范账号名 |
| `wechat_fakeid` | text nullable | downloader 使用的 fakeid/base64 id |
| `biz` | text nullable | 文章 URL 中的 `__biz` |
| `avatar_url` | text nullable | 头像 URL |
| `intro` | text nullable | 账号简介 |
| `status` | text | `active`、`inactive`、`archived`、`needs_review` |
| `tier` | text | `core`、`normal`、`long_tail` |
| `source_type` | text | `ocr`、`csv`、`json`、`article_url`、`manual`、`api` |

### `source_imports`

账号解析前的原始导入行。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | text pk | 导入项 id |
| `batch_id` | text | 导入批次 |
| `raw_name` | text nullable | OCR/CSV/list 名称 |
| `raw_url` | text nullable | 文章或账号 URL |
| `raw_payload` | json | 原始行、OCR 元数据或其他证据 |
| `source_type` | text | `ocr`、`csv`、`json`、`article_url`、`manual` |
| `status` | text | `pending`、`resolved`、`ignored`、`error` |

### `source_candidates`

搜索返回的候选公众号。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | text pk | 候选 id |
| `import_id` | text fk | 对应 `source_imports.id` |
| `candidate_name` | text | 候选账号名 |
| `wechat_fakeid` | text nullable | 候选 fakeid |
| `biz` | text nullable | 候选 `__biz` |
| `avatar_url` | text nullable | 头像 URL |
| `intro` | text nullable | 简介/签名 |
| `score` | real | 0-1 匹配分 |
| `decision` | text | `auto_accept`、`manual_accept`、`reject`、`pending` |
| `raw_payload` | json nullable | 原始 adapter payload |

### `articles`

文章元数据。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | text pk | 稳定文章 id |
| `source_id` | text fk | 对应来源 |
| `title` | text | 标题 |
| `url` | text unique | 原文链接 |
| `digest` | text nullable | 列表摘要 |
| `cover_url` | text nullable | 封面 URL |
| `publish_time` | timestamp nullable | 发布时间 |
| `crawl_status` | text | `metadata_only`、`content_ok`、`content_failed`、`deleted` |
| `retention_level` | text | `metadata`、`content`、`full_archive` |
| `archive_status` | text | `not_requested`、`pending`、`cached`、`failed` |

### `article_contents`

正文提取结果。

| 字段 | 类型 | 说明 |
|---|---|---|
| `article_id` | text pk/fk | 对应文章 |
| `content_html` | text nullable | 清洗后的 HTML |
| `content_text` | text nullable | 纯文本 |
| `content_markdown` | text nullable | Markdown |
| `content_structure` | json nullable | 按原文顺序排列的 text/image/video blocks |
| `fetch_error` | text nullable | 抓取失败原因 |
| `extracted_at` | timestamp | 提取时间 |

### `article_assets`

正文中的图片、视频和其他资产。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | text pk | 资产 id |
| `article_id` | text fk | 对应文章 |
| `asset_type` | text | `image`、`video`、`audio`、`iframe`、`file` |
| `url` | text | 远程 URL |
| `block_index` | integer nullable | 在 `content_structure` 中的位置 |
| `content_ref` | text nullable | 稳定引用，例如 `block:12` |
| `local_path` | text nullable | 本地缓存路径 |
| `metadata` | json nullable | 尺寸、封面、平台、归档决策、启用 OCR 时的图片文字等元数据 |
| `download_status` | text | `url_only`、`cached`、`skipped`、`failed`、`unsupported` |

### `classifications`

账号或文章分类结果。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | text pk | 分类记录 id |
| `entity_type` | text | `source` 或 `article` |
| `entity_id` | text | source/article id |
| `taxonomy` | text | 例如 `default`、`finance` |
| `category` | text | 主分类 |
| `tags` | json | 多标签 |
| `confidence` | real | 0-1 置信度 |
| `method` | text | `rules_v1`、`llm:<agent-or-model>`、`manual` |

### `source_classification_reviews`

首次 LLM 账号分类建议。用户确认前，这里只代表建议，不代表正式账号分类。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | text pk | review id |
| `source_id` | text fk | 对应来源 |
| `taxonomy` | text | 使用的 taxonomy |
| `category` | text | 最接近的现有账号主分类 |
| `source_attribute` | text nullable | 最接近的现有来源属性 |
| `tags` | json | 现有标签 id |
| `confidence` | real | 0-1 置信度 |
| `method` | text | `llm:<agent-or-model>` |
| `reason` | text nullable | 推荐理由 |
| `taxonomy_suggestions` | json | 建议新增的分类、来源属性或标签 |
| `status` | text | `pending`、`confirmed`、`rejected` |
| `raw_payload` | json nullable | 原始 LLM 结果 |

### `digests`

摘要和评分结果。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | text pk | 摘要 id |
| `article_id` | text fk | 对应文章 |
| `summary` | text | 摘要 |
| `key_points` | json nullable | 要点 |
| `importance_score` | real | 0-1 重要性分数 |
| `score_breakdown` | json nullable | 评分 rubric 的分项分数 |
| `application_targets` | json nullable | 后续应用路由，例如日度摘要、周报、策略池或风险监控 |
| `reason` | text nullable | 重要性理由 |
| `model` | text nullable | 模型或 agent 标识 |
| `analysis_stage` | text | `metadata`、`content` 或 `rules` |

`importance_score` 用于文章保存层级和摘要（digest）选择。metadata 阶段分数用于正文抓取优先级，目标偏召回；系统根据公式分和确定性的低信号覆盖规则计算正文抓取动作，agent 不应自由返回 triage 决策。content 阶段分数用于最终摘要（digest）、路由和保存层级，并优先于 metadata 阶段分数。rules 分数作为 fallback。LLM 导入时，最终保存分数按 `score_breakdown` 公式计算，不直接信任自由综合分。`score_breakdown` 会保留分项评分、公式分和 agent 综合参考分，方便后续按误收、漏收、存储成本和用户反馈调整评分规则。`application_targets` 用于把文章路由到日度摘要、周报、策略池、市场观点、产业跟踪、风险监控、来源质量评估或忽略流程。

## 审核策略

- 原始名称先进入 `source_imports`。
- 搜索候选进入 `source_candidates`。
- 只有审核通过或严格匹配的账号进入 `sources`。
- 金融 onboarding 中，`review auto-exact --finance-only` 同时要求名称严格匹配和金融分类证据。
- 非严格匹配、低置信度、非金融或未解析行保留给 LLM/人工审核。

## Onboarding 导出

`export onboarding` 是基于导入项、候选、来源、文章证据和分类结果生成的审核视图。

关键规则：

- 最终 `matched_account` / `匹配账号` 是后续抓取的账号身份依据。
- `ocr_account` 只作为审计证据。
- `similar`、`different`、`unresolved` 行进入候选账号列，等待人工确认。
- 简介和最新文章用于分类证据，不放宽账号身份匹配。

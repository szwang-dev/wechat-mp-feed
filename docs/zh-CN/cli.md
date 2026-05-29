# CLI 参考

命令名：`mpfeed`。

## 基本原则

- 命令可组合，默认写入本地文件和 SQLite。
- 导入、搜索、审核、采集分步骤执行，方便用户检查不确定匹配。
- 需要登录态的 downloader adapter 由用户显式配置。
- 大批量真实运行使用保守限频。

## 全局参数

```bash
mpfeed --db ./data/mpfeed.sqlite <command>
```

部分命令支持配置文件，例如 `run feed --config` 会读取 feed JSON 配置。

## `demo`

生成合成示例数据并离线导出 feed：

```bash
mpfeed --db ./work/demo-feed.sqlite demo seed-feed --work-dir ./work/demo-feed
mpfeed run feed --config ./work/demo-feed/feed-config.demo.json
```

输出：

```text
feed-items.csv
feed-summary.json
feed-failures.csv
```

## `import`

导入公众号线索。

```bash
mpfeed import csv accounts.csv --name-column name
mpfeed import json accounts.json --name-field name
mpfeed import urls article_urls.txt
mpfeed import url 'https://mp.weixin.qq.com/s?...'
```

截图/录屏 OCR：

```bash
mpfeed import images screenshots/*.png --ocr paddle
mpfeed import video wechat_accounts.mp4 --fps 2 --ocr paddle --min-occurrences 2 --names-output accounts.txt --raw-output ocr.json
mpfeed import video following.mp4 --fps 0.5 --ocr paddle --crop 220,0,900,2556 --scale-width 480 --names-output accounts.txt --raw-output ocr.json
```

OCR/视频依赖按需安装：

```bash
python3 -m pip install -e "packages/wechat_mp_feed[ocr]"
```

手机录屏 OCR 建议裁剪账号文字区域并降低帧图宽度：

```bash
--crop x,y,w,h
--scale-width 480
```

`--scale-width 480` 适合容器或 agent 运行时的快速验证；`--scale-width 720` 保留更多细节，适合正式全量接入。

## `resolve`

把导入名称或 URL 解析为公众号候选。

```bash
mpfeed resolve imports --source-type csv --limit 100
mpfeed resolve imports --source-type recording --query-variants --retry-empty
mpfeed resolve search '第一财经'
```

`--query-variants` 会追加规范化查询，用于处理 OCR 空格、标点、繁简或轻微字形差异。

## `review`

审核搜索候选并写入正式来源库。

```bash
mpfeed review list
mpfeed review accept <candidate_id> --tier core
mpfeed review reject <candidate_id>
mpfeed review apply reviewed.csv
mpfeed review auto-exact --source-type recording --finance-only --taxonomy finance
```

`review auto-exact --finance-only` 只自动提升名称严格匹配且金融相关的候选。其他行保留给 LLM 或人工审核。

## `export onboarding`

导出首次批量接入审核表。

```bash
mpfeed export onboarding --source-type recording --taxonomy finance --format csv > work/onboarding.csv
mpfeed export onboarding --source-type recording --view compact --taxonomy finance --format csv > work/onboarding-review.csv
```

审核表包含：

- OCR/list 名称；
- 匹配账号；
- 候选账号；
- 匹配类型；
- 简介和最新文章证据；
- 金融分类；
- 是否需要人工确认；
- 人工确认账号名、文章链接、分类和备注列。

## `archive`

缓存高价值文章的图片资产：

```bash
mpfeed archive assets --output-dir work/archive/assets --limit 100
```

该命令只处理 `retention_level=full_archive` 的文章图片，默认在写入本地文件前过滤二维码、小图标、分隔线、小 logo、低复杂度装饰图、推广图和模板图。保留图片会更新 `article_assets.local_path` 和 `download_status=cached`；过滤图片会写入 `download_status=skipped`，跳过原因保存在 `article_assets.metadata.archive_decision`。图片在原文中的顺序通过 `block_index` / `content_ref` 保留。需要保存全部图片时使用 `--no-filter`；已安装 OCR extras 时，可以用 `--asset-filter-ocr paddle` 增加图片文字识别信号。

## `llm`

导出/导入 agent-agnostic LLM jobs。

```bash
mpfeed llm export-onboarding-jobs --source-type recording --taxonomy finance --output work/onboarding-jobs.json
mpfeed llm import-results work/onboarding-results.json --model llm:agent

mpfeed llm export-jobs --entity-type article --taxonomy finance --output work/article-llm-jobs.json
mpfeed llm export-jobs --entity-type article --article-content-scope metadata --output work/article-score-jobs.json
mpfeed llm export-jobs --entity-type article --article-updated-after 2026-05-20T00:00:00+00:00 --output work/article-score-jobs.json
mpfeed llm export-jobs --entity-type article --article-content-scope metadata --article-analysis-stage metadata --article-published-after 2026-05-11T00:00:00+08:00 --article-published-before 2026-05-17T23:59:59+08:00 --output work/article-metadata-jobs.json
mpfeed llm export-jobs --entity-type article --article-content-scope content --article-analysis-stage content --article-retention content_or_archive --output work/article-content-jobs.json
mpfeed llm export-jobs --entity-type source --source-article-limit 8 --output work/source-classification-jobs.json
mpfeed llm export-jobs --entity-type source --source-id mp_xxx --source-article-limit 8 --output work/source-intake-job.json
mpfeed llm import-results work/article-llm-results.json --model llm:agent
mpfeed export digests --application-target weekly_report --analysis-stage content --min-score 0.6 --format markdown
mpfeed export digest-context --application-target weekly_report --analysis-stage content --min-score 0.6 --format json
```

这个入口同时服务账号分类和文章分析：

- 账号分类任务会带上账号信息和最近文章证据，供 agent/LLM 做语义判断；`--source-id` 可把任务限定到单个账号，用于单账号 intake。
- 文章任务支持正文评分，也支持 metadata-only 初筛，用于正文抓取前的初步重要性判断。
- 文章任务会带上 `article_score_rubric`，agent 应先返回摘要评分字段 `digest.score_breakdown`，再给出综合参考分 `importance_score`。导入时保存按分项公式计算出的最终分数，LLM 的综合分会作为校验参考保存在 `score_breakdown.model_importance_score`。
- `--article-updated-after` 可以把文章任务限定到本轮刷新过的文章，避免日更反复重评历史文章。
- `--article-published-after`、`--article-published-before` 和 `--article-retention` 可以按发布时间窗口和保存层级导出任务。
- `mpfeed export digests` 可以按 `application_targets`、分数和分析阶段导出摘要包（digest pack）。
- `mpfeed export digest-context` 可以按同样条件导出最终应用层上下文，包含摘要、原文正文、结构化正文和图片资产；最终周报或深度摘要（digest）应优先读取这个上下文，而不是只读取二级摘要。
- 导入结果会校验 taxonomy；未知分类和未知标签不会直接落库。
- 来源属性可以通过 `source_attribute` 返回，也可以放在 `classification.tags` 中；导入时会统一落到 tags。
- 首次账号分类应设置 `requires_user_confirmation=true`；导入时会保存为待确认的 `source_classification_reviews`，等用户通过审核表或 `mpfeed source confirm-classification` 确认后再落库。确认时可传 `--review-id` 标记这条建议已确认。
- 如需新增分类、来源属性或标签，LLM 应使用最接近的现有 id，并在 `taxonomy_suggestions` 中结构化给出新增建议，不能直接发明正式 id。
- 文章 LLM 分数分阶段保存：`analysis_stage=metadata` 用于正文抓取前的初筛排序，`analysis_stage=content` 用于最终摘要（digest）和保存层级；两者同时存在时优先使用 content 分，`rules` 分只是 fallback。
- 文章评分使用显式 rubric：研究含量、决策价值、策略可复现性、时效价值、来源相关性、原创/稀缺性、证据完整度和噪声惩罚。rubric 会随任务一起导出，后续可根据误收、漏收和用户反馈调整。
- `rules_v1` 保留为本地 fallback，正式 agent 工作流应优先使用 LLM 导入的重要性分数。

## `run llm-feed`

推进两阶段语义 feed 流程，同时保留 LLM 审核边界。

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

行为：

- 每次都会写出 `article-metadata-jobs.json`
- 传入 `--metadata-results` 后，会导入 metadata 分数；如同时传入 `--fetch-content`，会抓取 retained 正文，并写出 `article-content-jobs.json`
- 传入 `--content-results` 后，会导入最终 content 分数，并在 `digest-packs/` 下导出不同应用目标的摘要包（digest pack）
- LLM 执行仍由外部 agent 完成，Codex、Claude Code 或其他 agent 都可以处理 JSON jobs

账号分类输出应包含：

- `inclusion_tier`
- `classification.category`
- `source_attribute`
- `tags`
- `needs_manual_review`

文章分析输出应包含分类、摘要、重要性分数和原因。

## `source`

面向 agent 的单账号安全接口。

```bash
mpfeed source status --name "示例科技观察"
mpfeed source set-status --name "示例科技观察" --status inactive
mpfeed source taxonomy --taxonomy finance
mpfeed source confirm-classification \
  --name "示例科技观察" \
  --category industrial \
  --source-attribute kol \
  --tags ai \
  --confidence 0.78
mpfeed source refresh-latest --name "示例科技观察" --count 5 --base-url http://127.0.0.1:5001
```

规则：

- `source status` 和 `source taxonomy` 只读。
- `source set-status` 是 agent 安全写入口；退订使用 `inactive`，明确重新关注使用 `active`，长期移除使用 `archived`，需要人工判断使用 `needs_review`。
- `source confirm-classification` 会校验 taxonomy，写入用户确认后的来源分类，并返回回读结果。
- `source refresh-latest` 只刷新一个 active source 的最近文章元数据。
- 不在 taxonomy 中的 id 会被拒绝，应作为建议项返回给用户，而不是写入正式分类。

## `collect`

采集文章列表和正文。

```bash
mpfeed collect latest --tier core --count 10 --delay-min 3 --delay-max 8
mpfeed collect history --tier all --days 90 --count 100 --max-sources 20 --delay-min 3 --delay-max 8
mpfeed collect content --tier core --limit 20 --delay-min 3 --delay-max 8
```

`collect history` 是首次 onboarding 或大批量新增账号后的最后阶段。它通过 downloader 的 `fakeid + begin/count` 分页回补近期文章元数据，按时间窗口、接口 `total_count`、短页或安全页数上限停止。大批量运行时用 `--source-offset` 和 `--max-sources` 分批续跑。

正文抓取会保存 text/html/markdown、正文结构、图片 URL 和图片在正文中的位置。

## `run onboarding`

首次批量接入的统一入口。

```bash
mpfeed run onboarding \
  --work-dir ./work/onboarding \
  --source-type onboarding \
  --video-file following.mp4 \
  --crop 220,0,900,2556 \
  --scale-width 480
```

流程：

```text
导入
-> 多轮搜索
-> 慢速重试空候选
-> latest article evidence
-> 导出 LLM onboarding jobs
-> 导入 LLM 结果
-> 导出 compact review table
-> 应用审核表
-> 回补近 90 天文章元数据
```

审核表完成修改后，使用 review/apply 或后续 resolver 处理改动部分。

## `run feed`

第一层 feed 运行入口。

```bash
mpfeed run feed --config ./work/feed-config.json
```

`full: true` 会刷新文章列表、评分、抓取 retained 正文并导出：

```text
feed-items.csv/json
feed-summary.json/csv
feed-failures.csv/json
```

`--progress-every N` 会在文章列表刷新和正文抓取时定期输出进度。定时任务建议保留进度输出，避免长时间无日志被误判为卡住。

文章列表刷新支持两种模式：

- `--refresh-mode latest`：每个公众号只抓取最新一页。
- `--refresh-mode incremental`：从最新页开始向后翻，直到遇到数据库里该公众号的最新已知文章，或达到 `--incremental-max-pages` 安全上限。

`--count` 是 downloader 的分页大小。对微信公众号来说，它可能对应最近发布批次；一个发布批次可以包含多篇文章，入库时会展开成多条文章元数据。

## `run daily`

稳定日更 runner，面向定时任务和 agent 监督。

```bash
mpfeed --db ./work/feed.sqlite run daily \
  --date 2026-05-25 \
  --base-url http://127.0.0.1:5001 \
  --work-dir ./work/daily-feed
```

命令会写入日期目录：

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

状态含义：

- `RUNNING`：代码仍在执行，agent 应继续观察 `progress.ndjson`，不要直接判定失败。
- `WAITING_FOR_METADATA_LLM`：文章元数据刷新完成，已导出元数据阶段 LLM 任务；agent 完成 `article-metadata-llm-jobs.json` 后，用 `--metadata-results` 继续。
- `WAITING_FOR_CONTENT_LLM`：正文抓取完成，已导出正文阶段 LLM 任务；agent 完成 `article-llm-jobs.json` 后，用 `--content-results` 继续。
- `DONE`：摘要包和上下文导出完成。
- `LOGIN_REQUIRED`、`DOWNLOADER_UNREACHABLE`、`REFRESH_CIRCUIT_BREAKER`、`FAILED`：需要 agent 或调度层处理的失败状态。

恢复示例：

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

运行约定：

- runner 属于 `wechat-mp-feed`，不放进 downloader。downloader 只作为可替换 HTTP adapter。
- 本地、Docker 和 agent 调用同一条 CLI；Docker 场景需要挂载持久化 `work/` 目录。
- 图片 OCR 默认关闭。高成本图片复核应作为独立任务，不进入每日 feed 主链路。
- agent 应读取 `run-manifest.json` 和 `progress.ndjson`；只要 `RUNNING` 仍有进展，就不应输出失败。

## `run agent-smoke`

离线验证 agent 是否能够运行 feed 层并读取输出。

```bash
mpfeed run agent-smoke --work-dir ./work/agent-smoke
```

输出：

```text
agent-smoke.sqlite
feed-items.csv
feed-summary.json
feed-failures.csv
article-llm-jobs.json
agent-smoke-report.md
```

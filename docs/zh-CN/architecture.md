# 架构设计

`wechat-mp-feed` 的核心定位是微信公众号长期跟踪的编排层。Feed 层负责来源接入、文章更新、正文提取和本地存储；项目通过 HTTP adapter 调用常驻下载服务，再把结果沉淀为可审核、可调度、可摘要的本地 feed 数据。具体业务行为由分类体系、评分规则、标签和应用目标决定，内置金融投研领域包可按实际研究目标调整或替换。

## 总览图

```mermaid
flowchart TB
  User[用户] --> Agent[Agent / Cron]
  Agent --> Skill[wechat-mp-feed Skill]
  Skill --> CLI[mpfeed CLI]
  CLI --> Package[wechat_mp_feed Python Package]

  Package --> Importers[Importers<br/>CSV / JSON / URL / OCR]
  Package --> Resolver[Resolver + Review Queue]
  Package --> Onboarding[Onboarding Review Table]
  Package --> Scheduler[Scheduler / Tier Policy]
  Package --> Adapter[HTTP Adapter]
  Package --> Storage[(SQLite Storage)]
  Package --> Classifier[Classifier / Scoring]
  Package --> Digest[摘要 / 分发（Delivery）]

  Adapter --> WDA[wechat-download-api<br/>HTTP Service]
  WDA --> WeChat[WeChat MP Backend<br/>Article Pages]

  Importers --> Storage
  Resolver --> Storage
  Onboarding --> Storage
  Scheduler --> Storage
  Classifier --> Storage
  Digest --> Storage

  Digest --> Agent
```

## 模块职责

| 模块 | 职责 |
|---|---|
| Agent skill | 告诉 agent 如何安全调用 `mpfeed`，如何检查服务状态，何时请求用户确认 |
| `mpfeed` CLI | 提供可脚本化命令，供用户、cron、agent 调用 |
| Importers | 导入 CSV/JSON 账号、文章 URL、录屏 OCR，保留原始输入 |
| Resolver | 调用 HTTP 服务搜索公众号，把结果写入候选队列 |
| Review Queue | `review list/accept/reject`，把候选晋升到长期来源库 |
| Onboarding Review | 导出首次录入审核表，合并 OCR 名、候选、简介、最新文章、LLM 分类和推荐动作 |
| Source Registry | 保存长期跟踪的公众号来源、fakeid、`__biz`、tier、状态 |
| HTTP Adapter | 通过 base URL 调用外部下载服务 |
| Collector | 从长期来源库拉取文章列表并写入 `articles` |
| Extractor | 获取正文、图片元数据并写入内容表 |
| Rate Limit / Backoff | 按 tier 控制默认来源数、文章数、间隔和重试 |
| 领域包 | 提供分类体系、评分规则、标签、来源属性和应用目标；内置金融投研领域包 |
| 分类 / 摘要 | 规则版分类、重要性评分、摘要（digest）；LLM job export/import |

## 数据流：导入与审核

```mermaid
sequenceDiagram
  participant U as 用户 / Agent
  participant CLI as mpfeed CLI
  participant A as HTTP Adapter
  participant S as wechat-download-api
  participant DB as SQLite

  U->>CLI: mpfeed resolve search "第一财经"
  CLI->>A: search_sources(query)
  A->>S: GET /api/public/searchbiz?query=...
  S-->>A: candidates
  A-->>CLI: raw response
  CLI->>CLI: normalize_source_candidates()
  CLI->>DB: insert source_imports
  CLI->>DB: insert source_candidates

  U->>CLI: mpfeed review list
  CLI->>DB: select pending candidates
  DB-->>CLI: candidates

  U->>CLI: mpfeed review accept <candidate_id> --tier core
  CLI->>DB: insert/update sources
  CLI->>DB: mark candidate manual_accept
  CLI->>DB: mark import resolved
```

这条链路保证所有长期来源都有来源记录和审核记录；不确定账号进入审核表。

## 数据流：首次录入大批量公众号

```mermaid
sequenceDiagram
  participant U as 用户
  participant CLI as mpfeed CLI
  participant OCR as PaddleOCR / ffmpeg
  participant A as HTTP Adapter
  participant S as wechat-download-api
  participant DB as SQLite
  participant L as LLM / Agent

  U->>CLI: import video / import names / import csv
  CLI->>OCR: 抽帧 + OCR + 清洗
  CLI->>DB: insert source_imports

  CLI->>A: resolve imports
  A->>S: search account names
  S-->>A: candidates + intro + fakeid
  CLI->>DB: insert source_candidates

  CLI->>A: collect candidate-latest
  A->>S: list latest articles by candidate fakeid
  CLI->>DB: save article_probe into source_candidates.raw_payload

  CLI->>DB: export onboarding / llm export-onboarding-jobs
  DB-->>CLI: OCR 名 / 候选 / 简介 / 最新文章 / 匹配状态
  CLI-->>L: onboarding jobs

  L-->>CLI: 分类、tier、是否人工确认
  CLI->>DB: import LLM results / review apply
  CLI->>DB: export strict compact review table
  CLI-->>U: onboarding-review.xlsx/csv
  U->>CLI: apply reviewed table
  CLI->>DB: sources marked active
  CLI->>A: collect history, 90-day metadata backfill
  A->>S: paged article list by fakeid
  CLI->>DB: save recent article metadata
```

首次录入写入顺序：

1. `source_imports`：原始 OCR/list 名称。
2. `source_candidates`：公众号搜索候选。
3. `sources`：人工、规则或 LLM 确认后的长期管理来源。
4. `articles`：首次接入完成后的近期文章元数据回补结果。

`export onboarding` 是这条链路的检查点，用于让用户看到每个候选的状态和证据。

当前 strict review 口径：

- `match_type=exact` 表示显示文本完全一致。
- `match_type=normalized` 表示忽略空格、大小写、常见繁简和标点后的规范化一致。
- `similar`、`different`、`unresolved` 不进入 `match账号`，只进入候选账号，并强制人工确认。
- LLM onboarding 可以接受、忽略、拒绝或标记人工确认；重新导入 LLM 结果时，若新判断为忽略/拒绝，会把此前误接纳的关联 source 标记为 `archived`。

## 数据流：语义 feed 层

```mermaid
flowchart TB
  A["增量文章元数据"] --> B["metadata 阶段 LLM jobs"]
  B --> C["分项公式评分 + 低信号覆盖规则"]
  C --> D["正文抓取队列"]
  D --> E["正文 / HTML / 结构 / 图片资产"]
  E --> F["content 阶段 LLM jobs"]
  F --> G["最终评分 + 应用目标"]
  G --> H["摘要包（digest packs）"]
  G --> I["摘要上下文（digest context）"]
  I --> J["最终周报 / 研究 inbox / 策略池"]
```

语义 feed 层把“是否值得抓正文”和“最终如何使用”分开。metadata 阶段偏召回，用于筛选正文抓取队列；content 阶段读取正文后生成最终评分、应用目标、保存层级和摘要记录（digest records）。摘要上下文（`digest-context`）把最终摘要（digest）与原文正文、结构化内容和图片资产连接起来，避免应用层输出只依赖中间摘要。

## 数据流：长期采集目标

```mermaid
sequenceDiagram
  participant Cron as Cron / Agent
  participant CLI as mpfeed CLI
  participant DB as SQLite
  participant A as HTTP Adapter
  participant S as wechat-download-api
  participant D as 摘要 / 分发

  Cron->>CLI: mpfeed collect latest --tier core
  CLI->>DB: read active sources by tier
  CLI->>A: list_articles(fakeid, count, since)
  A->>S: GET /api/public/articles
  S-->>A: article metadata
  A-->>CLI: normalized article list
  CLI->>DB: upsert articles

  CLI->>A: fetch_article(url) for selected articles
  A->>S: POST /api/article
  S-->>A: content/assets
  CLI->>DB: upsert article_contents/assets

  CLI->>D: classify + 摘要生成
  D->>DB: write classifications/digests
  D-->>Cron: daily summary / exceptions
```

当前已实现 `collect latest` 的文章元数据采集，以及 `collect content` 的正文和图片 URL 元数据提取。视频元数据和更强的内容清洗仍是后续重点。

采集命令会按 `core/normal/long_tail` 使用不同默认策略，避免默认高频抓取。临时 429/5xx 会按 backoff 策略重试。

## 数据库核心表

```mermaid
erDiagram
  source_imports ||--o{ source_candidates : creates
  source_candidates }o--|| sources : accepted_into
  sources ||--o{ articles : publishes
  articles ||--|| article_contents : has
  articles ||--o{ article_assets : embeds
  sources ||--o{ classifications : classified_as
  articles ||--o{ classifications : classified_as
  articles ||--o{ digests : summarized_as
  articles ||--o{ delivery_logs : delivered_as

  source_imports {
    text id PK
    text batch_id
    text raw_name
    text raw_url
    json raw_payload
    text source_type
    text status
  }

  source_candidates {
    text id PK
    text import_id FK
    text candidate_name
    text wechat_fakeid
    text biz
    real score
    text decision
    text intro
  }

  sources {
    text id PK
    text name
    text wechat_fakeid
    text biz
    text status
    text tier
  }

  articles {
    text id PK
    text source_id FK
    text title
    text url
    text publish_time
    text crawl_status
  }
```

其中：

- `source_candidates.intro` 保存搜索返回的公众号简介，能辅助来源分类。
- `source_candidates.raw_payload.article_probe` 可保存未接受候选的最新文章探测结果。
- `articles.digest` 保存文章列表里的摘要或描述。
- `article_contents` 保存正文抓取结果；当简介不足以判断时，可以用最新文章标题、摘要或正文帮助 LLM 分类。
- `classifications.method` 区分 `rules_v1`、`llm:<agent>`、`manual`，方便后续覆盖或审计。

## 部署形态

```mermaid
flowchart LR
  subgraph Host[宿主机 / NAS / VPS]
    DB[(Private SQLite)]
    WDA[wechat-download-api Service]
  end

  subgraph AgentRuntime[Agent Runtime]
    OC[Agent Gateway]
    Agent[Agents / Cron]
    Skill[wechat-mp-feed Skill]
    CLI[mpfeed CLI]
  end

  Agent --> Skill
  Skill --> CLI
  CLI --> DB
  CLI -->|WECHAT_DOWNLOAD_API_BASE_URL| WDA
```

常见 `WECHAT_DOWNLOAD_API_BASE_URL`：

| 场景 | 示例 |
|---|---|
| 本机 agent 调本机服务 | `http://127.0.0.1:3000` |
| 容器内 agent 调宿主机服务 | `http://host.docker.internal:3000` |
| 同一 Docker network | `http://wechat-download-api:3000` |
| NAS/VPS 服务 | `https://mpfeed.example.com` |

## 设计边界

本项目负责：

- 来源库、候选队列和人工审核。
- 调度策略和分层抓取。
- 统一存储。
- 分类、摘要和投递。
- Agent 工作流。

底层下载服务负责：

- 登录态和 cookie。
- 公众号后台相关接口。
- fakeid / `__biz` 获取。
- 历史文章列表和文章内容获取。
- 服务健康状态。

该边界使 `wechat-mp-feed` 能够替换底层 adapter，并保持核心存储和 feed 逻辑稳定。

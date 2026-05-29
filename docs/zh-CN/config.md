# Feed 配置说明

`run feed --config` 使用 JSON 配置文件运行第一层文章流。示例文件是：

```text
examples/feed-config.example.json
```

命令行参数优先级高于配置文件。也就是说，日常运行可以固定用一份配置，临时测试时只在命令行覆盖少量参数。

## 基本结构

```json
{
  "storage": {
    "path": "./data/mpfeed.sqlite"
  },
  "downloader": {
    "base_url": "http://127.0.0.1:5001",
    "timeout": 30
  },
  "feed": {
    "tier": "all",
    "max_sources": 0,
    "count": 5,
    "full": true,
    "work_dir": "./work/feed"
  }
}
```

代码也支持把 CLI 参数名直接放在顶层，但面向用户的配置建议使用上面这种分组结构。

## 存储

| 字段 | 含义 |
|---|---|
| `storage.path` | SQLite 数据库路径，等价于 `--db`。真实数据库应放在仓库外或被 `.gitignore` 排除。 |

## Downloader

微信文章访问由用户自行运行并登录的下载服务处理。将 `wechat-mp-feed` 配置为通过 HTTP 调用该服务。本地开发时，仓库提供了一个辅助脚本，可启动外部 `wechat-download-api` 服务：

```bash
./scripts/bootstrap_wechat_download_api.sh
```

服务启动后，打开终端输出的登录地址；如需登录，使用微信扫码。然后配置服务地址：

```bash
export WECHAT_DOWNLOAD_API_BASE_URL=http://127.0.0.1:5000
```

| 字段 | 含义 |
|---|---|
| `downloader.base_url` | 外部下载服务地址，等价于 `--base-url`。 |
| `downloader.timeout` | HTTP 超时时间，单位秒，等价于 `--timeout`。 |

下载服务由用户自行部署和登录。登录态由下载服务或用户配置的本地环境管理；`wechat-mp-feed` 通过 base URL 和可选 token 调用已配置的服务。

默认适配器（adapter）面向 `wechat-download-api`；其他下载服务只要实现相同操作和响应结构，也可以接入 feed 层。必需操作和响应结构见 [下载服务 Adapter 契约](downloader-adapter.md)。

## Feed 阶段

### 刷新文章列表

| 字段 | 默认值 | 含义 |
|---|---:|---|
| `tier` | `all` | 要刷新的来源层级：`all`、`core`、`normal`、`long_tail`。 |
| `max_sources` | `0` | 最多刷新多少个 active source，`0` 表示全部。 |
| `count` | `5` | 每个公众号拉取多少篇文章元数据。 |
| `begin` | `0` | 文章列表起始偏移。 |
| `retries` | `2` | 文章列表请求重试次数。 |
| `backoff_seconds` | `3.0` | 文章列表重试间隔。 |
| `delay_min` | `1.0` | 公众号之间的最小等待秒数。 |
| `delay_max` | `3.0` | 公众号之间的最大等待秒数。 |
| `no_delay` | `false` | 仅用于本地测试，关闭等待。 |

### 运行模式

| 字段 | 默认值 | 含义 |
|---|---:|---|
| `full` | `false` | 刷新文章列表、评分、抓取 retained 正文、导出文件。 |
| `skip_refresh` | `false` | 不调用 downloader，只从现有数据库导出。 |
| `score_articles` | `false` | 只额外执行文章评分。 |
| `fetch_retained_content` | `false` | 只额外抓取 retained 正文。 |

完整 feed 运行使用 `full: true`。已有抓取结果、仅需重新导出时，使用 `skip_refresh: true`。

### 评分

| 字段 | 默认值 | 含义 |
|---|---:|---|
| `taxonomy` | `finance` | 文章评分使用的分类体系。 |
| `score_limit` | `0` | 最多评分多少篇文章，`0` 表示全部。 |
| `min_score` | `0.0` | 保存 rules 摘要（digest）的最低分数。 |

`rules_v1` 是本地 fallback，用于初始保存层级。agent 工作流可以通过 `mpfeed llm export-jobs` 导出文章任务，再用 `mpfeed llm import-results` 导入 LLM 语义重要性分数；导入后的分数会影响保存层级和正文抓取优先级。LLM 分数分阶段保存：metadata 分用于正文抓取前排序，content 分在正文可用后优先用于最终摘要（digest）和保存层级。重要性分数和阈值后续需要结合误收、漏收、存储成本和用户反馈持续调整。

文章 LLM 任务分阶段处理。

元数据阶段（metadata stage）使用偏召回的正文抓取评分。agent 根据标题研究信号、来源相关性、摘要具体度、后续分析潜力、时效性、证据完整度和噪声惩罚打分。系统根据公式分和确定性的低信号覆盖规则计算正文抓取动作；agent 不再自由返回 `triage_decision`。metadata 分数的目标是避免误杀，而不是做最终投研判断。

正文阶段（content stage）使用最终投研价值评分。agent 应返回摘要评分字段 `digest.score_breakdown`，包括研究含量、策略可复现性、决策价值、时效价值、来源相关性、原创/稀缺性、证据完整度和噪声惩罚。`digest.importance_score` 可以作为 LLM 的综合参考分返回，但导入时会按 `score_breakdown` 公式计算并保存最终分数：

```text
score =
  研究含量 * 0.25
+ 决策价值 * 0.15
+ 策略可复现性 * 0.25
+ 时效价值 * 0.10
+ 来源相关性 * 0.10
+ 原创/稀缺性 * 0.10
+ 证据完整度 * 0.05
+ 噪声惩罚
```

这里的“研究含量”奖励分析深度，不奖励单纯篇幅长、材料多。“决策价值”指对后续研究、风险监控和中期配置思考有明确帮助，不奖励泛泛的启发感，也不要求文章具备即时交易意义。公众号文章天然有一定滞后，评分应更重视研究质量、策略逻辑和框架可复用性。这套评分规则（rubric）需要保持可审阅，后续根据误收、漏收和存储成本继续调整。

### 正文抓取

| 字段 | 默认值 | 含义 |
|---|---:|---|
| `content_limit` | `0` | 最多抓取多少篇 retained 文章，`0` 表示全部。 |
| `content_retention` | `content_or_archive` | 可抓取的保存层级：`content_or_archive`、`content`、`full_archive`、`all`。 |
| `content_retries` | `2` | 单篇正文抓取重试次数。 |
| `content_backoff_seconds` | `3.0` | 正文抓取重试间隔。 |
| `content_delay_min` | `3.2` | 正文请求之间的最小等待秒数。 |
| `content_delay_max` | `6.0` | 正文请求之间的最大等待秒数。 |
| `content_passes` | `3` | 对同一批待抓取文章做几轮 pass。 |
| `content_pass_cooldown_seconds` | `30.0` | 两轮 pass 之间的等待秒数。 |

正文抓取使用固定队列和多轮 pass。可抓取文章按重要性分数、保存层级、来源层级和发布时间排序。遇到正文接口返回类似 `请 N 秒后重试` 的提示时，会按提示等待后再继续。

默认保存阈值：

| 分数区间 | 处理方式 |
|---:|---|
| `>= 0.75` | `full_archive`：保存正文，并进入本地图片归档队列 |
| `0.42 - 0.75` | `content`：保存正文、结构和远端图片链接 |
| `< 0.42` | `metadata`：只保存文章元数据 |

摘要应用层（digest layer）可以再细分：`>= 0.75` 作为 full-archive 重点文章，`0.60 - 0.75` 作为重要日度摘要候选，`0.42 - 0.60` 作为普通跟踪。

正文阶段（content stage）还应返回摘要路由字段 `digest.application_targets`，用于把文章路由到后续应用：

| 应用目标（target） | 用途 |
|---|---|
| `daily_digest` | 日度 feed 摘要候选 |
| `weekly_report` | 周报材料 |
| `strategy_backlog` | 值得复现或测试的策略/模型思路 |
| `market_view` | 市场择时、风格、宏观、利率、信用或配置观点 |
| `industry_tracking` | 行业供需、政策、价格周期或公司跟踪 |
| `risk_monitoring` | 风险事件、监管变化、压力信号或负面信号 |
| `source_quality_signal` | 用于评估来源质量的证据 |
| `ignore` | 不进入用户侧摘要（digest）的低信号文章 |

### 两阶段 LLM 流程

正式 feed 流程拆成几个可审核阶段：

1. 用 `run feed` 刷新文章元数据。
2. 按发布时间窗口导出元数据阶段（metadata stage）文章任务。
3. 导入元数据阶段（metadata stage）LLM 结果。系统根据 `score_breakdown` 计算稳定公式分，并把文章更新为 `metadata` 或 `content` 保存层级。
4. 用慢速、可重试队列抓取 retained 正文。
5. 对已抓到正文的文章导出正文阶段（content stage）任务。
6. 导入正文阶段（content stage）LLM 结果，写入最终分数和 `application_targets`。
7. 按应用目标、分数或分析阶段导出摘要包（digest pack）。

元数据阶段（metadata stage）导出示例：

```bash
PYTHONPATH=packages/wechat_mp_feed/src python3 -m wechat_mp_feed.cli \
  --db ./data/mpfeed.sqlite \
  llm export-jobs \
  --entity-type article \
  --article-content-scope metadata \
  --article-analysis-stage metadata \
  --article-published-after 2026-05-11T00:00:00+08:00 \
  --article-published-before 2026-05-17T23:59:59+08:00 \
  --output ./work/feed/article-metadata-jobs.json
```

导入元数据阶段（metadata stage）结果后抓取正文：

```bash
PYTHONPATH=packages/wechat_mp_feed/src python3 -m wechat_mp_feed.cli \
  --db ./data/mpfeed.sqlite \
  collect content \
  --base-url http://127.0.0.1:5001 \
  --tier all \
  --limit 0 \
  --passes 3 \
  --delay-min 4 \
  --delay-max 8 \
  --timeout 45
```

正文阶段（content stage）导出示例：

```bash
PYTHONPATH=packages/wechat_mp_feed/src python3 -m wechat_mp_feed.cli \
  --db ./data/mpfeed.sqlite \
  llm export-jobs \
  --entity-type article \
  --article-content-scope content \
  --article-analysis-stage content \
  --article-retention content_or_archive \
  --article-published-after 2026-05-11T00:00:00+08:00 \
  --article-published-before 2026-05-17T23:59:59+08:00 \
  --output ./work/feed/article-content-jobs.json
```

摘要包（digest pack）导出示例：

```bash
PYTHONPATH=packages/wechat_mp_feed/src python3 -m wechat_mp_feed.cli \
  --db ./data/mpfeed.sqlite \
  export digests \
  --format markdown \
  --application-target weekly_report \
  --analysis-stage content \
  --min-score 0.6 \
  --limit 100
```

### 图片归档

正文阶段（content stage）LLM 评分完成后，可以运行 `mpfeed archive assets`。该命令只处理 `full_archive` 文章的图片，并默认启用保存前过滤。二维码、小图标、分隔线、小 logo、低复杂度装饰图、推广图和模板图会标记为 `download_status=skipped`，跳过原因写入 `article_assets.metadata.archive_decision`，本地不保存文件。保留下来的图片通过 `article_assets.block_index` 和 `content_ref` 保留正文顺序。只有明确需要保存全部图片时才使用 `--no-filter`。

## 输出

| 字段 | 默认值 | 含义 |
|---|---|---|
| `work_dir` | `work/feed` | 默认输出目录。 |
| `feed_output` | `<work_dir>/feed-items.<format>` | 文章流明细。 |
| `feed_format` | `csv` | `csv` 或 `json`。 |
| `summary_output` | `<work_dir>/feed-summary.<format>` | 汇总统计。 |
| `summary_format` | `json` | `json` 或 `csv`。 |
| `failures_output` | `<work_dir>/feed-failures.<format>` | 正文失败文章表。 |
| `failures_format` | `csv` | `csv` 或 `json`。 |
| `feed_limit` | `3000` | 最多导出多少行 feed。 |

`feed-failures` 是正式输出的一部分，用来区分临时限流、文章删除/受限、解析失败等情况。

## 常用运行方式

离线示例：

```bash
PYTHONPATH=packages/wechat_mp_feed/src python3 -m wechat_mp_feed.cli \
  --db ./work/demo-feed.sqlite \
  demo seed-feed \
  --work-dir ./work/demo-feed

PYTHONPATH=packages/wechat_mp_feed/src python3 -m wechat_mp_feed.cli \
  run feed \
  --config ./work/demo-feed/feed-config.demo.json
```

真实 feed：

```bash
cp examples/feed-config.example.json ./work/feed-config.json

PYTHONPATH=packages/wechat_mp_feed/src python3 -m wechat_mp_feed.cli \
  run feed \
  --config ./work/feed-config.json
```

临时只测 10 个 source：

```bash
PYTHONPATH=packages/wechat_mp_feed/src python3 -m wechat_mp_feed.cli \
  run feed \
  --config ./work/feed-config.json \
  --max-sources 10
```

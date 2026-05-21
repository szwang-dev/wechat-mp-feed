# Feed Config

`run feed --config` reads a JSON file for first-layer feed runs. The example file is:

```text
examples/feed-config.example.json
```

CLI flags override config values. This makes the config suitable as a stable daily-run file while still allowing small one-off changes from the command line.

## Shape

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

Top-level keys matching CLI option names are also accepted, but the grouped shape above is preferred for user-facing configs.

## Storage

| key | meaning |
|---|---|
| `storage.path` | SQLite database path. Equivalent to `--db`. For real runs, point it at a user-controlled local data path. |

## Downloader

WeChat article access is handled by a download service that the user runs and authenticates. Configure `wechat-mp-feed` to call that service through HTTP. For local development, the repository includes a helper script that can bootstrap the external `wechat-download-api` service:

```bash
./scripts/bootstrap_wechat_download_api.sh
```

After the service starts, open the printed login URL, scan the WeChat QR code if required, and set the service URL:

```bash
export WECHAT_DOWNLOAD_API_BASE_URL=http://127.0.0.1:5000
```

| key | meaning |
|---|---|
| `downloader.base_url` | External downloader service URL. Equivalent to `--base-url`. |
| `downloader.timeout` | HTTP timeout in seconds. Equivalent to `--timeout`. |

The downloader service is user-operated. Keep login state outside the repository; `wechat-mp-feed` only calls the configured service URL.

The default adapter targets `wechat-download-api`. Other download services can connect to the feed layer by implementing the same operations and response shapes. See [Downloader Adapter Contract](downloader-adapter.md) for the required contract.

## Feed

### Source Refresh

| key | default | meaning |
|---|---:|---|
| `tier` | `all` | Source tier to refresh: `all`, `core`, `normal`, or `long_tail`. |
| `max_sources` | `0` | Maximum active sources to refresh. `0` means all matching sources. |
| `count` | `5` | Article list count requested per source. |
| `begin` | `0` | Article list offset. |
| `retries` | `2` | Retries for article-list calls. |
| `backoff_seconds` | `3.0` | Backoff between article-list retries. |
| `delay_min` | `1.0` | Minimum delay between source-list calls. |
| `delay_max` | `3.0` | Maximum delay between source-list calls. |
| `no_delay` | `false` | Disable delays for local tests only. |

### Run Mode

| key | default | meaning |
|---|---:|---|
| `full` | `false` | Refresh article metadata, score articles, fetch retained content, then export files. |
| `skip_refresh` | `false` | Export from existing SQLite rows without calling the downloader. |
| `score_articles` | `false` | Run rules-based article scoring before export. |
| `fetch_retained_content` | `false` | Fetch content for retained articles. |

Use `full: true` for normal production-like feed runs. Use `skip_refresh: true` for offline inspection after a previous run.

### Scoring

| key | default | meaning |
|---|---:|---|
| `taxonomy` | `finance` | Taxonomy used for article scoring. |
| `score_limit` | `0` | Maximum articles to score. `0` means all current articles. |
| `min_score` | `0.0` | Minimum score required to save a rules digest. |

`rules_v1` provides local fallback scoring and initial retention decisions. Agent workflows can export article jobs with `mpfeed llm export-jobs`, import LLM semantic scores with `mpfeed llm import-results`, and let those imported scores drive retention and content-fetch priority. LLM scores are staged: metadata scores rank the pre-content queue, while content scores take precedence after full text is available. Tune thresholds with real feedback.

Article LLM jobs are staged.

Metadata-stage jobs use a recall-oriented rubric before content fetch. Agents score title research signal, source relevance, abstract specificity, follow-up potential, freshness, evidence quality, and noise penalty. The system computes the content-fetch action from the formula score plus deterministic low-signal overrides; agents should not return a free-form triage decision. The metadata score should avoid false negatives rather than make final research judgments.

Content-stage jobs use the final research-value rubric. Agents should return `digest.score_breakdown` with sub-scores for research depth, strategy reproducibility, decision value, timeliness, source relevance, originality, evidence quality, and noise penalty. `digest.importance_score` can be returned as a holistic reference score, but imports store the formula score derived from `score_breakdown`:

```text
score =
  research_depth * 0.25
+ decision_value * 0.15
+ strategy_reproducibility * 0.25
+ timeliness * 0.10
+ source_relevance * 0.10
+ originality * 0.10
+ evidence_quality * 0.05
+ noise_penalty
```

`research_depth` should reward analytical depth, not article length. `decision_value` means concrete usefulness for research follow-up, risk monitoring, or medium-term allocation thinking. It should not reward vague inspiration or require immediate trading action, because Official Account articles often lag the market. Keep this rubric reviewable; adjust it when false positives, missed useful articles, or storage cost show that the current scoring policy is off.

### Content Fetch

| key | default | meaning |
|---|---:|---|
| `content_limit` | `0` | Maximum retained articles to fetch. `0` means all eligible articles. |
| `content_retention` | `content_or_archive` | Eligible retention levels: `content_or_archive`, `content`, `full_archive`, or `all`. |
| `content_retries` | `2` | Retries per content fetch. |
| `content_backoff_seconds` | `3.0` | Backoff between content retries. |
| `content_delay_min` | `3.2` | Minimum delay between content fetches. |
| `content_delay_max` | `6.0` | Maximum delay between content fetches. |
| `content_passes` | `3` | Internal passes over the same retained-content queue. |
| `content_pass_cooldown_seconds` | `30.0` | Cooldown between content passes. |

Content fetching uses a fixed queue and multiple passes. Eligible articles are ordered by importance score, retention level, source tier, and publish time. If the downloader returns a body-level retry hint such as `请 N 秒后重试`, the retry loop honors that hint.

Default retention thresholds:

| score band | handling |
|---:|---|
| `>= 0.75` | `full_archive`: keep content and queue local image archival |
| `0.42 - 0.75` | `content`: keep content, structure, and remote asset URLs |
| `< 0.42` | `metadata`: keep article metadata only |

For digest applications, a practical semantic split is `>= 0.75` for full-archive highlights, `0.60 - 0.75` for important daily-digest candidates, and `0.42 - 0.60` for routine tracking.

Content-stage jobs should also return `digest.application_targets` using controlled ids. These targets route articles after scoring:

| target | use |
|---|---|
| `daily_digest` | daily feed digest candidate |
| `weekly_report` | weekly report material |
| `strategy_backlog` | strategy/model idea worth reproduction or testing |
| `market_view` | market timing, style, macro, rates, credit, or allocation view |
| `industry_tracking` | industry, supply-demand, policy, price cycle, or company tracking |
| `risk_monitoring` | risk event, regulatory change, stress signal, or negative signal |
| `source_quality_signal` | evidence for evaluating the source itself |
| `ignore` | low-signal article that should not enter user-facing digest workflows |

### Two-Stage LLM Flow

The production feed flow is intentionally split into reviewable stages:

1. Refresh article metadata with `run feed`.
2. Export metadata-stage article jobs for a publish-time window.
3. Import metadata-stage LLM results. The importer computes stable formula scores from `score_breakdown` and updates retention to `metadata` or `content`.
4. Fetch retained content with a slow, retryable queue.
5. Export content-stage article jobs for articles with fetched content.
6. Import content-stage LLM results with final score and `application_targets`.
7. Export digest packs by target, score, or stage.

Example metadata-stage export:

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

Fetch retained content after importing metadata-stage results:

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

Example content-stage export:

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

Example digest pack export:

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

### Asset Archive

Use `mpfeed archive assets` after content-stage LLM scoring. It caches images only for `full_archive` articles, preserving image order through `article_assets.block_index` and `content_ref`. This keeps routine feed storage small while allowing high-value articles to be reconstructed with local image files.

## Outputs

| key | default | meaning |
|---|---|---|
| `work_dir` | `work/feed` | Directory for default outputs. |
| `feed_output` | `<work_dir>/feed-items.<format>` | Article-level feed rows. |
| `feed_format` | `csv` | `csv` or `json`. |
| `summary_output` | `<work_dir>/feed-summary.<format>` | Aggregate status summary. |
| `summary_format` | `json` | `json` or `csv`. |
| `failures_output` | `<work_dir>/feed-failures.<format>` | Rows where `crawl_status=content_failed`. |
| `failures_format` | `csv` | `csv` or `json`. |
| `feed_limit` | `3000` | Maximum exported feed rows. |

`feed-failures` is part of the normal workflow. It lets users distinguish temporary limits, deleted/restricted articles, and parser failures without digging into SQLite.

## Common Runs

Offline demo:

```bash
PYTHONPATH=packages/wechat_mp_feed/src python3 -m wechat_mp_feed.cli \
  --db ./work/demo-feed.sqlite \
  demo seed-feed \
  --work-dir ./work/demo-feed

PYTHONPATH=packages/wechat_mp_feed/src python3 -m wechat_mp_feed.cli \
  run feed \
  --config ./work/demo-feed/feed-config.demo.json
```

Real feed:

```bash
cp examples/feed-config.example.json ./work/feed-config.json

PYTHONPATH=packages/wechat_mp_feed/src python3 -m wechat_mp_feed.cli \
  run feed \
  --config ./work/feed-config.json
```

Override only the source count for a test run:

```bash
PYTHONPATH=packages/wechat_mp_feed/src python3 -m wechat_mp_feed.cli \
  run feed \
  --config ./work/feed-config.json \
  --max-sources 10
```

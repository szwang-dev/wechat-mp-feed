# Downloader Adapter Contract

`wechat-mp-feed` is the feed and workflow layer. It does not ship a built-in WeChat crawler or hosted download service.

The package talks to a user-operated WeChat article download service through an adapter. The default adapter targets `wechat-download-api`, and the repository includes a helper script for local setup:

```bash
./scripts/bootstrap_wechat_download_api.sh
```

Other services can be used if they expose the same operations or if a new adapter maps their responses into the normalized shapes below.

## Required Operations

| Operation | Purpose | Default adapter endpoint |
|---|---|---|
| Health check | Verify the service is reachable | `GET /api/health` |
| Auth status | Check whether WeChat login is available | `GET /api/admin/status` |
| Search accounts | Resolve an Official Account name into candidates | `GET /api/public/searchbiz?query=...` |
| List articles | Fetch article metadata by account `fakeid` | `GET /api/public/articles?fakeid=...&begin=...&count=...` |
| Fetch article | Fetch one article body by URL | `POST /api/article` with `{ "url": "..." }` |

The service owns login state, cookies, request mechanics, and WeChat-specific behavior. `wechat-mp-feed` owns source review, normalized storage, scoring, retention, exports, and agent workflows.

## Response Envelope

The default adapter treats HTTP `2xx` responses as transport success. If the response body is a JSON object with `success: false`, it is treated as a failed adapter call.

For custom adapters, return the same internal envelope:

```json
{
  "ok": true,
  "operation": "list_articles",
  "status": 200,
  "url": "http://127.0.0.1:5000/api/public/articles?...",
  "body": {}
}
```

## Account Search Output

Account search should return a list directly, or inside one of these keys:

```text
items, list, data, results, records
```

Each candidate can use any of the following field names. The normalizer maps them into the internal source-candidate model.

| Normalized field | Accepted source fields |
|---|---|
| `candidate_name` | `nickname`, `nick_name`, `name`, `title`, `alias` |
| `wechat_fakeid` | `fakeid`, `fake_id`, `wechat_fakeid` |
| `biz` | `__biz`, `biz` |
| `avatar_url` | `avatar_url`, `head_img`, `round_head_img`, `cover` |
| `intro` | `intro`, `signature`, `description`, `desc` |
| `score` | `score`, `confidence`, or computed locally from name similarity |

Minimal useful candidate:

```json
{
  "nickname": "Example Research",
  "fakeid": "Mz...",
  "__biz": "Mz...",
  "signature": "Research account description"
}
```

## Article List Output

Article list responses should return a list directly, or inside one of these keys:

```text
app_msg_list, articles, items, list, data, results, records
```

Each article can use any of the following field names.

| Normalized field | Accepted source fields |
|---|---|
| `title` | `title` |
| `url` | `url`, `link`, `content_url` |
| `digest` | `digest`, `summary`, `desc` |
| `cover_url` | `cover_url`, `cover`, `thumb_url` |
| `publish_time` | `publish_time`, `update_time`, `datetime`, `create_time` |

The normalizer also flattens common WeChat nested objects such as `app_msg_ext_info` and `comm_msg_info`.

Minimal useful article:

```json
{
  "title": "Weekly Strategy Review",
  "url": "https://mp.weixin.qq.com/s/...",
  "digest": "Short abstract",
  "publish_time": 1716200000
}
```

## Article Content Output

Article content responses should return a JSON object directly, or put article fields inside one of these keys:

```text
data, article, content, result
```

Accepted content fields:

| Normalized field | Accepted source fields |
|---|---|
| `content_html` | `content_html`, `html`, `content`, `content_noencode` |
| `content_text` | `content_text`, `text`, `plain_text`, `plain_content` |
| `content_markdown` | `content_markdown`, `markdown`, `md` |
| `assets` | `assets`, `images`, `image_urls`, `imgs` |

Assets may be strings or objects:

```json
{
  "content_html": "<p>Text</p><img data-src=\"https://...\">",
  "content_text": "Text",
  "assets": [
    { "url": "https://example.com/image.png", "type": "image" }
  ]
}
```

When HTML is available, `wechat-mp-feed` parses text and image blocks to preserve article order in `content_structure`. Asset rows are stored in `article_assets` and can later be cached locally for `full_archive` articles.

## Custom Service Guidance

A custom downloader service should provide:

- account search by name;
- article list by `fakeid`, `begin`, and `count`;
- article body fetch by URL;
- clear errors for invalid session, login required, rate limits, and deleted/restricted articles;
- stable `fakeid` or equivalent account id for each source.

If the service uses different endpoints or response names, implement a new adapter under `packages/wechat_mp_feed/src/wechat_mp_feed/adapters/` and map its output to the normalized source candidate, article metadata, and article content shapes described here.

# 下载服务 Adapter 契约

`wechat-mp-feed` 是 feed 和工作流层，不内置微信爬虫，也不提供托管式下载服务。

项目通过适配器（adapter）调用用户自行运行的微信文章下载服务。默认适配器面向 `wechat-download-api`，仓库提供了本地辅助启动脚本：

```bash
./scripts/bootstrap_wechat_download_api.sh
```

如果其他服务提供相同能力，或者新增适配器（adapter）把响应映射成下方规范，也可以接入。

## 必需操作

| 操作 | 用途 | 默认适配器 endpoint |
|---|---|---|
| 健康检查 | 判断服务是否可访问 | `GET /api/health` |
| 登录状态 | 判断微信登录是否可用 | `GET /api/admin/status` |
| 搜索账号 | 通过公众号名称解析候选 | `GET /api/public/searchbiz?query=...` |
| 文章列表 | 通过账号 `fakeid` 获取文章元数据 | `GET /api/public/articles?fakeid=...&begin=...&count=...` |
| 抓取正文 | 通过文章 URL 获取正文 | `POST /api/article`，body 为 `{ "url": "..." }` |

下载服务负责登录态、cookie、请求细节和微信相关行为。`wechat-mp-feed` 负责来源审核、规范化存储、评分、保存层级、导出和 agent 工作流。

## 响应 Envelope

默认适配器（adapter）把 HTTP `2xx` 视为传输成功。如果响应体是 JSON 对象且包含 `success: false`，则视为适配器调用失败。

自定义适配器（adapter）应返回相同的内部 envelope：

```json
{
  "ok": true,
  "operation": "list_articles",
  "status": 200,
  "url": "http://127.0.0.1:5000/api/public/articles?...",
  "body": {}
}
```

## 账号搜索输出

账号搜索可以直接返回 list，也可以把 list 放在以下字段中：

```text
items, list, data, results, records
```

每个候选可以使用下列字段名。规范化模块会映射成内部候选模型。

| 内部字段 | 可接受字段 |
|---|---|
| `candidate_name` | `nickname`, `nick_name`, `name`, `title`, `alias` |
| `wechat_fakeid` | `fakeid`, `fake_id`, `wechat_fakeid` |
| `biz` | `__biz`, `biz` |
| `avatar_url` | `avatar_url`, `head_img`, `round_head_img`, `cover` |
| `intro` | `intro`, `signature`, `description`, `desc` |
| `score` | `score`, `confidence`，或由本地名称相似度计算 |

最小可用候选：

```json
{
  "nickname": "Example Research",
  "fakeid": "Mz...",
  "__biz": "Mz...",
  "signature": "Research account description"
}
```

## 文章列表输出

文章列表可以直接返回 list，也可以把 list 放在以下字段中：

```text
app_msg_list, articles, items, list, data, results, records
```

每篇文章可以使用下列字段名。

| 内部字段 | 可接受字段 |
|---|---|
| `title` | `title` |
| `url` | `url`, `link`, `content_url` |
| `digest` | `digest`, `summary`, `desc` |
| `cover_url` | `cover_url`, `cover`, `thumb_url` |
| `publish_time` | `publish_time`, `update_time`, `datetime`, `create_time` |

规范化模块也会展开常见微信嵌套字段，例如 `app_msg_ext_info` 和 `comm_msg_info`。

最小可用文章：

```json
{
  "title": "Weekly Strategy Review",
  "url": "https://mp.weixin.qq.com/s/...",
  "digest": "文章摘要",
  "publish_time": 1716200000
}
```

## 文章正文输出

文章正文可以直接返回 JSON object，也可以把文章字段放在以下字段中：

```text
data, article, content, result
```

可接受字段：

| 内部字段 | 可接受字段 |
|---|---|
| `content_html` | `content_html`, `html`, `content`, `content_noencode` |
| `content_text` | `content_text`, `text`, `plain_text`, `plain_content` |
| `content_markdown` | `content_markdown`, `markdown`, `md` |
| `assets` | `assets`, `images`, `image_urls`, `imgs` |

资产可以是字符串，也可以是对象：

```json
{
  "content_html": "<p>Text</p><img data-src=\"https://...\">",
  "content_text": "Text",
  "assets": [
    { "url": "https://example.com/image.png", "type": "image" }
  ]
}
```

如果提供 HTML，`wechat-mp-feed` 会解析文本和图片块，在 `content_structure` 中保留文章顺序。图片资产会写入 `article_assets`，`full_archive` 文章可进一步缓存图片到本地。

## 自定义服务建议

自定义下载服务应提供：

- 按名称搜索公众号；
- 按 `fakeid`、`begin`、`count` 获取文章列表；
- 按 URL 获取文章正文；
- 对登录失效、需要扫码、限流、删除或受限文章返回清晰错误；
- 为每个来源提供稳定的 `fakeid` 或等价账号 id。

如果服务 endpoint 或字段名不同，可以在 `packages/wechat_mp_feed/src/wechat_mp_feed/adapters/` 下新增适配器（adapter），并把输出映射为本文档定义的账号候选、文章元数据和文章正文结构。

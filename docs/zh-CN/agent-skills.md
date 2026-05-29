# Agent Skill 包

`wechat-mp-feed` 提供正式的通用 agent skill 包：

```text
skills/wechat-mp-feed/
```

适用对象包括 Claude Code、Codex 风格 skill loader，以及其他可以注册 markdown workflow instructions 的 agent 系统。

Skill 是 agent 使用项目的标准入口。CLI 负责执行，Skill 说明 agent 操作顺序：执行哪些命令、读取哪些输出、如何处理登录失效、如何管理单账号 intake 和订阅状态，以及如何从 feed 数据进入应用层工作流。

## Skill 做什么

该 Skill 说明以下操作：

- 运行离线 feed 验证；
- 从名单、截图、录屏或文章链接批量接入首次公众号列表；
- 使用 `mpfeed run daily` 执行定时刷新或 agent 监督运行；
- 使用 `mpfeed run feed --config` 执行手动刷新和导出；
- 读取 feed 健康状态和失败表；
- 执行两阶段语义 feed 工作流；
- 导出文章级 LLM jobs 和最终应用层摘要上下文（digest context）；
- 通过受控的 `archive assets` 命令归档高价值文章图片资产；
- 通过安全来源命令处理单账号 intake、分类确认、退订和重新启用；
- 使用用户配置的本地路径保存公众号名单、录屏、下载服务凭据和数据库；
- 在 feed 层之上构建应用层 inbox / 摘要（digest），包括内置金融投研工作流。

## Agent 操作约定

使用该 Skill 的 agent 应当：

- 把 `mpfeed` 作为主要操作入口；
- 使用 `agent-smoke` 验证运行环境；
- 首次接入时使用分阶段审核表处理账号身份和分类；
- 以审核后的来源库作为账号身份依据；
- 汇报定时日更状态前先读取 `run-manifest.json` 和 `progress.ndjson`；
- 基于 `feed-summary` 汇报 feed 健康状态；
- 基于 `feed-failures` 判断重试范围；
- 使用分阶段 article LLM jobs 执行文章语义分析；
- 最终报告需要原文证据时使用摘要上下文（`digest-context`）读取正文、结构化内容和图片资产；
- 使用 `archive assets` 缓存本地图片，并默认保留低价值图片过滤，除非用户明确要求保存全部图片；
- 来源分类和订阅状态只通过 `mpfeed source` 安全写入口更新；
- 下载服务登录失效时清楚提示扫码需求；
- 把生成文件保存在用户控制的本地路径。

## 包结构

```text
skills/wechat-mp-feed/
├── SKILL.md
├── agents/openai.yaml
└── references/
```

`SKILL.md` 保持简洁，细节放在 `references/` 中，agent 按需读取。

## 不同平台

- Claude Code：复制到 `.claude/skills/wechat-mp-feed/` 或 `~/.claude/skills/wechat-mp-feed/`。
- Codex 风格 loader：复制到 `~/.codex/skills/wechat-mp-feed/` 或配置的 skills 目录。
- 其他 agent：支持 `SKILL.md` 的系统可直接复制目录；手动注册型系统可把 `SKILL.md` 注册成工作流说明，并确保 agent 能运行 `mpfeed`。

## 第一次验证

验证指令：

```text
Use the wechat-mp-feed skill to run the offline validation test and summarize the report.
```

预期命令：

```bash
mpfeed run agent-smoke --work-dir ./work/agent-smoke
```

然后读取：

```text
work/agent-smoke/agent-smoke-report.md
work/agent-smoke/feed-summary.json
work/agent-smoke/feed-failures.csv
work/agent-smoke/article-llm-jobs.json
```

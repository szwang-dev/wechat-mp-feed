# Agent Skill Package

`wechat-mp-feed` ships a canonical agent skill package:

```text
skills/wechat-mp-feed/
```

It is intended to work across SKILL.md-compatible agent systems, including Claude Code, Codex-style skill loaders, and other tools that can register markdown workflow instructions.

The skill is a first-class interface for this project. The CLI remains the execution layer; the skill gives an agent the operating playbook: which command to run, which outputs to read, how to handle login failures, how to manage single-source intake or subscription state, and how to move from feed data into application workflows.

## What The Skill Does

The skill teaches an agent how to:

- run an offline feed validation test;
- onboard large first-run account lists from files, screenshots, recordings, or article URLs;
- operate `mpfeed run feed --config`;
- inspect feed health and failures;
- run the two-stage semantic feed workflow;
- export article-level LLM jobs and application-layer digest context;
- manage single-source intake, classification confirmation, unsubscribe, and reactivation through safe source commands;
- keep private WeChat/account data outside the public repository;
- build application inbox/digest workflows above the feed layer, including the built-in finance research workflow.

## Agent Operating Contract

An agent using this skill should:

- treat `mpfeed` as the main interface;
- run the `agent-smoke` validation command before claiming the environment is ready;
- use staged review tables for account identity and classification during first-run onboarding;
- use reviewed sources as the identity source of truth;
- read `feed-summary` before summarizing feed health;
- read `feed-failures` before recommending retries;
- use staged article LLM jobs before semantic article analysis;
- use `digest-context` when final reports require original text, structured content, or image assets;
- write source classification and subscription state only through `mpfeed source` helpers;
- report downloader login needs clearly and wait for the user to scan when needed;
- keep generated files under user-controlled local paths.

## Install Shape

Canonical package:

```text
skills/wechat-mp-feed/
├── SKILL.md
├── agents/openai.yaml
└── references/
```

Use this package directly when the agent supports `SKILL.md` folders. Otherwise, register `SKILL.md` as the agent's workflow instruction document and make sure the agent can run `mpfeed`.

## Platform Notes

- Claude Code: copy to `.claude/skills/wechat-mp-feed/` or `~/.claude/skills/wechat-mp-feed/`.
- Codex-style loaders: copy to `~/.codex/skills/wechat-mp-feed/` or the configured skills directory.
- Other agents: use the same `SKILL.md` package if supported; otherwise attach/register the markdown instructions manually.

## First Test

Ask the agent:

```text
Use the wechat-mp-feed skill to run the offline validation test and summarize the report.
```

The agent should run:

```bash
mpfeed run agent-smoke --work-dir ./work/agent-smoke
```

Then it should inspect:

```text
work/agent-smoke/agent-smoke-report.md
work/agent-smoke/feed-summary.json
work/agent-smoke/feed-failures.csv
work/agent-smoke/article-llm-jobs.json
```

# Finance Research Applications

The finance research layer is built above feed outputs and digest context. It should use stored article metadata, fetched content, image asset context, source classification, article LLM scores, and application targets.

## Research Inbox

Inputs:

- `feed-items`
- article content and fetch status
- source classification: `inclusion_tier + primary_domain + source_attribute`
- article LLM jobs/results
- `digest-context` rows when source-level evidence is needed
- application targets such as `daily_digest`, `weekly_report`, `strategy_backlog`, `market_view`, `industry_tracking`, and `risk_monitoring`

Outputs:

- high-signal article list;
- low-signal suppression reasons;
- Chinese summaries;
- theme tags;
- importance score and reason;
- manual-review flags.

## Recurring Weekly Reports

For recurring reports, use application targets and article text together:

1. Export content-stage `digest-context` for the target, such as `weekly_report`.
2. Group rows by source, category, and application target.
3. Re-read original article text from `digest-context` for high-importance rows instead of relying only on previous summaries.
4. Preserve source title, publish time, article URL, and evidence text in the working context.
5. Generate the final table or document only after resolving conflicting source views.

For market view or quantitative weekly reports, prefer one row per broker/source when the user expects a recurring source-level summary. Choose the article that best matches the recurring report theme; ignore ETF tracking, events, product promotion, and single-factor notes unless the user asks for those topics.

## Strategy Backlog

Use `strategy_backlog` for articles that describe reusable models, factors, allocation frameworks, industry rotation methods, or testable signals.

When adding a strategy item, preserve:

- source and article URL;
- strategy idea;
- required data;
- portfolio universe;
- rebalance frequency;
- stated evidence;
- implementation gaps;
- whether reproduction is feasible.

Do not treat generic market commentary as a strategy candidate unless it contains a concrete, testable rule or model.

## Scoring Guidance

Raise priority for:

- durable deep research;
- policy interpretation with asset implications;
- earnings or company updates with clear marginal changes;
- industry tracking with data, price, supply-demand, or competitive structure;
- risk events;
- cross-asset macro or strategy pieces.

Lower priority for:

- recruiting, internships, job collections;
- events, courses, webinars, roadshow marketing;
- product sales and account promotion;
- generic market wrap with no reusable analysis;
- deleted/restricted articles with insufficient evidence.

Use source context as a prior. Core sources can still publish low-signal articles.

## User Feedback Loop

After producing a first inbox:

1. Ask which high-score articles were low value.
2. Ask which missed articles should have been included.
3. Update taxonomy, tags, prompts, or thresholds.
4. Track whether the importance score needs adjustment over time.

Keep scoring rules auditable. If an LLM changes importance, require a short reason that can be reviewed later.

# Finance Taxonomy

The finance pack is the built-in domain package for investment research workflows. It uses a controlled taxonomy for source classification, article classification, tags, and importance scoring. The same domain-pack shape can be reused for other research domains by replacing the taxonomy, scoring rubric, and application targets.

Core shape:

```text
inclusion_tier + primary_domain + source_attribute + tags
```

## Source Inclusion

| id | meaning |
|---|---|
| `core_finance` | Main finance sources for the formal feed and digest pool. |
| `finance_related` | Finance-adjacent sources with lower default priority. |
| `optional_extension` | Industrial or technology sources useful for investment research. |
| `exclude` | Non-finance, low-signal, recruiting, marketing, lifestyle, or entertainment sources. |
| `needs_review` | Insufficient evidence or unclear boundary. |

## Primary Domains

| id | meaning |
|---|---|
| `macro_policy` | Macro, policy, central bank, fiscal, geopolitics, global macro, asset allocation. |
| `strategy` | Equity strategy, market style, flows, A/H/US market views. |
| `quant` | Quant, index, ETF, FOF, fund research, derivatives, factor research. |
| `fixed_income` | Rates, credit, convertibles, bonds, FICC, REITs. |
| `industry_research` | Sector and industry research. |
| `company_research` | Company, stock, earnings, and operating updates. |
| `market_infra` | Exchanges, index providers, regulators, associations, market infrastructure. |
| `finance_thinktank` | Think tanks, forums, and high-quality finance views. |
| `industrial` | Industrial or technology observation useful for investment research. |
| `news` | Finance news and information feeds. |
| `opinion_kol` | Investment KOLs or individual commentary. |
| `recruiting_career` | Finance recruiting, career service, internships. |
| `low_signal` | Non-finance or low-information-density content. |

## Source Attributes

Buy-side and sell-side are source attributes, not primary categories. Sell-side research can cover macro, strategy, fixed income, quant, industries, and companies, so the research domain stays in `primary_domain`; the institution type goes into `source_attribute`.

Common attributes:

- `sell_side`
- `buy_side`
- `media`
- `kol`
- `recruiting`
- `product_provider`

## Article Types

| id | meaning |
|---|---|
| `deep_research` | Structured research with data, framework, and conclusion. |
| `daily_commentary` | Daily comments, morning meetings, market wrap, quick notes. |
| `policy_interpretation` | Policy, regulation, meeting, and document interpretation. |
| `earnings_review` | Earnings, forecasts, operating data review. |
| `industry_tracking` | Industry supply/demand, price, competition, cycle changes. |
| `company_tracking` | Company events, orders, products, operations. |
| `data_review` | Macro, industry, company, or market data review. |
| `roadshow_notes` | Roadshows, calls, expert meetings, field trip notes. |
| `market_signal` | Style, flows, trading structure, risk appetite. |
| `risk_event` | Sudden risks and negative events. |
| `overseas_mapping` | Overseas assets or industries mapped to domestic opportunities. |
| `recruiting_event` | Recruiting, events, courses, meetings, livestreams. |
| `low_signal` | Marketing, repeated, or low-density content. |

## LLM Output Shape

Use stable internal ids:

```json
{
  "inclusion_tier": "core_finance",
  "primary_category_id": "industry_research",
  "tag_ids": ["sell_side", "semiconductor", "ai", "a_share"],
  "source_attribute": "sell_side",
  "importance_score": 0.78,
  "push_level": "digest",
  "reason": "Semiconductor supply-chain update with demand and price changes.",
  "suggested_new_tags": []
}
```

New labels should be returned as suggestions and reviewed before they become part of the official taxonomy.

## Classification Guidance

Use account name, intro/signature, latest articles, and repeated editorial focus together.

Sell-side evidence often includes broker or research-team patterns such as company name plus coverage domain, research institute names, strategy teams, fixed-income teams, quant teams, industry weeklies, company comments, deep reports, morning notes, and similar recurring article formats.

Strong exclusion examples: schools, alumni groups, government services, hospitals, sports, entertainment, lifestyle, local services, exam prep, generic programming accounts, consumer marketing, and general low-signal accounts. A single article mentioning AI, economy, a company, recruiting, or a financial employer is not enough; classify by account-level evidence and recurring article focus.

## Importance Score

First version uses a 0-1 score:

| score | meaning |
|---:|---|
| `>= 0.85` | High priority, alert candidate. |
| `0.65-0.85` | Daily digest candidate. |
| `0.35-0.65` | Normal archive. |
| `< 0.35` | Low signal. |

Track false positives, missed useful articles, storage cost, and user feedback before changing thresholds.

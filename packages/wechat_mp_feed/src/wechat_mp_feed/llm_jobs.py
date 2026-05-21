"""Agent-agnostic LLM job export/import helpers."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .name_match import name_similarity, names_equivalent
from .storage import Store
from .taxonomy import Taxonomy, TaxonomyEntry


LLM_JOB_VERSION = 1
ONBOARDING_JOB_ENTITY_TYPE = "source_onboarding"
ONBOARDING_REVIEW_CATEGORIES = {
    "finance_research": "金融投研",
    "finance_related": "金融相关",
    "finance_career": "金融招聘/职业服务",
    "industry_tech": "产业/科技相关",
    "recruiting": "招聘求职",
    "non_finance": "非金融",
    "uncertain": "不确定",
}

SOURCE_ATTRIBUTE_TAGS = {
    "sell_side",
    "buy_side",
    "product_provider",
    "market_infrastructure",
    "media",
    "kol",
    "recruiting",
    "academic_alumni",
    "non_finance_org",
}

ARTICLE_IMPORTANCE_BANDS = {
    "full_archive": "0.75-1.00: durable, high-quality research with reusable strategy logic or analytical framework; preserve full text, HTML, structure, and local images when available",
    "important": "0.60-0.75: useful article for daily digest or follow-up research; preserve full text and article-level digest",
    "tracking": "0.42-0.60: routine but relevant tracking item; preserve metadata, summary, and available evidence",
    "low_signal": "0.00-0.42: recruiting, events, product marketing, generic news, reposts, or articles without reusable research logic",
}

ARTICLE_METADATA_SCORE_RUBRIC = {
    "title_research_signal": {
        "weight": 0.25,
        "description": "Whether the title suggests research, strategy, model tracking, deep dive, data comment, or sector/company analysis.",
    },
    "source_relevance": {
        "weight": 0.20,
        "description": "Fit with reviewed finance sources and the user's finance research scope.",
    },
    "abstract_specificity": {
        "weight": 0.20,
        "description": "Whether the available digest/abstract contains concrete claims, data, market view, or research object instead of vague promotion.",
    },
    "followup_potential": {
        "weight": 0.15,
        "description": "Likelihood that full text could contain reusable research logic, strategy signals, or evidence worth extracting.",
    },
    "freshness": {
        "weight": 0.10,
        "description": "Freshness for the current feed cycle or near-term research workflow.",
    },
    "evidence_quality": {
        "weight": 0.10,
        "description": "How much reliable evidence is available from metadata alone; lower this when title/digest are too thin.",
    },
    "noise_penalty": {
        "weight": "penalty",
        "description": "Subtract for recruiting, events, courses, product sales, ads, boilerplate news, or account promotion.",
    },
}

ARTICLE_SCORE_RUBRIC = {
    "research_depth": {
        "weight": 0.25,
        "description": "Data, model, reasoning chain, framework, or non-trivial analytical evidence; length alone is not depth.",
    },
    "decision_value": {
        "weight": 0.15,
        "description": "Concrete usefulness for research follow-up, risk monitoring, or medium-term allocation thinking; do not reward vague inspiration or immediate trading action.",
    },
    "strategy_reproducibility": {
        "weight": 0.25,
        "description": "Whether signals, factors, indicators, portfolio logic, backtests, or data requirements can be reproduced.",
    },
    "timeliness": {
        "weight": 0.10,
        "description": "Relevance to the current market regime or near-term research workflow.",
    },
    "source_relevance": {
        "weight": 0.10,
        "description": "Fit with reviewed finance sources, especially strategy, quant, macro, fixed income, and industry research.",
    },
    "originality": {
        "weight": 0.10,
        "description": "Original or scarce insight rather than generic reposting, news aggregation, or marketing copy.",
    },
    "evidence_quality": {
        "weight": 0.05,
        "description": "Enough article evidence is available to support the score; lower this when only metadata is available.",
    },
    "noise_penalty": {
        "weight": "penalty",
        "description": "Subtract for recruiting, events, courses, product sales, ads, boilerplate news, or account promotion.",
    },
}

ARTICLE_APPLICATION_TARGETS = {
    "daily_digest": "Daily feed digest candidate.",
    "weekly_report": "Weekly report material, especially recurring sell-side strategy/quant/macro views.",
    "strategy_backlog": "Strategy or model idea worth reproducing, testing, or adding to a research backlog.",
    "market_view": "Market timing, style, macro, rates, credit, or asset-allocation view.",
    "industry_tracking": "Industry, supply-demand, policy, price cycle, or company-tracking material.",
    "risk_monitoring": "Risk event, regulatory change, market stress, or negative signal worth monitoring.",
    "source_quality_signal": "Useful evidence for evaluating the source itself, even if the article is not a digest highlight.",
    "ignore": "Low-signal article that should not enter user-facing digest workflows.",
}

ONBOARDING_SEMANTIC_GUIDANCE = [
    (
        "First classify inclusion_tier, then primary research domain, then source_attribute. "
        "Do not use sell_side or buy_side as the primary category; they are source attributes."
    ),
    (
        "Treat sell-side signals as a strong prior when the account name or recurring article titles contain a "
        "securities-house/research-team pattern, for example bracketed prefixes like 【中金策略】, 【天风固收】, "
        "【华泰金工】, 【招商电子】, 【东吴交运】, or recurring words such as 证券研究, 研究所, 宏观团队, "
        "策略团队, 固收团队, 金工, 行业周报, 公司点评, 深度报告, 晨会, 早间速递."
    ),
    (
        "Common sell-side institution cues include 中金, 华泰, 国泰海通/国君, 申万宏源/申万, 广发, 兴证, "
        "东吴, 招商, 华创, 国投/安信, 开源, 国联民生, 华福, 西部, 天风, 光大, 国盛, 华源, "
        "财通, 长江, 国金, 浙商, 银河, 中信/中信建投, 东方, 平安, 东北, 中银, 中泰, 国信, "
        "信达, 西南, 太平洋. Use the coverage words to choose the primary domain."
    ),
    (
        "Map coverage words to primary domains: 宏观/经济/央行/财政/货币/大类资产 -> macro_policy; "
        "策略/权益/A股/港股/美股/市场风格/金股 -> strategy; 固收/债市/利率/信用/转债/FICC/REITs -> "
        "fixed_income; 金工/量化/ETF/FOF/LOF/指数/基金评价/衍生品/CTA/因子 -> quant; "
        "电子/计算机/通信/医药/地产/交运/汽车/非银/银行/商贸零售/机械/新能源/化工等 -> industry_research."
    ),
    (
        "Financial data, research tooling, fund sales, and product-service accounts such as Tushare-like APIs, "
        "Ricequant-like platforms, third-party fund research, or wealth/fund product channels are usually "
        "finance_related or core_finance with primary_domain=quant and source_attribute=product_provider, "
        "unless their evidence is mainly independent research."
    ),
    (
        "Recruiting/career accounts should be recruiting_career with source_attribute=recruiting. Even financial "
        "recruiting is not core digest material unless the user explicitly promotes it."
    ),
    (
        "Strong non-finance accounts should be excluded before keyword matching: schools/alumni/event groups, "
        "public-service accounts, hospitals, sports, entertainment, lifestyle, culture, local guides, exam/civil-service "
        "prep, generic tech/programming accounts, and consumer/marketing accounts. Do not promote them merely because "
        "a recent article mentions a company, AI, economy, recruitment, or a finance employer."
    ),
    (
        "Use latest articles as evidence of recurring editorial focus, not as one-off keyword triggers. If evidence is "
        "stale, empty, migrated, or dominated by notices, lower confidence or request review."
    ),
    (
        "When latest article evidence includes content_fetch_ok=false, treat that article as unavailable, deleted, "
        "restricted, or backend-stale for freshness/classification. Prefer the newest article whose content_fetch_ok is true; "
        "if none is fetchable, lower confidence and request review."
    ),
]


def build_llm_jobs(
    store: Store,
    taxonomy: Taxonomy,
    entity_type: str = "all",
    limit: int = 100,
    source_id: str | None = None,
    content_chars: int = 6000,
    source_article_limit: int = 5,
    article_content_scope: str = "content",
    article_updated_after: str | None = None,
    article_published_after: str | None = None,
    article_published_before: str | None = None,
    article_retention_levels: tuple[str, ...] | None = None,
    article_analysis_stage: str | None = None,
) -> dict[str, Any]:
    jobs: list[dict[str, Any]] = []
    resolved_article_analysis_stage = article_analysis_stage or (
        "metadata" if article_content_scope == "metadata" else "content"
    )

    if entity_type in {"all", "source"}:
        if source_id:
            source = store.get_source(source_id)
            sources = [source] if source else []
        else:
            sources = store.list_sources(limit=limit)
        for source in sources:
            latest_articles = store.list_articles(limit=source_article_limit, source_id=source["id"])
            jobs.append(
                {
                    "job_id": f"source:{source['id']}",
                    "entity_type": "source",
                    "entity_id": source["id"],
                    "taxonomy": taxonomy.name,
                    "source": compact_source_for_llm(source, latest_articles),
                    "task": (
                        "Classify this account using semantic judgment over the account metadata and latest article "
                        "evidence. Use the controlled taxonomy only; if no category/tag fits, return the closest "
                        "existing option and set requires_user_confirmation=true with a short new-category suggestion."
                    ),
                    "expected_result": source_expected_result(taxonomy),
                }
            )

    if entity_type in {"all", "article"}:
        for article in store.list_articles_for_llm(
            limit=limit,
            source_id=source_id,
            content_scope=article_content_scope,
            updated_after=article_updated_after,
            published_after=article_published_after,
            published_before=article_published_before,
            retention_levels=article_retention_levels,
        ):
            jobs.append(
                {
                    "job_id": f"article:{article['id']}",
                    "entity_type": "article",
                    "entity_id": article["id"],
                    "analysis_stage": resolved_article_analysis_stage,
                    "taxonomy": taxonomy.name,
                    "article": compact_article(article, content_chars=content_chars),
                    "task": article_task_for_stage(resolved_article_analysis_stage),
                    "expected_result": article_expected_result(taxonomy),
                }
            )

    return {
        "version": LLM_JOB_VERSION,
        "taxonomy": taxonomy_to_dict(taxonomy),
        "instructions": [
            "Prioritize finance/investment/research usefulness.",
            "For sources, keep finance-related accounts active and put unrelated or stale accounts into inactive/archived status.",
            "For articles, summarize only material content; score low-signal marketing, recruiting, or event notices below normal digest thresholds.",
            "For article scoring, treat securities/fund recruiting, product sales, event invitations, account promotion, course promotion, and generic market wrap text as low-signal unless the article contains reusable research logic, data, or original analysis.",
            "Recommended article importance bands: 0.75+ for durable research with reusable strategy/framework logic; 0.60-0.75 for daily digest candidates; 0.42-0.60 for routine tracking; below 0.42 for notices, recruiting, product marketing, events, reposts, or mostly boilerplate content.",
            "For metadata-stage jobs, use article_metadata_score_rubric to produce score_breakdown for content-fetch recall. Do not return a free-form triage_decision; the system computes fetch_required/fetch_candidate/metadata_only from the formula score plus deterministic low-signal overrides.",
            "For content-stage jobs, use article_score_rubric for final research value scoring.",
            "Return digest.importance_score as a reference holistic score; the importer stores the formula score computed from score_breakdown so scoring stays stable across agents.",
            "For content-stage jobs, also return digest.application_targets using only ids from article_application_targets so downstream workflows can route articles to digest, weekly report, strategy backlog, industry tracking, or risk monitoring.",
            "When article content is unavailable, use title, abstract, source context, and publish metadata for a preliminary score; mark the reason as metadata-only and lower confidence when the title/abstract is insufficient.",
            "Use source context as prior evidence, but do not let a core_finance source automatically promote a low-signal article.",
            "Use only category and tag ids from the supplied taxonomy. If the taxonomy is missing a useful category or tag, use the closest existing id and explain the suggested addition in reason/notes instead of inventing a formal id.",
            "Return JSON only, shaped as {'results': [...]} where every result references job_id, entity_type, and entity_id.",
        ]
        + ONBOARDING_SEMANTIC_GUIDANCE,
        "result_schema": {
            "classification": {
                "taxonomy": taxonomy.name,
                "category": "one taxonomy category id",
                "tags": ["taxonomy tag ids"],
                "confidence": "0.0-1.0",
                "method": "llm:<agent-or-model-name>",
            },
            "source_update": {"status": "active|inactive|archived|needs_review", "tier": "core|normal|long_tail"},
            "digest": {
                "summary": "short Chinese summary",
                "key_points": ["1-5 concise points"],
                "importance_score": "0.0-1.0",
                "reason": "why this matters or why it is low signal",
                "model": "llm:<agent-or-model-name>",
            },
        },
        "article_importance_bands": ARTICLE_IMPORTANCE_BANDS,
        "article_metadata_score_rubric": ARTICLE_METADATA_SCORE_RUBRIC,
        "article_score_rubric": ARTICLE_SCORE_RUBRIC,
        "article_application_targets": ARTICLE_APPLICATION_TARGETS,
        "article_job_filter": {
            "content_scope": article_content_scope,
            "updated_after": article_updated_after,
            "published_after": article_published_after,
            "published_before": article_published_before,
            "retention_levels": list(article_retention_levels) if article_retention_levels else None,
            "analysis_stage": resolved_article_analysis_stage,
        },
        "count": len(jobs),
        "jobs": jobs,
    }


def article_task_for_stage(analysis_stage: str) -> str:
    if analysis_stage == "metadata":
        return (
            "Use only article metadata, source context, title, digest, and publish time to score whether full content "
            "should be fetched. Return classification and digest.score_breakdown using article_metadata_score_rubric. "
            "Do not return a free-form triage decision."
        )
    return (
        "Use the article content excerpt plus source context to classify the article, produce a reviewable finance digest, "
        "score final research value with article_score_rubric, and assign application_targets."
    )


def build_onboarding_llm_jobs(
    store: Store,
    taxonomy: Taxonomy,
    source_type: str | None = None,
    decision: str | None = "pending",
    limit: int = 100,
    candidate_limit: int = 5,
    article_limit: int = 3,
    strict_match_only: bool = False,
) -> dict[str, Any]:
    imports = store.list_imports(limit=limit, source_type=source_type)
    candidate_read_limit = max(limit * max(candidate_limit, 10), 1000)
    candidates = store.list_candidates(decision=decision, limit=candidate_read_limit)
    sources = store.list_sources(limit=max(limit * 2, 1000))

    candidates_by_import: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        candidates_by_import[candidate["import_id"]].append(candidate)

    source_by_fakeid = {source.get("wechat_fakeid"): source for source in sources if source.get("wechat_fakeid")}
    source_by_biz = {source.get("biz"): source for source in sources if source.get("biz")}
    source_by_name = {source["name"]: source for source in sources}

    jobs = []
    for import_row in imports:
        raw_name = import_row.get("raw_name")
        if not raw_name:
            continue
        manual_review = manual_onboarding_review(import_row)
        match_name = manual_review.get("manual_account_name") or raw_name

        ranked_candidates = sorted(
            candidates_by_import.get(import_row["id"], []),
            key=lambda candidate: (
                names_equivalent(candidate.get("candidate_name"), match_name),
                name_similarity(candidate.get("candidate_name"), match_name),
                float(candidate.get("score") or 0),
                candidate.get("created_at") or "",
            ),
            reverse=True,
        )[:candidate_limit]
        if strict_match_only and not any(
            names_equivalent(candidate.get("candidate_name"), match_name) for candidate in ranked_candidates
        ):
            continue
        compact_candidates = [
            compact_onboarding_candidate(
                candidate,
                raw_name=match_name,
                source=source_by_fakeid.get(candidate.get("wechat_fakeid"))
                or source_by_biz.get(candidate.get("biz"))
                or source_by_name.get(candidate.get("candidate_name")),
                article_limit=article_limit,
            )
            for candidate in ranked_candidates
        ]

        jobs.append(
            {
                "job_id": f"onboarding:{import_row['id']}",
                "entity_type": ONBOARDING_JOB_ENTITY_TYPE,
                "entity_id": import_row["id"],
                "taxonomy": taxonomy.name,
                "import": {
                    "id": import_row["id"],
                    "raw_name": raw_name,
                    "manual_review": manual_review,
                    "identity_match_name": match_name,
                    "raw_url": import_row.get("raw_url"),
                    "source_type": import_row.get("source_type"),
                    "status": import_row.get("status"),
                },
                "candidates": compact_candidates,
                "task": (
                    "Decide whether this imported WeChat account should enter the managed finance-source registry. "
                    "Use semantic judgment over the OCR/list name, candidate match quality, intro, and latest article evidence. "
                    "Classify the primary research domain separately from source attributes such as sell_side or buy_side. "
                    "Prefer accepting clear finance/research/investment accounts, rejecting clear non-finance accounts, "
                    "and leaving only genuinely ambiguous cases for manual review. Do not rely on keyword matches alone."
                ),
                "expected_result": onboarding_expected_result(taxonomy),
            }
        )

    return {
        "version": LLM_JOB_VERSION,
        "taxonomy": taxonomy_to_dict(taxonomy),
        "instructions": [
            "Return JSON only, shaped as {'results': [...]} where every result references job_id, entity_type, and entity_id.",
            "For source onboarding, choose at most one selected_candidate_id per imported account.",
            "Use action='accept_source' only when the selected candidate is finance-related and the match is plausible.",
            "Use action='ignore_non_finance' for clear non-finance accounts, action='reject_all' for false/noise imports, and action='needs_manual_review' for ambiguous cases.",
            "Always set review_category to one of the coarse onboarding categories; this is shown to the user in the review table.",
            "Classify accepted finance accounts by primary research domain in classification.category; put source attributes such as sell_side, buy_side, media, kol, or recruiting into classification.tags.",
            "Financial recruiting/career accounts should normally be review_category='finance_career' or 'recruiting' and should not enter the core digest tier.",
            "Clear exams, sports, entertainment, lifestyle, or marketing accounts should normally be ignored even if their latest article mentions a finance employer or business term.",
        ]
        + ONBOARDING_SEMANTIC_GUIDANCE,
        "result_schema": {
            ONBOARDING_JOB_ENTITY_TYPE: onboarding_expected_result(taxonomy),
        },
        "count": len(jobs),
        "jobs": jobs,
    }


def manual_onboarding_review(import_row: dict[str, Any]) -> dict[str, Any]:
    review = (import_row.get("raw_payload") or {}).get("manual_onboarding_review") or {}
    return review if isinstance(review, dict) else {}


def apply_llm_results(
    store: Store,
    payload: dict[str, Any] | list[Any],
    default_taxonomy: str | Taxonomy,
    default_model: str,
) -> dict[str, Any]:
    taxonomy = default_taxonomy if isinstance(default_taxonomy, Taxonomy) else None
    default_taxonomy_name = taxonomy.name if taxonomy else str(default_taxonomy)
    results = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(results, list):
        raise ValueError("LLM result JSON expects a list or an object with a 'results' list")

    saved_classifications = []
    saved_digests = []
    saved_reviews = []
    source_updates = []
    skipped = []

    for item in results:
        if not isinstance(item, dict):
            skipped.append({"reason": "result_not_object", "value": item})
            continue

        entity_type = item.get("entity_type")
        entity_id = item.get("entity_id")
        if not entity_type or not entity_id:
            skipped.append({"reason": "missing_entity", "result": item})
            continue

        if entity_type == ONBOARDING_JOB_ENTITY_TYPE:
            onboarding_result = apply_onboarding_result(store, item, default_taxonomy_name, default_model, taxonomy)
            if onboarding_result.get("classification"):
                saved_classifications.append(onboarding_result["classification"])
            if onboarding_result.get("source_update"):
                source_updates.append(onboarding_result["source_update"])
            if onboarding_result.get("skipped"):
                skipped.append(onboarding_result["skipped"])
            continue

        if entity_type == "source" and item.get("requires_user_confirmation"):
            review = normalize_source_classification_review(item, default_taxonomy_name, default_model)
            if not review:
                skipped.append({"reason": "requires_user_confirmation_without_classification", "result": item})
                continue
            validation_error = validate_source_classification_review_for_taxonomy(review, taxonomy)
            if validation_error:
                skipped.append({"reason": validation_error, "result": item})
                continue
            saved_reviews.append(store.save_source_classification_review(review))
            continue

        classification = normalize_classification(item, default_taxonomy_name, default_model)
        if classification:
            validation_error = validate_classification_for_taxonomy(classification, taxonomy)
            if validation_error:
                skipped.append({"reason": validation_error, "result": item})
            else:
                saved_classifications.append(store.save_classification(classification))

        if entity_type == "source":
            source_update = item.get("source_update") if isinstance(item.get("source_update"), dict) else item
            status = source_update.get("status") or source_update.get("source_status")
            tier = source_update.get("tier") or source_update.get("source_tier")
            if status or tier:
                source_updates.append(store.update_source(entity_id, status=status, tier=tier))

        digest = normalize_digest(item, default_model)
        if digest:
            saved_digests.append(store.save_digest(digest))

    return {
        "ok": True,
        "results_seen": len(results),
        "classifications_saved": len(saved_classifications),
        "digests_saved": len(saved_digests),
        "source_classification_reviews_saved": len(saved_reviews),
        "source_updates": len(source_updates),
        "skipped": skipped,
        "items": {
            "classifications": saved_classifications,
            "digests": saved_digests,
            "source_classification_reviews": saved_reviews,
            "source_updates": source_updates,
        },
    }


def apply_onboarding_result(
    store: Store,
    item: dict[str, Any],
    default_taxonomy: str,
    default_model: str,
    taxonomy: Taxonomy | None = None,
) -> dict[str, Any]:
    import_id = item["entity_id"]
    action = str(item.get("action") or item.get("decision") or "").strip().lower()
    candidate_id = item.get("selected_candidate_id") or item.get("candidate_id")
    source_update = item.get("source_update") if isinstance(item.get("source_update"), dict) else {}
    store.record_import_review(import_id, normalize_onboarding_review(item, default_model))

    if action in {"accept", "accept_source", "active"}:
        if not candidate_id:
            return {"skipped": {"reason": "missing_selected_candidate_id", "result": item}}
        tier = source_update.get("tier") or item.get("tier") or "normal"
        accepted = store.accept_candidate(candidate_id, tier=tier)
        status = source_update.get("status")
        if status and status != "active":
            store.update_source(accepted["source_id"], status=status, tier=tier)

        saved_classification = None
        classification = normalize_onboarding_classification(item, accepted["source_id"], default_taxonomy, default_model)
        if classification:
            validation_error = validate_classification_for_taxonomy(classification, taxonomy)
            if validation_error:
                return {"skipped": {"reason": validation_error, "result": item}}
            saved_classification = store.save_classification(classification)
        return {
            "classification": saved_classification,
            "source_update": {
                "ok": True,
                "action": "accept_source",
                "candidate_id": candidate_id,
                "source_id": accepted["source_id"],
                "tier": tier,
                "status": status or "active",
            },
        }

    if action in {"ignore", "ignore_non_finance", "reject_all", "reject", "archive"}:
        rejected = store.reject_all_candidates_for_import(import_id)
        archived = store.archive_sources_for_import(import_id)
        status = "ignored" if action in {"ignore", "ignore_non_finance"} else "rejected"
        store.update_import_status(import_id, status)
        return {
            "source_update": {
                "ok": True,
                "action": action,
                "import_id": import_id,
                "candidates_rejected": rejected["count"],
                "sources_archived": archived["count"],
            }
        }

    if action in {"needs_manual_review", "manual_review", "review", "uncertain"}:
        store.update_import_status(import_id, "needs_review")
        return {"source_update": {"ok": True, "action": "needs_manual_review", "import_id": import_id}}

    return {"skipped": {"reason": "unknown_onboarding_action", "result": item}}


def normalize_onboarding_review(item: dict[str, Any], default_model: str) -> dict[str, Any]:
    nested = item.get("classification") if isinstance(item.get("classification"), dict) else {}
    return {
        "action": str(item.get("action") or item.get("decision") or "").strip().lower(),
        "selected_candidate_id": item.get("selected_candidate_id") or item.get("candidate_id"),
        "review_category": item.get("review_category") or item.get("coarse_category"),
        "inclusion_tier": item.get("inclusion_tier") or item.get("inclusion"),
        "source_attribute": item.get("source_attribute"),
        "primary_domain": nested.get("category") or item.get("primary_domain") or item.get("category"),
        "requires_user_confirmation": bool(item.get("requires_user_confirmation", False)),
        "reason": item.get("reason") or item.get("notes") or "",
        "confidence": nested.get("confidence", item.get("confidence", 0)),
        "method": nested.get("method") or item.get("method") or item.get("model") or default_model,
    }


def normalize_onboarding_classification(
    item: dict[str, Any],
    source_id: str,
    default_taxonomy: str,
    default_model: str,
) -> dict[str, Any] | None:
    nested = item.get("classification") if isinstance(item.get("classification"), dict) else {}
    category = nested.get("category") or item.get("category")
    if not category:
        return None
    tags = nested.get("tags", item.get("tags") or [])
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(",") if part.strip()]
    source_attribute = item.get("source_attribute")
    if source_attribute and source_attribute not in tags:
        tags.append(source_attribute)
    return {
        "entity_type": "source",
        "entity_id": source_id,
        "taxonomy": nested.get("taxonomy") or item.get("taxonomy") or default_taxonomy,
        "category": category,
        "tags": tags,
        "confidence": nested.get("confidence", item.get("confidence", 0)),
        "method": nested.get("method") or item.get("method") or default_model,
    }


def normalize_classification(item: dict[str, Any], default_taxonomy: str, default_model: str) -> dict[str, Any] | None:
    nested = item.get("classification") if isinstance(item.get("classification"), dict) else {}
    category = nested.get("category") or item.get("category")
    if not category:
        return None
    tags = nested.get("tags", item.get("tags") or [])
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(",") if part.strip()]
    source_attribute = nested.get("source_attribute") or item.get("source_attribute")
    if item.get("entity_type") == "source" and source_attribute and source_attribute not in tags:
        tags.append(source_attribute)
    return {
        "entity_type": item["entity_type"],
        "entity_id": item["entity_id"],
        "taxonomy": nested.get("taxonomy") or item.get("taxonomy") or default_taxonomy,
        "category": category,
        "tags": tags,
        "confidence": nested.get("confidence", item.get("confidence", 0)),
        "method": nested.get("method") or item.get("method") or default_model,
    }


def normalize_digest(item: dict[str, Any], default_model: str) -> dict[str, Any] | None:
    digest = item.get("digest")
    if not isinstance(digest, dict):
        return None
    article_id = digest.get("article_id") or (item.get("entity_id") if item.get("entity_type") == "article" else None)
    summary = digest.get("summary")
    if not article_id or not summary:
        return None
    analysis_stage = normalize_analysis_stage(digest.get("analysis_stage") or item.get("analysis_stage"))
    model_score = normalize_importance_score(digest.get("importance_score"))
    score_breakdown = normalize_score_breakdown(digest.get("score_breakdown"), analysis_stage=analysis_stage)
    importance_score = model_score
    if score_breakdown:
        importance_score = compute_importance_score_from_breakdown(score_breakdown, analysis_stage=analysis_stage)
        score_breakdown["model_importance_score"] = model_score
        score_breakdown["computed_importance_score"] = importance_score
    return {
        "article_id": article_id,
        "summary": summary,
        "key_points": digest.get("key_points") or [],
        "importance_score": importance_score,
        "score_breakdown": score_breakdown,
        "application_targets": normalize_application_targets(digest.get("application_targets")),
        "reason": digest.get("reason"),
        "model": digest.get("model") or default_model,
        "analysis_stage": analysis_stage,
    }


def normalize_source_classification_review(
    item: dict[str, Any],
    default_taxonomy: str,
    default_model: str,
) -> dict[str, Any] | None:
    classification = normalize_classification(item, default_taxonomy, default_model)
    if not classification or classification.get("entity_type") != "source":
        return None
    nested = item.get("classification") if isinstance(item.get("classification"), dict) else {}
    source_attribute = nested.get("source_attribute") or item.get("source_attribute")
    return {
        "source_id": classification["entity_id"],
        "taxonomy": classification["taxonomy"],
        "category": classification["category"],
        "source_attribute": source_attribute,
        "tags": classification.get("tags") or [],
        "confidence": classification.get("confidence") or 0,
        "method": classification.get("method") or default_model,
        "reason": item.get("reason") or item.get("notes") or "",
        "taxonomy_suggestions": item.get("taxonomy_suggestions") or [],
        "status": "pending",
        "raw_payload": item,
    }


def normalize_importance_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(1.0, score)), 3)


def normalize_score_breakdown(value: Any, analysis_stage: str = "content") -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    rubric = score_rubric_for_analysis_stage(analysis_stage, value)
    normalized: dict[str, Any] = {}
    for key in rubric:
        if key not in value:
            continue
        raw_score = value.get(key)
        if key == "noise_penalty":
            try:
                score = float(raw_score)
            except (TypeError, ValueError):
                continue
            normalized[key] = round(max(-1.0, min(0.0, score)), 3)
            continue
        normalized[key] = normalize_importance_score(raw_score)
    notes = value.get("notes") or value.get("reason")
    if notes:
        normalized["notes"] = str(notes)
    return normalized


def score_rubric_for_analysis_stage(analysis_stage: str, score_breakdown: dict[str, Any] | None = None) -> dict[str, Any]:
    if analysis_stage == "metadata":
        if not score_breakdown:
            return ARTICLE_METADATA_SCORE_RUBRIC
        metadata_keys = set(ARTICLE_METADATA_SCORE_RUBRIC) - {"noise_penalty"}
        if any(key in score_breakdown for key in metadata_keys):
            return ARTICLE_METADATA_SCORE_RUBRIC
    return ARTICLE_SCORE_RUBRIC


def compute_importance_score_from_breakdown(score_breakdown: dict[str, Any], analysis_stage: str = "content") -> float:
    score = 0.0
    has_weighted_item = False
    rubric_items = score_rubric_for_analysis_stage(analysis_stage, score_breakdown)
    for key, rubric in rubric_items.items():
        if key == "noise_penalty" or key not in score_breakdown:
            continue
        weight = rubric.get("weight")
        if isinstance(weight, (int, float)):
            score += float(score_breakdown[key]) * weight
            has_weighted_item = True
    if not has_weighted_item:
        return 0.0
    score += float(score_breakdown.get("noise_penalty") or 0)
    return normalize_importance_score(score)


def normalize_application_targets(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, list):
        return []
    targets: list[str] = []
    for item in value:
        target = str(item).strip()
        if target in ARTICLE_APPLICATION_TARGETS and target not in targets:
            targets.append(target)
    return targets


def normalize_analysis_stage(value: Any) -> str:
    stage = str(value or "").strip().lower()
    if stage in {"metadata", "content", "rules"}:
        return stage
    return "content"


def validate_classification_for_taxonomy(classification: dict[str, Any], taxonomy: Taxonomy | None) -> str | None:
    if taxonomy is None:
        return None
    entity_type = classification.get("entity_type")
    if entity_type == "source":
        category_ids = {entry.id for entry in taxonomy.source_categories}
    elif entity_type == "article":
        category_ids = {entry.id for entry in taxonomy.article_categories}
    else:
        return f"unsupported_entity_type:{entity_type}"
    category = classification.get("category")
    if category not in category_ids:
        return f"invalid_category:{category}"
    tag_ids = {tag.id for group in taxonomy.tag_groups for tag in group.tags}
    for tag in classification.get("tags") or []:
        if tag not in tag_ids:
            return f"invalid_tag:{tag}"
    return None


def validate_source_classification_review_for_taxonomy(review: dict[str, Any], taxonomy: Taxonomy | None) -> str | None:
    if taxonomy is None:
        return None
    classification = {
        "entity_type": "source",
        "entity_id": review["source_id"],
        "taxonomy": review["taxonomy"],
        "category": review["category"],
        "tags": review.get("tags") or [],
    }
    validation_error = validate_classification_for_taxonomy(classification, taxonomy)
    if validation_error:
        return validation_error
    source_attribute = review.get("source_attribute")
    if source_attribute:
        source_attributes = {
            tag.id
            for group in taxonomy.tag_groups
            if group.id == "source_attribute"
            for tag in group.tags
        }
        if source_attribute not in source_attributes:
            return f"invalid_source_attribute:{source_attribute}"
    suggestions = review.get("taxonomy_suggestions") or []
    if suggestions and not isinstance(suggestions, list):
        return "invalid_taxonomy_suggestions"
    return None


def compact_source_for_llm(source: dict[str, Any], latest_articles: list[dict[str, Any]]) -> dict[str, Any]:
    item = compact_source(source) or {}
    for key in ("wechat_fakeid", "biz", "source_type", "created_at", "updated_at"):
        if source.get(key):
            item[key] = source.get(key)
    item["latest_articles"] = [
        {
            "id": article.get("id"),
            "title": article.get("title"),
            "digest": article.get("digest"),
            "publish_time": article.get("publish_time"),
            "url": article.get("url"),
            "retention_level": article.get("retention_level"),
        }
        for article in latest_articles
    ]
    return item


def compact_article(article: dict[str, Any], content_chars: int) -> dict[str, Any]:
    item = {
        key: article.get(key)
        for key in (
            "id",
            "source_id",
            "source_name",
            "source_status",
            "source_tier",
            "source_category",
            "source_tags",
            "title",
            "url",
            "digest",
            "publish_time",
            "crawl_status",
            "fetch_error",
        )
    }
    text = article.get("content_text") or article.get("content_markdown") or strip_html(article.get("content_html"))
    if text:
        item["content_excerpt"] = str(text)[:content_chars]
    return item


def compact_onboarding_candidate(
    candidate: dict[str, Any],
    raw_name: str,
    source: dict[str, Any] | None,
    article_limit: int,
) -> dict[str, Any]:
    probe = (candidate.get("raw_payload") or {}).get("article_probe")
    probe_articles = probe.get("articles") if isinstance(probe, dict) else []
    if not isinstance(probe_articles, list):
        probe_articles = []

    return {
        "candidate_id": candidate["id"],
        "candidate_name": candidate.get("candidate_name"),
        "wechat_fakeid": candidate.get("wechat_fakeid"),
        "biz": candidate.get("biz"),
        "intro": candidate.get("intro"),
        "score": candidate.get("score"),
        "decision": candidate.get("decision"),
        "display_exact_match": candidate.get("candidate_name") == raw_name,
        "normalized_match": names_equivalent(candidate.get("candidate_name"), raw_name),
        "name_similarity": name_similarity(candidate.get("candidate_name"), raw_name),
        "existing_source": compact_source(source),
        "latest_articles": [compact_probe_article(article) for article in probe_articles[:article_limit]],
    }


def compact_source(source: dict[str, Any] | None) -> dict[str, Any] | None:
    if not source:
        return None
    return {
        "source_id": source.get("id"),
        "name": source.get("name"),
        "status": source.get("status"),
        "tier": source.get("tier"),
        "intro": source.get("intro"),
    }


def compact_probe_article(article: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": article.get("title"),
        "digest": article.get("digest"),
        "publish_time": article.get("publish_time"),
        "url": article.get("url"),
        "content_fetch_ok": article.get("content_fetch_ok"),
        "content_fetch_error": article.get("content_fetch_error"),
    }


def taxonomy_to_dict(taxonomy: Taxonomy) -> dict[str, Any]:
    return {
        "name": taxonomy.name,
        "source_categories": [entry_to_dict(entry) for entry in taxonomy.source_categories],
        "article_categories": [entry_to_dict(entry) for entry in taxonomy.article_categories],
        "tag_groups": [
            {"id": group.id, "name_zh": group.name_zh, "tags": [entry_to_dict(tag) for tag in group.tags]}
            for group in taxonomy.tag_groups
        ],
    }


def source_expected_result(taxonomy: Taxonomy) -> dict[str, Any]:
    return {
        "entity_type": "source",
        "classification.category": [entry.id for entry in taxonomy.source_categories],
        "source_attribute": sorted(SOURCE_ATTRIBUTE_TAGS),
        "classification.tags": "taxonomy tag ids only",
        "classification.confidence": "0.0-1.0",
        "source_update.status": ["active", "inactive", "archived", "needs_review"],
        "source_update.tier": ["core", "normal", "long_tail"],
        "requires_user_confirmation": "true for first-time source classification, and true whenever the account needs user review",
        "taxonomy_suggestions": [
            {
                "type": "category|source_attribute|tag",
                "suggested_id": "snake_case id",
                "name_zh": "中文名称",
                "reason": "why the current taxonomy is insufficient",
            }
        ],
        "reason": "short Chinese explanation; include proposed new category/tag here instead of inventing formal ids",
    }


def article_expected_result(taxonomy: Taxonomy) -> dict[str, Any]:
    return {
        "entity_type": "article",
        "classification.category": [entry.id for entry in taxonomy.article_categories],
        "classification.tags": "taxonomy tag ids only",
        "classification.confidence": "0.0-1.0",
        "digest.required": ["summary", "key_points", "importance_score", "score_breakdown", "reason", "model"],
        "digest.importance_score": "0.0-1.0 reference holistic score; importer stores the formula score computed from digest.score_breakdown",
        "digest.score_breakdown": "Use article_metadata_score_rubric for metadata-stage jobs and article_score_rubric for content-stage jobs.",
        "digest.application_targets": list(ARTICLE_APPLICATION_TARGETS),
        "analysis_stage": ["metadata", "content"],
    }


def onboarding_expected_result(taxonomy: Taxonomy) -> dict[str, Any]:
    return {
        "entity_type": ONBOARDING_JOB_ENTITY_TYPE,
        "action": ["accept_source", "ignore_non_finance", "reject_all", "needs_manual_review"],
        "selected_candidate_id": "candidate id, required only for accept_source",
        "review_category": list(ONBOARDING_REVIEW_CATEGORIES.keys()),
        "review_category_names": ONBOARDING_REVIEW_CATEGORIES,
        "inclusion_tier": ["core_finance", "finance_related", "exclude", "needs_review"],
        "source_attribute": sorted(SOURCE_ATTRIBUTE_TAGS),
        "requires_user_confirmation": "true only when the account or category needs human confirmation",
        "classification.category": [entry.id for entry in taxonomy.source_categories],
        "classification.tags": "taxonomy tag ids, including source_attribute tags when known",
        "classification.confidence": "0.0-1.0",
        "source_update.status": ["active", "inactive", "archived", "needs_review"],
        "source_update.tier": ["core", "normal", "long_tail"],
        "reason": "short Chinese explanation",
    }


def entry_to_dict(entry: TaxonomyEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "name_zh": entry.name_zh,
        "aliases_zh": list(entry.aliases_zh),
        "description_zh": entry.description_zh,
    }


def strip_html(value: Any) -> str | None:
    if not value:
        return None
    import html
    import re

    return html.unescape(re.sub(r"<[^>]+>", " ", str(value)))

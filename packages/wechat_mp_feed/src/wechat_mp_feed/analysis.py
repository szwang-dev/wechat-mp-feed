"""Deterministic rule-based classification and digest helpers."""

from __future__ import annotations

import html
import json
import re
from typing import Any

from .taxonomy import Taxonomy, TaxonomyEntry


RULE_METHOD = "rules_v1"


def classify_source(source: dict[str, Any], taxonomy: Taxonomy) -> dict[str, Any]:
    text = _join_text(source.get("name"), source.get("intro"), source.get("source_type"))
    category, confidence = _best_category(text, taxonomy.source_categories)
    tags = _matched_tags(text, taxonomy)
    return {
        "entity_type": "source",
        "entity_id": source["id"],
        "taxonomy": taxonomy.name,
        "category": category,
        "tags": tags,
        "confidence": confidence,
        "method": RULE_METHOD,
    }


def classify_article(article: dict[str, Any], taxonomy: Taxonomy) -> dict[str, Any]:
    text = _join_text(
        article.get("title"),
        article.get("digest"),
        article.get("content_text"),
        article.get("content_markdown"),
        _strip_html(article.get("content_html")),
    )
    category, confidence = _best_category(text, taxonomy.article_categories)
    tags = _matched_tags(text, taxonomy)
    if tags and confidence < 0.45:
        confidence = 0.45
    return {
        "entity_type": "article",
        "entity_id": article["id"],
        "taxonomy": taxonomy.name,
        "category": category,
        "tags": tags,
        "confidence": confidence,
        "method": RULE_METHOD,
    }


def generate_article_digest(
    article: dict[str, Any],
    classification: dict[str, Any],
    taxonomy: Taxonomy,
) -> dict[str, Any]:
    text = _join_text(
        article.get("digest"),
        article.get("content_text"),
        article.get("content_markdown"),
        _strip_html(article.get("content_html")),
    )
    sentences = _sentences(text)
    summary = article.get("digest") or (sentences[0] if sentences else article["title"])
    key_points = _key_points(article, sentences)
    importance_score = _importance_score(article, classification)
    reason = _importance_reason(classification, taxonomy)
    return {
        "article_id": article["id"],
        "summary": summary,
        "key_points": key_points,
        "importance_score": importance_score,
        "reason": reason,
        "model": RULE_METHOD,
        "analysis_stage": "rules",
    }


def _best_category(text: str, entries: tuple[TaxonomyEntry, ...]) -> tuple[str, float]:
    best_id = "uncategorized"
    best_hits = 0
    best_keyword_count = 1

    for entry in entries:
        hits = sum(_keyword_hits(text, keyword) for keyword in entry.keywords)
        if hits > best_hits:
            best_id = entry.id
            best_hits = hits
            best_keyword_count = max(1, len(entry.keywords))

    if best_hits == 0:
        return best_id, 0.0

    confidence = min(0.95, 0.35 + (best_hits / best_keyword_count) * 0.45)
    return best_id, round(confidence, 3)


def _matched_tags(text: str, taxonomy: Taxonomy) -> list[str]:
    matches: list[tuple[int, str]] = []
    for group in taxonomy.tag_groups:
        for tag in group.tags:
            hits = sum(_keyword_hits(text, keyword) for keyword in tag.keywords)
            if hits:
                matches.append((hits, tag.id))
    matches.sort(key=lambda item: (-item[0], item[1]))
    return [tag_id for _, tag_id in matches[:8]]


def _keyword_hits(text: str, keyword: str) -> int:
    keyword = keyword.strip()
    if not keyword:
        return 0
    if _is_ascii_keyword(keyword):
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(keyword.lower())}(?![A-Za-z0-9_])"
        return len(re.findall(pattern, text.lower()))
    return text.count(keyword)


def _importance_score(article: dict[str, Any], classification: dict[str, Any]) -> float:
    category = classification["category"]
    score = 0.25 + float(classification.get("confidence") or 0) * 0.35
    score += {
        "deep_research": 0.25,
        "policy_interpretation": 0.22,
        "risk_event": 0.22,
        "earnings_review": 0.18,
        "industry_tracking": 0.16,
        "company_tracking": 0.16,
        "market_signal": 0.14,
        "data_review": 0.12,
        "daily_commentary": 0.08,
        "recruiting_event": -0.15,
        "low_signal": -0.2,
    }.get(category, 0.0)
    text_length = len(_join_text(article.get("content_text"), article.get("content_markdown"), article.get("digest")))
    if text_length > 2500:
        score += 0.08
    elif text_length > 800:
        score += 0.04
    if classification.get("tags"):
        score += 0.04
    return round(max(0.0, min(1.0, score)), 3)


def _importance_reason(classification: dict[str, Any], taxonomy: Taxonomy) -> str:
    category = _entry_name(classification["category"], taxonomy.article_categories)
    tags = [_tag_name(tag_id, taxonomy) for tag_id in classification.get("tags", [])]
    tags = [tag for tag in tags if tag]
    if tags:
        return f"Matched {category}; related tags: {', '.join(tags[:5])}."
    return f"Matched {category}."


def _entry_name(entry_id: str, entries: tuple[TaxonomyEntry, ...]) -> str:
    for entry in entries:
        if entry.id == entry_id:
            return entry.name_zh or entry.id
    return entry_id


def _tag_name(tag_id: str, taxonomy: Taxonomy) -> str | None:
    for group in taxonomy.tag_groups:
        for tag in group.tags:
            if tag.id == tag_id:
                return tag.name_zh or tag.id
    return None


def _key_points(article: dict[str, Any], sentences: list[str]) -> list[str]:
    points = sentences[:3]
    if not points:
        points = [article["title"]]
    return [point[:240] for point in points]


def _sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    chunks = re.split(r"(?<=[。！？.!?])\s+", text)
    if len(chunks) == 1 and len(chunks[0]) > 180:
        chunks = [chunks[0][index : index + 180] for index in range(0, min(len(chunks[0]), 540), 180)]
    return [chunk.strip() for chunk in chunks if chunk.strip()]


def _strip_html(value: Any) -> str | None:
    if not value:
        return None
    text = re.sub(r"<[^>]+>", " ", str(value))
    return html.unescape(text)


def _join_text(*values: Any) -> str:
    parts = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple, dict)):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        text = str(value).strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def _is_ascii_keyword(keyword: str) -> bool:
    try:
        keyword.encode("ascii")
    except UnicodeEncodeError:
        return False
    return True

"""Collection policies for long-running feed tracking."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TierPolicy:
    tier: str
    max_sources: int
    article_count: int
    content_limit: int
    delay_min_seconds: float
    delay_max_seconds: float
    retries: int
    backoff_seconds: float


TIER_POLICIES = {
    "core": TierPolicy("core", max_sources=50, article_count=10, content_limit=50, delay_min_seconds=3, delay_max_seconds=8, retries=2, backoff_seconds=5),
    "normal": TierPolicy("normal", max_sources=100, article_count=5, content_limit=50, delay_min_seconds=5, delay_max_seconds=15, retries=2, backoff_seconds=8),
    "long_tail": TierPolicy("long_tail", max_sources=200, article_count=3, content_limit=30, delay_min_seconds=10, delay_max_seconds=30, retries=1, backoff_seconds=15),
}


ALL_TIER_POLICY = TierPolicy(
    "all",
    max_sources=50,
    article_count=5,
    content_limit=50,
    delay_min_seconds=5,
    delay_max_seconds=15,
    retries=1,
    backoff_seconds=10,
)


def tier_policy(tier: str) -> TierPolicy:
    if tier == "all":
        return ALL_TIER_POLICY
    return TIER_POLICIES[tier]


def retryable_status(payload: dict[str, Any]) -> bool:
    status = payload.get("status")
    if status == 0:
        return True
    if isinstance(status, int) and (status == 429 or 500 <= status <= 599):
        return True
    body = payload.get("body")
    error = body.get("error") if isinstance(body, dict) else None
    text = str(error or "")
    retryable_fragments = (
        "Rate limited",
        "过快",
        "重试",
        "Network is unreachable",
        "Connection refused",
        "Connection reset",
        "timed out",
        "Temporary failure",
        "Name or service not known",
        "nodename nor servname",
    )
    return any(fragment in text for fragment in retryable_fragments)

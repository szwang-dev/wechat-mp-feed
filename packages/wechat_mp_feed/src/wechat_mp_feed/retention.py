"""Article retention policy helpers."""

from __future__ import annotations

from dataclasses import dataclass


METADATA_LEVEL = "metadata"
CONTENT_LEVEL = "content"
FULL_ARCHIVE_LEVEL = "full_archive"

ARCHIVE_NOT_REQUESTED = "not_requested"
ARCHIVE_PENDING = "pending"
METADATA_FETCH_REQUIRED_THRESHOLD = 0.55
METADATA_FETCH_CANDIDATE_THRESHOLD = 0.40

LOW_SIGNAL_SOURCE_KEYWORDS = (
    "招聘",
    "求职",
    "内推",
    "career",
    "hr",
    "人事",
    "告示牌",
    "伯乐",
    "职位速递",
    "职位",
    "岗位",
)
LOW_SIGNAL_TITLE_KEYWORDS = (
    "招聘",
    "社招",
    "校招",
    "实习",
    "内推",
    "岗位",
    "职位",
)
LOW_SIGNAL_EVENT_KEYWORDS = (
    "邀请函",
    "报名",
    "课程",
    "活动",
    "策略会",
    "论坛",
    "会议邀请",
)
RESEARCH_EVENT_RESCUE_KEYWORDS = (
    "会议纪要",
    "纪要",
    "报告",
    "观点",
)
LOW_SIGNAL_GENERIC_KEYWORDS = (
    "招聘",
    "社招",
    "校招",
    "实习",
    "内推",
    "岗位",
    "职位",
    "邀请函",
    "报名",
    "课程",
    "活动",
)
RESEARCH_RESCUE_KEYWORDS = (
    "深度",
    "周报",
    "月报",
    "点评",
    "策略",
    "金工",
    "量化",
    "宏观",
    "固收",
    "行业",
    "公司",
    "因子",
    "模型",
    "配置",
    "轮动",
    "复盘",
    "会议纪要",
)


@dataclass(frozen=True)
class RetentionDecision:
    retention_level: str
    archive_status: str
    reason: str


def retention_decision_for_score(
    importance_score: float,
    content_threshold: float = 0.42,
    archive_threshold: float = 0.75,
    analysis_stage: str = "content",
    title: str | None = None,
    source_name: str | None = None,
    digest: str | None = None,
) -> RetentionDecision:
    """Map an article importance score to a storage-retention tier."""
    if analysis_stage == "metadata":
        triage_decision = metadata_triage_decision(
            importance_score,
            title=title,
            source_name=source_name,
            digest=digest,
        )
        if triage_decision == "metadata_only":
            return RetentionDecision(
                retention_level=METADATA_LEVEL,
                archive_status=ARCHIVE_NOT_REQUESTED,
                reason="metadata_triage=metadata_only; keep metadata only before content fetch",
            )
        return RetentionDecision(
            retention_level=CONTENT_LEVEL,
            archive_status=ARCHIVE_NOT_REQUESTED,
            reason=f"metadata_triage={triage_decision}; queue content fetch",
        )
    if importance_score >= archive_threshold:
        return RetentionDecision(
            retention_level=FULL_ARCHIVE_LEVEL,
            archive_status=ARCHIVE_PENDING,
            reason=f"importance_score >= {archive_threshold}; keep content and queue image archival",
        )
    if importance_score >= content_threshold:
        return RetentionDecision(
            retention_level=CONTENT_LEVEL,
            archive_status=ARCHIVE_NOT_REQUESTED,
            reason=f"importance_score >= {content_threshold}; keep article content and asset URLs",
        )
    return RetentionDecision(
        retention_level=METADATA_LEVEL,
        archive_status=ARCHIVE_NOT_REQUESTED,
        reason=f"importance_score < {content_threshold}; keep metadata only by policy",
    )


def metadata_triage_decision(
    importance_score: float,
    *,
    title: str | None = None,
    source_name: str | None = None,
    digest: str | None = None,
) -> str:
    """Map metadata-stage score plus deterministic noise checks to a content-fetch action."""
    if metadata_hard_exclude(title=title, source_name=source_name, digest=digest):
        return "metadata_only"
    if importance_score >= METADATA_FETCH_REQUIRED_THRESHOLD:
        return "fetch_required"
    if importance_score >= METADATA_FETCH_CANDIDATE_THRESHOLD:
        return "fetch_candidate"
    return "metadata_only"


def metadata_hard_exclude(
    *,
    title: str | None = None,
    source_name: str | None = None,
    digest: str | None = None,
) -> bool:
    title_text = (title or "").strip().lower()
    source_text = (source_name or "").strip().lower()
    digest_text = (digest or "").strip().lower()
    combined = f"{title_text} {digest_text}"
    if any(keyword.lower() in source_text for keyword in LOW_SIGNAL_SOURCE_KEYWORDS):
        return True
    if any(keyword.lower() in combined for keyword in LOW_SIGNAL_TITLE_KEYWORDS):
        return True
    if any(keyword.lower() in combined for keyword in LOW_SIGNAL_EVENT_KEYWORDS):
        return not any(keyword.lower() in combined for keyword in RESEARCH_EVENT_RESCUE_KEYWORDS)
    if not any(keyword.lower() in combined for keyword in LOW_SIGNAL_GENERIC_KEYWORDS):
        return False
    if any(keyword.lower() in combined for keyword in RESEARCH_RESCUE_KEYWORDS):
        return False
    return True

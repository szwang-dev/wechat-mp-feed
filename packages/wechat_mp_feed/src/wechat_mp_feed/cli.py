"""Command-line interface for mpfeed."""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import sys
import time
import zipfile
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from .adapters.wechat_download_api import WeChatDownloadAPIAdapter, WeChatDownloadAPIConfig
from .analysis import classify_article, classify_source, generate_article_digest
from .articles import normalize_article_items
from .assets import cache_full_archive_assets
from .candidates import normalize_source_candidates
from .content import normalize_article_content
from .llm_jobs import apply_llm_results, build_llm_jobs, build_onboarding_llm_jobs
from .media_import import extract_account_names_from_video, write_ocr_json
from .name_match import names_equivalent, search_query_variants
from .onboarding import (
    build_compact_onboarding_rows,
    build_onboarding_rows,
    manual_account_name_for_import,
    names_match_manual_or_raw,
    pick_best_candidate,
)
from .paths import resolve_db_path
from .policy import retryable_status, tier_policy
from .storage import Store
from .taxonomy import load_taxonomy


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mpfeed")
    parser.add_argument("--db", help="SQLite database path. Defaults to WECHAT_MP_FEED_DB or ~/.wechat-mp-feed/mpfeed.sqlite.")

    subcommands = parser.add_subparsers(dest="command", required=True)

    init_parser = subcommands.add_parser("init", help="Create the local database schema.")
    init_parser.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    init_parser.set_defaults(func=cmd_init)

    demo_parser = subcommands.add_parser("demo", help="Create small public demo datasets.")
    demo_subcommands = demo_parser.add_subparsers(dest="demo_command", required=True)

    demo_feed = demo_subcommands.add_parser("seed-feed", help="Seed a synthetic first-layer feed demo database.")
    demo_feed.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    demo_feed.add_argument("--work-dir", default="work/demo-feed", help="Directory for demo config and feed outputs.")
    demo_feed.add_argument("--config-output", help="Optional path for a generated offline feed config.")
    demo_feed.set_defaults(func=cmd_demo_seed_feed)

    doctor_parser = subcommands.add_parser("doctor", help="Check local database and downloader service readiness.")
    doctor_parser.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    doctor_parser.add_argument("--base-url", help="Service base URL. Defaults to WECHAT_DOWNLOAD_API_BASE_URL.")
    doctor_parser.add_argument("--timeout", type=float, default=10, help="HTTP timeout in seconds.")
    doctor_parser.set_defaults(func=cmd_doctor)

    import_parser = subcommands.add_parser("import", help="Import sources into the reviewable queue.")
    import_subcommands = import_parser.add_subparsers(dest="import_command", required=True)

    import_url = import_subcommands.add_parser("url", help="Import a single WeChat article URL.")
    import_url.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    import_url.add_argument("url")
    import_url.set_defaults(func=cmd_import_url)

    import_urls = import_subcommands.add_parser("urls", help="Import WeChat article URLs from a text file.")
    import_urls.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    import_urls.add_argument("file")
    import_urls.set_defaults(func=cmd_import_urls)

    import_names = import_subcommands.add_parser("names", help="Import account names from a newline-delimited text file.")
    import_names.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    import_names.add_argument("--source-type", default="names")
    import_names.add_argument("file")
    import_names.set_defaults(func=cmd_import_names)

    import_video = import_subcommands.add_parser("video", help="Import account names from a WeChat following-list recording.")
    import_video.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    import_video.add_argument("--ocr", choices=("paddle",), default="paddle")
    import_video.add_argument("--fps", type=float, default=1.0, help="Frames per second to OCR.")
    import_video.add_argument("--crop", help="Optional crop rectangle as x,y,w,h before OCR.")
    import_video.add_argument("--scale-width", type=int, help="Optional output frame width before OCR, preserving aspect ratio.")
    import_video.add_argument("--lang", default="ch", help="PaddleOCR language, default: ch.")
    import_video.add_argument("--source-type", default="recording")
    import_video.add_argument("--dedupe-threshold", type=float, default=0.92)
    import_video.add_argument(
        "--min-occurrences",
        type=int,
        default=1,
        help="Keep OCR names seen in at least this many frames. Use 2+ for dense slow recordings.",
    )
    import_video.add_argument("--save-frames", help="Optional directory to keep extracted frames.")
    import_video.add_argument("--names-output", help="Optional text output path for detected account names.")
    import_video.add_argument("--raw-output", help="Optional JSON output path for OCR details.")
    import_video.add_argument("file")
    import_video.set_defaults(func=cmd_import_video)

    import_csv = import_subcommands.add_parser("csv", help="Import account names from a CSV file.")
    import_csv.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    import_csv.add_argument("--name-column", default="name")
    import_csv.add_argument("--url-column")
    import_csv.add_argument("file")
    import_csv.set_defaults(func=cmd_import_csv)

    import_json = import_subcommands.add_parser("json", help="Import account names from a JSON file.")
    import_json.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    import_json.add_argument("--name-field", default="name")
    import_json.add_argument("--url-field")
    import_json.add_argument("file")
    import_json.set_defaults(func=cmd_import_json)

    export_parser = subcommands.add_parser("export", help="Export stored data.")
    export_subcommands = export_parser.add_subparsers(dest="export_command", required=True)

    export_imports = export_subcommands.add_parser("imports", help="Export source_imports.")
    export_imports.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    export_imports.add_argument("--format", choices=("json", "csv"), default="json")
    export_imports.add_argument("--limit", type=int, default=100)
    export_imports.add_argument("--status")
    export_imports.add_argument("--source-type")
    export_imports.set_defaults(func=cmd_export_imports)

    export_sources = export_subcommands.add_parser("sources", help="Export accepted sources.")
    export_sources.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    export_sources.add_argument("--format", choices=("json", "csv"), default="json")
    export_sources.add_argument("--limit", type=int, default=100)
    export_sources.set_defaults(func=cmd_export_sources)

    export_articles = export_subcommands.add_parser("articles", help="Export collected articles.")
    export_articles.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    export_articles.add_argument("--format", choices=("json", "csv"), default="json")
    export_articles.add_argument("--limit", type=int, default=100)
    export_articles.add_argument("--source-id")
    export_articles.set_defaults(func=cmd_export_articles)

    export_feed = export_subcommands.add_parser("feed", help="Export user-friendly feed items with source, content, and digest status.")
    export_feed.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    export_feed.add_argument("--format", choices=("json", "csv"), default="json")
    export_feed.add_argument("--limit", type=int, default=100)
    export_feed.add_argument("--source-id")
    export_feed.add_argument("--tier", choices=("core", "normal", "long_tail"))
    export_feed.add_argument("--status", choices=("active", "inactive", "archived", "needs_review"))
    export_feed.add_argument("--crawl-status", choices=("metadata_only", "content_ok", "content_failed", "deleted"))
    export_feed.set_defaults(func=cmd_export_feed)

    export_feed_summary = export_subcommands.add_parser("feed-summary", help="Export feed pipeline counts for monitoring.")
    export_feed_summary.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    export_feed_summary.add_argument("--format", choices=("json", "csv"), default="json")
    export_feed_summary.set_defaults(func=cmd_export_feed_summary)

    export_contents = export_subcommands.add_parser("contents", help="Export fetched article contents.")
    export_contents.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    export_contents.add_argument("--format", choices=("json", "csv"), default="json")
    export_contents.add_argument("--limit", type=int, default=100)
    export_contents.set_defaults(func=cmd_export_contents)

    export_candidates = export_subcommands.add_parser("candidates", help="Export source candidates for review.")
    export_candidates.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    export_candidates.add_argument("--format", choices=("json", "csv"), default="json")
    export_candidates.add_argument("--decision", default="pending", help="Candidate decision filter. Use 'all' for no filter.")
    export_candidates.add_argument("--limit", type=int, default=100)
    export_candidates.set_defaults(func=cmd_export_candidates)

    export_onboarding = export_subcommands.add_parser("onboarding", help="Export first-run source onboarding review table.")
    export_onboarding.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    export_onboarding.add_argument("--format", choices=("json", "csv"), default="csv")
    export_onboarding.add_argument("--view", choices=("full", "compact"), default="full")
    export_onboarding.add_argument("--source-type")
    export_onboarding.add_argument("--taxonomy", default="finance")
    export_onboarding.add_argument("--limit", type=int, default=1000)
    export_onboarding.set_defaults(func=cmd_export_onboarding)

    export_classifications = export_subcommands.add_parser("classifications", help="Export classifications.")
    export_classifications.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    export_classifications.add_argument("--format", choices=("json", "csv"), default="json")
    export_classifications.add_argument("--limit", type=int, default=100)
    export_classifications.add_argument("--entity-type", choices=("source", "article"))
    export_classifications.add_argument("--taxonomy")
    export_classifications.set_defaults(func=cmd_export_classifications)

    export_digests = export_subcommands.add_parser("digests", help="Export generated digests.")
    export_digests.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    export_digests.add_argument("--format", choices=("json", "csv", "markdown"), default="json")
    export_digests.add_argument("--limit", type=int, default=100)
    export_digests.add_argument("--application-target", help="Filter by digest.application_targets id, such as weekly_report.")
    export_digests.add_argument("--min-score", type=float, help="Filter by minimum stored importance score.")
    export_digests.add_argument("--analysis-stage", choices=("metadata", "content", "rules"), help="Filter by digest analysis stage.")
    export_digests.set_defaults(func=cmd_export_digests)

    export_digest_context = export_subcommands.add_parser(
        "digest-context",
        help="Export digest rows with full article content and image asset context for downstream application digests.",
    )
    export_digest_context.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    export_digest_context.add_argument("--format", choices=("json", "jsonl", "markdown"), default="json")
    export_digest_context.add_argument("--limit", type=int, default=100)
    export_digest_context.add_argument("--application-target", help="Filter by digest.application_targets id, such as weekly_report.")
    export_digest_context.add_argument("--min-score", type=float, help="Filter by minimum stored importance score.")
    export_digest_context.add_argument("--analysis-stage", choices=("metadata", "content", "rules"), help="Filter by digest analysis stage.")
    export_digest_context.add_argument("--max-content-chars", type=int, default=0, help="Truncate text/markdown/html fields. Use 0 for full content.")
    export_digest_context.add_argument("--include-html", action="store_true", help="Include content_html in exported context.")
    export_digest_context.add_argument("--no-assets", action="store_true", help="Do not include article_assets rows.")
    export_digest_context.add_argument("--require-content", action="store_true", help="Only export rows with fetched article content.")
    export_digest_context.set_defaults(func=cmd_export_digest_context)

    resolve_parser = subcommands.add_parser("resolve", help="Resolve imported source names into candidates.")
    resolve_subcommands = resolve_parser.add_subparsers(dest="resolve_command", required=True)

    resolve_search = resolve_subcommands.add_parser("search", help="Search candidates with the HTTP downloader service.")
    resolve_search.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    resolve_search.add_argument("--base-url", help="Service base URL. Defaults to WECHAT_DOWNLOAD_API_BASE_URL.")
    resolve_search.add_argument("--timeout", type=float, default=30, help="HTTP timeout in seconds.")
    resolve_search.add_argument("query")
    resolve_search.set_defaults(func=cmd_resolve_search)

    resolve_imports = resolve_subcommands.add_parser("imports", help="Search candidates for pending imported source names.")
    resolve_imports.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    resolve_imports.add_argument("--base-url", help="Service base URL. Defaults to WECHAT_DOWNLOAD_API_BASE_URL.")
    resolve_imports.add_argument("--timeout", type=float, default=30, help="HTTP timeout in seconds.")
    resolve_imports.add_argument("--limit", type=int, default=20)
    resolve_imports.add_argument("--source-type")
    resolve_imports.add_argument("--status", default="pending", help="Import status to resolve. Use 'all' for no status filter.")
    resolve_imports.add_argument("--retry-empty", action="store_true", help="Only retry imports that do not have any saved candidates.")
    resolve_imports.add_argument("--query-variants", action="store_true", help="Also search normalized query variants when resolving names.")
    resolve_imports.add_argument(
        "--replace-pending-candidates",
        action="store_true",
        help="Replace existing pending candidates for each import before saving new search results.",
    )
    resolve_imports.add_argument("--delay-min", type=float, default=1.0)
    resolve_imports.add_argument("--delay-max", type=float, default=3.0)
    resolve_imports.add_argument("--no-delay", action="store_true", help="Disable inter-query delay for local testing.")
    resolve_imports.set_defaults(func=cmd_resolve_imports)

    resolve_manual_names = resolve_subcommands.add_parser(
        "manual-names",
        help="Search only onboarding rows that have a manually confirmed account name.",
    )
    resolve_manual_names.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    resolve_manual_names.add_argument("--base-url", help="Service base URL. Defaults to WECHAT_DOWNLOAD_API_BASE_URL.")
    resolve_manual_names.add_argument("--timeout", type=float, default=30, help="HTTP timeout in seconds.")
    resolve_manual_names.add_argument("--limit", type=int, default=1000)
    resolve_manual_names.add_argument("--source-type")
    resolve_manual_names.add_argument("--status", default="needs_review", help="Import status to resolve. Use 'all' for no status filter.")
    resolve_manual_names.add_argument("--query-variants", action="store_true", default=True)
    resolve_manual_names.add_argument("--replace-pending-candidates", action="store_true")
    resolve_manual_names.add_argument("--delay-min", type=float, default=1.0)
    resolve_manual_names.add_argument("--delay-max", type=float, default=3.0)
    resolve_manual_names.add_argument("--no-delay", action="store_true")
    resolve_manual_names.set_defaults(func=cmd_resolve_manual_names)

    review_parser = subcommands.add_parser("review", help="Review and promote source candidates.")
    review_subcommands = review_parser.add_subparsers(dest="review_command", required=True)

    review_list = review_subcommands.add_parser("list", help="List source candidates.")
    review_list.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    review_list.add_argument("--decision", default="pending", help="Candidate decision filter. Use 'all' for no filter.")
    review_list.add_argument("--limit", type=int, default=100)
    review_list.set_defaults(func=cmd_review_list)

    review_auto_exact = review_subcommands.add_parser("auto-exact", help="Auto-accept exact high-score candidates.")
    review_auto_exact.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    review_auto_exact.add_argument("--source-type")
    review_auto_exact.add_argument("--limit", type=int, default=200)
    review_auto_exact.add_argument("--exact-score", type=float, default=0.9)
    review_auto_exact.add_argument("--taxonomy", default="finance")
    review_auto_exact.add_argument("--finance-only", action="store_true", help="Only accept candidates matched by the finance taxonomy.")
    review_auto_exact.add_argument("--min-confidence", type=float, default=0.35)
    review_auto_exact.add_argument("--dry-run", action="store_true")
    review_auto_exact.set_defaults(func=cmd_review_auto_exact)

    review_accept = review_subcommands.add_parser("accept", help="Accept a candidate into the source registry.")
    review_accept.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    review_accept.add_argument("--tier", choices=("core", "normal", "long_tail"), default="normal")
    review_accept.add_argument("candidate_id")
    review_accept.set_defaults(func=cmd_review_accept)

    review_reject = review_subcommands.add_parser("reject", help="Reject a candidate.")
    review_reject.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    review_reject.add_argument("candidate_id")
    review_reject.set_defaults(func=cmd_review_reject)

    review_apply = review_subcommands.add_parser("apply", help="Apply accept/reject decisions from a CSV or JSON file.")
    review_apply.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    review_apply.add_argument("file")
    review_apply.set_defaults(func=cmd_review_apply)

    review_apply_onboarding = review_subcommands.add_parser(
        "apply-onboarding",
        help="Apply first-run onboarding review edits from a CSV, JSON, or XLSX file.",
    )
    review_apply_onboarding.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    review_apply_onboarding.add_argument("--source-type")
    review_apply_onboarding.add_argument("--taxonomy", default="finance")
    review_apply_onboarding.add_argument("file")
    review_apply_onboarding.set_defaults(func=cmd_review_apply_onboarding)

    review_import_classified = review_subcommands.add_parser(
        "import-classified-sources",
        help="Import reviewed onboarding classification/freshness JSON into the source registry.",
    )
    review_import_classified.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    review_import_classified.add_argument("--taxonomy", default="finance")
    review_import_classified.add_argument("--source-type", default="reviewed_onboarding")
    review_import_classified.add_argument(
        "--include-tiers",
        default="core_finance,finance_related",
        help="Comma-separated inclusion_tier values to import.",
    )
    review_import_classified.add_argument("--allow-unfetchable", action="store_true")
    review_import_classified.add_argument("--dry-run", action="store_true")
    review_import_classified.add_argument("file")
    review_import_classified.set_defaults(func=cmd_review_import_classified_sources)

    review_validate_classified = review_subcommands.add_parser(
        "validate-classified-sources",
        help="Validate a final reviewed onboarding classification/freshness table before source import.",
    )
    review_validate_classified.add_argument(
        "--include-tiers",
        default="core_finance,finance_related",
        help="Comma-separated inclusion tiers to validate as formal source imports.",
    )
    review_validate_classified.add_argument("--format", choices=("json", "csv"), default="json")
    review_validate_classified.add_argument(
        "--allow-unfetchable",
        action="store_true",
        help="Allow rows whose article metadata exists but latest article content cannot be fetched.",
    )
    review_validate_classified.add_argument("file")
    review_validate_classified.set_defaults(func=cmd_review_validate_classified_sources)

    collect_parser = subcommands.add_parser("collect", help="Collect articles from accepted sources.")
    collect_subcommands = collect_parser.add_subparsers(dest="collect_command", required=True)

    collect_latest = collect_subcommands.add_parser("latest", help="Collect latest article metadata through the HTTP downloader service.")
    collect_latest.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    collect_latest.add_argument("--base-url", help="Service base URL. Defaults to WECHAT_DOWNLOAD_API_BASE_URL.")
    collect_latest.add_argument("--timeout", type=float, default=30, help="HTTP timeout in seconds.")
    collect_latest.add_argument("--tier", choices=("all", "core", "normal", "long_tail"), default="core")
    collect_latest.add_argument("--max-sources", type=int)
    collect_latest.add_argument("--begin", type=int, default=0)
    collect_latest.add_argument("--count", type=int)
    collect_latest.add_argument("--delay-min", type=float)
    collect_latest.add_argument("--delay-max", type=float)
    collect_latest.add_argument("--retries", type=int)
    collect_latest.add_argument("--backoff-seconds", type=float)
    collect_latest.add_argument("--no-delay", action="store_true", help="Disable inter-source delay for local testing.")
    collect_latest.set_defaults(func=cmd_collect_latest)

    collect_history = collect_subcommands.add_parser(
        "history",
        help="Backfill recent article metadata for active sources by paging the downloader article list.",
    )
    collect_history.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    collect_history.add_argument("--base-url", help="Service base URL. Defaults to WECHAT_DOWNLOAD_API_BASE_URL.")
    collect_history.add_argument("--timeout", type=float, default=30, help="HTTP timeout in seconds.")
    collect_history.add_argument("--days", type=int, default=90, help="Backfill articles newer than this many days.")
    collect_history.add_argument("--tier", choices=("all", "core", "normal", "long_tail"), default="all")
    collect_history.add_argument("--source-offset", type=int, default=0, help="Start offset in the active source list.")
    collect_history.add_argument("--max-sources", type=int, default=20, help="Max sources in this batch. Use 0 for all after offset.")
    collect_history.add_argument("--source-name", help="Optional exact source name filter for repair runs.")
    collect_history.add_argument("--count", type=int, default=100, help="Downloader page size. The current downloader supports up to 100.")
    collect_history.add_argument("--max-pages-per-source", type=int, default=20, help="Safety cap per source.")
    collect_history.add_argument("--delay-min", type=float, default=2.5)
    collect_history.add_argument("--delay-max", type=float, default=6.0)
    collect_history.add_argument("--page-delay-min", type=float, default=1.5)
    collect_history.add_argument("--page-delay-max", type=float, default=3.5)
    collect_history.add_argument("--retries", type=int, default=2)
    collect_history.add_argument("--backoff-seconds", type=float, default=8.0)
    collect_history.add_argument("--dry-run", action="store_true", help="Fetch and report without saving articles.")
    collect_history.add_argument("--no-delay", action="store_true", help="Disable source/page delays for local testing.")
    collect_history.set_defaults(func=cmd_collect_history)

    collect_content = collect_subcommands.add_parser("content", help="Fetch content for collected articles.")
    collect_content.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    collect_content.add_argument("--base-url", help="Service base URL. Defaults to WECHAT_DOWNLOAD_API_BASE_URL.")
    collect_content.add_argument("--timeout", type=float, default=30, help="HTTP timeout in seconds.")
    collect_content.add_argument("--tier", choices=("all", "core", "normal", "long_tail"), default="core")
    collect_content.add_argument("--limit", type=int)
    collect_content.add_argument("--delay-min", type=float)
    collect_content.add_argument("--delay-max", type=float)
    collect_content.add_argument("--retries", type=int)
    collect_content.add_argument("--backoff-seconds", type=float)
    collect_content.add_argument("--passes", type=int, default=1, help="How many internal passes to run over the same content queue.")
    collect_content.add_argument("--pass-cooldown-seconds", type=float, default=0.0, help="Cooldown between content passes.")
    collect_content.add_argument("--progress-every", type=int, default=10, help="Emit content-fetch progress every N articles.")
    collect_content.add_argument("--no-delay", action="store_true", help="Disable inter-article delay for local testing.")
    collect_content.set_defaults(func=cmd_collect_content)

    collect_candidate_latest = collect_subcommands.add_parser(
        "candidate-latest",
        help="Probe latest article metadata for unresolved source candidates without accepting them.",
    )
    collect_candidate_latest.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    collect_candidate_latest.add_argument("--base-url", help="Service base URL. Defaults to WECHAT_DOWNLOAD_API_BASE_URL.")
    collect_candidate_latest.add_argument("--timeout", type=float, default=30, help="HTTP timeout in seconds.")
    collect_candidate_latest.add_argument("--source-type")
    collect_candidate_latest.add_argument("--decision", default="pending", help="Candidate decision filter. Use 'all' for no filter.")
    collect_candidate_latest.add_argument("--limit", type=int, default=50)
    collect_candidate_latest.add_argument("--count", type=int, default=1)
    collect_candidate_latest.add_argument(
        "--best-per-import",
        action="store_true",
        help="Probe only the current best candidate for each import instead of arbitrary candidate rows.",
    )
    collect_candidate_latest.add_argument(
        "--strict-match-only",
        action="store_true",
        help="With --best-per-import, probe only candidates whose name strictly matches the OCR/manual account name.",
    )
    collect_candidate_latest.add_argument("--delay-min", type=float, default=1.0)
    collect_candidate_latest.add_argument("--delay-max", type=float, default=3.0)
    collect_candidate_latest.add_argument(
        "--validate-content",
        action="store_true",
        help="Fetch candidate article URLs until a usable latest article is found.",
    )
    collect_candidate_latest.add_argument("--no-delay", action="store_true")
    collect_candidate_latest.set_defaults(func=cmd_collect_candidate_latest)

    classify_parser = subcommands.add_parser("classify", help="Classify sources or articles with a local taxonomy.")
    classify_subcommands = classify_parser.add_subparsers(dest="classify_command", required=True)

    classify_sources = classify_subcommands.add_parser("sources", help="Classify accepted sources.")
    classify_sources.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    classify_sources.add_argument("--taxonomy", default="finance")
    classify_sources.add_argument("--limit", type=int, default=100)
    classify_sources.set_defaults(func=cmd_classify_sources)

    classify_articles = classify_subcommands.add_parser("articles", help="Classify collected articles.")
    classify_articles.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    classify_articles.add_argument("--taxonomy", default="finance")
    classify_articles.add_argument("--limit", type=int, default=100)
    classify_articles.add_argument("--source-id")
    classify_articles.set_defaults(func=cmd_classify_articles)

    source_parser = subcommands.add_parser("source", help="Agent-safe source status, taxonomy, classification, and refresh helpers.")
    source_subcommands = source_parser.add_subparsers(dest="source_command", required=True)

    source_status = source_subcommands.add_parser("status", help="Return a source snapshot without writing to the database.")
    source_status.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    source_status.add_argument("--name", default="")
    source_status.add_argument("--source-id", default="")
    source_status.add_argument("--biz", default="")
    source_status.add_argument("--taxonomy", default="finance")
    source_status.set_defaults(func=cmd_source_status)

    source_set_status = source_subcommands.add_parser("set-status", help="Set a source status through an agent-safe write path.")
    source_set_status.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    source_set_status.add_argument("--name", default="")
    source_set_status.add_argument("--source-id", default="")
    source_set_status.add_argument("--biz", default="")
    source_set_status.add_argument("--status", choices=("active", "inactive", "archived", "needs_review"), required=True)
    source_set_status.set_defaults(func=cmd_source_set_status)

    source_taxonomy = source_subcommands.add_parser("taxonomy", help="Return allowed source categories, attributes, and tags.")
    source_taxonomy.add_argument("--taxonomy", default="finance")
    source_taxonomy.set_defaults(func=cmd_source_taxonomy)

    source_confirm = source_subcommands.add_parser("confirm-classification", help="Validate and persist a user-confirmed source classification.")
    source_confirm.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    source_confirm.add_argument("--name", default="")
    source_confirm.add_argument("--source-id", default="")
    source_confirm.add_argument("--biz", default="")
    source_confirm.add_argument("--taxonomy", default="finance")
    source_confirm.add_argument("--category", required=True)
    source_confirm.add_argument("--source-attribute", default="")
    source_confirm.add_argument("--tags", default="", help="Comma-separated tag ids.")
    source_confirm.add_argument("--confidence", type=float, default=0.7)
    source_confirm.add_argument("--method", default="manual:agent_confirmed")
    source_confirm.add_argument("--review-id", default="", help="Optional pending source classification review id to mark confirmed.")
    source_confirm.set_defaults(func=cmd_source_confirm_classification)

    source_refresh = source_subcommands.add_parser("refresh-latest", help="Refresh latest article metadata for one active source.")
    source_refresh.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    source_refresh.add_argument("--base-url", help="Service base URL. Defaults to WECHAT_DOWNLOAD_API_BASE_URL.")
    source_refresh.add_argument("--timeout", type=float, default=30)
    source_refresh.add_argument("--name", default="")
    source_refresh.add_argument("--source-id", default="")
    source_refresh.add_argument("--biz", default="")
    source_refresh.add_argument("--count", type=int, default=5)
    source_refresh.add_argument("--retries", type=int, default=2)
    source_refresh.add_argument("--backoff-seconds", type=float, default=3.0)
    source_refresh.set_defaults(func=cmd_source_refresh_latest)

    digest_parser = subcommands.add_parser("digest", help="Generate reviewable article digests.")
    digest_subcommands = digest_parser.add_subparsers(dest="digest_command", required=True)

    digest_articles = digest_subcommands.add_parser("articles", help="Generate article digests with local rules.")
    digest_articles.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    digest_articles.add_argument("--taxonomy", default="finance")
    digest_articles.add_argument("--limit", type=int, default=100)
    digest_articles.add_argument("--source-id")
    digest_articles.add_argument("--min-score", type=float, default=0.0)
    digest_articles.set_defaults(func=cmd_digest_articles)

    archive_parser = subcommands.add_parser("archive", help="Archive retained article assets.")
    archive_subcommands = archive_parser.add_subparsers(dest="archive_command", required=True)

    archive_assets = archive_subcommands.add_parser("assets", help="Cache image assets for full_archive articles.")
    archive_assets.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    archive_assets.add_argument("--output-dir", default="work/archive/assets")
    archive_assets.add_argument("--limit", type=int, default=100)
    archive_assets.add_argument("--timeout", type=float, default=30.0)
    archive_assets.add_argument("--overwrite", action="store_true")
    archive_assets.set_defaults(func=cmd_archive_assets)

    llm_parser = subcommands.add_parser("llm", help="Export/import agent-agnostic LLM analysis jobs.")
    llm_subcommands = llm_parser.add_subparsers(dest="llm_command", required=True)

    llm_export = llm_subcommands.add_parser("export-jobs", help="Export review/classification/digest jobs for an LLM agent.")
    llm_export.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    llm_export.add_argument("--taxonomy", default="finance")
    llm_export.add_argument("--entity-type", choices=("all", "source", "article"), default="all")
    llm_export.add_argument("--limit", type=int, default=100)
    llm_export.add_argument("--source-id")
    llm_export.add_argument("--content-chars", type=int, default=6000)
    llm_export.add_argument("--source-article-limit", type=int, default=5)
    llm_export.add_argument("--article-content-scope", choices=("content", "metadata", "all"), default="content")
    llm_export.add_argument("--article-updated-after", help="Export only article jobs whose article.updated_at is at or after this ISO timestamp.")
    llm_export.add_argument("--article-published-after", help="Export only article jobs whose publish_time is at or after this ISO timestamp.")
    llm_export.add_argument("--article-published-before", help="Export only article jobs whose publish_time is at or before this ISO timestamp.")
    llm_export.add_argument(
        "--article-retention",
        choices=("metadata", "content", "full_archive", "content_or_archive", "all"),
        default="all",
        help="Filter article jobs by retention level.",
    )
    llm_export.add_argument(
        "--article-analysis-stage",
        choices=("metadata", "content"),
        help="Declare the expected article LLM stage. Defaults to metadata for metadata scope, otherwise content.",
    )
    llm_export.add_argument("--output", help="Optional JSON output path. Defaults to stdout.")
    llm_export.set_defaults(func=cmd_llm_export_jobs)

    llm_export_onboarding = llm_subcommands.add_parser(
        "export-onboarding-jobs",
        help="Export first-run source onboarding jobs for an LLM agent.",
    )
    llm_export_onboarding.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    llm_export_onboarding.add_argument("--taxonomy", default="finance")
    llm_export_onboarding.add_argument("--source-type")
    llm_export_onboarding.add_argument("--decision", default="pending", help="Candidate decision filter. Use 'all' for no filter.")
    llm_export_onboarding.add_argument("--limit", type=int, default=100)
    llm_export_onboarding.add_argument("--candidate-limit", type=int, default=5)
    llm_export_onboarding.add_argument("--article-limit", type=int, default=3)
    llm_export_onboarding.add_argument(
        "--strict-match-only",
        action="store_true",
        help="Export jobs only for imports whose best candidate strictly matches the OCR/manual account name.",
    )
    llm_export_onboarding.add_argument("--output", help="Optional JSON output path. Defaults to stdout.")
    llm_export_onboarding.set_defaults(func=cmd_llm_export_onboarding_jobs)

    llm_import = llm_subcommands.add_parser("import-results", help="Import LLM classification/source/digest results.")
    llm_import.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    llm_import.add_argument("--taxonomy", default="finance")
    llm_import.add_argument("--model", default="llm:agent")
    llm_import.add_argument("file")
    llm_import.set_defaults(func=cmd_llm_import_results)

    run_parser = subcommands.add_parser("run", help="Run higher-level workflows.")
    run_subcommands = run_parser.add_subparsers(dest="run_command", required=True)

    run_batch = run_subcommands.add_parser("batch", help="Run import/resolve/review/collect/digest for a small batch.")
    run_batch.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    run_batch.add_argument("--base-url", help="Service base URL. Defaults to WECHAT_DOWNLOAD_API_BASE_URL.")
    run_batch.add_argument("--timeout", type=float, default=30, help="HTTP timeout in seconds.")
    run_batch.add_argument("--names-file", help="Optional newline-delimited account list to import first.")
    run_batch.add_argument("--source-type", default="screenshot")
    run_batch.add_argument("--resolve-limit", type=int, default=50)
    run_batch.add_argument("--candidate-limit", type=int, default=200)
    run_batch.add_argument("--exact-score", type=float, default=0.9)
    run_batch.add_argument("--no-auto-exact", action="store_true", help="Do not auto-accept exact high-score candidate matches.")
    run_batch.add_argument("--tier", choices=("all", "core", "normal", "long_tail"), default="all")
    run_batch.add_argument("--max-sources", type=int, default=20)
    run_batch.add_argument("--article-count", type=int, default=3)
    run_batch.add_argument("--content-limit", type=int, default=20)
    run_batch.add_argument("--digest-limit", type=int, default=50)
    run_batch.add_argument("--min-score", type=float, default=0.35)
    run_batch.add_argument("--taxonomy", default="finance")
    run_batch.add_argument("--digest-output", help="Optional markdown path for the digest export.")
    run_batch.add_argument("--no-delay", action="store_true", help="Disable delays for local testing.")
    run_batch.set_defaults(func=cmd_run_batch)

    run_agent_smoke = run_subcommands.add_parser(
        "agent-smoke",
        help="Run an offline agent smoke test with synthetic feed data.",
    )
    run_agent_smoke.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    run_agent_smoke.add_argument("--work-dir", default="work/agent-smoke")
    run_agent_smoke.add_argument("--taxonomy", default="finance")
    run_agent_smoke.add_argument("--limit", type=int, default=20)
    run_agent_smoke.add_argument("--content-chars", type=int, default=2500)
    run_agent_smoke.set_defaults(func=cmd_run_agent_smoke)

    run_llm_feed = run_subcommands.add_parser(
        "llm-feed",
        help="Prepare and advance the two-stage LLM feed workflow.",
    )
    run_llm_feed.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    run_llm_feed.add_argument("--taxonomy", default="finance")
    run_llm_feed.add_argument("--work-dir", default="work/feed-llm")
    run_llm_feed.add_argument("--limit", type=int, default=0, help="Maximum article jobs per stage. Use 0 for all matching articles.")
    run_llm_feed.add_argument("--published-after", help="Article publish_time lower bound for both LLM stages.")
    run_llm_feed.add_argument("--published-before", help="Article publish_time upper bound for both LLM stages.")
    run_llm_feed.add_argument("--metadata-results", help="Optional metadata-stage LLM results to import before content fetch.")
    run_llm_feed.add_argument("--content-results", help="Optional content-stage LLM results to import before digest-pack export.")
    run_llm_feed.add_argument("--model", default="llm:agent")
    run_llm_feed.add_argument("--content-chars", type=int, default=6000)
    run_llm_feed.add_argument("--fetch-content", action="store_true", help="Fetch retained content after metadata results are imported.")
    run_llm_feed.add_argument("--base-url", help="Service base URL. Defaults to WECHAT_DOWNLOAD_API_BASE_URL.")
    run_llm_feed.add_argument("--timeout", type=float, default=45, help="HTTP timeout in seconds for content fetches.")
    run_llm_feed.add_argument("--content-limit", type=int, default=0, help="Retained article count to fetch. Use 0 for all matching articles.")
    run_llm_feed.add_argument(
        "--content-retention",
        choices=("content_or_archive", "content", "full_archive", "all"),
        default="content_or_archive",
        help="Which retention tier is eligible for content fetching.",
    )
    run_llm_feed.add_argument("--content-retries", type=int, default=2)
    run_llm_feed.add_argument("--content-backoff-seconds", type=float, default=5.0)
    run_llm_feed.add_argument("--content-delay-min", type=float, default=4.0)
    run_llm_feed.add_argument("--content-delay-max", type=float, default=8.0)
    run_llm_feed.add_argument("--content-passes", type=int, default=3)
    run_llm_feed.add_argument("--content-pass-cooldown-seconds", type=float, default=30.0)
    run_llm_feed.add_argument("--progress-every", type=int, default=20)
    run_llm_feed.add_argument("--digest-min-score", type=float, default=0.6)
    run_llm_feed.add_argument("--no-delay", action="store_true", help="Disable delays for local testing.")
    run_llm_feed.set_defaults(func=cmd_run_llm_feed)

    run_onboarding = run_subcommands.add_parser(
        "onboarding",
        help="Run first-run account onboarding: import, multi-round search, evidence probe, and review exports.",
    )
    run_onboarding.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    run_onboarding.add_argument("--base-url", help="Service base URL. Defaults to WECHAT_DOWNLOAD_API_BASE_URL.")
    run_onboarding.add_argument("--timeout", type=float, default=30, help="HTTP timeout in seconds.")
    run_onboarding.add_argument("--names-file", help="Optional newline-delimited account list to import first.")
    run_onboarding.add_argument("--video-file", help="Optional WeChat following-list recording to OCR and import first.")
    run_onboarding.add_argument("--ocr", choices=("paddle",), default="paddle")
    run_onboarding.add_argument("--fps", type=float, default=2.0)
    run_onboarding.add_argument("--crop", help="Optional crop rectangle as x,y,w,h before OCR.")
    run_onboarding.add_argument("--scale-width", type=int, help="Optional output frame width before OCR, preserving aspect ratio.")
    run_onboarding.add_argument("--lang", default="ch")
    run_onboarding.add_argument("--dedupe-threshold", type=float, default=0.92)
    run_onboarding.add_argument("--min-occurrences", type=int, default=2)
    run_onboarding.add_argument("--save-frames", help="Optional directory to keep extracted frames.")
    run_onboarding.add_argument("--names-output", help="Optional text output path for detected account names.")
    run_onboarding.add_argument("--raw-output", help="Optional JSON output path for OCR details.")
    run_onboarding.add_argument("--source-type", default="onboarding")
    run_onboarding.add_argument("--limit", type=int, default=1000)
    run_onboarding.add_argument("--taxonomy", default="finance")
    run_onboarding.add_argument("--work-dir", default="work")
    run_onboarding.add_argument("--llm-jobs-output", help="JSON output path for LLM onboarding jobs.")
    run_onboarding.add_argument("--llm-results", help="Optional LLM onboarding results JSON to import before final review export.")
    run_onboarding.add_argument("--review-output", help="Compact review table output path.")
    run_onboarding.add_argument("--review-format", choices=("csv", "json"), default="csv")
    run_onboarding.add_argument("--candidate-count", type=int, default=3, help="Latest article count to probe for each best candidate.")
    run_onboarding.add_argument("--candidate-limit", type=int, default=5, help="Candidate count per LLM onboarding job.")
    run_onboarding.add_argument("--article-limit", type=int, default=3, help="Article evidence count per LLM onboarding job.")
    run_onboarding.add_argument(
        "--no-validate-latest-content",
        action="store_true",
        help="Do not verify that latest candidate article URLs can be fetched.",
    )
    run_onboarding.add_argument("--delay-min", type=float, default=1.0)
    run_onboarding.add_argument("--delay-max", type=float, default=3.0)
    run_onboarding.add_argument("--retry-delay-min", type=float, default=1.5)
    run_onboarding.add_argument("--retry-delay-max", type=float, default=4.0)
    run_onboarding.add_argument("--no-delay", action="store_true", help="Disable delays for local testing.")
    run_onboarding.set_defaults(func=cmd_run_onboarding)

    run_feed = run_subcommands.add_parser(
        "feed",
        help="Refresh first-layer article metadata for accepted sources and export feed status files.",
    )
    run_feed.add_argument("--config", help="Optional JSON config file for feed runs. CLI flags override config values.")
    run_feed.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database path.")
    run_feed.add_argument("--base-url", help="Service base URL. Defaults to WECHAT_DOWNLOAD_API_BASE_URL.")
    run_feed.add_argument("--timeout", type=float, default=30, help="HTTP timeout in seconds.")
    run_feed.add_argument("--tier", choices=("all", "core", "normal", "long_tail"), default="all")
    run_feed.add_argument("--max-sources", type=int, default=0, help="Maximum active sources to refresh. Use 0 for all active sources.")
    run_feed.add_argument("--count", type=int, default=5, help="Article count to request per source.")
    run_feed.add_argument("--begin", type=int, default=0)
    run_feed.add_argument(
        "--refresh-mode",
        choices=("latest", "incremental"),
        default="latest",
        help="Use latest for one page per source, or incremental to page until the latest known article is reached.",
    )
    run_feed.add_argument(
        "--incremental-max-pages",
        type=int,
        default=4,
        help="Safety cap for incremental refresh pages per source.",
    )
    run_feed.add_argument("--incremental-page-delay-min", type=float, default=1.0)
    run_feed.add_argument("--incremental-page-delay-max", type=float, default=2.0)
    run_feed.add_argument("--retries", type=int, default=2)
    run_feed.add_argument("--backoff-seconds", type=float, default=3.0)
    run_feed.add_argument("--delay-min", type=float, default=1.0)
    run_feed.add_argument("--delay-max", type=float, default=3.0)
    run_feed.add_argument(
        "--progress-every",
        type=int,
        default=10,
        help="Emit refresh progress every N sources. Use 0 to disable periodic progress.",
    )
    run_feed.add_argument("--feed-limit", type=int, default=3000)
    run_feed.add_argument("--work-dir", default="work/feed")
    run_feed.add_argument("--feed-output", help="Output path for feed items. Defaults to <work-dir>/feed-items.<format>.")
    run_feed.add_argument("--feed-format", choices=("json", "csv"), default="csv")
    run_feed.add_argument("--summary-output", help="Output path for feed summary. Defaults to <work-dir>/feed-summary.json.")
    run_feed.add_argument("--summary-format", choices=("json", "csv"), default="json")
    run_feed.add_argument("--failures-output", help="Output path for failed article rows. Defaults to <work-dir>/feed-failures.<format>.")
    run_feed.add_argument("--failures-format", choices=("json", "csv"), default="csv")
    run_feed.add_argument("--taxonomy", default="finance")
    run_feed.add_argument("--skip-refresh", action="store_true", help="Only export the current feed view without calling the downloader.")
    run_feed.add_argument("--full", action="store_true", help="Refresh, score articles, fetch retained content, and export the feed.")
    run_feed.add_argument("--score-articles", action="store_true", help="Run rules-based article scoring before export.")
    run_feed.add_argument("--score-limit", type=int, default=0, help="Article count to score. Use 0 for all current articles.")
    run_feed.add_argument("--min-score", type=float, default=0.0, help="Minimum score to save a rules digest.")
    run_feed.add_argument("--fetch-retained-content", action="store_true", help="Fetch content for articles selected by retention policy.")
    run_feed.add_argument("--content-limit", type=int, default=0, help="Retained article count to fetch. Use 0 for all matching articles.")
    run_feed.add_argument(
        "--content-retention",
        choices=("content_or_archive", "content", "full_archive", "all"),
        default="content_or_archive",
        help="Which retention tier is eligible for content fetching.",
    )
    run_feed.add_argument("--content-retries", type=int, default=2)
    run_feed.add_argument("--content-backoff-seconds", type=float, default=3.0)
    run_feed.add_argument("--content-delay-min", type=float, default=3.2)
    run_feed.add_argument("--content-delay-max", type=float, default=6.0)
    run_feed.add_argument("--content-passes", type=int, default=3, help="Internal passes over the same retained-content queue.")
    run_feed.add_argument("--content-pass-cooldown-seconds", type=float, default=30.0, help="Cooldown between retained-content passes.")
    run_feed.add_argument("--no-delay", action="store_true", help="Disable delays for local testing.")
    run_feed.set_defaults(func=cmd_run_feed)

    adapter_parser = subcommands.add_parser("adapter", help="Call configured downloader adapters.")
    adapter_subcommands = adapter_parser.add_subparsers(dest="adapter_command", required=True)

    wd_parser = adapter_subcommands.add_parser("wechat-download-api", help="Use a wechat-download-api HTTP service.")
    wd_parser.add_argument("--base-url", help="Service base URL. Defaults to WECHAT_DOWNLOAD_API_BASE_URL.")
    wd_parser.add_argument("--timeout", type=float, default=30, help="HTTP timeout in seconds.")
    wd_subcommands = wd_parser.add_subparsers(dest="wechat_download_api_command", required=True)

    wd_health = wd_subcommands.add_parser("health", help="Check service health.")
    wd_health.set_defaults(func=cmd_wd_health)

    wd_auth = wd_subcommands.add_parser("auth-status", help="Check service login/auth status.")
    wd_auth.set_defaults(func=cmd_wd_auth_status)

    wd_login = wd_subcommands.add_parser("login-url", help="Print the service login page URL.")
    wd_login.set_defaults(func=cmd_wd_login_url)

    wd_search = wd_subcommands.add_parser("search", help="Search WeChat Official Account candidates.")
    wd_search.add_argument("query")
    wd_search.set_defaults(func=cmd_wd_search)

    wd_articles = wd_subcommands.add_parser("articles", help="List articles for a fakeid.")
    wd_articles.add_argument("fakeid")
    wd_articles.add_argument("--begin", type=int, default=0)
    wd_articles.add_argument("--count", type=int, default=10)
    wd_articles.add_argument("--keyword")
    wd_articles.set_defaults(func=cmd_wd_articles)

    wd_article = wd_subcommands.add_parser("article", help="Fetch/parse a single article URL.")
    wd_article.add_argument("url")
    wd_article.set_defaults(func=cmd_wd_article)

    return parser


def get_store(args: argparse.Namespace) -> Store:
    return Store(resolve_db_path(args.db))


RUN_FEED_CONFIG_FLAGS = {
    "db": ("--db",),
    "base_url": ("--base-url",),
    "timeout": ("--timeout",),
    "tier": ("--tier",),
    "max_sources": ("--max-sources",),
    "count": ("--count",),
    "begin": ("--begin",),
    "refresh_mode": ("--refresh-mode",),
    "incremental_max_pages": ("--incremental-max-pages",),
    "incremental_page_delay_min": ("--incremental-page-delay-min",),
    "incremental_page_delay_max": ("--incremental-page-delay-max",),
    "retries": ("--retries",),
    "backoff_seconds": ("--backoff-seconds",),
    "delay_min": ("--delay-min",),
    "delay_max": ("--delay-max",),
    "progress_every": ("--progress-every",),
    "feed_limit": ("--feed-limit",),
    "work_dir": ("--work-dir",),
    "feed_output": ("--feed-output",),
    "feed_format": ("--feed-format",),
    "summary_output": ("--summary-output",),
    "summary_format": ("--summary-format",),
    "failures_output": ("--failures-output",),
    "failures_format": ("--failures-format",),
    "taxonomy": ("--taxonomy",),
    "skip_refresh": ("--skip-refresh",),
    "full": ("--full",),
    "score_articles": ("--score-articles",),
    "score_limit": ("--score-limit",),
    "min_score": ("--min-score",),
    "fetch_retained_content": ("--fetch-retained-content",),
    "content_limit": ("--content-limit",),
    "content_retention": ("--content-retention",),
    "content_retries": ("--content-retries",),
    "content_backoff_seconds": ("--content-backoff-seconds",),
    "content_delay_min": ("--content-delay-min",),
    "content_delay_max": ("--content-delay-max",),
    "content_passes": ("--content-passes",),
    "content_pass_cooldown_seconds": ("--content-pass-cooldown-seconds",),
    "no_delay": ("--no-delay",),
}


FEED_EXPORT_FIELDNAMES = [
    "article_id",
    "source_name",
    "source_tier",
    "source_status",
    "source_category",
    "title",
    "publish_time",
    "url",
    "crawl_status",
    "content_length",
    "asset_count",
    "importance_score",
    "digest_summary",
    "digest_reason",
    "retention_level",
    "archive_status",
    "fetch_error",
]


def write_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def cli_flag_present(args: argparse.Namespace, flags: tuple[str, ...]) -> bool:
    argv = getattr(args, "_argv", []) or []
    for item in argv:
        for flag in flags:
            if item == flag or item.startswith(f"{flag}="):
                return True
    return False


def load_feed_config(path: str | None) -> dict[str, object]:
    if not path:
        return {}
    config_path = Path(path).expanduser()
    with config_path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("Feed config must be a JSON object.")
    return payload


def feed_config_values(payload: dict[str, object]) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in payload.items():
        if key in RUN_FEED_CONFIG_FLAGS:
            values[key] = value

    storage = payload.get("storage")
    if isinstance(storage, dict) and storage.get("path") is not None:
        values["db"] = storage["path"]

    downloader = payload.get("downloader")
    if isinstance(downloader, dict):
        if downloader.get("base_url") is not None:
            values["base_url"] = downloader["base_url"]
        if downloader.get("timeout") is not None:
            values["timeout"] = downloader["timeout"]

    feed = payload.get("feed")
    if isinstance(feed, dict):
        for key, value in feed.items():
            if key in RUN_FEED_CONFIG_FLAGS:
                values[key] = value

    return values


def apply_run_feed_config(args: argparse.Namespace) -> dict[str, object]:
    values = feed_config_values(load_feed_config(getattr(args, "config", None)))
    applied: dict[str, object] = {}
    for key, value in values.items():
        flags = RUN_FEED_CONFIG_FLAGS[key]
        if cli_flag_present(args, flags):
            continue
        setattr(args, key, value)
        applied[key] = value
    return applied


def compact_import_result(payload: dict[str, object]) -> dict[str, object]:
    return {
        "batch_id": payload.get("batch_id"),
        "count": payload.get("count", 0),
    }


def get_wechat_download_api(args: argparse.Namespace) -> WeChatDownloadAPIAdapter:
    config = WeChatDownloadAPIConfig.from_env(
        base_url=args.base_url,
        timeout_seconds=args.timeout,
    )
    return WeChatDownloadAPIAdapter(config)


def cmd_init(args: argparse.Namespace) -> int:
    store = get_store(args)
    store.init()
    write_json({"ok": True, "db": str(store.db_path)})
    return 0


DEMO_FEED_SOURCES = [
    {
        "name": "示例宏观研究",
        "tier": "core",
        "category": "macro_policy",
        "tags": ["sell_side"],
        "articles": [
            {
                "title": "示例宏观点评：通胀回落后的政策空间",
                "url": "https://mp.weixin.qq.com/s/demo-macro-policy",
                "digest": "通胀回落后，政策重心可能转向稳增长和信用修复。",
                "publish_time": "2026-05-15T08:30:00+08:00",
                "content_text": "通胀回落为政策留出空间，后续重点观察财政发力节奏、信用扩张和地产链修复。",
                "importance_score": 0.82,
                "summary": "宏观研究样例：政策空间打开，关注财政、信用和地产链。",
                "assets": [{"url": "https://example.test/assets/macro-chart.png", "metadata": {"kind": "chart"}}],
            },
            {
                "title": "示例每日市场观察",
                "url": "https://mp.weixin.qq.com/s/demo-market-note",
                "digest": "市场风险偏好小幅回升，风格仍偏均衡。",
                "publish_time": "2026-05-14T18:00:00+08:00",
                "importance_score": 0.36,
                "summary": "低优先级日评样例，仅保留元数据。",
            },
        ],
    },
    {
        "name": "示例银行研究",
        "tier": "core",
        "category": "industry_research",
        "tags": ["sell_side", "banks"],
        "articles": [
            {
                "title": "示例银行财报点评：息差企稳",
                "url": "https://mp.weixin.qq.com/s/demo-bank-earnings",
                "digest": "银行板块息差压力缓和，资产质量边际改善。",
                "publish_time": "2026-05-15T07:45:00+08:00",
                "content_text": "银行一季报显示息差边际企稳，不良生成率下降，拨备安全垫仍较充足。",
                "importance_score": 0.68,
                "summary": "行业研究样例：银行息差企稳，关注估值修复。",
                "assets": [{"url": "https://example.test/assets/bank-table.png", "metadata": {"kind": "table"}}],
            }
        ],
    },
    {
        "name": "示例金融招聘",
        "tier": "normal",
        "category": "recruiting_career",
        "tags": ["recruiting"],
        "articles": [
            {
                "title": "示例招聘：研究实习生岗位合集",
                "url": "https://mp.weixin.qq.com/s/demo-recruiting",
                "digest": "多家机构招聘研究实习生。",
                "publish_time": "2026-05-13T12:00:00+08:00",
                "importance_score": 0.18,
                "summary": "招聘低信号样例，不进入正文保留。",
            }
        ],
    },
    {
        "name": "示例受限文章源",
        "tier": "normal",
        "category": "finance_related",
        "tags": ["media"],
        "articles": [
            {
                "title": "示例受限文章：无法抓取正文",
                "url": "https://mp.weixin.qq.com/s/demo-restricted",
                "digest": "该样例用于展示失败文章清单。",
                "publish_time": "2026-05-12T09:00:00+08:00",
                "importance_score": 0.55,
                "summary": "失败清单样例。",
                "fetch_error": "无法获取文章内容。可能原因：文章被删除、访问受限或需要验证。",
            }
        ],
    },
]


def cmd_demo_seed_feed(args: argparse.Namespace) -> int:
    store = get_store(args)
    store.init()
    seeded = seed_demo_feed(store)
    work_dir = Path(args.work_dir).expanduser()
    config_output = Path(args.config_output).expanduser() if args.config_output else work_dir / "feed-config.demo.json"
    config_output.parent.mkdir(parents=True, exist_ok=True)
    config = {
        "storage": {"path": str(store.db_path)},
        "feed": {
            "skip_refresh": True,
            "feed_limit": 100,
            "work_dir": str(work_dir),
            "feed_format": "csv",
            "summary_format": "json",
            "failures_format": "csv",
        },
    }
    config_output.write_text(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_json(
        {
            "ok": True,
            "db": str(store.db_path),
            "config": str(config_output),
            "sources": seeded["sources"],
            "articles": seeded["articles"],
            "next_command": f"mpfeed run feed --config {config_output}",
        }
    )
    return 0


def seed_demo_feed(store: Store) -> dict[str, int]:
    sources_seen = 0
    articles_seen = 0
    for source in DEMO_FEED_SOURCES:
        saved_source = store.upsert_source(
            source["name"],
            wechat_fakeid=f"demo_{sources_seen}",
            status="active",
            tier=source["tier"],
            source_type="demo",
            identity_key=f"demo:{source['name']}",
            match_existing_by_external_id=False,
        )
        source_id = saved_source["source_id"]
        sources_seen += 1
        store.save_classification(
            {
                "entity_type": "source",
                "entity_id": source_id,
                "taxonomy": "finance",
                "category": source["category"],
                "tags": source["tags"],
                "confidence": 1.0,
                "method": "demo_seed",
            }
        )
        article_payloads = [
            {
                "title": article["title"],
                "url": article["url"],
                "digest": article["digest"],
                "publish_time": article["publish_time"],
                "raw_payload": {"demo": True},
            }
            for article in source["articles"]
        ]
        saved_articles = store.upsert_articles(source_id, article_payloads)
        articles_by_url = {article["url"]: article for article in saved_articles["items"]}
        for article in source["articles"]:
            saved_article = articles_by_url[article["url"]]
            store.save_digest(
                {
                    "article_id": saved_article["id"],
                    "summary": article["summary"],
                    "key_points": [],
                    "importance_score": article["importance_score"],
                    "reason": "demo seed",
                    "model": "rules_v1",
                }
            )
            if article.get("fetch_error"):
                store.upsert_article_content(saved_article["id"], {}, fetch_error=article["fetch_error"])
            elif article.get("content_text"):
                store.upsert_article_content(
                    saved_article["id"],
                    {
                        "content_text": article["content_text"],
                        "content_markdown": article["content_text"],
                        "content_structure": [{"type": "text", "text": article["content_text"]}],
                        "assets": article.get("assets", []),
                    },
                )
            articles_seen += 1
    return {"sources": sources_seen, "articles": articles_seen}


def cmd_doctor(args: argparse.Namespace) -> int:
    store = get_store(args)
    db_ok = True
    db_error = None
    try:
        store.init()
    except Exception as exc:
        db_ok = False
        db_error = str(exc)

    service = {
        "configured": bool(args.base_url or adapter_base_url_from_env()),
        "base_url": args.base_url or adapter_base_url_from_env(),
        "health": None,
        "auth_status": None,
        "login_url": None,
    }
    if service["configured"]:
        service["login_url"] = login_url_for_base(str(service["base_url"]))
        adapter = get_wechat_download_api(args)
        service["health"] = safe_adapter_call(adapter.health)
        if service["health"].get("ok"):
            service["auth_status"] = safe_adapter_call(adapter.auth_status)

    health_ok = bool(service["health"] and service["health"].get("ok"))
    auth_ok = bool(service["auth_status"] and auth_status_logged_in(service["auth_status"]))
    next_steps = []
    if not service["configured"]:
        next_steps.append("Set WECHAT_DOWNLOAD_API_BASE_URL or pass --base-url.")
    elif not health_ok:
        next_steps.append("Start the downloader service, then re-run doctor.")
    elif not auth_ok:
        next_steps.append(f"Open {service['login_url']} and scan the WeChat QR code, then re-run doctor.")
    else:
        next_steps.append("Downloader is ready. Run resolve imports / collect latest next.")

    write_json(
        {
            "ok": db_ok and health_ok and auth_ok,
            "db": {"ok": db_ok, "path": str(store.db_path), "error": db_error},
            "service": service,
            "next_steps": next_steps,
        }
    )
    return 0 if db_ok and health_ok and auth_ok else 1


def cmd_import_url(args: argparse.Namespace) -> int:
    store = get_store(args)
    payload = store.import_article_urls([args.url])
    write_json(payload)
    return 0


def cmd_import_urls(args: argparse.Namespace) -> int:
    with open(args.file, encoding="utf-8") as handle:
        urls = [line.strip() for line in handle if line.strip() and not line.lstrip().startswith("#")]
    store = get_store(args)
    payload = store.import_article_urls(urls)
    write_json(compact_import_result(payload))
    return 0


def cmd_import_names(args: argparse.Namespace) -> int:
    with open(args.file, encoding="utf-8-sig") as handle:
        rows = [{"raw_name": line.strip()} for line in handle if line.strip() and not line.lstrip().startswith("#")]

    store = get_store(args)
    write_json(compact_import_result(store.import_source_rows(args.source_type, rows)))
    return 0


def cmd_import_video(args: argparse.Namespace) -> int:
    ocr_result = extract_account_names_from_video(
        args.file,
        fps=args.fps,
        ocr=args.ocr,
        crop=args.crop,
        scale_width=args.scale_width,
        lang=args.lang,
        save_frames=args.save_frames,
        dedupe_threshold=args.dedupe_threshold,
        min_occurrences=args.min_occurrences,
    )
    rows = [
        {
            "raw_name": name,
            "source_video": str(Path(args.file).expanduser()),
            "ocr": args.ocr,
            "fps": args.fps,
            "crop": args.crop,
            "scale_width": args.scale_width,
        }
        for name in ocr_result["names"]
    ]
    imported = get_store(args).import_source_rows(args.source_type, rows)

    if args.names_output:
        output_path = Path(args.names_output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(ocr_result["names"]) + ("\n" if ocr_result["names"] else ""), encoding="utf-8")
    if args.raw_output:
        write_ocr_json(args.raw_output, ocr_result)

    write_json(
        {
            "ok": True,
            "source_type": args.source_type,
            "video": str(Path(args.file).expanduser()),
            "ocr": {
                "frames_seen": ocr_result["frames_seen"],
                "names_detected": ocr_result["count"],
                "frame_dir": ocr_result.get("frame_dir"),
                "raw_output": args.raw_output,
                "names_output": args.names_output,
                "min_occurrences": args.min_occurrences,
            },
            "imported": compact_import_result(imported),
        }
    )
    return 0


def cmd_import_csv(args: argparse.Namespace) -> int:
    rows = []
    with open(args.file, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            item = {**row, "raw_name": row.get(args.name_column)}
            if args.url_column:
                item["raw_url"] = row.get(args.url_column)
            rows.append(item)

    store = get_store(args)
    write_json(compact_import_result(store.import_source_rows("csv", rows)))
    return 0


def cmd_import_json(args: argparse.Namespace) -> int:
    with open(args.file, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError("JSON import expects a list of objects")

    rows = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("JSON import expects every item to be an object")
        row = {**item, "raw_name": item.get(args.name_field)}
        if args.url_field:
            row["raw_url"] = item.get(args.url_field)
        rows.append(row)

    store = get_store(args)
    write_json(compact_import_result(store.import_source_rows("json", rows)))
    return 0


def cmd_export_imports(args: argparse.Namespace) -> int:
    store = get_store(args)
    rows = store.list_imports(limit=args.limit, status=args.status, source_type=args.source_type)

    if args.format == "json":
        write_json(rows)
        return 0

    fieldnames = ["id", "batch_id", "raw_name", "raw_url", "raw_payload", "source_type", "status", "created_at"]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        row = {**row, "raw_payload": json.dumps(row["raw_payload"], ensure_ascii=False, sort_keys=True)}
        writer.writerow(row)
    return 0


def cmd_export_sources(args: argparse.Namespace) -> int:
    store = get_store(args)
    rows = store.list_sources(limit=args.limit)

    if args.format == "json":
        write_json(rows)
        return 0

    fieldnames = [
        "id",
        "platform",
        "name",
        "wechat_fakeid",
        "biz",
        "avatar_url",
        "intro",
        "status",
        "tier",
        "source_type",
        "created_at",
        "updated_at",
    ]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key) for key in fieldnames})
    return 0


def cmd_export_articles(args: argparse.Namespace) -> int:
    store = get_store(args)
    rows = store.list_articles(limit=args.limit, source_id=args.source_id)

    if args.format == "json":
        write_json(rows)
        return 0

    fieldnames = [
        "id",
        "source_id",
        "title",
        "url",
        "digest",
        "cover_url",
        "publish_time",
        "crawl_status",
        "raw_payload",
        "created_at",
        "updated_at",
    ]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({**row, "raw_payload": json.dumps(row["raw_payload"], ensure_ascii=False, sort_keys=True)})
    return 0


def cmd_export_feed(args: argparse.Namespace) -> int:
    store = get_store(args)
    rows = store.list_feed_items(
        limit=args.limit,
        source_id=args.source_id,
        tier=args.tier,
        status=args.status,
        crawl_status=args.crawl_status,
    )

    if args.format == "json":
        write_json(rows)
        return 0

    writer = csv.DictWriter(sys.stdout, fieldnames=FEED_EXPORT_FIELDNAMES)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key) for key in FEED_EXPORT_FIELDNAMES})
    return 0


def cmd_export_feed_summary(args: argparse.Namespace) -> int:
    summary = get_store(args).feed_summary()

    if args.format == "json":
        write_json(summary)
        return 0

    writer = csv.DictWriter(sys.stdout, fieldnames=["section", "key", "value", "count"])
    writer.writeheader()
    for key in ("sources", "articles", "digests", "article_assets", "sources_with_articles", "active_sources_without_articles"):
        writer.writerow({"section": "total", "key": key, "value": "", "count": summary[key]})
    for row in summary["sources_by_tier_status"]:
        writer.writerow(
            {
                "section": "sources_by_tier_status",
                "key": row["tier"],
                "value": row["status"],
                "count": row["count"],
            }
        )
    for row in summary["articles_by_crawl_status"]:
        writer.writerow(
            {
                "section": "articles_by_crawl_status",
                "key": row["crawl_status"],
                "value": "",
                "count": row["count"],
            }
        )
    return 0


def write_feed_items_file(path: Path, rows: list[dict[str, object]], output_format: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json":
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return

    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=FEED_EXPORT_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in FEED_EXPORT_FIELDNAMES})


def feed_summary_csv_rows(summary: dict[str, object]) -> list[dict[str, object]]:
    rows = []
    for key in ("sources", "articles", "digests", "article_assets", "sources_with_articles", "active_sources_without_articles"):
        rows.append({"section": "total", "key": key, "value": "", "count": summary[key]})
    for row in summary["sources_by_tier_status"]:
        rows.append({"section": "sources_by_tier_status", "key": row["tier"], "value": row["status"], "count": row["count"]})
    for row in summary["articles_by_crawl_status"]:
        rows.append({"section": "articles_by_crawl_status", "key": row["crawl_status"], "value": "", "count": row["count"]})
    return rows


def write_feed_summary_file(path: Path, summary: dict[str, object], output_format: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if output_format == "json":
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return

    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["section", "key", "value", "count"])
        writer.writeheader()
        writer.writerows(feed_summary_csv_rows(summary))


def effective_unbounded_limit(limit: int) -> int:
    return limit if limit and limit > 0 else 1_000_000


def retention_levels_for_content_fetch(selection: str) -> tuple[str, ...] | None:
    if selection == "content_or_archive":
        return ("content", "full_archive")
    if selection == "content":
        return ("content",)
    if selection == "full_archive":
        return ("full_archive",)
    return None


def cmd_export_contents(args: argparse.Namespace) -> int:
    store = get_store(args)
    rows = store.list_article_contents(limit=args.limit)

    if args.format == "json":
        write_json(rows)
        return 0

    fieldnames = [
        "article_id",
        "title",
        "url",
        "source_id",
        "crawl_status",
        "content_html",
        "content_text",
        "content_markdown",
        "fetch_error",
        "extracted_at",
    ]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return 0


def cmd_export_candidates(args: argparse.Namespace) -> int:
    decision = None if args.decision == "all" else args.decision
    rows = get_store(args).list_candidates(decision=decision, limit=args.limit)

    if args.format == "json":
        write_json(rows)
        return 0

    fieldnames = [
        "id",
        "import_id",
        "query",
        "candidate_name",
        "wechat_fakeid",
        "biz",
        "intro",
        "score",
        "decision",
        "tier",
        "raw_payload",
    ]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                **row,
                "tier": "normal",
                "raw_payload": json.dumps(row.get("raw_payload") or {}, ensure_ascii=False, sort_keys=True),
            }
        )
    return 0


def cmd_export_onboarding(args: argparse.Namespace) -> int:
    store = get_store(args)
    taxonomy = load_taxonomy(resolve_taxonomy_arg(args.taxonomy))
    if args.view == "compact":
        rows = build_compact_onboarding_rows(store=store, taxonomy=taxonomy, source_type=args.source_type, limit=args.limit)
    else:
        rows = build_onboarding_rows(store=store, taxonomy=taxonomy, source_type=args.source_type, limit=args.limit)
    if args.format == "json":
        write_json(rows)
        return 0

    compact_fieldnames = [
        "ocr_account",
        "matched_account",
        "candidate_account",
        "account_category",
        "system_decision",
        "requires_manual_confirmation",
        "match_type",
        "latest_probe_status",
        "evidence_summary",
        "manual_account_name",
        "manual_article_url",
        "manual_account_category",
        "manual_decision",
        "notes",
    ]
    full_fieldnames = [
        "import_id",
        "batch_id",
        "source_type",
        "import_status",
        "identity_match_name",
        "manual_account_name",
        "manual_article_url",
        "manual_account_category",
        "manual_decision",
        "manual_notes",
        "system_decision",
        "requires_user_action",
        "ocr_name",
        "best_candidate_name",
        "candidate_score",
        "exact_match",
        "display_exact_match",
        "match_type",
        "name_similarity",
        "candidate_decision",
        "candidate_intro",
        "wechat_fakeid",
        "biz",
        "source_id",
        "source_name",
        "source_status",
        "is_active",
        "tier",
        "latest_publish_time",
        "latest_article_title",
        "latest_article_digest",
        "latest_article_url",
        "latest_probe_status",
        "latest_probe_refreshed",
        "classification_category",
        "classification_confidence",
        "classification_method",
        "llm_review_category",
        "llm_review_action",
        "llm_review_confidence",
        "llm_review_method",
        "llm_review_reason",
        "llm_requires_user_confirmation",
        "is_finance_candidate",
        "needs_manual_review",
        "evidence_needs_review",
        "recommended_action",
        "user_decision",
        "user_selected_candidate_id",
        "user_tier",
        "user_note",
    ]
    fieldnames = compact_fieldnames if args.view == "compact" else full_fieldnames
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return 0


def cmd_export_classifications(args: argparse.Namespace) -> int:
    store = get_store(args)
    rows = store.list_classifications(limit=args.limit, entity_type=args.entity_type, taxonomy=args.taxonomy)

    if args.format == "json":
        write_json(rows)
        return 0

    fieldnames = ["id", "entity_type", "entity_id", "taxonomy", "category", "tags", "confidence", "method", "created_at"]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({**row, "tags": json.dumps(row["tags"], ensure_ascii=False, sort_keys=True)})
    return 0


def cmd_export_digests(args: argparse.Namespace) -> int:
    store = get_store(args)
    rows = store.list_digests(
        limit=args.limit,
        application_target=args.application_target,
        min_score=args.min_score,
        analysis_stage=args.analysis_stage,
    )

    if args.format == "json":
        write_json(rows)
        return 0
    if args.format == "markdown":
        print(format_digests_markdown(rows))
        return 0

    fieldnames = [
        "id",
        "article_id",
        "source_id",
        "source_name",
        "title",
        "url",
        "publish_time",
        "summary",
        "key_points",
        "importance_score",
        "application_targets",
        "analysis_stage",
        "reason",
        "model",
        "created_at",
    ]
    writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                **row,
                "key_points": json.dumps(row["key_points"], ensure_ascii=False, sort_keys=True),
                "application_targets": json.dumps(row.get("application_targets") or [], ensure_ascii=False, sort_keys=True),
            }
        )
    return 0


def cmd_export_digest_context(args: argparse.Namespace) -> int:
    store = get_store(args)
    rows = build_digest_context_rows(
        store=store,
        limit=args.limit,
        application_target=args.application_target,
        min_score=args.min_score,
        analysis_stage=args.analysis_stage,
        include_html=args.include_html,
        include_assets=not args.no_assets,
        require_content=args.require_content,
        max_content_chars=args.max_content_chars,
    )

    if args.format == "json":
        write_json(rows)
        return 0
    if args.format == "jsonl":
        for row in rows:
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
        return 0

    print(format_digest_context_markdown(rows))
    return 0


def cmd_resolve_search(args: argparse.Namespace) -> int:
    adapter_payload = get_wechat_download_api(args).search_sources(args.query)
    if not adapter_payload["ok"]:
        write_json(adapter_payload)
        return 1

    candidates = normalize_source_candidates(adapter_payload["body"], args.query)
    result = get_store(args).save_search_candidates(args.query, candidates, adapter_payload["body"])
    write_json(result)
    return 0


def cmd_resolve_imports(args: argparse.Namespace) -> int:
    write_json(
        resolve_import_candidates(
            store=get_store(args),
            adapter=get_wechat_download_api(args),
            limit=args.limit,
            source_type=args.source_type,
            status=None if args.status == "all" else args.status,
            retry_empty=args.retry_empty,
            query_variants=args.query_variants,
            replace_pending_candidates=args.replace_pending_candidates,
            delay_min=args.delay_min,
            delay_max=args.delay_max,
            no_delay=args.no_delay,
        )
    )
    return 0


def cmd_resolve_manual_names(args: argparse.Namespace) -> int:
    write_json(
        resolve_manual_onboarding_names(
            store=get_store(args),
            adapter=get_wechat_download_api(args),
            limit=args.limit,
            source_type=args.source_type,
            status=None if args.status == "all" else args.status,
            query_variants=args.query_variants,
            replace_pending_candidates=args.replace_pending_candidates,
            delay_min=args.delay_min,
            delay_max=args.delay_max,
            no_delay=args.no_delay,
        )
    )
    return 0


def resolve_manual_onboarding_names(
    store: Store,
    adapter: WeChatDownloadAPIAdapter,
    limit: int,
    source_type: str | None,
    status: str | None,
    query_variants: bool,
    replace_pending_candidates: bool,
    delay_min: float,
    delay_max: float,
    no_delay: bool,
) -> dict:
    imports = [
        item
        for item in store.list_imports(limit=limit, status=status, source_type=source_type)
        if manual_account_name_for_import(item)
    ]

    ok_count = 0
    failed_count = 0
    strict_count = 0
    candidate_count = 0
    zero_candidate_count = 0
    results = []

    for index, item in enumerate(imports):
        query = manual_account_name_for_import(item) or ""
        adapter_payload, candidates = search_candidates_with_optional_variants(adapter, query, query_variants)
        if not adapter_payload["ok"]:
            failed_count += 1
            results.append({"import_id": item["id"], "query": query, "ok": False})
            delay_between_items(index, len(imports), delay_min, delay_max, disabled=no_delay)
            continue

        save_status = "searched" if candidates else "needs_review"
        saved = store.save_candidates_for_import(
            item["id"],
            candidates,
            {
                "manual_account_name": query,
                "response": adapter_payload["body"],
            },
            status=save_status,
            replace_pending=replace_pending_candidates,
        )
        best = pick_best_candidate(item, saved["items"])
        strict = bool(best and names_match_manual_or_raw(item, best.get("candidate_name")))
        ok_count += 1
        candidate_count += saved["count"]
        if saved["count"] == 0:
            zero_candidate_count += 1
        if strict:
            strict_count += 1
        results.append(
            {
                "import_id": item["id"],
                "ocr_name": item.get("raw_name"),
                "manual_account_name": query,
                "ok": True,
                "candidates_saved": saved["count"],
                "strict_match": strict,
                "best_candidate_name": (best or {}).get("candidate_name") or "",
                "status": save_status,
            }
        )
        delay_between_items(index, len(imports), delay_min, delay_max, disabled=no_delay)

    return {
        "ok": True,
        "imports_seen": len(imports),
        "searches_ok": ok_count,
        "searches_failed": failed_count,
        "strict_matches": strict_count,
        "zero_candidate_imports": zero_candidate_count,
        "candidates_saved": candidate_count,
        "result_sample": results[:50],
        "results_truncated": max(0, len(results) - 50),
    }


def resolve_import_candidates(
    store: Store,
    adapter: WeChatDownloadAPIAdapter,
    limit: int,
    source_type: str | None,
    status: str | None,
    retry_empty: bool,
    query_variants: bool,
    replace_pending_candidates: bool,
    delay_min: float,
    delay_max: float,
    no_delay: bool,
) -> dict:
    candidate_import_ids = set()
    if retry_empty:
        candidate_import_ids = {
            candidate["import_id"]
            for candidate in store.list_candidates(decision=None, limit=max(limit * 10, 1000))
        }
    imports = [
        item
        for item in store.list_imports(limit=limit, status=status, source_type=source_type)
        if item.get("raw_name")
    ]
    if retry_empty:
        imports = [item for item in imports if item["id"] not in candidate_import_ids]

    results = []
    ok_count = 0
    failed_count = 0
    candidate_count = 0
    zero_candidate_count = 0

    for index, item in enumerate(imports):
        query = item["raw_name"]
        adapter_payload, candidates = search_candidates_with_optional_variants(adapter, query, query_variants)
        if not adapter_payload["ok"]:
            failed_count += 1
            results.append({"import_id": item["id"], "query": query, "ok": False})
            delay_between_items(index, len(imports), delay_min, delay_max, disabled=no_delay)
            continue

        saved = store.save_candidates_for_import(
            item["id"],
            candidates,
            adapter_payload["body"],
            replace_pending=replace_pending_candidates,
        )
        ok_count += 1
        candidate_count += saved["count"]
        if saved["count"] == 0:
            zero_candidate_count += 1
        delay_between_items(index, len(imports), delay_min, delay_max, disabled=no_delay)

    failures = results[:20]
    return {
        "ok": True,
        "imports_seen": len(imports),
        "searches_ok": ok_count,
        "searches_failed": failed_count,
        "zero_candidate_imports": zero_candidate_count,
        "candidates_saved": candidate_count,
        "failures": failures,
        "failures_truncated": max(0, len(results) - len(failures)),
    }


def search_candidates_with_optional_variants(
    adapter: WeChatDownloadAPIAdapter,
    query: str,
    query_variants: bool = False,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    queries = search_query_variants(query) if query_variants else [query]
    merged_candidates: list[dict[str, object]] = []
    seen_keys = set()
    responses = []
    first_payload: dict[str, object] | None = None

    for item_query in queries:
        payload = adapter.search_sources(item_query)
        if first_payload is None:
            first_payload = payload
        responses.append({"query": item_query, "ok": payload.get("ok"), "status": payload.get("status"), "body": payload.get("body")})
        if not payload.get("ok"):
            continue
        candidates = normalize_source_candidates(payload.get("body"), query)
        for candidate in candidates:
            key = candidate.get("wechat_fakeid") or candidate.get("biz") or candidate.get("candidate_name")
            if key in seen_keys:
                continue
            seen_keys.add(key)
            merged_candidates.append(candidate)
        if merged_candidates and not query_variants:
            break

    ok = any(response["ok"] for response in responses)
    body: object = (first_payload or {}).get("body")
    if query_variants:
        body = {"query": query, "variant_responses": responses}
    payload = {
        **(first_payload or {"operation": "search_sources", "status": 0, "url": None}),
        "ok": ok,
        "body": body,
    }
    return payload, merged_candidates


def cmd_review_list(args: argparse.Namespace) -> int:
    decision = None if args.decision == "all" else args.decision
    write_json(get_store(args).list_candidates(decision=decision, limit=args.limit))
    return 0


def cmd_review_auto_exact(args: argparse.Namespace) -> int:
    taxonomy = load_taxonomy(resolve_taxonomy_arg(args.taxonomy)) if args.finance_only else None
    write_json(
        auto_accept_exact_candidates(
            store=get_store(args),
            source_type=args.source_type,
            limit=args.limit,
            exact_score=args.exact_score,
            taxonomy=taxonomy,
            finance_only=args.finance_only,
            min_confidence=args.min_confidence,
            dry_run=args.dry_run,
        )
    )
    return 0


def cmd_review_accept(args: argparse.Namespace) -> int:
    write_json(get_store(args).accept_candidate(args.candidate_id, tier=args.tier))
    return 0


def cmd_review_reject(args: argparse.Namespace) -> int:
    write_json(get_store(args).reject_candidate(args.candidate_id))
    return 0


def cmd_review_apply(args: argparse.Namespace) -> int:
    store = get_store(args)
    rows = load_review_decisions(args.file)
    results = []

    for row in rows:
        candidate_id = clean_optional(row.get("candidate_id") or row.get("id"))
        decision = clean_optional(row.get("decision") or row.get("action"))
        if not candidate_id or not decision:
            results.append({"ok": False, "row": row, "reason": "missing_candidate_id_or_decision"})
            continue

        decision = decision.lower()
        if decision in {"skip", "pending", ""}:
            results.append({"ok": True, "candidate_id": candidate_id, "decision": "skip"})
            continue
        if decision in {"accept", "manual_accept"}:
            tier = clean_optional(row.get("tier")) or "normal"
            results.append(store.accept_candidate(candidate_id, tier=tier))
            continue
        if decision in {"reject", "manual_reject"}:
            results.append(store.reject_candidate(candidate_id))
            continue
        results.append({"ok": False, "candidate_id": candidate_id, "decision": decision, "reason": "unknown_decision"})

    write_json(
        {
            "ok": all(item.get("ok") for item in results),
            "count": len(results),
            "accepted": sum(1 for item in results if item.get("source_id")),
            "rejected": sum(1 for item in results if item.get("decision") == "reject"),
            "skipped": sum(1 for item in results if item.get("decision") == "skip"),
            "results": results,
        }
    )
    return 0 if all(item.get("ok") for item in results) else 1


def cmd_review_apply_onboarding(args: argparse.Namespace) -> int:
    store = get_store(args)
    rows = load_review_decisions(args.file)
    imports = store.list_imports(limit=max(len(rows) * 2, 1000), source_type=args.source_type)
    import_by_id = {item["id"]: item for item in imports}
    imports_by_name: dict[str, list[dict]] = {}
    for item in imports:
        if item.get("raw_name"):
            imports_by_name.setdefault(item["raw_name"], []).append(item)
    candidates_by_import: dict[str, list[dict]] = {}
    for candidate in store.list_candidates(decision=None, limit=max(len(imports) * 10, 1000)):
        candidates_by_import.setdefault(candidate["import_id"], []).append(candidate)

    results = []
    for row in rows:
        decision = normalize_manual_onboarding_decision(first_value(row, "manual_decision", "人工决策", "decision"))
        manual_name = clean_optional(first_value(row, "manual_account_name", "人工确认账号名", "人工确认的新账号名称"))
        manual_url = clean_optional(first_value(row, "manual_article_url", "人工确认文章链接", "文章链接", "manual_url"))
        manual_category = clean_optional(first_value(row, "manual_account_category", "人工确认分类", "人工确认的账号分类"))
        notes = clean_optional(first_value(row, "notes", "备注", "判断理由"))
        if not any([decision, manual_name, manual_url, manual_category, notes]):
            continue

        import_row = resolve_review_import(row, import_by_id, imports_by_name)
        if not import_row:
            results.append({"ok": False, "row": row, "reason": "import_not_found"})
            continue

        review = {
            "decision": decision or "needs_review",
            "manual_account_name": manual_name,
            "manual_article_url": manual_url,
            "manual_account_category": manual_category,
            "notes": notes,
            "method": "manual:review_table",
        }
        review = {key: value for key, value in review.items() if value not in (None, "")}

        status = status_for_manual_decision(decision, manual_name, manual_url)
        store.record_manual_import_review(import_row["id"], review, status=status)
        result = {"ok": True, "import_id": import_row["id"], "decision": decision or "needs_review", "status": status}

        if decision == "confirm_candidate":
            best = pick_best_candidate(import_row, candidates_by_import.get(import_row["id"], []))
            if best:
                result["accepted"] = store.accept_candidate(best["id"], tier="normal")
            else:
                result["ok"] = False
                result["reason"] = "no_candidate_to_confirm"
        elif decision in {"ignore", "invalid"}:
            store.reject_all_candidates_for_import(import_row["id"])

        results.append(result)

    write_json(
        {
            "ok": all(item.get("ok") for item in results),
            "rows_seen": len(rows),
            "rows_applied": len(results),
            "accepted_candidates": sum(1 for item in results if item.get("accepted")),
            "needs_review": sum(1 for item in results if item.get("status") == "needs_review"),
            "ignored": sum(1 for item in results if item.get("status") == "ignored"),
            "rejected": sum(1 for item in results if item.get("status") == "rejected"),
            "results": results[:50],
            "results_truncated": max(0, len(results) - 50),
        }
    )
    return 0 if all(item.get("ok") for item in results) else 1


def cmd_review_import_classified_sources(args: argparse.Namespace) -> int:
    store = get_store(args)
    store.init()
    include_tiers = {item.strip() for item in args.include_tiers.split(",") if item.strip()}
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("Classification import expects a JSON object with rows or a list of rows")

    imported = []
    skipped = []
    for row in rows:
        decision = source_import_decision(row, include_tiers, allow_unfetchable=args.allow_unfetchable)
        if not decision["import"]:
            skipped.append(decision)
            continue
        if args.dry_run:
            imported.append({**decision, "dry_run": True})
            continue

        source_name = reviewed_source_name(row)
        source = store.upsert_source(
            name=source_name,
            wechat_fakeid=row.get("freshness_fakeid"),
            status=decision["status"],
            tier=decision["tier"],
            source_type=args.source_type,
            identity_key=source_name,
            match_existing_by_external_id=False,
        )
        classification = store.save_classification(
            {
                "entity_type": "source",
                "entity_id": source["source_id"],
                "taxonomy": args.taxonomy,
                "category": row.get("primary_domain") or "uncategorized",
                "tags": reviewed_source_tags(row),
                "confidence": row.get("confidence") or 0,
                "method": "reviewed:onboarding",
            }
        )
        articles_saved = None
        evidence_articles = reviewed_latest_articles(row)
        if evidence_articles:
            articles_saved = store.upsert_articles(source["source_id"], evidence_articles)
        imported.append(
            {
                **decision,
                "source": source,
                "classification": classification,
                "articles_saved": articles_saved,
            }
        )

    summary = {
        "ok": True,
        "rows_seen": len(rows),
        "imported": len(imported),
        "skipped": len(skipped),
        "dry_run": args.dry_run,
        "skip_reasons": count_by(skipped, "reason"),
        "status": count_by(imported, "status"),
        "tier": count_by(imported, "tier"),
        "items_sample": imported[:20],
        "items_truncated": max(0, len(imported) - 20),
        "skipped_sample": skipped[:20],
        "skipped_truncated": max(0, len(skipped) - 20),
    }
    write_json(summary)
    return 0


def cmd_review_validate_classified_sources(args: argparse.Namespace) -> int:
    include_tiers = {item.strip() for item in args.include_tiers.split(",") if item.strip()}
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("Classification validation expects a JSON object with rows or a list of rows")

    issues: list[dict[str, object]] = []
    included = 0
    ready = 0
    for index, row in enumerate(rows, start=1):
        tier_id = clean_optional(row.get("inclusion_tier"))
        if tier_id not in include_tiers:
            continue
        included += 1
        row_issues = final_matched_account_issues(row, index, allow_unfetchable=args.allow_unfetchable)
        if row_issues:
            issues.extend(row_issues)
        if not any(item["severity"] == "blocking" for item in row_issues):
            ready += 1

    summary = {
        "ok": not any(item["severity"] == "blocking" for item in issues),
        "rows_seen": len(rows),
        "included_rows": included,
        "ready_rows": ready,
        "blocking_rows": len({item["row_index"] for item in issues if item["severity"] == "blocking"}),
        "review_rows": len({item["row_index"] for item in issues if item["severity"] == "review"}),
        "issue_counts": count_by(issues, "issue"),
        "issues": issues,
    }

    if args.format == "csv":
        fieldnames = [
            "row_index",
            "severity",
            "issue",
            "ocr_account",
            "matched_account",
            "freshness_candidate_name",
            "inclusion_tier",
            "primary_domain",
            "action",
        ]
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows([{key: item.get(key, "") for key in fieldnames} for item in issues])
    else:
        write_json(summary)
    return 0 if summary["ok"] else 1


def source_import_decision(row: dict, include_tiers: set[str], allow_unfetchable: bool) -> dict:
    name = reviewed_source_name(row)
    fakeid_owner_name = reviewed_fakeid_owner_name(row)
    tier_id = clean_optional(row.get("inclusion_tier"))
    freshness_status = clean_optional(row.get("freshness_status"))
    content_fetch_ok = row.get("latest_article_content_fetch_ok")
    fakeid = clean_optional(row.get("freshness_fakeid"))

    base = {
        "import": False,
        "name": name,
        "inclusion_tier": tier_id,
        "freshness_status": freshness_status,
        "content_fetch_ok": content_fetch_ok,
    }
    if tier_id not in include_tiers:
        return {**base, "reason": "tier_not_included"}
    if not name:
        return {**base, "reason": "missing_name"}
    if not fakeid:
        return {**base, "reason": "missing_fakeid"}
    if not fakeid_owner_name or not names_equivalent(fakeid_owner_name, name):
        return {**base, "reason": "fakeid_not_for_confirmed_name", "fakeid_owner_name": fakeid_owner_name}
    acceptable_freshness = {"ok"}
    if allow_unfetchable:
        acceptable_freshness.add("ok_no_fetchable_articles")
    if freshness_status not in acceptable_freshness:
        return {**base, "reason": "freshness_not_ok"}
    if content_fetch_ok is False and not allow_unfetchable:
        return {**base, "reason": "latest_article_unfetchable"}

    active_status = clean_optional(row.get("active_status")) or ""
    if active_status == "长期未更新":
        status = "inactive"
        tier = "long_tail"
    elif tier_id == "core_finance":
        status = "active"
        tier = "core"
    else:
        status = "active"
        tier = "normal"
    return {**base, "import": True, "reason": "accepted", "status": status, "tier": tier}


def reviewed_source_name(row: dict) -> str | None:
    return clean_optional(
        first_value(
            row,
            "matched_account",
            "匹配账号",
            "final_matched_account",
            "最终匹配账号",
        )
    )


def reviewed_fakeid_owner_name(row: dict) -> str | None:
    return clean_optional(
        first_value(
            row,
            "freshness_candidate_name",
            "matched_account",
        )
    )


def final_matched_account_issues(row: dict, row_index: int, allow_unfetchable: bool = False) -> list[dict[str, object]]:
    issues: list[dict[str, object]] = []
    matched_name = reviewed_source_name(row)
    fakeid_owner_name = reviewed_fakeid_owner_name(row)
    ocr_name = clean_optional(row.get("ocr_account"))
    fakeid = clean_optional(row.get("freshness_fakeid"))
    freshness_status = clean_optional(row.get("freshness_status"))
    content_fetch_ok = row.get("latest_article_content_fetch_ok")
    base = {
        "row_index": row_index,
        "ocr_account": ocr_name or "",
        "matched_account": matched_name or "",
        "freshness_candidate_name": fakeid_owner_name or "",
        "inclusion_tier": clean_optional(row.get("inclusion_tier")) or "",
        "primary_domain": clean_optional(row.get("primary_domain")) or "",
    }
    if not matched_name:
        issues.append({**base, "severity": "blocking", "issue": "missing_matched_account", "action": "fill final matched_account"})
        return issues
    if ocr_name and not names_equivalent(ocr_name, matched_name):
        issues.append(
            {
                **base,
                "severity": "review",
                "issue": "matched_differs_from_ocr",
                "action": "confirm this is an intentional user-reviewed match",
            }
        )
    if not fakeid:
        issues.append({**base, "severity": "blocking", "issue": "missing_fakeid", "action": "re-search final matched_account"})
    if fakeid and (not fakeid_owner_name or not names_equivalent(fakeid_owner_name, matched_name)):
        issues.append(
            {
                **base,
                "severity": "blocking",
                "issue": "fakeid_not_for_matched_account",
                "action": "re-search final matched_account and refresh evidence",
            }
        )
    if freshness_status == "ok_no_fetchable_articles" and allow_unfetchable:
        issues.append(
            {
                **base,
                "severity": "review",
                "issue": "metadata_only_no_fetchable_content",
                "action": "import source and article metadata; content/digest will be degraded",
            }
        )
    elif freshness_status != "ok":
        issues.append({**base, "severity": "blocking", "issue": "freshness_not_ok", "action": "refresh latest articles"})
    if content_fetch_ok is False and not allow_unfetchable:
        issues.append(
            {
                **base,
                "severity": "blocking",
                "issue": "latest_article_unfetchable",
                "action": "refresh latest articles or import with explicit allow-unfetchable",
            }
        )
    return issues


def reviewed_source_tags(row: dict) -> list[str]:
    tags = []
    for key in ("inclusion_tier", "source_attribute", "active_status"):
        value = clean_optional(row.get(key))
        if value and value not in tags:
            tags.append(value)
    return tags


def reviewed_latest_articles(row: dict) -> list[dict]:
    articles = []
    for article in row.get("refreshed_latest_articles") or []:
        if not article.get("url") or not article.get("title"):
            continue
        articles.append(
            {
                "title": article.get("title"),
                "url": article.get("url"),
                "digest": article.get("digest"),
                "publish_time": article.get("publish_time"),
                "raw_payload": article,
            }
        )
    return articles


def count_by(items: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key) or "")
        counts[value] = counts.get(value, 0) + 1
    return counts


def cmd_collect_latest(args: argparse.Namespace) -> int:
    store = get_store(args)
    adapter = get_wechat_download_api(args)
    policy = tier_policy(args.tier)
    tier = None if args.tier == "all" else args.tier
    max_sources = args.max_sources or policy.max_sources
    count = args.count or policy.article_count
    retries = policy.retries if args.retries is None else args.retries
    backoff_seconds = policy.backoff_seconds if args.backoff_seconds is None else args.backoff_seconds
    delay_min = policy.delay_min_seconds if args.delay_min is None else args.delay_min
    delay_max = policy.delay_max_seconds if args.delay_max is None else args.delay_max
    sources = store.list_collectable_sources(tier=tier, limit=max_sources)
    results = []
    total_articles = 0
    skipped = []

    for index, source in enumerate(sources):
        fakeid = source.get("wechat_fakeid")
        if not fakeid:
            skipped.append({"source_id": source["id"], "name": source["name"], "reason": "missing_wechat_fakeid"})
            continue

        payload = with_retries(
            lambda: adapter.list_articles(fakeid=fakeid, begin=args.begin, count=count),
            retries=retries,
            backoff_seconds=backoff_seconds,
        )
        if not payload["ok"]:
            results.append({"source_id": source["id"], "name": source["name"], "ok": False, "adapter": payload})
            delay_between_items(index, len(sources), delay_min, delay_max, disabled=args.no_delay)
            continue

        articles = normalize_article_items(payload["body"])
        saved = store.upsert_articles(source["id"], articles)
        total_articles += saved["count"]
        results.append(
            {
                "source_id": source["id"],
                "name": source["name"],
                "fakeid": fakeid,
                "ok": True,
                "count": saved["count"],
            }
        )
        delay_between_items(index, len(sources), delay_min, delay_max, disabled=args.no_delay)

    write_json(
        {
            "ok": True,
            "tier": args.tier,
            "policy": {
                "max_sources": max_sources,
                "article_count": count,
                "delay_min_seconds": 0 if args.no_delay else delay_min,
                "delay_max_seconds": 0 if args.no_delay else delay_max,
                "retries": retries,
                "backoff_seconds": backoff_seconds,
            },
            "sources_seen": len(sources),
            "sources_skipped": len(skipped),
            "articles_saved": total_articles,
            "results": results,
            "skipped": skipped,
        }
    )
    return 0


def cmd_collect_history(args: argparse.Namespace) -> int:
    store = get_store(args)
    adapter = get_wechat_download_api(args)
    store.init()

    tier = None if args.tier == "all" else args.tier
    all_sources = store.list_collectable_sources(tier=tier, limit=100_000)
    if args.source_name:
        all_sources = [source for source in all_sources if source["name"] == args.source_name]
    sources = all_sources[max(0, args.source_offset) :]
    if args.max_sources > 0:
        sources = sources[: args.max_sources]

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    totals = {
        "sources_seen": len(sources),
        "sources_ok": 0,
        "sources_failed": 0,
        "sources_skipped": 0,
        "articles_seen": 0,
        "articles_in_window": 0,
        "articles_upserted": 0,
        "pages_fetched": 0,
        "stopped_by_cutoff": 0,
        "stopped_by_short_page": 0,
        "stopped_by_total_count": 0,
        "stopped_by_page_cap": 0,
    }
    results = []

    for index, source in enumerate(sources):
        result = backfill_article_metadata_for_source(
            store=store,
            adapter=adapter,
            source=source,
            cutoff=cutoff,
            count=args.count,
            max_pages=args.max_pages_per_source,
            retries=args.retries,
            backoff_seconds=args.backoff_seconds,
            page_delay_min=args.page_delay_min,
            page_delay_max=args.page_delay_max,
            dry_run=args.dry_run,
            no_delay=args.no_delay,
        )
        results.append(result)
        update_history_totals(totals, result)
        delay_between_items(index, len(sources), args.delay_min, args.delay_max, disabled=args.no_delay)

    payload = {
        "ok": totals["sources_failed"] == 0,
        "status": "dry_run" if args.dry_run else "saved",
        "tier": args.tier,
        "days": args.days,
        "cutoff_utc": cutoff.replace(microsecond=0).isoformat(),
        "source_offset": args.source_offset,
        "max_sources": args.max_sources,
        "count": args.count,
        "max_pages_per_source": args.max_pages_per_source,
        "policy": {
            "delay_min_seconds": 0 if args.no_delay else args.delay_min,
            "delay_max_seconds": 0 if args.no_delay else args.delay_max,
            "page_delay_min_seconds": 0 if args.no_delay else args.page_delay_min,
            "page_delay_max_seconds": 0 if args.no_delay else args.page_delay_max,
            "retries": args.retries,
            "backoff_seconds": args.backoff_seconds,
        },
        "totals": totals,
        "results": results,
    }
    write_json(payload)
    return 0 if payload["ok"] else 1


def backfill_article_metadata_for_source(
    *,
    store: Store,
    adapter: WeChatDownloadAPIAdapter,
    source: dict,
    cutoff: datetime,
    count: int,
    max_pages: int,
    retries: int,
    backoff_seconds: float,
    page_delay_min: float,
    page_delay_max: float,
    dry_run: bool,
    no_delay: bool,
) -> dict:
    fakeid = source.get("wechat_fakeid")
    if not fakeid:
        return {
            "source_id": source["id"],
            "source_name": source["name"],
            "status": "skipped",
            "reason": "missing_wechat_fakeid",
        }

    articles_seen = 0
    articles_in_window = 0
    articles_upserted = 0
    pages_fetched = 0
    oldest: datetime | None = None
    seen_urls: set[str] = set()
    total_count: int | None = None
    dynamic_page_cap = max(1, max_pages)
    stop_reason = "page_cap"

    for page in range(max(1, max_pages)):
        if page >= dynamic_page_cap:
            stop_reason = "total_count_reached"
            break

        begin = page * count
        payload = with_retries(
            lambda: adapter.list_articles(fakeid=fakeid, begin=begin, count=count),
            retries=retries,
            backoff_seconds=backoff_seconds,
        )
        if not payload.get("ok"):
            return {
                "source_id": source["id"],
                "source_name": source["name"],
                "status": "failed",
                "pages_fetched": pages_fetched,
                "articles_seen": articles_seen,
                "articles_in_window": articles_in_window,
                "articles_upserted": articles_upserted,
                "adapter": payload,
            }

        articles = normalize_article_items(payload.get("body"))
        if total_count is None:
            total_count = extract_article_total_count(payload.get("body"))
            if total_count is not None and count > 0:
                dynamic_page_cap = min(dynamic_page_cap, max(1, math.ceil(total_count / count) + 1))

        pages_fetched += 1
        if not articles:
            stop_reason = "empty_page"
            break

        unique_articles = []
        for article in articles:
            url = article.get("url")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            unique_articles.append(article)

        articles_seen += len(unique_articles)
        page_dates = [parse_article_publish_time(article.get("publish_time")) for article in unique_articles]
        page_dates = [publish_time for publish_time in page_dates if publish_time]
        if page_dates:
            page_oldest = min(page_dates)
            oldest = page_oldest if oldest is None else min(oldest, page_oldest)

        articles_to_save = [article for article in unique_articles if article_metadata_in_window(article, cutoff)]
        articles_in_window += len(articles_to_save)
        if not dry_run:
            saved = store.upsert_articles(source["id"], articles_to_save)
            articles_upserted += saved["count"]

        if page_dates and min(page_dates) < cutoff:
            stop_reason = "cutoff_reached"
            break
        if total_count is not None and begin + count >= total_count:
            stop_reason = "total_count_reached"
            break
        if total_count is None and len(articles) < count:
            stop_reason = "short_page"
            break
        delay_between_items(page, dynamic_page_cap, page_delay_min, page_delay_max, disabled=no_delay)

    return {
        "source_id": source["id"],
        "source_name": source["name"],
        "status": "ok",
        "pages_fetched": pages_fetched,
        "articles_seen": articles_seen,
        "articles_in_window": articles_in_window,
        "articles_upserted": articles_upserted,
        "oldest_publish_time": oldest.replace(microsecond=0).isoformat() if oldest else None,
        "downloader_total_count": total_count,
        "effective_page_cap": dynamic_page_cap,
        "stop_reason": stop_reason,
    }


def parse_article_publish_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def article_metadata_in_window(article: dict, cutoff: datetime) -> bool:
    publish_time = parse_article_publish_time(article.get("publish_time"))
    if publish_time is None:
        return True
    return publish_time >= cutoff


def extract_article_total_count(body: object) -> int | None:
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        data = body
    for key in ("total", "total_count"):
        value = data.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def update_history_totals(totals: dict[str, int], result: dict) -> None:
    status = result.get("status")
    if status == "ok":
        totals["sources_ok"] += 1
    elif status == "skipped":
        totals["sources_skipped"] += 1
    else:
        totals["sources_failed"] += 1

    totals["articles_seen"] += int(result.get("articles_seen") or 0)
    totals["articles_in_window"] += int(result.get("articles_in_window") or 0)
    totals["articles_upserted"] += int(result.get("articles_upserted") or 0)
    totals["pages_fetched"] += int(result.get("pages_fetched") or 0)

    stop_reason = result.get("stop_reason")
    if stop_reason == "cutoff_reached":
        totals["stopped_by_cutoff"] += 1
    elif stop_reason in {"short_page", "empty_page"}:
        totals["stopped_by_short_page"] += 1
    elif stop_reason == "total_count_reached":
        totals["stopped_by_total_count"] += 1
    elif stop_reason == "page_cap":
        totals["stopped_by_page_cap"] += 1


def cmd_collect_content(args: argparse.Namespace) -> int:
    store = get_store(args)
    adapter = get_wechat_download_api(args)
    policy = tier_policy(args.tier)
    limit = args.limit or policy.content_limit
    retries = policy.retries if args.retries is None else args.retries
    backoff_seconds = policy.backoff_seconds if args.backoff_seconds is None else args.backoff_seconds
    delay_min = policy.delay_min_seconds if args.delay_min is None else args.delay_min
    delay_max = policy.delay_max_seconds if args.delay_max is None else args.delay_max
    content_result = fetch_content_queue(
        store=store,
        adapter=adapter,
        limit=limit,
        retention_levels=None,
        retries=retries,
        backoff_seconds=backoff_seconds,
        delay_min=delay_min,
        delay_max=delay_max,
        passes=args.passes,
        pass_cooldown_seconds=args.pass_cooldown_seconds,
        progress_every=args.progress_every,
        no_delay=args.no_delay,
    )

    write_json(
        {
            "ok": True,
            "tier": args.tier,
            "policy": {
                "limit": limit,
                "delay_min_seconds": 0 if args.no_delay else delay_min,
                "delay_max_seconds": 0 if args.no_delay else delay_max,
                "retries": retries,
                "backoff_seconds": backoff_seconds,
                "passes": args.passes,
                "pass_cooldown_seconds": 0 if args.no_delay else args.pass_cooldown_seconds,
                "progress_every": args.progress_every,
            },
            **content_result,
        }
    )
    return 0


def fetch_content_queue(
    *,
    store: Store,
    adapter: WeChatDownloadAPIAdapter,
    limit: int,
    retention_levels: tuple[str, ...] | None,
    retries: int,
    backoff_seconds: float,
    delay_min: float,
    delay_max: float,
    passes: int,
    pass_cooldown_seconds: float,
    progress_every: int = 0,
    no_delay: bool,
) -> dict:
    """Fetch a fixed content queue over one or more internal passes."""
    target_articles = store.list_articles_for_content_fetch(limit=limit, retention_levels=retention_levels)
    pending_by_id = {article["id"]: article for article in target_articles}
    succeeded_ids: set[str] = set()
    final_failed_ids: set[str] = set()
    attempts = []
    pass_summaries = []
    max_passes = max(1, passes)

    for pass_index in range(max_passes):
        pass_articles = list(pending_by_id.values())
        if not pass_articles:
            break

        print(
            f"mpfeed: content pass {pass_index + 1}/{max_passes}, {len(pass_articles)} article(s) pending",
            file=sys.stderr,
            flush=True,
        )
        pass_ok = 0
        pass_failed = 0
        progress_every = max(0, int(progress_every or 0))
        for index, article in enumerate(pass_articles):
            result = fetch_and_store_article_content(
                store=store,
                adapter=adapter,
                article=article,
                retries=retries,
                backoff_seconds=backoff_seconds,
            )
            attempts.append({"pass": pass_index + 1, **result})
            if result["ok"]:
                pass_ok += 1
                succeeded_ids.add(article["id"])
                pending_by_id.pop(article["id"], None)
            else:
                pass_failed += 1
                if not result.get("retryable"):
                    final_failed_ids.add(article["id"])
                    pending_by_id.pop(article["id"], None)
            if progress_every and ((index + 1) % progress_every == 0 or index == len(pass_articles) - 1):
                print(
                    "mpfeed: content progress "
                    f"pass={pass_index + 1}/{max_passes}, "
                    f"{index + 1}/{len(pass_articles)} article(s), "
                    f"ok={pass_ok}, failed={pass_failed}, remaining={len(pending_by_id)}, "
                    f"last={article.get('title')!r}",
                    file=sys.stderr,
                    flush=True,
                )
            delay_between_items(index, len(pass_articles), delay_min, delay_max, disabled=no_delay)

        pass_summaries.append(
            {
                "pass": pass_index + 1,
                "articles_seen": len(pass_articles),
                "content_ok": pass_ok,
                "content_failed": pass_failed,
                "remaining": len(pending_by_id),
                "final_failed": len(final_failed_ids),
            }
        )
        if not pending_by_id:
            break
        if pass_index < max_passes - 1 and not no_delay and pass_cooldown_seconds > 0:
            time.sleep(pass_cooldown_seconds)

    return {
        "articles_seen": len(target_articles),
        "attempts": len(attempts),
        "passes": pass_summaries,
        "content_ok": len(succeeded_ids),
        "content_failed": len(final_failed_ids) + len(pending_by_id),
        "retryable_remaining": len(pending_by_id),
        "sample_results": attempts[:20],
        "results_truncated": max(0, len(attempts) - 20),
    }


def fetch_and_store_article_content(
    *,
    store: Store,
    adapter: WeChatDownloadAPIAdapter,
    article: dict,
    retries: int,
    backoff_seconds: float,
) -> dict:
    payload = with_retries(
        lambda: adapter.fetch_article(article["url"]),
        retries=retries,
        backoff_seconds=backoff_seconds,
    )
    if not payload["ok"]:
        retry_after_seconds = article_probe_retry_after_seconds(payload)
        saved = store.upsert_article_content(article["id"], {}, fetch_error=adapter_error_message(payload))
        return {
            "article_id": article["id"],
            "title": article["title"],
            "ok": False,
            "retryable": retryable_status(payload),
            "retry_after_seconds": retry_after_seconds,
            "error": adapter_error_message(payload),
            "saved": saved,
        }

    content = normalize_article_content(payload["body"])
    has_content = content.get("content_html") or content.get("content_text") or content.get("content_markdown")
    fetch_error = None if has_content else "empty_content"
    saved = store.upsert_article_content(article["id"], content, fetch_error=fetch_error)
    return {
        "article_id": article["id"],
        "title": article["title"],
        "ok": fetch_error is None,
        "retryable": False,
        "error": fetch_error,
        "assets": len(content.get("assets", [])),
        "saved": saved,
    }


def cmd_collect_candidate_latest(args: argparse.Namespace) -> int:
    write_json(
        collect_candidate_latest_probes(
            store=get_store(args),
            adapter=get_wechat_download_api(args),
            source_type=args.source_type,
            decision=None if args.decision == "all" else args.decision,
            limit=args.limit,
            count=args.count,
            best_per_import=args.best_per_import,
            strict_match_only=args.strict_match_only,
            delay_min=args.delay_min,
            delay_max=args.delay_max,
            validate_content=args.validate_content,
            no_delay=args.no_delay,
        )
    )
    return 0


def collect_candidate_latest_probes(
    store: Store,
    adapter: WeChatDownloadAPIAdapter,
    source_type: str | None,
    decision: str | None,
    limit: int,
    count: int,
    best_per_import: bool,
    strict_match_only: bool,
    delay_min: float,
    delay_max: float,
    validate_content: bool,
    no_delay: bool,
) -> dict:
    candidate_limit = max(limit * 10, 1000) if best_per_import else limit
    candidates = store.list_candidates(decision=decision, limit=candidate_limit)
    if source_type:
        candidates = [candidate for candidate in candidates if candidate.get("import_source_type") == source_type]
    if best_per_import:
        candidates = best_candidates_for_imports(
            store,
            candidates,
            source_type=source_type,
            limit=limit,
            strict_match_only=strict_match_only,
        )
    elif strict_match_only:
        raise ValueError("--strict-match-only requires --best-per-import")

    results = []
    skipped = []
    articles_saved = 0
    for index, candidate in enumerate(candidates):
        fakeid = candidate.get("wechat_fakeid")
        if not fakeid:
            skipped.append({"candidate_id": candidate["id"], "query": candidate.get("query"), "reason": "missing_wechat_fakeid"})
            continue

        payload = adapter.list_articles(fakeid=fakeid, begin=0, count=count)
        if not payload["ok"]:
            saved = store.save_candidate_article_probe(
                candidate["id"],
                [],
                payload.get("body"),
                fetch_error=f"adapter_status:{payload.get('status')}",
            )
            results.append({"candidate_id": candidate["id"], "query": candidate.get("query"), "ok": False, **saved})
            delay_between_items(index, len(candidates), delay_min, delay_max, disabled=no_delay)
            continue

        articles = normalize_article_items(payload["body"])
        if validate_content:
            articles = validate_latest_article_content(adapter, articles)
        saved = store.save_candidate_article_probe(candidate["id"], articles, payload["body"])
        articles_saved += saved["count"]
        results.append(
            {
                "candidate_id": candidate["id"],
                "query": candidate.get("query"),
                "candidate_name": candidate.get("candidate_name"),
                "ok": True,
                "count": saved["count"],
            }
        )
        delay_between_items(index, len(candidates), delay_min, delay_max, disabled=no_delay)

    return {
        "ok": True,
        "candidates_seen": len(candidates),
        "strict_match_only": strict_match_only,
        "content_validated": validate_content,
        "articles_saved_to_candidate_payload": articles_saved,
        "skipped": skipped,
        "result_sample": results[:20],
        "results_truncated": max(0, len(results) - 20),
    }


def validate_latest_article_content(adapter: WeChatDownloadAPIAdapter, articles: list[dict[str, object]]) -> list[dict[str, object]]:
    """Mark candidate articles as content-fetchable without storing full bodies."""
    validated = []
    found_usable = False
    for article in articles:
        item = dict(article)
        if not found_usable:
            payload = fetch_article_for_probe(adapter, str(item.get("url") or ""))
            content = normalize_article_content(payload.get("body")) if payload.get("ok") else {}
            has_content = bool(content.get("content_html") or content.get("content_text") or content.get("content_markdown"))
            item["content_fetch_ok"] = has_content
            if not has_content:
                body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
                item["content_fetch_error"] = body.get("error") or f"adapter_status:{payload.get('status')}"
            found_usable = has_content
        validated.append(item)
    return validated


def fetch_article_for_probe(adapter: WeChatDownloadAPIAdapter, url: str, retries: int = 2, sleep_seconds: float = 3.2) -> dict:
    attempts = 0
    while True:
        payload = adapter.fetch_article(url)
        if payload.get("ok") or attempts >= retries or not article_probe_rate_limited(payload):
            return payload
        attempts += 1
        time.sleep(max(sleep_seconds * attempts, article_probe_retry_after_seconds(payload)))


def article_probe_rate_limited(payload: dict) -> bool:
    body = payload.get("body")
    error = body.get("error") if isinstance(body, dict) else None
    text = str(error or "")
    return "Rate limited" in text or "过快" in text or "重试" in text


def article_probe_retry_after_seconds(payload: dict) -> float:
    body = payload.get("body")
    error = body.get("error") if isinstance(body, dict) else None
    match = re.search(r"(\d+)\s*秒", str(error or ""))
    if match:
        return float(match.group(1)) + 0.8
    return 0.0


def best_candidates_for_imports(
    store: Store,
    candidates: list[dict[str, object]],
    source_type: str | None,
    limit: int,
    strict_match_only: bool = False,
) -> list[dict[str, object]]:
    candidates_by_import: dict[str, list[dict[str, object]]] = {}
    for candidate in candidates:
        candidates_by_import.setdefault(str(candidate["import_id"]), []).append(candidate)

    selected = []
    for item in store.list_imports(limit=limit, source_type=source_type):
        best = pick_best_candidate(item, candidates_by_import.get(item["id"], []))
        if best and (not strict_match_only or names_match_manual_or_raw(item, best.get("candidate_name"))):
            selected.append(best)
    return selected


def cmd_classify_sources(args: argparse.Namespace) -> int:
    store = get_store(args)
    taxonomy = load_taxonomy(resolve_taxonomy_arg(args.taxonomy))
    rows = store.list_sources(limit=args.limit)
    items = [store.save_classification(classify_source(row, taxonomy)) for row in rows]
    write_json({"ok": True, "taxonomy": taxonomy.name, "entity_type": "source", "count": len(items), "items": items})
    return 0


def cmd_classify_articles(args: argparse.Namespace) -> int:
    store = get_store(args)
    taxonomy = load_taxonomy(resolve_taxonomy_arg(args.taxonomy))
    rows = store.list_articles_for_llm(limit=args.limit, source_id=args.source_id, content_scope="all")
    items = [store.save_classification(classify_article(row, taxonomy)) for row in rows]
    write_json({"ok": True, "taxonomy": taxonomy.name, "entity_type": "article", "count": len(items), "items": items})
    return 0


def cmd_source_status(args: argparse.Namespace) -> int:
    store = get_store(args)
    source = find_source_for_agent(store, source_id=args.source_id, name=args.name, biz=args.biz)
    if not source:
        write_json(
            {
                "ok": True,
                "status": "not_tracked",
                "input": {"source_id": args.source_id or None, "name": args.name or None, "biz": args.biz or None},
                "taxonomy": source_taxonomy_options(args.taxonomy),
                "next_action": "If the user asked to follow/add, run the intake flow. Otherwise ask before adding.",
            }
        )
        return 0

    write_json(
        {
            "ok": True,
            "status": "source_status",
            "intake_state": source.get("status"),
            "source": source,
            "input": {"source_id": args.source_id or None, "name": args.name or None, "biz": args.biz or None},
            "source_snapshot": source_snapshot(store, source["id"]),
            "taxonomy": source_taxonomy_options(args.taxonomy),
            "next_action": "Use source taxonomy and classification helpers; do not edit SQLite directly.",
        }
    )
    return 0


def cmd_source_set_status(args: argparse.Namespace) -> int:
    store = get_store(args)
    source = find_source_for_agent(store, source_id=args.source_id, name=args.name, biz=args.biz)
    if not source:
        write_json(
            {
                "ok": False,
                "status": "source_not_found",
                "input": {"source_id": args.source_id or None, "name": args.name or None, "biz": args.biz or None},
                "next_action": "Run intake/onboarding before setting status for an unknown source.",
            }
        )
        return 2

    update = store.update_source(source["id"], status=args.status)
    snapshot = source_snapshot(store, source["id"])
    write_json(
        {
            "ok": True,
            "status": "source_status_updated",
            "requested_status": args.status,
            "update": update,
            "source": store.get_source(source["id"]),
            "source_snapshot": snapshot,
            "next_action": "Use inactive for unsubscribe, active for explicit re-follow, archived for long-term removal, and needs_review when user confirmation is required.",
        }
    )
    return 0


def cmd_source_taxonomy(args: argparse.Namespace) -> int:
    write_json({"ok": True, "status": "taxonomy", "taxonomy": source_taxonomy_options(args.taxonomy)})
    return 0


def cmd_source_confirm_classification(args: argparse.Namespace) -> int:
    store = get_store(args)
    source = find_source_for_agent(store, source_id=args.source_id, name=args.name, biz=args.biz)
    if not source:
        write_json(
            {
                "ok": False,
                "status": "source_not_found",
                "input": {"source_id": args.source_id or None, "name": args.name or None, "biz": args.biz or None},
                "next_action": "Run source status or onboarding first; do not classify an unknown source.",
            }
        )
        return 2

    requested_tags = parse_tag_ids(args.tags)
    validation = validate_source_classification_request(
        taxonomy_name=args.taxonomy,
        category=args.category,
        source_attribute=args.source_attribute,
        tags=requested_tags,
    )
    if not validation["ok"]:
        write_json(
            {
                "ok": False,
                "status": "classification_validation_failed",
                "source": source,
                "classification_request": {
                    "category": args.category,
                    "source_attribute": args.source_attribute or None,
                    "tags": requested_tags,
                    "confidence": args.confidence,
                    "method": args.method,
                },
                "validation": validation,
                "taxonomy": source_taxonomy_options(args.taxonomy),
                "next_action": "Ask the user to choose allowed taxonomy ids or keep unsupported labels as suggestions.",
            }
        )
        return 2

    classification = store.replace_source_classification(
        {
            "entity_type": "source",
            "entity_id": source["id"],
            "taxonomy": args.taxonomy,
            "category": args.category,
            "tags": validation["normalized_tags"],
            "confidence": max(0.0, min(1.0, args.confidence)),
            "method": args.method,
        }
    )
    confirmed_review = None
    if args.review_id:
        confirmed_review = store.update_source_classification_review_status(args.review_id, "confirmed")
    snapshot = source_snapshot(store, source["id"])
    write_json(
        {
            "ok": True,
            "status": "classification_confirmed",
            "source": source,
            "classification": classification,
            "confirmed_review": confirmed_review,
            "readback": snapshot.get("latest_classification"),
            "source_snapshot": snapshot,
            "taxonomy": source_taxonomy_options(args.taxonomy),
            "next_action": "Report the classification only after checking readback.category and readback.tags.",
        }
    )
    return 0


def cmd_source_refresh_latest(args: argparse.Namespace) -> int:
    store = get_store(args)
    source = find_source_for_agent(store, source_id=args.source_id, name=args.name, biz=args.biz, statuses=("active",))
    if not source:
        write_json(
            {
                "ok": False,
                "status": "source_not_active",
                "input": {"source_id": args.source_id or None, "name": args.name or None, "biz": args.biz or None},
                "next_action": "Check source status first. Reactivate only when the user explicitly asks to follow again.",
            }
        )
        return 2
    fakeid = source.get("wechat_fakeid")
    if not fakeid:
        write_json({"ok": False, "status": "missing_wechat_fakeid", "source": source})
        return 2
    if args.count <= 0:
        latest = {"ok": True, "count": 0, "items": [], "status": "skipped_by_article_count"}
        write_json(
            {
                "ok": True,
                "status": "latest_refreshed",
                "source": source,
                "latest_articles": latest,
                "source_snapshot": source_snapshot(store, source["id"]),
            }
        )
        return 0

    adapter = get_wechat_download_api(args)
    payload = with_retries(
        lambda: adapter.list_articles(fakeid=fakeid, begin=0, count=args.count),
        retries=args.retries,
        backoff_seconds=args.backoff_seconds,
    )
    if not payload["ok"]:
        write_json({"ok": False, "status": "latest_refresh_failed", "source": source, "adapter": payload})
        return 2
    articles = normalize_article_items(payload["body"])
    saved = store.upsert_articles(source["id"], articles)
    write_json(
        {
            "ok": True,
            "status": "latest_refreshed",
            "source": source,
            "latest_articles": {"ok": True, "count": saved["count"], "items": saved["items"]},
            "source_snapshot": source_snapshot(store, source["id"]),
        }
    )
    return 0


def find_source_for_agent(
    store: Store,
    *,
    source_id: str = "",
    name: str = "",
    biz: str = "",
    statuses: tuple[str, ...] = ("active", "inactive", "archived", "needs_review"),
) -> dict | None:
    sources = store.list_sources(limit=100_000)
    if source_id:
        return next((source for source in sources if source["id"] == source_id), None)
    if biz:
        return next((source for source in sources if source.get("biz") == biz and source.get("status") in statuses), None)
    if name:
        for source in sources:
            if source.get("status") in statuses and names_equivalent(name, source["name"]):
                return source
    return None


def source_snapshot(store: Store, source_id: str) -> dict:
    classifications = [
        row
        for row in store.list_classifications(limit=100_000, entity_type="source")
        if row.get("entity_id") == source_id
    ]
    articles = store.list_articles(limit=5, source_id=source_id)
    all_articles = store.list_articles(limit=100_000, source_id=source_id)
    pending_reviews = store.list_source_classification_reviews(limit=20, source_id=source_id, status="pending")
    content_ok_count = sum(1 for row in all_articles if row.get("crawl_status") == "content_ok")
    return {
        "latest_classification": classifications[0] if classifications else None,
        "classifications": classifications,
        "pending_classification_reviews": pending_reviews,
        "article_count": len(all_articles),
        "content_ok_count": content_ok_count,
        "latest_articles": articles,
    }


def source_taxonomy_options(taxonomy_name: str) -> dict:
    taxonomy = load_taxonomy(resolve_taxonomy_arg(taxonomy_name))
    source_attributes = []
    tag_groups = []
    for group in taxonomy.tag_groups:
        tags = [{"id": tag.id, "name_zh": tag.name_zh} for tag in group.tags]
        tag_groups.append({"id": group.id, "name_zh": group.name_zh, "tags": tags})
        if group.id == "source_attribute":
            source_attributes = tags
    return {
        "ok": True,
        "name": taxonomy.name,
        "source_categories": [{"id": entry.id, "name_zh": entry.name_zh} for entry in taxonomy.source_categories],
        "source_attributes": source_attributes,
        "tag_groups": tag_groups,
        "invalid_ids_must_not_be_written": True,
    }


def parse_tag_ids(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def validate_source_classification_request(
    *,
    taxonomy_name: str,
    category: str,
    source_attribute: str,
    tags: list[str],
) -> dict:
    taxonomy = load_taxonomy(resolve_taxonomy_arg(taxonomy_name))
    allowed_categories = {entry.id for entry in taxonomy.source_categories}
    source_attribute_ids = set()
    all_tag_ids = set()
    for group in taxonomy.tag_groups:
        group_ids = {tag.id for tag in group.tags}
        all_tag_ids.update(group_ids)
        if group.id == "source_attribute":
            source_attribute_ids = group_ids

    errors = []
    if not category:
        errors.append("category_required")
    elif category not in allowed_categories:
        errors.append(f"invalid_category:{category}")

    normalized_tags = []
    if source_attribute:
        if source_attribute not in source_attribute_ids:
            errors.append(f"invalid_source_attribute:{source_attribute}")
        else:
            normalized_tags.append(source_attribute)

    for tag in tags:
        if tag not in all_tag_ids:
            errors.append(f"invalid_tag:{tag}")
        elif tag not in normalized_tags:
            normalized_tags.append(tag)

    return {
        "ok": not errors,
        "errors": errors,
        "normalized_tags": normalized_tags,
        "allowed_taxonomy": source_taxonomy_options(taxonomy_name),
    }


def cmd_digest_articles(args: argparse.Namespace) -> int:
    store = get_store(args)
    taxonomy = load_taxonomy(resolve_taxonomy_arg(args.taxonomy))
    rows = store.list_articles_with_content(limit=args.limit, source_id=args.source_id)
    saved = []
    skipped = []

    for row in rows:
        classification = classify_article(row, taxonomy)
        store.save_classification(classification)
        digest = generate_article_digest(row, classification, taxonomy)
        if digest["importance_score"] < args.min_score:
            skipped.append(
                {
                    "article_id": row["id"],
                    "title": row["title"],
                    "importance_score": digest["importance_score"],
                    "reason": "below_min_score",
                }
            )
            continue
        saved.append(store.save_digest(digest))

    write_json(
        {
            "ok": True,
            "taxonomy": taxonomy.name,
            "articles_seen": len(rows),
            "digests_saved": len(saved),
            "digests_skipped": len(skipped),
            "items": saved,
            "skipped": skipped,
        }
    )
    return 0


def cmd_archive_assets(args: argparse.Namespace) -> int:
    result = cache_full_archive_assets(
        store=get_store(args),
        output_dir=Path(args.output_dir).expanduser(),
        limit=effective_unbounded_limit(args.limit),
        timeout=args.timeout,
        overwrite=args.overwrite,
    )
    write_json(result)
    return 0 if result["ok"] else 1


def cmd_llm_export_jobs(args: argparse.Namespace) -> int:
    store = get_store(args)
    taxonomy = load_taxonomy(resolve_taxonomy_arg(args.taxonomy))
    retention_levels = retention_levels_for_llm_export(args.article_retention)
    payload = build_llm_jobs(
        store=store,
        taxonomy=taxonomy,
        entity_type=args.entity_type,
        limit=args.limit,
        source_id=args.source_id,
        content_chars=args.content_chars,
        source_article_limit=args.source_article_limit,
        article_content_scope=args.article_content_scope,
        article_updated_after=args.article_updated_after,
        article_published_after=args.article_published_after,
        article_published_before=args.article_published_before,
        article_retention_levels=retention_levels,
        article_analysis_stage=args.article_analysis_stage,
    )
    if args.output:
        output_path = Path(args.output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        write_json({"ok": True, "output": str(output_path), "count": payload["count"]})
        return 0
    write_json(payload)
    return 0


def retention_levels_for_llm_export(value: str) -> tuple[str, ...] | None:
    if value == "all":
        return None
    if value == "content_or_archive":
        return ("content", "full_archive")
    return (value,)


def cmd_llm_export_onboarding_jobs(args: argparse.Namespace) -> int:
    decision = None if args.decision == "all" else args.decision
    payload = build_onboarding_llm_jobs(
        store=get_store(args),
        taxonomy=load_taxonomy(resolve_taxonomy_arg(args.taxonomy)),
        source_type=args.source_type,
        decision=decision,
        limit=args.limit,
        candidate_limit=args.candidate_limit,
        article_limit=args.article_limit,
        strict_match_only=args.strict_match_only,
    )
    if args.output:
        output_path = Path(args.output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        write_json({"ok": True, "output": str(output_path), "count": payload["count"]})
        return 0
    write_json(payload)
    return 0


def cmd_llm_import_results(args: argparse.Namespace) -> int:
    with open(args.file, encoding="utf-8") as handle:
        payload = json.load(handle)
    taxonomy = load_taxonomy(resolve_taxonomy_arg(args.taxonomy))
    result = apply_llm_results(
        store=get_store(args),
        payload=payload,
        default_taxonomy=taxonomy,
        default_model=args.model,
    )
    write_json(result)
    return 0 if result["ok"] else 1


def cmd_run_batch(args: argparse.Namespace) -> int:
    store = get_store(args)
    store.init()
    adapter = get_wechat_download_api(args)
    health = safe_adapter_call(adapter.health)
    auth_status = safe_adapter_call(adapter.auth_status) if health.get("ok") else None
    if not health.get("ok") or not (auth_status and auth_status_logged_in(auth_status)):
        write_json(
            {
                "ok": False,
                "stage": "doctor",
                "health": health,
                "auth_status": auth_status,
                "login_url": login_url_for_base(args.base_url or adapter_base_url_from_env(required=True)),
            }
        )
        return 1

    imported = None
    if args.names_file:
        with open(args.names_file, encoding="utf-8-sig") as handle:
            rows = [{"raw_name": line.strip()} for line in handle if line.strip() and not line.lstrip().startswith("#")]
        imported = store.import_source_rows(args.source_type, rows)

    resolved = resolve_pending_imports(
        store=store,
        adapter=adapter,
        limit=args.resolve_limit,
        source_type=args.source_type,
        delay_min=0 if args.no_delay else 1.0,
        delay_max=0 if args.no_delay else 3.0,
        no_delay=args.no_delay,
    )
    auto_reviewed = None
    if not args.no_auto_exact:
        auto_reviewed = auto_accept_exact_candidates(
            store=store,
            source_type=args.source_type,
            limit=args.candidate_limit,
            exact_score=args.exact_score,
        )

    collected = collect_latest_articles(
        store=store,
        adapter=adapter,
        tier=args.tier,
        max_sources=args.max_sources,
        count=args.article_count,
        begin=0,
        no_delay=args.no_delay,
    )
    content = collect_article_contents(
        store=store,
        adapter=adapter,
        tier=args.tier,
        limit=args.content_limit,
        no_delay=args.no_delay,
    )
    digested = classify_and_digest_articles(
        store=store,
        taxonomy=load_taxonomy(resolve_taxonomy_arg(args.taxonomy)),
        limit=args.digest_limit,
        min_score=args.min_score,
    )

    digest_rows = store.list_digests(limit=args.digest_limit)
    digest_markdown = format_digests_markdown(digest_rows)
    if args.digest_output:
        output_path = Path(args.digest_output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(digest_markdown + "\n", encoding="utf-8")

    write_json(
        {
            "ok": True,
            "db": str(store.db_path),
            "service": {
                "base_url": args.base_url or adapter_base_url_from_env(),
                "health": health,
                "auth_status": auth_status,
            },
            "imported": imported,
            "resolved": resolved,
            "auto_reviewed": auto_reviewed,
            "collected": collected,
            "content": content,
            "digested": digested,
            "digest_output": args.digest_output,
            "digest_preview": digest_rows[:5],
        }
    )
    return 0


def cmd_run_agent_smoke(args: argparse.Namespace) -> int:
    work_dir = Path(args.work_dir).expanduser()
    work_dir.mkdir(parents=True, exist_ok=True)
    if not getattr(args, "db", None):
        args.db = str(work_dir / "agent-smoke.sqlite")

    store = get_store(args)
    store.init()
    seeded = seed_demo_feed(store)
    taxonomy = load_taxonomy(resolve_taxonomy_arg(args.taxonomy))
    scored = classify_and_digest_articles(
        store=store,
        taxonomy=taxonomy,
        limit=effective_unbounded_limit(args.limit),
        min_score=0.0,
    )

    summary = store.feed_summary()
    feed_rows = store.list_feed_items(limit=args.limit)
    failed_rows = store.list_feed_items(limit=args.limit, crawl_status="content_failed")
    llm_jobs = build_llm_jobs(
        store=store,
        taxonomy=taxonomy,
        entity_type="article",
        limit=args.limit,
        content_chars=args.content_chars,
        article_content_scope="all",
    )

    feed_output = work_dir / "feed-items.csv"
    summary_output = work_dir / "feed-summary.json"
    failures_output = work_dir / "feed-failures.csv"
    llm_jobs_output = work_dir / "article-llm-jobs.json"
    report_output = work_dir / "agent-smoke-report.md"

    write_feed_items_file(feed_output, feed_rows, "csv")
    write_feed_summary_file(summary_output, summary, "json")
    write_feed_items_file(failures_output, failed_rows, "csv")
    llm_jobs_output.write_text(json.dumps(llm_jobs, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_output.write_text(
        format_agent_smoke_report(
            summary=summary,
            feed_rows=feed_rows,
            failed_rows=failed_rows,
            scored=scored,
            llm_jobs=llm_jobs,
            outputs={
                "feed": feed_output,
                "summary": summary_output,
                "failures": failures_output,
                "llm_jobs": llm_jobs_output,
            },
        )
        + "\n",
        encoding="utf-8",
    )

    write_json(
        {
            "ok": True,
            "mode": "offline_agent_smoke",
            "db": str(store.db_path),
            "seeded": seeded,
            "scored": {
                "articles_seen": scored["articles_seen"],
                "digests_saved": scored["digests_saved"],
                "digests_skipped": scored["digests_skipped"],
            },
            "summary": summary,
            "outputs": {
                "feed": str(feed_output),
                "summary": str(summary_output),
                "failures": str(failures_output),
                "llm_jobs": str(llm_jobs_output),
                "report": str(report_output),
                "feed_rows": len(feed_rows),
                "failure_rows": len(failed_rows),
                "llm_jobs_count": llm_jobs["count"],
            },
            "agent_checks": [
                "Read agent-smoke-report.md and summarize feed health.",
                "Inspect feed-failures.csv and explain whether failures are expected.",
                "Inspect article-llm-jobs.json and confirm it is suitable for article-level semantic analysis.",
                "For real deployment, replace the demo database/config with the user's private reviewed source registry.",
            ],
        }
    )
    return 0


def cmd_run_llm_feed(args: argparse.Namespace) -> int:
    work_dir = Path(args.work_dir).expanduser()
    work_dir.mkdir(parents=True, exist_ok=True)
    store = get_store(args)
    store.init()
    taxonomy = load_taxonomy(resolve_taxonomy_arg(args.taxonomy))
    limit = effective_unbounded_limit(args.limit)

    metadata_jobs_path = work_dir / "article-metadata-jobs.json"
    content_jobs_path = work_dir / "article-content-jobs.json"
    digest_dir = work_dir / "digest-packs"
    digest_dir.mkdir(parents=True, exist_ok=True)

    metadata_jobs = build_llm_jobs(
        store=store,
        taxonomy=taxonomy,
        entity_type="article",
        limit=limit,
        article_content_scope="metadata",
        article_published_after=args.published_after,
        article_published_before=args.published_before,
        article_analysis_stage="metadata",
    )
    metadata_jobs_path.write_text(json.dumps(metadata_jobs, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    metadata_imported = None
    content_fetch = None
    content_jobs = None
    content_imported = None
    digest_packs = []
    service = None

    if args.metadata_results:
        with open(args.metadata_results, encoding="utf-8") as handle:
            metadata_imported = apply_llm_results(
                store,
                json.load(handle),
                default_taxonomy=args.taxonomy,
                default_model=args.model,
                taxonomy=taxonomy,
            )

        if args.fetch_content:
            adapter = get_wechat_download_api(args)
            base_url = args.base_url or adapter_base_url_from_env(required=True)
            health = safe_adapter_call(adapter.health)
            auth_status = safe_adapter_call(adapter.auth_status) if health.get("ok") else None
            service = {"base_url": base_url, "health": health, "auth_status": auth_status}
            if not health.get("ok") or not (auth_status and auth_status_logged_in(auth_status)):
                write_json(
                    {
                        "ok": False,
                        "stage": "doctor",
                        "db": str(store.db_path),
                        "service": service,
                        "login_url": login_url_for_base(base_url),
                        "outputs": {"metadata_jobs": str(metadata_jobs_path)},
                    }
                )
                return 1
            content_fetch = fetch_content_queue(
                store=store,
                adapter=adapter,
                limit=effective_unbounded_limit(args.content_limit),
                retention_levels=retention_levels_for_content_fetch(args.content_retention),
                retries=args.content_retries,
                backoff_seconds=args.content_backoff_seconds,
                delay_min=args.content_delay_min,
                delay_max=args.content_delay_max,
                passes=args.content_passes,
                pass_cooldown_seconds=args.content_pass_cooldown_seconds,
                progress_every=args.progress_every,
                no_delay=args.no_delay,
            )

        content_jobs = build_llm_jobs(
            store=store,
            taxonomy=taxonomy,
            entity_type="article",
            limit=limit,
            content_chars=args.content_chars,
            article_content_scope="content",
            article_published_after=args.published_after,
            article_published_before=args.published_before,
            article_retention_levels=("content", "full_archive"),
            article_analysis_stage="content",
        )
        content_jobs_path.write_text(json.dumps(content_jobs, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.content_results:
        with open(args.content_results, encoding="utf-8") as handle:
            content_imported = apply_llm_results(
                store,
                json.load(handle),
                default_taxonomy=args.taxonomy,
                default_model=args.model,
                taxonomy=taxonomy,
            )
        for target in (
            "daily_digest",
            "weekly_report",
            "strategy_backlog",
            "market_view",
            "industry_tracking",
            "risk_monitoring",
        ):
            rows = store.list_digests(
                limit=limit,
                application_target=target,
                min_score=args.digest_min_score,
                analysis_stage="content",
            )
            path = digest_dir / f"{target}.md"
            path.write_text(format_digests_markdown(rows) + "\n", encoding="utf-8")
            context_rows = build_digest_context_rows(
                store=store,
                limit=limit,
                application_target=target,
                min_score=args.digest_min_score,
                analysis_stage="content",
                include_html=False,
                include_assets=True,
                require_content=False,
                max_content_chars=0,
            )
            context_path = digest_dir / f"{target}.context.json"
            context_path.write_text(json.dumps(context_rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            digest_packs.append({"target": target, "path": str(path), "context_path": str(context_path), "count": len(rows)})

    next_steps = []
    if not args.metadata_results:
        next_steps.append(f"Ask an agent/LLM to complete metadata jobs at {metadata_jobs_path}, then rerun with --metadata-results <file>.")
    elif content_jobs is not None and not args.content_results:
        next_steps.append(f"Ask an agent/LLM to complete content jobs at {content_jobs_path}, then rerun with --content-results <file>.")
    else:
        next_steps.append(f"Review digest packs in {digest_dir}.")

    write_json(
        {
            "ok": True,
            "db": str(store.db_path),
            "service": service,
            "window": {
                "published_after": args.published_after,
                "published_before": args.published_before,
            },
            "metadata_imported": metadata_imported,
            "content_fetch": content_fetch,
            "content_imported": content_imported,
            "outputs": {
                "metadata_jobs": str(metadata_jobs_path),
                "metadata_jobs_count": metadata_jobs["count"],
                "content_jobs": str(content_jobs_path) if content_jobs is not None else None,
                "content_jobs_count": content_jobs["count"] if content_jobs is not None else None,
                "digest_dir": str(digest_dir),
                "digest_packs": digest_packs,
            },
            "next_steps": next_steps,
        }
    )
    return 0


def cmd_run_onboarding(args: argparse.Namespace) -> int:
    store = get_store(args)
    store.init()
    adapter = get_wechat_download_api(args)
    base_url = args.base_url or adapter_base_url_from_env(required=True)
    health = safe_adapter_call(adapter.health)
    auth_status = safe_adapter_call(adapter.auth_status) if health.get("ok") else None
    if not health.get("ok") or not (auth_status and auth_status_logged_in(auth_status)):
        write_json(
            {
                "ok": False,
                "stage": "doctor",
                "db": str(store.db_path),
                "service": {"base_url": base_url, "health": health, "auth_status": auth_status},
                "login_url": login_url_for_base(base_url),
                "next_steps": ["Open the login URL and scan the WeChat QR code, then run this command again."],
            }
        )
        return 1

    work_dir = Path(args.work_dir).expanduser()
    llm_jobs_output = Path(args.llm_jobs_output).expanduser() if args.llm_jobs_output else work_dir / "onboarding-jobs.json"
    review_output = Path(args.review_output).expanduser() if args.review_output else work_dir / f"onboarding-review.{args.review_format}"

    imported = import_onboarding_input(store, args)

    search_rounds = [
        {
            "stage": "search_original",
            "result": resolve_import_candidates(
                store=store,
                adapter=adapter,
                limit=args.limit,
                source_type=args.source_type,
                status="pending",
                retry_empty=False,
                query_variants=False,
                replace_pending_candidates=False,
                delay_min=args.delay_min,
                delay_max=args.delay_max,
                no_delay=args.no_delay,
            ),
        },
        {
            "stage": "retry_empty_original",
            "result": resolve_import_candidates(
                store=store,
                adapter=adapter,
                limit=args.limit,
                source_type=args.source_type,
                status=None,
                retry_empty=True,
                query_variants=False,
                replace_pending_candidates=False,
                delay_min=args.retry_delay_min,
                delay_max=args.retry_delay_max,
                no_delay=args.no_delay,
            ),
        },
        {
            "stage": "retry_empty_query_variants",
            "result": resolve_import_candidates(
                store=store,
                adapter=adapter,
                limit=args.limit,
                source_type=args.source_type,
                status=None,
                retry_empty=True,
                query_variants=True,
                replace_pending_candidates=False,
                delay_min=args.retry_delay_min,
                delay_max=args.retry_delay_max,
                no_delay=args.no_delay,
            ),
        },
    ]

    candidate_latest = collect_candidate_latest_probes(
        store=store,
        adapter=adapter,
        source_type=args.source_type,
        decision=None,
        limit=args.limit,
        count=args.candidate_count,
        best_per_import=True,
        strict_match_only=True,
        delay_min=args.delay_min,
        delay_max=args.delay_max,
        validate_content=not args.no_validate_latest_content,
        no_delay=args.no_delay,
    )

    taxonomy = load_taxonomy(resolve_taxonomy_arg(args.taxonomy))
    llm_jobs = build_onboarding_llm_jobs(
        store=store,
        taxonomy=taxonomy,
        source_type=args.source_type,
        decision=None,
        limit=args.limit,
        candidate_limit=args.candidate_limit,
        article_limit=args.article_limit,
    )
    llm_jobs_output.parent.mkdir(parents=True, exist_ok=True)
    llm_jobs_output.write_text(json.dumps(llm_jobs, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    llm_imported = None
    if args.llm_results:
        with open(args.llm_results, encoding="utf-8") as handle:
            llm_imported = apply_llm_results(
                store=store,
                payload=json.load(handle),
                default_taxonomy=taxonomy,
                default_model="llm:agent",
            )

    review_rows = build_compact_onboarding_rows(store=store, taxonomy=taxonomy, source_type=args.source_type, limit=args.limit)
    review_output.parent.mkdir(parents=True, exist_ok=True)
    if args.review_format == "json":
        review_output.write_text(json.dumps(review_rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        write_compact_onboarding_csv(review_output, review_rows)

    unresolved = summarize_onboarding_review(review_rows)
    write_json(
        {
            "ok": True,
            "db": str(store.db_path),
            "service": {"base_url": base_url, "health": health, "auth_status": auth_status},
            "source_type": args.source_type,
            "imported": imported,
            "search_rounds": search_rounds,
            "candidate_latest": candidate_latest,
            "llm_jobs_output": str(llm_jobs_output),
            "llm_jobs_count": llm_jobs["count"],
            "llm_imported": llm_imported,
            "review_output": str(review_output),
            "review_summary": unresolved,
            "next_steps": onboarding_next_steps(args.llm_results, llm_jobs_output, review_output),
        }
    )
    return 0


def cmd_run_feed(args: argparse.Namespace) -> int:
    applied_config = apply_run_feed_config(args)
    store = get_store(args)
    store.init()

    adapter = None
    service = None
    needs_downloader = not args.skip_refresh or args.full or args.fetch_retained_content
    if needs_downloader:
        adapter = get_wechat_download_api(args)
        base_url = args.base_url or adapter_base_url_from_env(required=True)
        health = safe_adapter_call(adapter.health)
        auth_status = safe_adapter_call(adapter.auth_status) if health.get("ok") else None
        service = {"base_url": base_url, "health": health, "auth_status": auth_status}
        if not health.get("ok") or not (auth_status and auth_status_logged_in(auth_status)):
            write_json(
                {
                    "ok": False,
                    "stage": "doctor",
                    "db": str(store.db_path),
                    "service": service,
                    "login_url": login_url_for_base(base_url),
                    "next_steps": ["Open the login URL and scan the WeChat QR code, then run this command again."],
                }
            )
            return 1

    work_dir = Path(args.work_dir).expanduser()
    feed_output = Path(args.feed_output).expanduser() if args.feed_output else work_dir / f"feed-items.{args.feed_format}"
    summary_output = Path(args.summary_output).expanduser() if args.summary_output else work_dir / f"feed-summary.{args.summary_format}"
    failures_output = (
        Path(args.failures_output).expanduser()
        if args.failures_output
        else work_dir / f"feed-failures.{args.failures_format}"
    )

    refreshed = None
    if not args.skip_refresh:
        tier = None if args.tier == "all" else args.tier
        max_sources = effective_unbounded_limit(args.max_sources)
        sources = store.list_collectable_sources(tier=tier, limit=max_sources)
        print(
            f"mpfeed: refreshing article metadata for {len(sources)} active source(s)",
            file=sys.stderr,
            flush=True,
        )
        results = []
        skipped = []
        total_articles = 0
        progress_every = max(0, int(getattr(args, "progress_every", 0) or 0))
        for index, source in enumerate(sources):
            result = refresh_article_metadata_for_source(store=store, adapter=adapter, source=source, args=args)
            if result["status"] == "skipped":
                skipped.append(result)
            else:
                results.append(result)
                total_articles += int(result.get("articles_upserted") or result.get("count") or 0)
            if progress_every and ((index + 1) % progress_every == 0 or index == len(sources) - 1):
                print_article_metadata_progress(index, sources, results, skipped, total_articles, source)
            delay_between_items(index, len(sources), args.delay_min, args.delay_max, disabled=args.no_delay)

        refreshed = {
            "tier": args.tier,
            "sources_seen": len(sources),
            "sources_skipped": len(skipped),
            "articles_saved": total_articles,
            "policy": {
                "article_count": args.count,
                "begin": args.begin,
                "refresh_mode": args.refresh_mode,
                "incremental_max_pages": args.incremental_max_pages,
                "incremental_page_delay_min_seconds": 0 if args.no_delay else args.incremental_page_delay_min,
                "incremental_page_delay_max_seconds": 0 if args.no_delay else args.incremental_page_delay_max,
                "delay_min_seconds": 0 if args.no_delay else args.delay_min,
                "delay_max_seconds": 0 if args.no_delay else args.delay_max,
                "retries": args.retries,
                "backoff_seconds": args.backoff_seconds,
                "max_sources": args.max_sources,
            },
            "results": results,
            "skipped": skipped,
        }

    scored = None
    if args.full or args.score_articles:
        print("mpfeed: scoring articles with rules_v1", file=sys.stderr, flush=True)
        scored_result = classify_and_digest_articles(
            store=store,
            taxonomy=load_taxonomy(resolve_taxonomy_arg(args.taxonomy)),
            limit=effective_unbounded_limit(args.score_limit),
            min_score=args.min_score,
        )
        scored = {
            "articles_seen": scored_result["articles_seen"],
            "digests_saved": scored_result["digests_saved"],
            "digests_skipped": scored_result["digests_skipped"],
            "sample_items": scored_result["items"][:10],
            "sample_skipped": scored_result["skipped"][:10],
        }

    content = None
    if args.full or args.fetch_retained_content:
        assert adapter is not None
        retention_levels = retention_levels_for_content_fetch(args.content_retention)
        content_limit = effective_unbounded_limit(args.content_limit)
        articles = store.list_articles_for_content_fetch(limit=content_limit, retention_levels=retention_levels)
        print(
            f"mpfeed: fetching retained content for {len(articles)} article(s)",
            file=sys.stderr,
            flush=True,
        )
        content = fetch_content_queue(
            store=store,
            adapter=adapter,
            limit=content_limit,
            retention_levels=retention_levels,
            retries=args.content_retries,
            backoff_seconds=args.content_backoff_seconds,
            delay_min=args.content_delay_min,
            delay_max=args.content_delay_max,
            passes=args.content_passes,
            pass_cooldown_seconds=args.content_pass_cooldown_seconds,
            progress_every=args.progress_every,
            no_delay=args.no_delay,
        )
        content["retention_levels"] = list(retention_levels) if retention_levels else "all"

    summary = store.feed_summary()
    feed_rows = store.list_feed_items(limit=args.feed_limit)
    failed_rows = store.list_feed_items(limit=args.feed_limit, crawl_status="content_failed")
    write_feed_summary_file(summary_output, summary, args.summary_format)
    write_feed_items_file(feed_output, feed_rows, args.feed_format)
    write_feed_items_file(failures_output, failed_rows, args.failures_format)

    write_json(
        {
            "ok": True,
            "config": {
                "path": getattr(args, "config", None),
                "applied_keys": sorted(applied_config.keys()),
            },
            "db": str(store.db_path),
            "service": service,
            "refreshed": refreshed,
            "scored": scored,
            "content": content,
            "summary": summary,
            "outputs": {
                "feed": str(feed_output),
                "feed_format": args.feed_format,
                "feed_rows": len(feed_rows),
                "summary": str(summary_output),
                "summary_format": args.summary_format,
                "failures": str(failures_output),
                "failures_format": args.failures_format,
                "failure_rows": len(failed_rows),
            },
        }
    )
    return 0


def refresh_article_metadata_for_source(
    *,
    store: Store,
    adapter: WeChatDownloadAPIAdapter,
    source: dict,
    args: argparse.Namespace,
) -> dict:
    fakeid = source.get("wechat_fakeid")
    if not fakeid:
        return {
            "source_id": source["id"],
            "name": source["name"],
            "status": "skipped",
            "reason": "missing_wechat_fakeid",
        }

    if args.refresh_mode == "latest":
        payload = with_retries(
            lambda: adapter.list_articles(fakeid=fakeid, begin=args.begin, count=args.count),
            retries=args.retries,
            backoff_seconds=args.backoff_seconds,
        )
        if not payload["ok"]:
            return {"source_id": source["id"], "name": source["name"], "ok": False, "status": "failed", "adapter": payload}

        articles = normalize_article_items(payload["body"])
        saved = store.upsert_articles(source["id"], articles)
        return {
            "source_id": source["id"],
            "name": source["name"],
            "fakeid": fakeid,
            "ok": True,
            "status": "ok",
            "refresh_mode": "latest",
            "pages_fetched": 1,
            "count": saved["count"],
            "articles_seen": len(articles),
            "articles_upserted": saved["count"],
            "stop_reason": "latest_page",
        }

    boundary = store.latest_article_for_source(source["id"])
    if not boundary:
        payload = with_retries(
            lambda: adapter.list_articles(fakeid=fakeid, begin=args.begin, count=args.count),
            retries=args.retries,
            backoff_seconds=args.backoff_seconds,
        )
        if not payload["ok"]:
            return {"source_id": source["id"], "name": source["name"], "ok": False, "status": "failed", "adapter": payload}
        articles = normalize_article_items(payload["body"])
        saved = store.upsert_articles(source["id"], articles)
        return {
            "source_id": source["id"],
            "name": source["name"],
            "fakeid": fakeid,
            "ok": True,
            "status": "ok",
            "refresh_mode": "incremental",
            "pages_fetched": 1,
            "count": saved["count"],
            "articles_seen": len(articles),
            "articles_upserted": saved["count"],
            "stop_reason": "no_local_boundary",
        }

    boundary_url = str(boundary.get("url") or "")
    boundary_time = parse_article_publish_time(boundary.get("publish_time"))
    articles_seen = 0
    articles_upserted = 0
    pages_fetched = 0
    seen_urls: set[str] = set()
    total_count: int | None = None
    dynamic_page_cap = max(1, int(args.incremental_max_pages or 1))
    stop_reason = "page_cap"

    for page in range(dynamic_page_cap):
        begin = args.begin + page * args.count
        payload = with_retries(
            lambda: adapter.list_articles(fakeid=fakeid, begin=begin, count=args.count),
            retries=args.retries,
            backoff_seconds=args.backoff_seconds,
        )
        if not payload["ok"]:
            return {
                "source_id": source["id"],
                "name": source["name"],
                "ok": False,
                "status": "failed",
                "refresh_mode": "incremental",
                "pages_fetched": pages_fetched,
                "articles_seen": articles_seen,
                "articles_upserted": articles_upserted,
                "adapter": payload,
            }

        articles = normalize_article_items(payload["body"])
        if total_count is None:
            total_count = extract_article_total_count(payload.get("body"))
            if total_count is not None and args.count > 0:
                dynamic_page_cap = min(dynamic_page_cap, max(1, math.ceil(total_count / args.count) + 1))
        pages_fetched += 1
        if not articles:
            stop_reason = "empty_page"
            break

        unique_articles = []
        for article in articles:
            url = article.get("url")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            unique_articles.append(article)

        articles_seen += len(unique_articles)
        boundary_seen = False
        articles_to_save = []
        page_dates = []
        for article in unique_articles:
            url = str(article.get("url") or "")
            publish_time = parse_article_publish_time(article.get("publish_time"))
            if publish_time:
                page_dates.append(publish_time)
            if boundary_url and url == boundary_url:
                boundary_seen = True
            if boundary_time is None or publish_time is None or publish_time >= boundary_time:
                articles_to_save.append(article)

        saved = store.upsert_articles(source["id"], articles_to_save)
        articles_upserted += saved["count"]

        if boundary_seen:
            stop_reason = "boundary_url_reached"
            break
        if boundary_time and page_dates and max(page_dates) < boundary_time:
            stop_reason = "older_than_boundary"
            break
        if total_count is not None and begin + args.count >= total_count:
            stop_reason = "total_count_reached"
            break
        delay_between_items(
            page,
            dynamic_page_cap,
            args.incremental_page_delay_min,
            args.incremental_page_delay_max,
            disabled=args.no_delay,
        )

    return {
        "source_id": source["id"],
        "name": source["name"],
        "fakeid": fakeid,
        "ok": True,
        "status": "ok",
        "refresh_mode": "incremental",
        "pages_fetched": pages_fetched,
        "count": articles_upserted,
        "articles_seen": articles_seen,
        "articles_upserted": articles_upserted,
        "boundary_article_id": boundary.get("id"),
        "boundary_publish_time": boundary.get("publish_time"),
        "downloader_total_count": total_count,
        "stop_reason": stop_reason,
    }


def import_onboarding_input(store: Store, args: argparse.Namespace) -> dict | None:
    if args.names_file and args.video_file:
        raise ValueError("Use only one of --names-file or --video-file.")
    if args.names_file:
        with open(args.names_file, encoding="utf-8-sig") as handle:
            rows = [{"raw_name": line.strip()} for line in handle if line.strip() and not line.lstrip().startswith("#")]
        return {"kind": "names", **compact_import_result(store.import_source_rows(args.source_type, rows))}
    if args.video_file:
        ocr_result = extract_account_names_from_video(
            args.video_file,
            fps=args.fps,
            ocr=args.ocr,
            crop=args.crop,
            scale_width=args.scale_width,
            lang=args.lang,
            save_frames=args.save_frames,
            dedupe_threshold=args.dedupe_threshold,
            min_occurrences=args.min_occurrences,
        )
        rows = [
            {
                "raw_name": name,
                "source_video": str(Path(args.video_file).expanduser()),
                "ocr": args.ocr,
                "fps": args.fps,
                "crop": args.crop,
                "scale_width": args.scale_width,
            }
            for name in ocr_result["names"]
        ]
        if args.names_output:
            output_path = Path(args.names_output).expanduser()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text("\n".join(ocr_result["names"]) + ("\n" if ocr_result["names"] else ""), encoding="utf-8")
        if args.raw_output:
            write_ocr_json(args.raw_output, ocr_result)
        return {
            "kind": "video",
            "video": str(Path(args.video_file).expanduser()),
            "ocr_names_detected": ocr_result["count"],
            "ocr_frames_seen": ocr_result["frames_seen"],
            **compact_import_result(store.import_source_rows(args.source_type, rows)),
        }
    return None


def write_compact_onboarding_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = [
        "ocr_account",
        "matched_account",
        "candidate_account",
        "account_category",
        "system_decision",
        "requires_manual_confirmation",
        "match_type",
        "latest_probe_status",
        "evidence_summary",
        "manual_account_name",
        "manual_article_url",
        "manual_account_category",
        "manual_decision",
        "notes",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def summarize_onboarding_review(rows: list[dict[str, object]]) -> dict[str, int]:
    summary = {
        "rows": len(rows),
        "matched": 0,
        "candidate_only": 0,
        "requires_manual_confirmation": 0,
        "accepted_or_finance": 0,
        "ignored_or_rejected": 0,
    }
    for row in rows:
        if row.get("matched_account"):
            summary["matched"] += 1
        if row.get("candidate_account"):
            summary["candidate_only"] += 1
        if str(row.get("requires_manual_confirmation")).lower() == "true":
            summary["requires_manual_confirmation"] += 1
        decision = str(row.get("system_decision") or "")
        if decision in {"accepted_finance", "accept_source"}:
            summary["accepted_or_finance"] += 1
        if decision in {"ignored", "ignore_non_finance", "rejected", "reject_all"}:
            summary["ignored_or_rejected"] += 1
    return summary


def onboarding_next_steps(llm_results: str | None, llm_jobs_output: Path, review_output: Path) -> list[str]:
    if llm_results:
        return [f"Review the compact table at {review_output} and edit only rows marked for manual confirmation."]
    return [
        f"Ask an LLM/agent to answer the onboarding jobs at {llm_jobs_output}.",
        "Re-run with --llm-results <results.json> to apply source decisions and regenerate the final review table.",
        f"Review the compact table at {review_output}; rows with non-strict name matches remain manually confirmed.",
    ]


def resolve_pending_imports(
    store: Store,
    adapter: WeChatDownloadAPIAdapter,
    limit: int,
    source_type: str | None,
    delay_min: float,
    delay_max: float,
    no_delay: bool,
) -> dict:
    imports = [
        item
        for item in store.list_imports(limit=limit, status="pending", source_type=source_type)
        if item.get("raw_name")
    ]
    results = []

    for index, item in enumerate(imports):
        query = item["raw_name"]
        adapter_payload = adapter.search_sources(query)
        if not adapter_payload["ok"]:
            results.append({"import_id": item["id"], "query": query, "ok": False, "adapter": adapter_payload})
            delay_between_items(index, len(imports), delay_min, delay_max, disabled=no_delay)
            continue

        candidates = normalize_source_candidates(adapter_payload["body"], query)
        saved = store.save_candidates_for_import(item["id"], candidates, adapter_payload["body"])
        results.append({"import_id": item["id"], "query": query, "ok": True, "candidate_count": saved["count"]})
        delay_between_items(index, len(imports), delay_min, delay_max, disabled=no_delay)

    return {"imports_seen": len(imports), "results": results}


def auto_accept_exact_candidates(
    store: Store,
    source_type: str | None,
    limit: int,
    exact_score: float,
    taxonomy=None,
    finance_only: bool = False,
    min_confidence: float = 0.35,
    dry_run: bool = False,
) -> dict:
    imports = store.list_imports(limit=max(limit, 100), source_type=source_type)
    import_payloads = {item["id"]: item.get("raw_payload") or {} for item in imports}
    candidates = store.list_candidates(decision="pending", limit=limit)
    results = []
    skipped_reasons: dict[str, int] = {}

    for candidate in candidates:
        if source_type and candidate.get("import_source_type") != source_type:
            skipped_reasons["source_type"] = skipped_reasons.get("source_type", 0) + 1
            continue
        if candidate.get("candidate_name") != candidate.get("query"):
            skipped_reasons["not_exact"] = skipped_reasons.get("not_exact", 0) + 1
            continue
        if float(candidate.get("score") or 0) < exact_score:
            skipped_reasons["low_score"] = skipped_reasons.get("low_score", 0) + 1
            continue
        classification = None
        if finance_only:
            if taxonomy is None:
                raise ValueError("taxonomy is required when finance_only=True")
            classification = classify_source(
                {
                    "id": candidate["id"],
                    "name": candidate["candidate_name"],
                    "intro": candidate.get("intro"),
                    "source_type": candidate.get("import_source_type"),
                },
                taxonomy,
            )
            category = classification.get("category")
            confidence = float(classification.get("confidence") or 0)
            if (
                category in {"uncategorized", "low_signal"}
                or confidence < min_confidence
                or not has_finance_source_signal(candidate, classification)
            ):
                skipped_reasons["not_finance"] = skipped_reasons.get("not_finance", 0) + 1
                continue
        tier = infer_tier(candidate["candidate_name"], import_payloads.get(candidate["import_id"], {}))
        item = {
            "candidate_id": candidate["id"],
            "candidate_name": candidate["candidate_name"],
            "decision": "accept",
            "tier": tier,
        }
        if classification:
            item["classification"] = {
                "category": classification["category"],
                "confidence": classification["confidence"],
                "tags": classification["tags"],
            }
        if dry_run:
            results.append(item)
            continue
        accepted = store.accept_candidate(candidate["id"], tier=tier)
        results.append({**item, **accepted})

    return {
        "accepted": 0 if dry_run else len(results),
        "would_accept": len(results) if dry_run else None,
        "skipped": sum(skipped_reasons.values()),
        "skipped_reasons": skipped_reasons,
        "result_sample": results[:20],
        "results_truncated": max(0, len(results) - 20),
    }


def has_finance_source_signal(candidate: dict, classification: dict) -> bool:
    text = f"{candidate.get('candidate_name') or ''}\n{candidate.get('intro') or ''}"
    category = classification.get("category")
    if category in {"macro_policy", "strategy", "quant", "fixed_income", "industry_research", "company_research"}:
        return bool(re.search(r"证券|券商|金工|金融|基金|固收|债|宏观|策略|投研|量化|资产|配置|财富|期货|研究|行业|公司", text))
    return True


def infer_tier(name: str, payload: dict) -> str:
    tier_hint = clean_optional(payload.get("tier_hint") or payload.get("tier"))
    if tier_hint in {"core", "normal", "long_tail"}:
        return tier_hint
    if any(keyword in name for keyword in ("金工", "策略", "CfetsOnline", "财通", "长江")):
        return "core"
    return "normal"


def collect_latest_articles(
    store: Store,
    adapter: WeChatDownloadAPIAdapter,
    tier: str,
    max_sources: int,
    count: int,
    begin: int,
    no_delay: bool,
) -> dict:
    policy = tier_policy(tier)
    selected_tier = None if tier == "all" else tier
    sources = store.list_collectable_sources(tier=selected_tier, limit=max_sources)
    delay_min = 0 if no_delay else policy.delay_min_seconds
    delay_max = 0 if no_delay else policy.delay_max_seconds
    results = []
    total_articles = 0
    skipped = []

    for index, source in enumerate(sources):
        fakeid = source.get("wechat_fakeid")
        if not fakeid:
            skipped.append({"source_id": source["id"], "name": source["name"], "reason": "missing_wechat_fakeid"})
            continue
        payload = with_retries(
            lambda: adapter.list_articles(fakeid=fakeid, begin=begin, count=count),
            retries=policy.retries,
            backoff_seconds=policy.backoff_seconds,
        )
        if not payload["ok"]:
            results.append({"source_id": source["id"], "name": source["name"], "ok": False, "adapter": payload})
            delay_between_items(index, len(sources), delay_min, delay_max, disabled=no_delay)
            continue
        articles = normalize_article_items(payload["body"])
        saved = store.upsert_articles(source["id"], articles)
        total_articles += saved["count"]
        results.append({"source_id": source["id"], "name": source["name"], "ok": True, "count": saved["count"]})
        delay_between_items(index, len(sources), delay_min, delay_max, disabled=no_delay)

    return {"sources_seen": len(sources), "sources_skipped": len(skipped), "articles_saved": total_articles, "results": results}


def collect_article_contents(
    store: Store,
    adapter: WeChatDownloadAPIAdapter,
    tier: str,
    limit: int,
    no_delay: bool,
) -> dict:
    policy = tier_policy(tier)
    articles = store.list_articles_for_content_fetch(limit=limit)
    delay_min = 0 if no_delay else policy.delay_min_seconds
    delay_max = 0 if no_delay else policy.delay_max_seconds
    results = []

    for index, article in enumerate(articles):
        payload = with_retries(lambda: adapter.fetch_article(article["url"]), retries=policy.retries, backoff_seconds=policy.backoff_seconds)
        if not payload["ok"]:
            saved = store.upsert_article_content(article["id"], {}, fetch_error=adapter_error_message(payload))
            results.append({"article_id": article["id"], "title": article["title"], "ok": False, "saved": saved})
            delay_between_items(index, len(articles), delay_min, delay_max, disabled=no_delay)
            continue
        content = normalize_article_content(payload["body"])
        has_content = content.get("content_html") or content.get("content_text") or content.get("content_markdown")
        fetch_error = None if has_content else "empty_content"
        saved = store.upsert_article_content(article["id"], content, fetch_error=fetch_error)
        results.append({"article_id": article["id"], "title": article["title"], "ok": fetch_error is None, "saved": saved})
        delay_between_items(index, len(articles), delay_min, delay_max, disabled=no_delay)

    return {"articles_seen": len(articles), "results": results}


def classify_and_digest_articles(
    store: Store,
    taxonomy,
    limit: int,
    min_score: float,
) -> dict:
    rows = store.list_articles_for_llm(limit=limit, content_scope="all")
    saved = []
    skipped = []
    for row in rows:
        classification = classify_article(row, taxonomy)
        store.save_classification(classification)
        digest = generate_article_digest(row, classification, taxonomy)
        if digest["importance_score"] < min_score:
            skipped.append({"article_id": row["id"], "title": row["title"], "importance_score": digest["importance_score"]})
            continue
        saved_digest = store.save_digest(digest)
        saved.append(
            {
                "id": saved_digest["id"],
                "article_id": row["id"],
                "title": row["title"],
                "importance_score": saved_digest["importance_score"],
                "summary": saved_digest["summary"],
            }
        )
    return {"articles_seen": len(rows), "digests_saved": len(saved), "digests_skipped": len(skipped), "items": saved, "skipped": skipped}


def resolve_taxonomy_arg(value: str) -> Path:
    if value in {"default", "finance"}:
        return Path(__file__).resolve().parents[4] / "examples" / "taxonomy.finance.yaml"

    path = Path(value).expanduser()
    if path.exists():
        return path

    repo_path = Path(__file__).resolve().parents[4] / value
    if repo_path.exists():
        return repo_path

    return path


def format_digests_markdown(rows: list[dict]) -> str:
    lines = ["# WeChat MP Digest", ""]
    if not rows:
        lines.append("_No digests found._")
        return "\n".join(lines)

    for row in rows:
        source = f" ({row['source_name']})" if row.get("source_name") else ""
        lines.append(f"## {row['title']}{source}")
        lines.append("")
        lines.append(f"- Score: {row['importance_score']}")
        if row.get("publish_time"):
            lines.append(f"- Published: {row['publish_time']}")
        lines.append(f"- URL: {row['url']}")
        lines.append("")
        lines.append(row["summary"])
        points = row.get("key_points") or []
        if points:
            lines.append("")
            for point in points:
                lines.append(f"- {point}")
        if row.get("reason"):
            lines.append("")
            lines.append(f"Reason: {row['reason']}")
        lines.append("")

    return "\n".join(lines).rstrip()


def build_digest_context_rows(
    *,
    store: Store,
    limit: int,
    application_target: str | None,
    min_score: float | None,
    analysis_stage: str | None,
    include_html: bool,
    include_assets: bool,
    require_content: bool,
    max_content_chars: int,
) -> list[dict]:
    digests = store.list_digests(
        limit=limit,
        application_target=application_target,
        min_score=min_score,
        analysis_stage=analysis_stage,
    )
    rows = []
    for digest in digests:
        content = store.get_article_content(digest["article_id"]) or {}
        content_available = bool(
            content.get("content_text")
            or content.get("content_markdown")
            or (include_html and content.get("content_html"))
            or content.get("content_structure")
        )
        if require_content and not content_available:
            continue

        article = {
            "article_id": digest["article_id"],
            "source_id": digest.get("source_id") or content.get("source_id"),
            "source_name": digest.get("source_name") or content.get("source_name"),
            "title": digest.get("title") or content.get("title"),
            "url": digest.get("url") or content.get("url"),
            "publish_time": digest.get("publish_time") or content.get("publish_time"),
            "abstract": content.get("digest"),
            "cover_url": content.get("cover_url"),
            "crawl_status": content.get("crawl_status"),
            "retention_level": content.get("retention_level"),
            "archive_status": content.get("archive_status"),
            "retention_reason": content.get("retention_reason"),
            "content_available": content_available,
            "content_text": truncate_text(content.get("content_text"), max_content_chars),
            "content_markdown": truncate_text(content.get("content_markdown"), max_content_chars),
            "content_structure": content.get("content_structure") or [],
            "fetch_error": content.get("fetch_error"),
            "extracted_at": content.get("extracted_at"),
        }
        if include_html:
            article["content_html"] = truncate_text(content.get("content_html"), max_content_chars)

        rows.append(
            {
                "digest": {
                    "id": digest.get("id"),
                    "summary": digest.get("summary"),
                    "key_points": digest.get("key_points") or [],
                    "importance_score": digest.get("importance_score"),
                    "score_breakdown": digest.get("score_breakdown") or {},
                    "application_targets": digest.get("application_targets") or [],
                    "analysis_stage": digest.get("analysis_stage"),
                    "reason": digest.get("reason"),
                    "model": digest.get("model"),
                    "created_at": digest.get("created_at"),
                },
                "article": article,
                "assets": store.list_article_assets(article_id=digest["article_id"], limit=1000) if include_assets else [],
            }
        )
    return rows


def truncate_text(value: str | None, max_chars: int) -> str | None:
    if value is None or not max_chars or max_chars <= 0 or len(value) <= max_chars:
        return value
    return value[:max_chars] + "\n...[truncated]"


def format_digest_context_markdown(rows: list[dict]) -> str:
    lines = ["# WeChat MP Digest Context", ""]
    if not rows:
        lines.append("_No digest contexts found._")
        return "\n".join(lines)

    for row in rows:
        article = row["article"]
        digest = row["digest"]
        assets = row.get("assets") or []
        source = f" ({article['source_name']})" if article.get("source_name") else ""
        lines.append(f"## {article['title']}{source}")
        lines.append("")
        lines.append(f"- Score: {digest.get('importance_score')}")
        if article.get("publish_time"):
            lines.append(f"- Published: {article['publish_time']}")
        lines.append(f"- URL: {article['url']}")
        lines.append(f"- Content available: {article.get('content_available')}")
        if assets:
            cached = sum(1 for asset in assets if asset.get("download_status") == "cached")
            lines.append(f"- Images/assets: {len(assets)} total, {cached} cached")
        lines.append("")
        if digest.get("summary"):
            lines.append("### Stored Digest")
            lines.append("")
            lines.append(digest["summary"])
            lines.append("")
        text = article.get("content_markdown") or article.get("content_text")
        if text:
            lines.append("### Source Content")
            lines.append("")
            lines.append(text)
            lines.append("")
        if assets:
            lines.append("### Assets")
            lines.append("")
            for asset in assets:
                location = asset.get("local_path") or asset.get("url")
                lines.append(f"- {asset.get('content_ref') or asset.get('id')}: {location}")
            lines.append("")

    return "\n".join(lines).rstrip()


def format_agent_smoke_report(
    summary: dict[str, object],
    feed_rows: list[dict[str, object]],
    failed_rows: list[dict[str, object]],
    scored: dict[str, object],
    llm_jobs: dict[str, object],
    outputs: dict[str, Path],
) -> str:
    lines = [
        "# Agent Feed Smoke Report",
        "",
        "This offline smoke test verifies that an agent can run the feed layer, read feed outputs, inspect failures, and prepare article-level LLM jobs without WeChat login or private data.",
        "",
        "## Status",
        "",
        "- Result: OK",
        f"- Sources: {summary.get('sources', 0)}",
        f"- Articles: {summary.get('articles', 0)}",
        f"- Digests: {summary.get('digests', 0)}",
        f"- Assets: {summary.get('article_assets', 0)}",
        f"- Feed rows: {len(feed_rows)}",
        f"- Failure rows: {len(failed_rows)}",
        f"- Article LLM jobs: {llm_jobs.get('count', 0)}",
        f"- Rule digests saved this run: {scored.get('digests_saved', 0)}",
        "",
        "## Outputs",
        "",
    ]
    for label, path in outputs.items():
        lines.append(f"- {label}: `{path}`")

    lines.extend(["", "## Feed Preview", ""])
    if feed_rows:
        lines.append("| score | source | title | status | retention |")
        lines.append("|---:|---|---|---|---|")
        for row in feed_rows[:8]:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _markdown_cell(row.get("importance_score") or ""),
                        _markdown_cell(row.get("source_name") or ""),
                        _markdown_cell(row.get("title") or ""),
                        _markdown_cell(row.get("crawl_status") or ""),
                        _markdown_cell(row.get("retention_level") or ""),
                    ]
                )
                + " |"
            )
    else:
        lines.append("_No feed rows exported._")

    lines.extend(["", "## Failure Review", ""])
    if failed_rows:
        lines.append("| source | title | error |")
        lines.append("|---|---|---|")
        for row in failed_rows[:8]:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _markdown_cell(row.get("source_name") or ""),
                        _markdown_cell(row.get("title") or ""),
                        _markdown_cell(row.get("fetch_error") or ""),
                    ]
                )
                + " |"
            )
    else:
        lines.append("_No failed content rows._")

    lines.extend(
        [
            "",
            "## Agent Next Checks",
            "",
            "1. Confirm `feed-summary.json` matches the counts reported above.",
            "2. Read `feed-failures.csv` and explain whether each failure is expected, retryable, or needs user action.",
            "3. Read `article-llm-jobs.json` and verify that article jobs include source context, title, digest/content, taxonomy, and expected result schema.",
            "4. For the real deployment, run the same report shape against the user's private reviewed source registry instead of this demo database.",
            "",
            "## Finance Application V0",
            "",
            "The next application layer should turn feed rows into a research inbox: high-signal article selection, source-aware importance scoring, concise Chinese summaries, theme tags, and low-signal suppression.",
        ]
    )
    return "\n".join(lines).rstrip()


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def load_review_decisions(path_value: str) -> list[dict]:
    path = Path(path_value)
    if path.suffix.lower() == ".json":
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise ValueError("Review decision JSON expects a list of objects")
        return payload
    if path.suffix.lower() == ".xlsx":
        return load_review_decisions_xlsx(path)

    with open(path, newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def load_review_decisions_xlsx(path: Path) -> list[dict]:
    ns_main = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    ns_rel = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}
    with zipfile.ZipFile(path) as archive:
        shared_strings = read_xlsx_shared_strings(archive, ns_main)
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_targets = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels.findall("r:Relationship", ns_rel)}
        selected_sheet_path = None
        for sheet in workbook.findall("m:sheets/m:sheet", ns_main):
            rel_id = sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            target = rel_targets.get(rel_id or "")
            if not target:
                continue
            if sheet.attrib.get("name") == "账号审核":
                selected_sheet_path = "xl/" + target.lstrip("/")
                break
            if selected_sheet_path is None:
                selected_sheet_path = "xl/" + target.lstrip("/")
        if not selected_sheet_path:
            raise ValueError(f"No worksheet found in {path}")
        worksheet = ET.fromstring(archive.read(selected_sheet_path))

    matrix: list[list[str]] = []
    for row in worksheet.findall("m:sheetData/m:row", ns_main):
        values: dict[int, str] = {}
        for cell in row.findall("m:c", ns_main):
            ref = cell.attrib.get("r", "")
            col_index = xlsx_column_index(ref)
            values[col_index] = read_xlsx_cell(cell, shared_strings, ns_main)
        if values:
            width = max(values) + 1
            matrix.append([values.get(index, "") for index in range(width)])
    if not matrix:
        return []
    headers = [header.strip() for header in matrix[0]]
    rows = []
    for values in matrix[1:]:
        padded = values + [""] * max(0, len(headers) - len(values))
        row = {headers[index]: padded[index] for index in range(len(headers)) if headers[index]}
        if any(str(value).strip() for value in row.values()):
            rows.append(row)
    return rows


def read_xlsx_shared_strings(archive: zipfile.ZipFile, ns_main: dict[str, str]) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    values = []
    for item in root.findall("m:si", ns_main):
        texts = [node.text or "" for node in item.findall(".//m:t", ns_main)]
        values.append("".join(texts))
    return values


def read_xlsx_cell(cell: ET.Element, shared_strings: list[str], ns_main: dict[str, str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//m:t", ns_main))
    value = cell.find("m:v", ns_main)
    if value is None or value.text is None:
        return ""
    if cell_type == "s":
        index = int(value.text)
        return shared_strings[index] if 0 <= index < len(shared_strings) else ""
    return value.text


def xlsx_column_index(cell_ref: str) -> int:
    letters = re.match(r"([A-Z]+)", cell_ref or "")
    if not letters:
        return 0
    index = 0
    for char in letters.group(1):
        index = index * 26 + (ord(char) - ord("A") + 1)
    return index - 1


def first_value(row: dict, *keys: str) -> object:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def resolve_review_import(
    row: dict,
    import_by_id: dict[str, dict],
    imports_by_name: dict[str, list[dict]],
) -> dict | None:
    import_id = clean_optional(first_value(row, "import_id", "系统ID", "系统id"))
    if import_id and import_id in import_by_id:
        return import_by_id[import_id]
    ocr_name = clean_optional(first_value(row, "ocr_account", "OCR识别账号", "ocr_name"))
    if not ocr_name:
        return None
    matches = imports_by_name.get(ocr_name) or []
    if len(matches) == 1:
        return matches[0]
    return None


def normalize_manual_onboarding_decision(value: object) -> str | None:
    text = clean_optional(value)
    if not text:
        return None
    normalized = text.strip().lower().replace(" ", "_").replace("-", "_")
    mapping = {
        "确认候选": "confirm_candidate",
        "确认": "confirm_candidate",
        "accept": "confirm_candidate",
        "confirm": "confirm_candidate",
        "confirm_candidate": "confirm_candidate",
        "忽略": "ignore",
        "不纳入": "ignore",
        "ignore": "ignore",
        "无效": "invalid",
        "噪声": "invalid",
        "invalid": "invalid",
        "稍后": "needs_review",
        "待确认": "needs_review",
        "needs_review": "needs_review",
    }
    return mapping.get(normalized, normalized)


def status_for_manual_decision(decision: str | None, manual_name: str | None, manual_url: str | None) -> str:
    if decision == "confirm_candidate":
        return "resolved"
    if decision == "ignore":
        return "ignored"
    if decision == "invalid":
        return "rejected"
    if manual_name or manual_url:
        return "needs_review"
    return "needs_review"


def clean_optional(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def with_retries(call, retries: int, backoff_seconds: float) -> dict:
    attempts = 0
    while True:
        try:
            payload = call()
        except Exception as exc:  # noqa: BLE001 - convert adapter/network exceptions into retryable payloads.
            payload = {
                "ok": False,
                "operation": "adapter_call",
                "status": 0,
                "body": {
                    "error": str(exc),
                    "exception_type": type(exc).__name__,
                },
            }
        if payload.get("ok") or not retryable_status(payload) or attempts >= retries:
            return payload
        attempts += 1
        retry_after = article_probe_retry_after_seconds(payload)
        time.sleep(max(backoff_seconds * attempts, retry_after))


def adapter_error_message(payload: dict) -> str:
    body = payload.get("body")
    if isinstance(body, dict):
        error = body.get("error") or body.get("message")
        if error:
            return str(error)
    return f"adapter_status_{payload.get('status')}"


def delay_between_items(index: int, total: int, delay_min: float, delay_max: float, disabled: bool = False) -> None:
    if disabled or index >= total - 1:
        return
    if delay_max <= 0 and delay_min <= 0:
        return
    low = max(0.0, min(delay_min, delay_max))
    high = max(delay_min, delay_max)
    time.sleep(random.uniform(low, high))


def print_article_metadata_progress(
    index: int,
    sources: list[dict],
    results: list[dict],
    skipped: list[dict],
    total_articles: int,
    source: dict,
) -> None:
    print(
        "mpfeed: article metadata progress "
        f"{index + 1}/{len(sources)} source(s), "
        f"saved={total_articles}, "
        f"ok={sum(1 for item in results if item.get('ok'))}, "
        f"failed={sum(1 for item in results if item.get('ok') is False)}, "
        f"skipped={len(skipped)}, "
        f"last={source['name']!r}",
        file=sys.stderr,
        flush=True,
    )


def cmd_wd_health(args: argparse.Namespace) -> int:
    write_json(get_wechat_download_api(args).health())
    return 0


def cmd_wd_auth_status(args: argparse.Namespace) -> int:
    write_json(get_wechat_download_api(args).auth_status())
    return 0


def cmd_wd_login_url(args: argparse.Namespace) -> int:
    write_json({"login_url": login_url_for_base(args.base_url or adapter_base_url_from_env(required=True))})
    return 0


def cmd_wd_search(args: argparse.Namespace) -> int:
    write_json(get_wechat_download_api(args).search_sources(args.query))
    return 0


def cmd_wd_articles(args: argparse.Namespace) -> int:
    write_json(
        get_wechat_download_api(args).list_articles(
            fakeid=args.fakeid,
            begin=args.begin,
            count=args.count,
            keyword=args.keyword,
        )
    )
    return 0


def cmd_wd_article(args: argparse.Namespace) -> int:
    write_json(get_wechat_download_api(args).fetch_article(args.url))
    return 0


def adapter_base_url_from_env(required: bool = False) -> str | None:
    from .adapters.wechat_download_api import BASE_URL_ENV

    import os

    value = os.environ.get(BASE_URL_ENV)
    if required and not value:
        raise ValueError(f"Missing base URL. Set {BASE_URL_ENV} or pass --base-url.")
    return value


def login_url_for_base(base_url: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", "login.html")


def safe_adapter_call(call) -> dict:
    try:
        return call()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def auth_status_logged_in(payload: dict) -> bool:
    if not payload.get("ok"):
        return False
    body = payload.get("body")
    if not isinstance(body, dict):
        return False
    for key in ("logged_in", "login", "is_login", "isLogin", "authenticated", "ok"):
        value = body.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in {"true", "1", "yes", "ok", "logged_in", "login"}
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args._argv = list(argv) if argv is not None else sys.argv[1:]
    try:
        return args.func(args)
    except Exception as exc:
        print(f"mpfeed: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

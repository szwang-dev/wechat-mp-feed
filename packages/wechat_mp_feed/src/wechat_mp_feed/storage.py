"""SQLite storage for the mpfeed MVP."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any
from uuid import uuid4

from .paths import ensure_parent
from .retention import retention_decision_for_score
from .wechat_url import parse_article_url


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  platform TEXT NOT NULL DEFAULT 'wechat_mp',
  name TEXT NOT NULL,
  wechat_fakeid TEXT,
  biz TEXT,
  avatar_url TEXT,
  intro TEXT,
  status TEXT NOT NULL DEFAULT 'needs_review',
  tier TEXT NOT NULL DEFAULT 'normal',
  source_type TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_imports (
  id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL,
  raw_name TEXT,
  raw_url TEXT,
  raw_payload TEXT NOT NULL,
  source_type TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_candidates (
  id TEXT PRIMARY KEY,
  import_id TEXT NOT NULL,
  candidate_name TEXT NOT NULL,
  wechat_fakeid TEXT,
  biz TEXT,
  avatar_url TEXT,
  intro TEXT,
  score REAL NOT NULL DEFAULT 0,
  decision TEXT NOT NULL DEFAULT 'pending',
  raw_payload TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(import_id) REFERENCES source_imports(id)
);

CREATE TABLE IF NOT EXISTS articles (
  id TEXT PRIMARY KEY,
  source_id TEXT,
  title TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,
  digest TEXT,
  cover_url TEXT,
  publish_time TEXT,
  crawl_status TEXT NOT NULL DEFAULT 'metadata_only',
  retention_level TEXT NOT NULL DEFAULT 'metadata',
  archive_status TEXT NOT NULL DEFAULT 'not_requested',
  retention_reason TEXT,
  raw_payload TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(source_id) REFERENCES sources(id)
);

CREATE TABLE IF NOT EXISTS article_contents (
  article_id TEXT PRIMARY KEY,
  content_html TEXT,
  content_text TEXT,
  content_markdown TEXT,
  content_structure TEXT,
  fetch_error TEXT,
  extracted_at TEXT NOT NULL,
  FOREIGN KEY(article_id) REFERENCES articles(id)
);

CREATE TABLE IF NOT EXISTS article_assets (
  id TEXT PRIMARY KEY,
  article_id TEXT NOT NULL,
  asset_type TEXT NOT NULL,
  url TEXT NOT NULL,
  block_index INTEGER,
  content_ref TEXT,
  local_path TEXT,
  metadata TEXT,
  download_status TEXT NOT NULL DEFAULT 'url_only',
  created_at TEXT NOT NULL,
  FOREIGN KEY(article_id) REFERENCES articles(id)
);

CREATE TABLE IF NOT EXISTS classifications (
  id TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  taxonomy TEXT NOT NULL,
  category TEXT NOT NULL,
  tags TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0,
  method TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_classification_reviews (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  taxonomy TEXT NOT NULL,
  category TEXT NOT NULL,
  source_attribute TEXT,
  tags TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0,
  method TEXT NOT NULL,
  reason TEXT,
  taxonomy_suggestions TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  raw_payload TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(source_id) REFERENCES sources(id)
);

CREATE TABLE IF NOT EXISTS digests (
  id TEXT PRIMARY KEY,
  article_id TEXT NOT NULL,
  summary TEXT NOT NULL,
  key_points TEXT,
  importance_score REAL NOT NULL DEFAULT 0,
  score_breakdown TEXT,
  application_targets TEXT,
  reason TEXT,
  model TEXT,
  analysis_stage TEXT NOT NULL DEFAULT 'content',
  created_at TEXT NOT NULL,
  FOREIGN KEY(article_id) REFERENCES articles(id)
);

CREATE TABLE IF NOT EXISTS delivery_logs (
  id TEXT PRIMARY KEY,
  target TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  status TEXT NOT NULL,
  payload TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sources_biz ON sources(biz);
CREATE INDEX IF NOT EXISTS idx_articles_source_publish_time ON articles(source_id, publish_time);
CREATE INDEX IF NOT EXISTS idx_source_candidates_import_score ON source_candidates(import_id, score);
CREATE INDEX IF NOT EXISTS idx_classifications_entity ON classifications(entity_type, entity_id, taxonomy);
CREATE INDEX IF NOT EXISTS idx_source_classification_reviews_source ON source_classification_reviews(source_id, taxonomy, status);
CREATE INDEX IF NOT EXISTS idx_digests_article ON digests(article_id);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def source_id_for(name: str, fakeid: str | None = None, biz: str | None = None) -> str:
    stable_key = fakeid or biz or name
    digest = sha1(stable_key.encode("utf-8")).hexdigest()[:16]
    return f"mp_{digest}"


def article_id_for(url: str) -> str:
    digest = sha1(url.encode("utf-8")).hexdigest()[:20]
    return f"art_{digest}"


def asset_id_for(article_id: str, url: str) -> str:
    digest = sha1(f"{article_id}:{url}".encode("utf-8")).hexdigest()[:20]
    return f"asset_{digest}"


def effective_article_importance_score(conn: sqlite3.Connection, article_id: str) -> float:
    row = conn.execute(
        """
        SELECT importance_score
        FROM digests
        WHERE article_id = ?
        ORDER BY
          CASE analysis_stage
            WHEN 'content' THEN 3
            WHEN 'metadata' THEN 2
            WHEN 'rules' THEN 1
            ELSE 0
          END DESC,
          created_at DESC,
          id DESC
        LIMIT 1
        """,
        (article_id,),
    ).fetchone()
    if not row:
        return 0.0
    return float(row["importance_score"] or 0)


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        ensure_parent(self.db_path)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            ensure_schema(conn)

    def import_article_urls(self, urls: Iterable[str]) -> dict[str, Any]:
        batch_id = new_id("batch")
        created_at = now_iso()
        rows: list[dict[str, Any]] = []

        with self.connect() as conn:
            ensure_schema(conn)
            for raw_url in urls:
                parsed = parse_article_url(raw_url)
                payload = {
                    "url": parsed.raw_url,
                    "host": parsed.host,
                    "path": parsed.path,
                    "biz": parsed.biz,
                    "mid": parsed.mid,
                    "idx": parsed.idx,
                    "sn": parsed.sn,
                }
                item_id = new_id("imp")
                conn.execute(
                    """
                    INSERT INTO source_imports
                      (id, batch_id, raw_name, raw_url, raw_payload, source_type, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item_id,
                        batch_id,
                        None,
                        parsed.raw_url,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        "article_url",
                        "pending",
                        created_at,
                    ),
                )
                rows.append({"id": item_id, "batch_id": batch_id, **payload})

        return {"batch_id": batch_id, "count": len(rows), "items": rows}

    def import_source_rows(self, source_type: str, rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
        batch_id = new_id("batch")
        created_at = now_iso()
        imported: list[dict[str, Any]] = []

        with self.connect() as conn:
            ensure_schema(conn)
            for row in rows:
                raw_name = _clean_optional(row.get("raw_name") or row.get("name"))
                raw_url = _clean_optional(row.get("raw_url") or row.get("url"))
                if not raw_name and not raw_url:
                    continue

                item_id = new_id("imp")
                payload = dict(row)
                conn.execute(
                    """
                    INSERT INTO source_imports
                      (id, batch_id, raw_name, raw_url, raw_payload, source_type, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item_id,
                        batch_id,
                        raw_name,
                        raw_url,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        source_type,
                        "pending",
                        created_at,
                    ),
                )
                imported.append(
                    {
                        "id": item_id,
                        "batch_id": batch_id,
                        "raw_name": raw_name,
                        "raw_url": raw_url,
                        "source_type": source_type,
                    }
                )

        return {"batch_id": batch_id, "count": len(imported), "items": imported}

    def save_search_candidates(self, query: str, candidates: Iterable[dict[str, Any]], raw_response: Any) -> dict[str, Any]:
        created_at = now_iso()
        batch_id = new_id("batch")
        imported: list[dict[str, Any]] = []

        with self.connect() as conn:
            ensure_schema(conn)
            import_id = new_id("imp")
            conn.execute(
                """
                INSERT INTO source_imports
                  (id, batch_id, raw_name, raw_url, raw_payload, source_type, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    import_id,
                    batch_id,
                    query,
                    None,
                    json.dumps({"query": query, "response": raw_response}, ensure_ascii=False, sort_keys=True),
                    "resolve_search",
                    "pending",
                    created_at,
                ),
            )

            for candidate in candidates:
                candidate_id = new_id("cand")
                conn.execute(
                    """
                    INSERT INTO source_candidates
                      (id, import_id, candidate_name, wechat_fakeid, biz, avatar_url, intro, score, decision, raw_payload, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_id,
                        import_id,
                        candidate["candidate_name"],
                        candidate.get("wechat_fakeid"),
                        candidate.get("biz"),
                        candidate.get("avatar_url"),
                        candidate.get("intro"),
                        float(candidate.get("score", 0)),
                        "pending",
                        json.dumps(candidate.get("raw_payload") or {}, ensure_ascii=False, sort_keys=True),
                        created_at,
                    ),
                )
                imported.append({"id": candidate_id, "import_id": import_id, **candidate})

        return {"batch_id": batch_id, "import_id": import_id, "count": len(imported), "items": imported}

    def list_candidates(self, decision: str | None = "pending", limit: int = 100) -> list[dict[str, Any]]:
        where = ""
        params: list[Any] = []
        if decision:
            where = "WHERE c.decision = ?"
            params.append(decision)
        params.append(limit)

        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute(
                f"""
                SELECT
                  c.id, c.import_id, c.candidate_name, c.wechat_fakeid, c.biz, c.avatar_url,
                  c.intro, c.score, c.decision, c.raw_payload, c.created_at,
                  i.raw_name AS query, i.source_type AS import_source_type
                FROM source_candidates c
                JOIN source_imports i ON i.id = c.import_id
                {where}
                ORDER BY c.created_at DESC, c.score DESC, c.id DESC
                LIMIT ?
                """,
                params,
            )
            return [_decode_row(row, json_fields=("raw_payload",)) for row in result.fetchall()]

    def accept_candidate(self, candidate_id: str, tier: str = "normal") -> dict[str, Any]:
        updated_at = now_iso()
        with self.connect() as conn:
            ensure_schema(conn)
            row = conn.execute(
                """
                SELECT id, import_id, candidate_name, wechat_fakeid, biz, avatar_url, intro
                FROM source_candidates
                WHERE id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"Candidate not found: {candidate_id}")

            candidate = dict(row)
            source_id = existing_source_id(conn, candidate.get("wechat_fakeid"), candidate.get("biz")) or source_id_for(
                candidate["candidate_name"],
                candidate.get("wechat_fakeid"),
                candidate.get("biz"),
            )
            conn.execute(
                """
                INSERT INTO sources
                  (id, platform, name, wechat_fakeid, biz, avatar_url, intro, status, tier, source_type, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  name = excluded.name,
                  wechat_fakeid = COALESCE(excluded.wechat_fakeid, sources.wechat_fakeid),
                  biz = COALESCE(excluded.biz, sources.biz),
                  avatar_url = COALESCE(excluded.avatar_url, sources.avatar_url),
                  intro = COALESCE(excluded.intro, sources.intro),
                  status = 'active',
                  tier = excluded.tier,
                  updated_at = excluded.updated_at
                """,
                (
                    source_id,
                    "wechat_mp",
                    candidate["candidate_name"],
                    candidate.get("wechat_fakeid"),
                    candidate.get("biz"),
                    candidate.get("avatar_url"),
                    candidate.get("intro"),
                    "active",
                    tier,
                    "resolve_search",
                    updated_at,
                    updated_at,
                ),
            )
            conn.execute("UPDATE source_candidates SET decision = ? WHERE id = ?", ("manual_accept", candidate_id))
            conn.execute("UPDATE source_imports SET status = ? WHERE id = ?", ("resolved", candidate["import_id"]))

        return {"ok": True, "candidate_id": candidate_id, "source_id": source_id}

    def upsert_source(
        self,
        name: str,
        wechat_fakeid: str | None = None,
        biz: str | None = None,
        avatar_url: str | None = None,
        intro: str | None = None,
        status: str = "active",
        tier: str = "normal",
        source_type: str = "manual",
        identity_key: str | None = None,
        match_existing_by_external_id: bool = True,
    ) -> dict[str, Any]:
        allowed_status = {"active", "inactive", "archived", "needs_review"}
        allowed_tier = {"core", "normal", "long_tail"}
        if status not in allowed_status:
            raise ValueError(f"Invalid source status: {status}")
        if tier not in allowed_tier:
            raise ValueError(f"Invalid source tier: {tier}")

        updated_at = now_iso()
        with self.connect() as conn:
            ensure_schema(conn)
            if identity_key:
                source_id = source_id_for(identity_key)
            elif match_existing_by_external_id:
                source_id = existing_source_id(conn, wechat_fakeid, biz) or source_id_for(name, wechat_fakeid, biz)
            else:
                source_id = source_id_for(name)

            conn.execute(
                """
                INSERT INTO sources
                  (id, platform, name, wechat_fakeid, biz, avatar_url, intro, status, tier, source_type, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  name = excluded.name,
                  wechat_fakeid = COALESCE(excluded.wechat_fakeid, sources.wechat_fakeid),
                  biz = COALESCE(excluded.biz, sources.biz),
                  avatar_url = COALESCE(excluded.avatar_url, sources.avatar_url),
                  intro = COALESCE(excluded.intro, sources.intro),
                  status = excluded.status,
                  tier = excluded.tier,
                  source_type = excluded.source_type,
                  updated_at = excluded.updated_at
                """,
                (
                    source_id,
                    "wechat_mp",
                    name,
                    wechat_fakeid,
                    biz,
                    avatar_url,
                    intro,
                    status,
                    tier,
                    source_type,
                    updated_at,
                    updated_at,
                ),
            )
        return {"ok": True, "source_id": source_id, "name": name, "status": status, "tier": tier}

    def reject_candidate(self, candidate_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute(
                "UPDATE source_candidates SET decision = ? WHERE id = ?",
                ("reject", candidate_id),
            )
            if result.rowcount == 0:
                raise ValueError(f"Candidate not found: {candidate_id}")
        return {"ok": True, "candidate_id": candidate_id, "decision": "reject"}

    def reject_candidates_for_import(self, import_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute(
                "UPDATE source_candidates SET decision = ? WHERE import_id = ? AND decision = ?",
                ("reject", import_id, "pending"),
            )
        return {"ok": True, "import_id": import_id, "count": result.rowcount}

    def reject_all_candidates_for_import(self, import_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute(
                "UPDATE source_candidates SET decision = ? WHERE import_id = ?",
                ("reject", import_id),
            )
        return {"ok": True, "import_id": import_id, "count": result.rowcount}

    def archive_sources_for_import(self, import_id: str) -> dict[str, Any]:
        updated_at = now_iso()
        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute(
                """
                UPDATE sources
                SET status = ?, updated_at = ?
                WHERE id IN (
                  SELECT s.id
                  FROM sources s
                  JOIN source_candidates c ON
                    (c.wechat_fakeid IS NOT NULL AND s.wechat_fakeid = c.wechat_fakeid)
                    OR (c.biz IS NOT NULL AND s.biz = c.biz)
                    OR s.name = c.candidate_name
                  WHERE c.import_id = ?
                )
                """,
                ("archived", updated_at, import_id),
            )
        return {"ok": True, "import_id": import_id, "count": result.rowcount}

    def update_import_status(self, import_id: str, status: str) -> dict[str, Any]:
        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute("UPDATE source_imports SET status = ? WHERE id = ?", (status, import_id))
            if result.rowcount == 0:
                raise ValueError(f"Import not found: {import_id}")
        return {"ok": True, "import_id": import_id, "status": status}

    def record_import_review(self, import_id: str, review: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute(
                """
                UPDATE source_imports
                SET raw_payload = json_set(raw_payload, '$.llm_onboarding_review', json(?))
                WHERE id = ?
                """,
                (json.dumps(review, ensure_ascii=False, sort_keys=True), import_id),
            )
            if result.rowcount == 0:
                raise ValueError(f"Import not found: {import_id}")
        return {"ok": True, "import_id": import_id}

    def record_manual_import_review(self, import_id: str, review: dict[str, Any], status: str | None = None) -> dict[str, Any]:
        assignments = ["raw_payload = json_set(raw_payload, '$.manual_onboarding_review', json(?))"]
        params: list[Any] = [json.dumps(review, ensure_ascii=False, sort_keys=True)]
        if status:
            assignments.append("status = ?")
            params.append(status)
        params.append(import_id)

        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute(
                f"""
                UPDATE source_imports
                SET {', '.join(assignments)}
                WHERE id = ?
                """,
                params,
            )
            if result.rowcount == 0:
                raise ValueError(f"Import not found: {import_id}")
        return {"ok": True, "import_id": import_id, "status": status}

    def save_candidate_article_probe(
        self,
        candidate_id: str,
        articles: Iterable[dict[str, Any]],
        raw_response: Any,
        fetch_error: str | None = None,
    ) -> dict[str, Any]:
        articles = list(articles)
        with self.connect() as conn:
            ensure_schema(conn)
            row = conn.execute("SELECT raw_payload FROM source_candidates WHERE id = ?", (candidate_id,)).fetchone()
            if not row:
                raise ValueError(f"Candidate not found: {candidate_id}")
            payload = json.loads(row["raw_payload"] or "{}")
            payload["article_probe"] = {
                "ok": fetch_error is None,
                "fetch_error": fetch_error,
                "articles": articles,
                "raw_response": raw_response,
                "updated_at": now_iso(),
            }
            conn.execute(
                "UPDATE source_candidates SET raw_payload = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False, sort_keys=True), candidate_id),
            )

        return {"ok": fetch_error is None, "candidate_id": candidate_id, "count": len(articles), "fetch_error": fetch_error}

    def list_sources(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute(
                """
                SELECT id, platform, name, wechat_fakeid, biz, avatar_url, intro, status, tier, source_type, created_at, updated_at
                FROM sources
                ORDER BY tier, name
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in result.fetchall()]

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            ensure_schema(conn)
            row = conn.execute(
                """
                SELECT id, platform, name, wechat_fakeid, biz, avatar_url, intro, status, tier, source_type, created_at, updated_at
                FROM sources
                WHERE id = ?
                """,
                (source_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_collectable_sources(self, tier: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        where = "WHERE status = 'active'"
        params: list[Any] = []
        if tier:
            where += " AND tier = ?"
            params.append(tier)
        params.append(limit)

        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute(
                f"""
                SELECT id, platform, name, wechat_fakeid, biz, avatar_url, intro, status, tier, source_type, created_at, updated_at
                FROM sources
                {where}
                ORDER BY tier, name
                LIMIT ?
                """,
                params,
            )
            return [dict(row) for row in result.fetchall()]

    def update_source(self, source_id: str, status: str | None = None, tier: str | None = None) -> dict[str, Any]:
        if not status and not tier:
            return {"ok": True, "source_id": source_id, "status": status, "tier": tier, "changed": False}

        allowed_status = {"active", "inactive", "archived", "needs_review"}
        allowed_tier = {"core", "normal", "long_tail"}
        if status and status not in allowed_status:
            raise ValueError(f"Invalid source status: {status}")
        if tier and tier not in allowed_tier:
            raise ValueError(f"Invalid source tier: {tier}")

        updated_at = now_iso()
        assignments = []
        params: list[Any] = []
        if status:
            assignments.append("status = ?")
            params.append(status)
        if tier:
            assignments.append("tier = ?")
            params.append(tier)
        assignments.append("updated_at = ?")
        params.append(updated_at)
        params.append(source_id)

        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute(
                f"UPDATE sources SET {', '.join(assignments)} WHERE id = ?",
                params,
            )
            if result.rowcount == 0:
                raise ValueError(f"Source not found: {source_id}")

        return {"ok": True, "source_id": source_id, "status": status, "tier": tier, "changed": True}

    def upsert_articles(self, source_id: str, articles: Iterable[dict[str, Any]]) -> dict[str, Any]:
        now = now_iso()
        processed: list[dict[str, Any]] = []
        with self.connect() as conn:
            ensure_schema(conn)
            for article in articles:
                article_id = article_id_for(article["url"])
                conn.execute(
                    """
                    INSERT INTO articles
                      (id, source_id, title, url, digest, cover_url, publish_time, crawl_status, raw_payload, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                      source_id = COALESCE(articles.source_id, excluded.source_id),
                      title = excluded.title,
                      digest = COALESCE(excluded.digest, articles.digest),
                      cover_url = COALESCE(excluded.cover_url, articles.cover_url),
                      publish_time = COALESCE(excluded.publish_time, articles.publish_time),
                      raw_payload = excluded.raw_payload,
                      updated_at = excluded.updated_at
                    """,
                    (
                        article_id,
                        source_id,
                        article["title"],
                        article["url"],
                        article.get("digest"),
                        article.get("cover_url"),
                        article.get("publish_time"),
                        "metadata_only",
                        json.dumps(article.get("raw_payload") or {}, ensure_ascii=False, sort_keys=True),
                        now,
                        now,
                    ),
                )
                processed.append({"id": article_id, "source_id": source_id, **article})

        return {"count": len(processed), "items": processed}

    def list_articles(self, limit: int = 100, source_id: str | None = None) -> list[dict[str, Any]]:
        where = ""
        params: list[Any] = []
        if source_id:
            where = "WHERE source_id = ?"
            params.append(source_id)
        params.append(limit)

        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute(
                f"""
                SELECT id, source_id, title, url, digest, cover_url, publish_time, crawl_status,
                       retention_level, archive_status, retention_reason,
                       raw_payload, created_at, updated_at
                FROM articles
                {where}
                ORDER BY COALESCE(publish_time, created_at) DESC, id DESC
                LIMIT ?
                """,
                params,
            )
            return [_decode_row(row, json_fields=("raw_payload",)) for row in result.fetchall()]

    def latest_article_for_source(self, source_id: str) -> dict[str, Any] | None:
        rows = self.list_articles(limit=1, source_id=source_id)
        return rows[0] if rows else None

    def list_feed_items(
        self,
        limit: int = 100,
        source_id: str | None = None,
        tier: str | None = None,
        status: str | None = None,
        crawl_status: str | None = None,
    ) -> list[dict[str, Any]]:
        where_parts = []
        params: list[Any] = []
        if source_id:
            where_parts.append("a.source_id = ?")
            params.append(source_id)
        if tier:
            where_parts.append("s.tier = ?")
            params.append(tier)
        if status:
            where_parts.append("s.status = ?")
            params.append(status)
        if crawl_status:
            where_parts.append("a.crawl_status = ?")
            params.append(crawl_status)
        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        params.append(limit)

        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute(
                f"""
                SELECT
                  a.id AS article_id, a.title, a.url, a.digest, a.cover_url, a.publish_time,
                  a.crawl_status, a.retention_level, a.archive_status, a.retention_reason,
                  a.created_at, a.updated_at,
                  s.id AS source_id, s.name AS source_name, s.status AS source_status,
                  s.tier AS source_tier, s.wechat_fakeid, s.biz,
                  (
                    SELECT sc.category
                    FROM classifications sc
                    WHERE sc.entity_type = 'source' AND sc.entity_id = s.id
                    ORDER BY sc.created_at DESC
                    LIMIT 1
                  ) AS source_category,
                  (
                    SELECT sc.tags
                    FROM classifications sc
                    WHERE sc.entity_type = 'source' AND sc.entity_id = s.id
                    ORDER BY sc.created_at DESC
                    LIMIT 1
                  ) AS source_tags,
                  CASE WHEN c.article_id IS NULL THEN 0 ELSE 1 END AS has_content_record,
                  length(coalesce(c.content_text, c.content_markdown, c.content_html, '')) AS content_length,
                  c.fetch_error,
                  (
                    SELECT count(*)
                    FROM article_assets aa
                    WHERE aa.article_id = a.id
                  ) AS asset_count,
                  d.summary AS digest_summary,
                  d.importance_score,
                  d.analysis_stage AS digest_analysis_stage,
                  d.reason AS digest_reason,
                  d.model AS digest_model
                FROM articles a
                LEFT JOIN sources s ON s.id = a.source_id
                LEFT JOIN article_contents c ON c.article_id = a.id
                LEFT JOIN digests d ON d.id = (
                  SELECT d2.id
                  FROM digests d2
                  WHERE d2.article_id = a.id
                  ORDER BY
                    CASE d2.analysis_stage
                      WHEN 'content' THEN 3
                      WHEN 'metadata' THEN 2
                      WHEN 'rules' THEN 1
                      ELSE 0
                    END DESC,
                    d2.created_at DESC,
                    d2.id DESC
                  LIMIT 1
                )
                {where}
                ORDER BY COALESCE(a.publish_time, a.created_at) DESC, a.id DESC
                LIMIT ?
                """,
                params,
            )
            return [_decode_row(row, json_fields=("source_tags",)) for row in result.fetchall()]

    def feed_summary(self) -> dict[str, Any]:
        with self.connect() as conn:
            ensure_schema(conn)
            return {
                "sources": conn.execute("SELECT count(*) FROM sources").fetchone()[0],
                "articles": conn.execute("SELECT count(*) FROM articles").fetchone()[0],
                "digests": conn.execute("SELECT count(*) FROM digests").fetchone()[0],
                "article_assets": conn.execute("SELECT count(*) FROM article_assets").fetchone()[0],
                "sources_with_articles": conn.execute("SELECT count(DISTINCT source_id) FROM articles WHERE source_id IS NOT NULL").fetchone()[0],
                "active_sources_without_articles": conn.execute(
                    """
                    SELECT count(*)
                    FROM sources s
                    WHERE s.status = 'active'
                      AND NOT EXISTS (SELECT 1 FROM articles a WHERE a.source_id = s.id)
                    """
                ).fetchone()[0],
                "sources_by_tier_status": [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT tier, status, count(*) AS count
                        FROM sources
                        GROUP BY tier, status
                        ORDER BY tier, status
                        """
                    ).fetchall()
                ],
                "articles_by_crawl_status": [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT crawl_status, count(*) AS count
                        FROM articles
                        GROUP BY crawl_status
                        ORDER BY crawl_status
                        """
                    ).fetchall()
                ],
            }

    def list_articles_for_content_fetch(
        self,
        limit: int = 20,
        retention_levels: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        where_parts = ["crawl_status IN ('metadata_only', 'content_failed')"]
        params: list[Any] = []
        if retention_levels:
            placeholders = ", ".join("?" for _ in retention_levels)
            where_parts.append(f"retention_level IN ({placeholders})")
            params.extend(retention_levels)
        params.append(limit)

        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute(
                f"""
                SELECT a.id, a.source_id, a.title, a.url, a.digest, a.cover_url, a.publish_time, a.crawl_status,
                       a.retention_level, a.archive_status, a.retention_reason,
                       a.raw_payload, a.created_at, a.updated_at,
                       COALESCE(d.importance_score, 0) AS importance_score,
                       d.analysis_stage AS importance_stage
                FROM articles a
                LEFT JOIN (
                    SELECT d1.article_id, d1.importance_score, d1.analysis_stage
                    FROM digests d1
                    WHERE d1.id = (
                        SELECT d2.id
                        FROM digests d2
                        WHERE d2.article_id = d1.article_id
                        ORDER BY
                          CASE d2.analysis_stage
                            WHEN 'content' THEN 3
                            WHEN 'metadata' THEN 2
                            WHEN 'rules' THEN 1
                            ELSE 0
                          END DESC,
                          d2.created_at DESC,
                          d2.id DESC
                        LIMIT 1
                    )
                ) d ON d.article_id = a.id
                LEFT JOIN sources s ON s.id = a.source_id
                WHERE {' AND '.join(where_parts)}
                ORDER BY
                    COALESCE(d.importance_score, 0) DESC,
                    CASE a.retention_level
                        WHEN 'full_archive' THEN 2
                        WHEN 'content' THEN 1
                        ELSE 0
                    END DESC,
                    CASE s.tier
                        WHEN 'core' THEN 3
                        WHEN 'normal' THEN 2
                        WHEN 'long_tail' THEN 1
                        ELSE 0
                    END DESC,
                    COALESCE(a.publish_time, a.created_at) DESC,
                    a.id DESC
                LIMIT ?
                """,
                params,
            )
            return [_decode_row(row, json_fields=("raw_payload",)) for row in result.fetchall()]

    def upsert_article_content(self, article_id: str, content: dict[str, Any], fetch_error: str | None = None) -> dict[str, Any]:
        extracted_at = now_iso()
        with self.connect() as conn:
            ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO article_contents
                  (article_id, content_html, content_text, content_markdown, content_structure, fetch_error, extracted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(article_id) DO UPDATE SET
                  content_html = excluded.content_html,
                  content_text = excluded.content_text,
                  content_markdown = excluded.content_markdown,
                  content_structure = excluded.content_structure,
                  fetch_error = excluded.fetch_error,
                  extracted_at = excluded.extracted_at
                """,
                (
                    article_id,
                    content.get("content_html"),
                    content.get("content_text"),
                    content.get("content_markdown"),
                    json.dumps(content.get("content_structure") or [], ensure_ascii=False, sort_keys=True),
                    fetch_error,
                    extracted_at,
                ),
            )

            structure_asset_indexes = asset_block_indexes(content.get("content_structure") or [])
            for asset_index, asset in enumerate(content.get("assets", [])):
                asset_id = asset_id_for(article_id, asset["url"])
                block_index = asset.get("block_index")
                if block_index is None:
                    block_index = structure_asset_indexes.get(asset["url"])
                conn.execute(
                    """
                    INSERT INTO article_assets
                      (id, article_id, asset_type, url, block_index, content_ref, local_path, metadata, download_status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      asset_type = excluded.asset_type,
                      block_index = excluded.block_index,
                      content_ref = excluded.content_ref,
                      local_path = excluded.local_path,
                      metadata = excluded.metadata,
                      download_status = excluded.download_status
                    """,
                    (
                        asset_id,
                        article_id,
                        asset.get("asset_type", "image"),
                        asset["url"],
                        block_index,
                        asset.get("content_ref") or (f"block:{block_index}" if block_index is not None else f"asset:{asset_index}"),
                        asset.get("local_path"),
                        json.dumps(asset.get("metadata") or {}, ensure_ascii=False, sort_keys=True),
                        asset.get("download_status", "url_only"),
                        extracted_at,
                    ),
                )

            status = "content_failed" if fetch_error else "content_ok"
            conn.execute(
                "UPDATE articles SET crawl_status = ?, updated_at = ? WHERE id = ?",
                (status, extracted_at, article_id),
            )

        return {"ok": True, "article_id": article_id, "crawl_status": "content_failed" if fetch_error else "content_ok"}

    def list_article_contents(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute(
                """
                SELECT
                  a.id AS article_id, a.title, a.url, a.source_id, a.crawl_status,
                  c.content_html, c.content_text, c.content_markdown, c.content_structure, c.fetch_error, c.extracted_at
                FROM articles a
                LEFT JOIN article_contents c ON c.article_id = a.id
                ORDER BY COALESCE(c.extracted_at, a.updated_at) DESC, a.id DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [_decode_row(row, json_fields=("content_structure",)) for row in result.fetchall()]

    def get_article_content(self, article_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            ensure_schema(conn)
            row = conn.execute(
                """
                SELECT
                  a.id AS article_id, a.title, a.url, a.source_id, s.name AS source_name,
                  a.digest, a.cover_url, a.publish_time, a.crawl_status,
                  a.retention_level, a.archive_status, a.retention_reason,
                  c.content_html, c.content_text, c.content_markdown, c.content_structure, c.fetch_error, c.extracted_at
                FROM articles a
                LEFT JOIN sources s ON s.id = a.source_id
                LEFT JOIN article_contents c ON c.article_id = a.id
                WHERE a.id = ?
                """,
                (article_id,),
            ).fetchone()
            if row is None:
                return None
            return _decode_row(row, json_fields=("content_structure",))

    def list_article_assets(self, article_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        where = ""
        params: list[Any] = []
        if article_id:
            where = "WHERE article_id = ?"
            params.append(article_id)
        params.append(limit)

        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute(
                f"""
                SELECT id, article_id, asset_type, url, block_index, content_ref, local_path, metadata, download_status, created_at
                FROM article_assets
                {where}
                ORDER BY COALESCE(block_index, 999999), created_at, id
                LIMIT ?
                """,
                params,
            )
            return [_decode_row(row, json_fields=("metadata",)) for row in result.fetchall()]

    def list_assets_for_archive(self, limit: int = 100, cached: bool = False) -> list[dict[str, Any]]:
        where_parts = [
            "a.retention_level = 'full_archive'",
            "aa.asset_type = 'image'",
            "aa.url IS NOT NULL",
        ]
        if not cached:
            where_parts.append("COALESCE(aa.download_status, 'url_only') NOT IN ('cached', 'skipped')")
        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute(
                f"""
                SELECT
                  aa.id, aa.article_id, aa.asset_type, aa.url, aa.block_index, aa.content_ref,
                  aa.local_path, aa.metadata, aa.download_status, aa.created_at,
                  a.title AS article_title, a.retention_level, a.archive_status
                FROM article_assets aa
                JOIN articles a ON a.id = aa.article_id
                WHERE {' AND '.join(where_parts)}
                ORDER BY
                  COALESCE(a.publish_time, a.created_at) DESC,
                  aa.article_id,
                  COALESCE(aa.block_index, 999999),
                  aa.id
                LIMIT ?
                """,
                (limit,),
            )
            return [_decode_row(row, json_fields=("metadata",)) for row in result.fetchall()]

    def update_article_asset_cache(
        self,
        asset_id: str,
        local_path: str | None,
        download_status: str,
        metadata_patch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            ensure_schema(conn)
            metadata_text = None
            if metadata_patch:
                row = conn.execute("SELECT metadata FROM article_assets WHERE id = ?", (asset_id,)).fetchone()
                metadata: dict[str, Any] = {}
                if row and row["metadata"]:
                    try:
                        parsed = json.loads(row["metadata"])
                        if isinstance(parsed, dict):
                            metadata = parsed
                    except json.JSONDecodeError:
                        metadata = {}
                metadata.update(metadata_patch)
                metadata_text = json.dumps(metadata, ensure_ascii=False, sort_keys=True)

            if metadata_text is not None:
                conn.execute(
                    """
                    UPDATE article_assets
                    SET local_path = ?, download_status = ?, metadata = ?
                    WHERE id = ?
                    """,
                    (local_path, download_status, metadata_text, asset_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE article_assets
                    SET local_path = ?, download_status = ?
                    WHERE id = ?
                    """,
                    (local_path, download_status, asset_id),
                )
        return {"ok": True, "asset_id": asset_id, "local_path": local_path, "download_status": download_status}

    def refresh_article_archive_status(self, article_id: str) -> dict[str, Any]:
        updated_at = now_iso()
        with self.connect() as conn:
            ensure_schema(conn)
            row = conn.execute(
                """
                SELECT
                  count(*) AS total_assets,
                  sum(CASE WHEN download_status = 'cached' THEN 1 ELSE 0 END) AS cached_assets,
                  sum(CASE WHEN download_status = 'skipped' THEN 1 ELSE 0 END) AS skipped_assets,
                  sum(CASE WHEN download_status = 'failed' THEN 1 ELSE 0 END) AS failed_assets
                FROM article_assets
                WHERE article_id = ? AND asset_type = 'image'
                """,
                (article_id,),
            ).fetchone()
            total = int(row["total_assets"] or 0)
            cached = int(row["cached_assets"] or 0)
            skipped = int(row["skipped_assets"] or 0)
            failed = int(row["failed_assets"] or 0)
            if total and cached + skipped == total:
                archive_status = "cached"
            elif failed:
                archive_status = "failed"
            else:
                archive_status = "pending"
            conn.execute(
                "UPDATE articles SET archive_status = ?, updated_at = ? WHERE id = ? AND retention_level = 'full_archive'",
                (archive_status, updated_at, article_id),
            )
        return {
            "ok": True,
            "article_id": article_id,
            "archive_status": archive_status,
            "total_assets": total,
            "cached_assets": cached,
            "failed_assets": failed,
        }

    def list_articles_for_llm(
        self,
        limit: int = 100,
        source_id: str | None = None,
        content_scope: str = "all",
        updated_after: str | None = None,
        published_after: str | None = None,
        published_before: str | None = None,
        retention_levels: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        if content_scope not in {"all", "content", "metadata"}:
            raise ValueError("content_scope must be one of: all, content, metadata")
        where_parts = []
        params: list[Any] = []
        if source_id:
            where_parts.append("a.source_id = ?")
            params.append(source_id)
        if updated_after:
            where_parts.append("a.updated_at >= ?")
            params.append(updated_after)
        if published_after:
            where_parts.append("a.publish_time >= ?")
            params.append(published_after)
        if published_before:
            where_parts.append("a.publish_time <= ?")
            params.append(published_before)
        if retention_levels:
            placeholders = ", ".join("?" for _ in retention_levels)
            where_parts.append(f"a.retention_level IN ({placeholders})")
            params.extend(retention_levels)
        if content_scope == "content":
            where_parts.append(
                "(c.content_text IS NOT NULL OR c.content_markdown IS NOT NULL OR c.content_html IS NOT NULL)"
            )
        elif content_scope == "metadata":
            where_parts.append(
                "(c.content_text IS NULL AND c.content_markdown IS NULL AND c.content_html IS NULL)"
            )
        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        params.append(limit)

        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute(
                f"""
                SELECT
                  a.id, a.source_id, s.name AS source_name, s.status AS source_status, s.tier AS source_tier,
                  (
                    SELECT sc.category
                    FROM classifications sc
                    WHERE sc.entity_type = 'source' AND sc.entity_id = s.id
                    ORDER BY sc.created_at DESC
                    LIMIT 1
                  ) AS source_category,
                  (
                    SELECT sc.tags
                    FROM classifications sc
                    WHERE sc.entity_type = 'source' AND sc.entity_id = s.id
                    ORDER BY sc.created_at DESC
                    LIMIT 1
                  ) AS source_tags,
                  a.title, a.url, a.digest,
                  a.cover_url, a.publish_time, a.crawl_status,
                  a.retention_level, a.archive_status, a.retention_reason,
                  a.raw_payload, a.created_at, a.updated_at,
                  c.content_html, c.content_text, c.content_markdown, c.content_structure, c.fetch_error, c.extracted_at
                FROM articles a
                LEFT JOIN sources s ON s.id = a.source_id
                LEFT JOIN article_contents c ON c.article_id = a.id
                {where}
                ORDER BY COALESCE(a.publish_time, a.created_at) DESC, a.id DESC
                LIMIT ?
                """,
                params,
            )
            return [_decode_row(row, json_fields=("raw_payload", "content_structure")) for row in result.fetchall()]

    def list_articles_with_content(
        self,
        limit: int = 100,
        source_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.list_articles_for_llm(limit=limit, source_id=source_id, content_scope="content")

    def save_classification(self, classification: dict[str, Any]) -> dict[str, Any]:
        created_at = now_iso()
        classification_id = new_id("cls")
        with self.connect() as conn:
            ensure_schema(conn)
            conn.execute(
                """
                DELETE FROM classifications
                WHERE entity_type = ? AND entity_id = ? AND taxonomy = ? AND method = ?
                """,
                (
                    classification["entity_type"],
                    classification["entity_id"],
                    classification["taxonomy"],
                    classification["method"],
                ),
            )
            conn.execute(
                """
                INSERT INTO classifications
                  (id, entity_type, entity_id, taxonomy, category, tags, confidence, method, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    classification_id,
                    classification["entity_type"],
                    classification["entity_id"],
                    classification["taxonomy"],
                    classification["category"],
                    json.dumps(classification.get("tags") or [], ensure_ascii=False, sort_keys=True),
                    float(classification.get("confidence") or 0),
                    classification["method"],
                    created_at,
                ),
            )

        return {"id": classification_id, "created_at": created_at, **classification}

    def replace_source_classification(self, classification: dict[str, Any]) -> dict[str, Any]:
        if classification.get("entity_type") != "source":
            raise ValueError("replace_source_classification only accepts source classifications")
        created_at = now_iso()
        classification_id = new_id("cls")
        with self.connect() as conn:
            ensure_schema(conn)
            conn.execute(
                """
                DELETE FROM classifications
                WHERE entity_type = 'source' AND entity_id = ? AND taxonomy = ?
                """,
                (
                    classification["entity_id"],
                    classification["taxonomy"],
                ),
            )
            conn.execute(
                """
                INSERT INTO classifications
                  (id, entity_type, entity_id, taxonomy, category, tags, confidence, method, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    classification_id,
                    classification["entity_type"],
                    classification["entity_id"],
                    classification["taxonomy"],
                    classification["category"],
                    json.dumps(classification.get("tags") or [], ensure_ascii=False, sort_keys=True),
                    float(classification.get("confidence") or 0),
                    classification["method"],
                    created_at,
                ),
            )

        return {"id": classification_id, "created_at": created_at, **classification}

    def save_source_classification_review(self, review: dict[str, Any]) -> dict[str, Any]:
        created_at = now_iso()
        review_id = new_id("scr")
        tags = review.get("tags") or []
        taxonomy_suggestions = review.get("taxonomy_suggestions") or []
        with self.connect() as conn:
            ensure_schema(conn)
            conn.execute(
                """
                DELETE FROM source_classification_reviews
                WHERE source_id = ? AND taxonomy = ? AND status = 'pending'
                """,
                (review["source_id"], review["taxonomy"]),
            )
            conn.execute(
                """
                INSERT INTO source_classification_reviews
                  (id, source_id, taxonomy, category, source_attribute, tags, confidence, method,
                   reason, taxonomy_suggestions, status, raw_payload, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    review["source_id"],
                    review["taxonomy"],
                    review["category"],
                    review.get("source_attribute"),
                    json.dumps(tags, ensure_ascii=False, sort_keys=True),
                    float(review.get("confidence") or 0),
                    review["method"],
                    review.get("reason"),
                    json.dumps(taxonomy_suggestions, ensure_ascii=False, sort_keys=True),
                    review.get("status") or "pending",
                    json.dumps(review.get("raw_payload") or {}, ensure_ascii=False, sort_keys=True),
                    created_at,
                    created_at,
                ),
            )

        return {"id": review_id, "created_at": created_at, "updated_at": created_at, **review}

    def list_source_classification_reviews(
        self,
        limit: int = 100,
        source_id: str | None = None,
        status: str | None = "pending",
    ) -> list[dict[str, Any]]:
        where_parts = []
        params: list[Any] = []
        if source_id:
            where_parts.append("source_id = ?")
            params.append(source_id)
        if status:
            where_parts.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        params.append(limit)
        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute(
                f"""
                SELECT id, source_id, taxonomy, category, source_attribute, tags, confidence, method,
                       reason, taxonomy_suggestions, status, raw_payload, created_at, updated_at
                FROM source_classification_reviews
                {where}
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                params,
            )
            return [
                _decode_row(row, json_fields=("tags", "taxonomy_suggestions", "raw_payload"))
                for row in result.fetchall()
            ]

    def update_source_classification_review_status(self, review_id: str, status: str) -> dict[str, Any] | None:
        updated_at = now_iso()
        with self.connect() as conn:
            ensure_schema(conn)
            row = conn.execute(
                """
                SELECT id, source_id, taxonomy, category, source_attribute, tags, confidence, method,
                       reason, taxonomy_suggestions, status, raw_payload, created_at, updated_at
                FROM source_classification_reviews
                WHERE id = ?
                """,
                (review_id,),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE source_classification_reviews SET status = ?, updated_at = ? WHERE id = ?",
                (status, updated_at, review_id),
            )
            item = _decode_row(row, json_fields=("tags", "taxonomy_suggestions", "raw_payload"))
            item["status"] = status
            item["updated_at"] = updated_at
            return item

    def list_classifications(
        self,
        limit: int = 100,
        entity_type: str | None = None,
        taxonomy: str | None = None,
    ) -> list[dict[str, Any]]:
        where_parts = []
        params: list[Any] = []
        if entity_type:
            where_parts.append("entity_type = ?")
            params.append(entity_type)
        if taxonomy:
            where_parts.append("taxonomy = ?")
            params.append(taxonomy)
        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        params.append(limit)

        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute(
                f"""
                SELECT id, entity_type, entity_id, taxonomy, category, tags, confidence, method, created_at
                FROM classifications
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                params,
            )
            return [_decode_row(row, json_fields=("tags",)) for row in result.fetchall()]

    def save_digest(self, digest: dict[str, Any]) -> dict[str, Any]:
        created_at = now_iso()
        digest_id = new_id("dig")
        analysis_stage = digest.get("analysis_stage") or "content"
        with self.connect() as conn:
            ensure_schema(conn)
            conn.execute(
                "DELETE FROM digests WHERE article_id = ? AND model = ? AND analysis_stage = ?",
                (digest["article_id"], digest.get("model"), analysis_stage),
            )
            conn.execute(
                """
                INSERT INTO digests
                  (id, article_id, summary, key_points, importance_score, score_breakdown, application_targets, reason, model, analysis_stage, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    digest_id,
                    digest["article_id"],
                    digest["summary"],
                    json.dumps(digest.get("key_points") or [], ensure_ascii=False, sort_keys=True),
                    float(digest.get("importance_score") or 0),
                    json.dumps(digest.get("score_breakdown") or {}, ensure_ascii=False, sort_keys=True),
                    json.dumps(digest.get("application_targets") or [], ensure_ascii=False, sort_keys=True),
                    digest.get("reason"),
                    digest.get("model"),
                    analysis_stage,
                    created_at,
                ),
            )
            score = effective_article_importance_score(conn, digest["article_id"])
            article_row = conn.execute(
                """
                SELECT a.title, a.digest, s.name AS source_name
                FROM articles a
                LEFT JOIN sources s ON s.id = a.source_id
                WHERE a.id = ?
                """,
                (digest["article_id"],),
            ).fetchone()
            decision = retention_decision_for_score(
                score,
                analysis_stage=analysis_stage,
                title=article_row["title"] if article_row else None,
                source_name=article_row["source_name"] if article_row else None,
                digest=article_row["digest"] if article_row else None,
            )
            conn.execute(
                """
                UPDATE articles
                SET retention_level = ?,
                    archive_status = ?,
                    retention_reason = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    decision.retention_level,
                    decision.archive_status,
                    decision.reason,
                    created_at,
                    digest["article_id"],
                ),
            )

        return {"id": digest_id, "created_at": created_at, "analysis_stage": analysis_stage, **digest}

    def list_digests(
        self,
        limit: int = 100,
        application_target: str | None = None,
        min_score: float | None = None,
        analysis_stage: str | None = None,
    ) -> list[dict[str, Any]]:
        where_parts = []
        params: list[Any] = []
        if application_target:
            where_parts.append(
                "EXISTS (SELECT 1 FROM json_each(COALESCE(d.application_targets, '[]')) WHERE value = ?)"
            )
            params.append(application_target)
        if min_score is not None:
            where_parts.append("d.importance_score >= ?")
            params.append(float(min_score))
        if analysis_stage:
            where_parts.append("d.analysis_stage = ?")
            params.append(analysis_stage)
        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        params.append(limit)
        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute(
                f"""
                SELECT
                  d.id, d.article_id, a.source_id, s.name AS source_name, a.title, a.url, a.publish_time,
                  d.summary, d.key_points, d.importance_score, d.score_breakdown, d.application_targets,
                  d.reason, d.model, d.analysis_stage, d.created_at
                FROM digests d
                JOIN articles a ON a.id = d.article_id
                LEFT JOIN sources s ON s.id = a.source_id
                {where}
                ORDER BY d.importance_score DESC, COALESCE(a.publish_time, d.created_at) DESC, d.id DESC
                LIMIT ?
                """,
                params,
            )
            return [_decode_row(row, json_fields=("key_points", "score_breakdown", "application_targets")) for row in result.fetchall()]

    def list_imports(
        self,
        limit: int = 100,
        status: str | None = None,
        source_type: str | None = None,
    ) -> list[dict[str, Any]]:
        where_parts = []
        params: list[Any] = []
        if status:
            where_parts.append("status = ?")
            params.append(status)
        if source_type:
            where_parts.append("source_type = ?")
            params.append(source_type)
        where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
        params.append(limit)

        with self.connect() as conn:
            ensure_schema(conn)
            result = conn.execute(
                f"""
                SELECT id, batch_id, raw_name, raw_url, raw_payload, source_type, status, created_at
                FROM source_imports
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                params,
            )
            rows = []
            for row in result.fetchall():
                item = dict(row)
                item["raw_payload"] = json.loads(item["raw_payload"])
                rows.append(item)
            return rows

    def save_candidates_for_import(
        self,
        import_id: str,
        candidates: Iterable[dict[str, Any]],
        raw_response: Any,
        status: str = "searched",
        replace_pending: bool = False,
    ) -> dict[str, Any]:
        candidates = list(candidates)
        created_at = now_iso()
        imported: list[dict[str, Any]] = []
        deleted_pending = 0
        with self.connect() as conn:
            ensure_schema(conn)
            row = conn.execute(
                "SELECT id, raw_name FROM source_imports WHERE id = ?",
                (import_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"Import not found: {import_id}")

            conn.execute(
                """
                UPDATE source_imports
                SET status = ?, raw_payload = json_set(raw_payload, '$.resolve_response', json(?))
                WHERE id = ?
                """,
                (status, json.dumps(raw_response, ensure_ascii=False, sort_keys=True), import_id),
            )
            if replace_pending and candidates:
                result = conn.execute(
                    "DELETE FROM source_candidates WHERE import_id = ? AND decision = ?",
                    (import_id, "pending"),
                )
                deleted_pending = result.rowcount

            for candidate in candidates:
                candidate_id = new_id("cand")
                conn.execute(
                    """
                    INSERT INTO source_candidates
                      (id, import_id, candidate_name, wechat_fakeid, biz, avatar_url, intro, score, decision, raw_payload, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_id,
                        import_id,
                        candidate["candidate_name"],
                        candidate.get("wechat_fakeid"),
                        candidate.get("biz"),
                        candidate.get("avatar_url"),
                        candidate.get("intro"),
                        float(candidate.get("score", 0)),
                        "pending",
                        json.dumps(candidate.get("raw_payload") or {}, ensure_ascii=False, sort_keys=True),
                        created_at,
                    ),
                )
                imported.append({"id": candidate_id, "import_id": import_id, **candidate})

        return {"import_id": import_id, "count": len(imported), "deleted_pending": deleted_pending, "items": imported}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(source_candidates)").fetchall()}
    if "raw_payload" not in columns:
        conn.execute("ALTER TABLE source_candidates ADD COLUMN raw_payload TEXT")
    content_columns = {row["name"] for row in conn.execute("PRAGMA table_info(article_contents)").fetchall()}
    if "content_structure" not in content_columns:
        conn.execute("ALTER TABLE article_contents ADD COLUMN content_structure TEXT")
    asset_columns = {row["name"] for row in conn.execute("PRAGMA table_info(article_assets)").fetchall()}
    if "block_index" not in asset_columns:
        conn.execute("ALTER TABLE article_assets ADD COLUMN block_index INTEGER")
    if "content_ref" not in asset_columns:
        conn.execute("ALTER TABLE article_assets ADD COLUMN content_ref TEXT")
    article_columns = {row["name"] for row in conn.execute("PRAGMA table_info(articles)").fetchall()}
    if "retention_level" not in article_columns:
        conn.execute("ALTER TABLE articles ADD COLUMN retention_level TEXT NOT NULL DEFAULT 'metadata'")
    if "archive_status" not in article_columns:
        conn.execute("ALTER TABLE articles ADD COLUMN archive_status TEXT NOT NULL DEFAULT 'not_requested'")
    if "retention_reason" not in article_columns:
        conn.execute("ALTER TABLE articles ADD COLUMN retention_reason TEXT")
    digest_columns = {row["name"] for row in conn.execute("PRAGMA table_info(digests)").fetchall()}
    if "analysis_stage" not in digest_columns:
        conn.execute("ALTER TABLE digests ADD COLUMN analysis_stage TEXT NOT NULL DEFAULT 'content'")
    if "score_breakdown" not in digest_columns:
        conn.execute("ALTER TABLE digests ADD COLUMN score_breakdown TEXT")
    if "application_targets" not in digest_columns:
        conn.execute("ALTER TABLE digests ADD COLUMN application_targets TEXT")


def asset_block_indexes(content_structure: list[dict[str, Any]]) -> dict[str, int]:
    indexes: dict[str, int] = {}
    for index, block in enumerate(content_structure):
        if not isinstance(block, dict) or block.get("type") not in {"image", "video", "audio", "file"}:
            continue
        url = _clean_optional(block.get("url"))
        if url and url not in indexes:
            indexes[url] = index
    return indexes


def existing_source_id(conn: sqlite3.Connection, fakeid: str | None, biz: str | None) -> str | None:
    if not fakeid and not biz:
        return None
    row = conn.execute(
        """
        SELECT id
        FROM sources
        WHERE (? IS NOT NULL AND wechat_fakeid = ?)
           OR (? IS NOT NULL AND biz = ?)
        LIMIT 1
        """,
        (fakeid, fakeid, biz, biz),
    ).fetchone()
    return row["id"] if row else None


def _decode_row(row: sqlite3.Row, json_fields: tuple[str, ...] = ()) -> dict[str, Any]:
    item = dict(row)
    for field in json_fields:
        value = item.get(field)
        if value:
            item[field] = json.loads(value)
        else:
            item[field] = None
    return item


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None

import json
import csv
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from wechat_mp_feed.analysis import classify_article, classify_source, generate_article_digest
from wechat_mp_feed.adapters.wechat_download_api import WeChatDownloadAPIAdapter, WeChatDownloadAPIConfig
from wechat_mp_feed.articles import normalize_article_items
from wechat_mp_feed.candidates import normalize_source_candidates
from wechat_mp_feed.cli import (
    adapter_error_message,
    article_probe_rate_limited,
    article_probe_retry_after_seconds,
    best_candidates_for_imports,
    fetch_content_queue,
    main,
    validate_latest_article_content,
    with_retries,
)
from wechat_mp_feed.content import normalize_article_content
from wechat_mp_feed.media_import import clean_account_names, normalize_crop_filter, normalize_scale_filter
from wechat_mp_feed.name_match import names_equivalent, search_query_variants
from wechat_mp_feed.onboarding import latest_probe_article
from wechat_mp_feed.policy import retryable_status, tier_policy
from wechat_mp_feed.retention import metadata_triage_decision, retention_decision_for_score
from wechat_mp_feed.storage import Store
from wechat_mp_feed.taxonomy import load_taxonomy
from wechat_mp_feed.wechat_url import parse_article_url
from wechat_mp_feed.llm_jobs import build_llm_jobs, build_onboarding_llm_jobs


ROOT = Path(__file__).resolve().parents[3]
FINANCE_TAXONOMY = ROOT / "examples" / "taxonomy.finance.yaml"


class MVPTest(unittest.TestCase):
    def test_parse_article_url_extracts_identifiers(self):
        parsed = parse_article_url("https://mp.weixin.qq.com/s?__biz=MzA123&mid=224748&idx=1&sn=abc")

        self.assertEqual(parsed.biz, "MzA123")
        self.assertEqual(parsed.mid, "224748")
        self.assertEqual(parsed.idx, "1")
        self.assertEqual(parsed.sn, "abc")

    def test_store_import_article_url(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "mpfeed.sqlite")
            store.init()

            result = store.import_article_urls(["https://mp.weixin.qq.com/s?__biz=MzA123&mid=224748&idx=1&sn=abc"])
            rows = store.list_imports()

            self.assertEqual(result["count"], 1)
            self.assertEqual(rows[0]["source_type"], "article_url")
            self.assertEqual(rows[0]["raw_payload"]["biz"], "MzA123")

    def test_store_import_source_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "mpfeed.sqlite")
            store.init()

            result = store.import_source_rows("csv", [{"raw_name": "第一财经", "category_hint": "finance"}])
            rows = store.list_imports()

            self.assertEqual(result["count"], 1)
            self.assertEqual(rows[0]["raw_name"], "第一财经")
            self.assertEqual(rows[0]["source_type"], "csv")

    def test_cli_import_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            names_path = Path(temp_dir) / "names.txt"
            names_path.write_text("# comment\n示例策略研究\n\n示例金工研究\n", encoding="utf-8")

            with redirect_stdout(StringIO()):
                exit_code = main(["--db", str(db_path), "import", "names", str(names_path), "--source-type", "screenshot"])
            store = Store(db_path)
            rows = store.list_imports()

            self.assertEqual(exit_code, 0)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["source_type"], "screenshot")

    def test_clean_ocr_account_names_filters_ui_and_dedupes(self):
        names = clean_account_names(
            [
                "公众号",
                "10:30",
                "示例策略研究",
                "示例策略研究 ",
                "示例市场发布",
                "500个公众号",
                "59篇原创内容",
                "视频号：江苏中",
                "全部",
                "贴图",
                "已关注么",
                "文育2000年进入公务员考试培训行业，核心",
                "训等",
                "A",
            ],
            dedupe_threshold=0.92,
        )

        self.assertEqual(names, ["示例策略研究", "示例市场发布"])
        self.assertEqual(clean_account_names(["示例金工研究", "示例金工研究", "单帧噪声"], min_occurrences=2), ["示例金工研究"])
        self.assertEqual(normalize_crop_filter("1,2,300,400"), "crop=300:400:1:2")
        self.assertEqual(normalize_scale_filter(480), "scale=480:-1")
        with self.assertRaises(ValueError):
            normalize_scale_filter(120)

    def test_account_name_matching_ignores_spaces_and_common_variants(self):
        self.assertTrue(names_equivalent("智堡 Mikko", "智堡Mikko"))
        self.assertTrue(names_equivalent("懒貓的丰收日", "懒猫的丰收日"))
        self.assertTrue(names_equivalent("表舅是养基大戶", "表舅是养基大户"))
        self.assertIn("懒猫的丰收日", search_query_variants("懒貓的丰收日"))

    def test_latest_article_probe_prefers_fetchable_article(self):
        class FakeAdapter:
            def fetch_article(self, url):
                if url.endswith("/deleted"):
                    return {"ok": False, "status": 200, "body": {"success": False, "error": "deleted"}}
                return {"ok": True, "status": 200, "body": {"data": {"plain_content": "usable article text"}}}

        articles = validate_latest_article_content(
            FakeAdapter(),
            [
                {"title": "deleted", "url": "https://mp.weixin.qq.com/s/deleted"},
                {"title": "usable", "url": "https://mp.weixin.qq.com/s/usable"},
            ],
        )
        picked = latest_probe_article({"raw_payload": {"article_probe": {"articles": articles}}})

        self.assertFalse(articles[0]["content_fetch_ok"])
        self.assertTrue(articles[1]["content_fetch_ok"])
        self.assertEqual(picked["title"], "usable")

    def test_article_probe_rate_limited_detects_body_level_rate_limit(self):
        payload = {"ok": False, "body": {"error": "Rate limited: 文章获取过快，请28秒后重试"}}
        self.assertTrue(article_probe_rate_limited(payload))
        self.assertEqual(article_probe_retry_after_seconds(payload), 28.8)
        self.assertFalse(article_probe_rate_limited({"ok": False, "body": {"error": "文章被删除"}}))

    def test_retention_policy_maps_scores_to_storage_levels(self):
        self.assertEqual(retention_decision_for_score(0.2).retention_level, "metadata")
        self.assertEqual(retention_decision_for_score(0.5).retention_level, "content")
        high = retention_decision_for_score(0.82)
        self.assertEqual(high.retention_level, "full_archive")
        self.assertEqual(high.archive_status, "pending")

    def test_metadata_triage_uses_score_with_hard_noise_override(self):
        self.assertEqual(
            metadata_triage_decision(0.82, title="金融招聘 | 某基金社招岗位", source_name="金融圈招聘"),
            "metadata_only",
        )
        self.assertEqual(
            metadata_triage_decision(0.62, title="行业轮动周报：风格扩散", source_name="量化研究"),
            "fetch_required",
        )
        self.assertEqual(
            metadata_triage_decision(0.48, title="政策双周报：关注财政节奏", source_name="固收研究"),
            "fetch_candidate",
        )
        self.assertEqual(
            retention_decision_for_score(
                0.82,
                analysis_stage="metadata",
                title="金融招聘 | 某基金社招岗位",
                source_name="金融圈招聘",
            ).retention_level,
            "metadata",
        )
        self.assertEqual(
            retention_decision_for_score(
                0.82,
                analysis_stage="metadata",
                title="行业轮动周报：风格扩散",
                source_name="量化研究",
            ).retention_level,
            "content",
        )

    def test_cli_import_video_uses_ocr_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            video_path = Path(temp_dir) / "following.mp4"
            names_output = Path(temp_dir) / "names.txt"
            raw_output = Path(temp_dir) / "ocr.json"
            video_path.write_bytes(b"not a real video")

            fake_ocr = {
                "ok": True,
                "video": str(video_path),
                "fps": 1.0,
                "crop": None,
                "ocr": "paddle",
                "lang": "ch",
                "frames_seen": 3,
                "frame_dir": None,
                "names": ["示例策略研究", "示例金工研究"],
                "count": 2,
                "raw_lines": [],
            }
            with patch("wechat_mp_feed.cli.extract_account_names_from_video", return_value=fake_ocr), redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "import",
                        "video",
                        str(video_path),
                        "--names-output",
                        str(names_output),
                        "--raw-output",
                        str(raw_output),
                    ]
                )
            payload = json.loads(stdout.getvalue())
            rows = Store(db_path).list_imports(source_type="recording")

            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["ocr"]["names_detected"], 2)
            self.assertEqual(payload["ocr"]["min_occurrences"], 1)
            self.assertEqual(payload["imported"]["count"], 2)
            self.assertNotIn("items", payload["imported"])
            self.assertEqual(len(rows), 2)
            self.assertEqual(names_output.read_text(encoding="utf-8").splitlines(), ["示例策略研究", "示例金工研究"])
            self.assertEqual(json.loads(raw_output.read_text(encoding="utf-8"))["count"], 2)

    def test_wechat_download_api_adapter_health(self):
        adapter = WeChatDownloadAPIAdapter(WeChatDownloadAPIConfig(base_url="http://example.test"))

        with patch("wechat_mp_feed.adapters.http.urlopen", side_effect=_fake_urlopen):
            health = adapter.health()
            auth_status = adapter.auth_status()
            search = adapter.search_sources("第一财经")

        self.assertTrue(health["ok"])
        self.assertEqual(health["body"]["status"], "ok")
        self.assertTrue(auth_status["body"]["logged_in"])
        self.assertIn("%E7%AC%AC%E4%B8%80%E8%B4%A2%E7%BB%8F", search["url"])
        self.assertEqual(search["body"]["items"], [])

    def test_wechat_download_api_adapter_marks_body_level_failure(self):
        adapter = WeChatDownloadAPIAdapter(WeChatDownloadAPIConfig(base_url="http://example.test"))

        with patch("wechat_mp_feed.adapters.http.urlopen", return_value=_FakeResponse({"success": False, "error": "invalid session"})):
            search = adapter.search_sources("国投证券研究")

        self.assertFalse(search["ok"])
        self.assertEqual(search["body"]["error"], "invalid session")

    def test_cli_doctor_ready(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"

            with patch("wechat_mp_feed.adapters.http.urlopen", side_effect=_fake_urlopen), redirect_stdout(StringIO()) as stdout:
                exit_code = main(["--db", str(db_path), "doctor", "--base-url", "http://example.test"])
            payload = json.loads(stdout.getvalue())

            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["service"]["login_url"], "http://example.test/login.html")

    def test_cli_login_url(self):
        with redirect_stdout(StringIO()) as stdout:
            exit_code = main(["adapter", "wechat-download-api", "--base-url", "http://example.test", "login-url"])
        payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["login_url"], "http://example.test/login.html")

    def test_candidate_review_flow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "mpfeed.sqlite")
            store.init()
            candidates = normalize_source_candidates(
                {
                    "items": [
                        {
                            "nickname": "第一财经",
                            "fakeid": "fake_001",
                            "__biz": "MzA123",
                            "signature": "财经资讯",
                        }
                    ]
                },
                "第一财经",
            )

            saved = store.save_search_candidates("第一财经", candidates, {"items": []})
            listed = store.list_candidates()
            accepted = store.accept_candidate(saved["items"][0]["id"], tier="core")
            sources = store.list_sources()

            self.assertEqual(saved["count"], 1)
            self.assertEqual(listed[0]["candidate_name"], "第一财经")
            self.assertTrue(accepted["ok"])
            self.assertEqual(sources[0]["name"], "第一财经")
            self.assertEqual(sources[0]["tier"], "core")

    def test_save_candidates_for_import(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "mpfeed.sqlite")
            store.init()
            imported = store.import_source_rows("screenshot", [{"raw_name": "示例策略研究"}])
            import_id = imported["items"][0]["id"]

            result = store.save_candidates_for_import(
                import_id,
                [{"candidate_name": "示例策略研究", "wechat_fakeid": "fake_004", "score": 0.9, "raw_payload": {}}],
                {"items": []},
            )
            imports = store.list_imports(status="searched")
            candidates = store.list_candidates()

            self.assertEqual(result["count"], 1)
            self.assertEqual(imports[0]["id"], import_id)
            self.assertEqual(candidates[0]["import_id"], import_id)

    def test_save_candidates_for_import_can_replace_pending_candidates(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "mpfeed.sqlite")
            store.init()
            imported = store.import_source_rows("recording", [{"raw_name": "示例策略研究"}])
            import_id = imported["items"][0]["id"]
            store.save_candidates_for_import(
                import_id,
                [{"candidate_name": "示例策略研究", "wechat_fakeid": "fake_004", "score": 0.9, "raw_payload": {}}],
                {"items": []},
            )

            result = store.save_candidates_for_import(
                import_id,
                [
                    {
                        "candidate_name": "示例策略研究",
                        "wechat_fakeid": "fake_004",
                        "intro": "策略研究与市场观点",
                        "score": 0.92,
                        "raw_payload": {"signature": "策略研究与市场观点"},
                    }
                ],
                {"items": []},
                replace_pending=True,
            )
            candidates = store.list_candidates()

            self.assertEqual(result["deleted_pending"], 1)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["intro"], "策略研究与市场观点")

    def test_replace_pending_candidates_keeps_old_candidates_when_new_search_is_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "mpfeed.sqlite")
            store.init()
            imported = store.import_source_rows("recording", [{"raw_name": "示例策略研究"}])
            import_id = imported["items"][0]["id"]
            store.save_candidates_for_import(
                import_id,
                [{"candidate_name": "示例策略研究", "wechat_fakeid": "fake_004", "score": 0.9, "raw_payload": {}}],
                {"items": []},
            )

            result = store.save_candidates_for_import(import_id, [], {"items": []}, replace_pending=True)
            candidates = store.list_candidates()

            self.assertEqual(result["deleted_pending"], 0)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]["candidate_name"], "示例策略研究")

    def test_cli_export_onboarding_table(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            store = Store(db_path)
            store.init()
            imported = store.import_source_rows("recording", [{"raw_name": "东北金工"}])
            import_id = imported["items"][0]["id"]
            saved = store.save_candidates_for_import(
                import_id,
                [
                    {
                        "candidate_name": "东北金工",
                        "wechat_fakeid": "fake_quant",
                        "intro": "金融工程与量化策略研究",
                        "score": 0.92,
                        "raw_payload": {},
                    }
                ],
                {"items": []},
            )
            accepted = store.accept_candidate(saved["items"][0]["id"], tier="core")
            source = store.list_sources()[0]
            taxonomy = load_taxonomy(FINANCE_TAXONOMY)
            store.save_classification(classify_source(source, taxonomy))
            store.upsert_articles(
                accepted["source_id"],
                [
                    {
                        "title": "量化周报",
                        "url": "https://mp.weixin.qq.com/s/demo",
                        "digest": "量化策略跟踪",
                        "publish_time": "2026-05-04T00:00:00+00:00",
                    }
                ],
            )

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "export",
                        "onboarding",
                        "--source-type",
                        "recording",
                        "--taxonomy",
                        str(FINANCE_TAXONOMY),
                    ]
                )
            rows = list(csv.DictReader(StringIO(stdout.getvalue())))

            self.assertEqual(exit_code, 0)
            self.assertEqual(rows[0]["ocr_name"], "东北金工")
            self.assertEqual(rows[0]["is_active"], "True")
            self.assertEqual(rows[0]["latest_article_title"], "量化周报")
            self.assertEqual(rows[0]["match_type"], "exact")
            self.assertEqual(rows[0]["recommended_action"], "accepted_finance")
            self.assertEqual(rows[0]["import_status"], "resolved")
            self.assertEqual(rows[0]["system_decision"], "accepted_finance")
            self.assertEqual(rows[0]["requires_user_action"], "False")

    def test_export_onboarding_uses_candidate_article_probe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            store = Store(db_path)
            store.init()
            imported = store.import_source_rows("recording", [{"raw_name": "GlobalAssetAllocator"}])
            saved = store.save_candidates_for_import(
                imported["items"][0]["id"],
                [
                    {
                        "candidate_name": "GlobalAssetAllocator",
                        "wechat_fakeid": "fake_global",
                        "score": 0.92,
                        "raw_payload": {},
                    }
                ],
                {"items": []},
            )
            store.save_candidate_article_probe(
                saved["items"][0]["id"],
                [
                    {
                        "title": "Global allocation weekly",
                        "url": "https://mp.weixin.qq.com/s/probe",
                        "digest": "Asset allocation notes",
                        "publish_time": "2026-05-03T00:00:00+00:00",
                    }
                ],
                {"items": []},
            )

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "export",
                        "onboarding",
                        "--source-type",
                        "recording",
                        "--taxonomy",
                        str(FINANCE_TAXONOMY),
                    ]
                )
            rows = list(csv.DictReader(StringIO(stdout.getvalue())))

            self.assertEqual(exit_code, 0)
            self.assertEqual(rows[0]["latest_article_title"], "Global allocation weekly")
            self.assertEqual(rows[0]["latest_article_url"], "https://mp.weixin.qq.com/s/probe")
            self.assertEqual(rows[0]["latest_probe_status"], "candidate_latest_ok")
            self.assertEqual(rows[0]["latest_probe_refreshed"], "True")

    def test_export_onboarding_marks_ignored_and_user_review_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            store = Store(db_path)
            store.init()
            ignored = store.import_source_rows("recording", [{"raw_name": "示例体育赛事"}])
            review = store.import_source_rows("recording", [{"raw_name": "策论金工"}])
            ignored_candidate = store.save_candidates_for_import(
                ignored["items"][0]["id"],
                [
                    {
                        "candidate_name": "示例体育赛事",
                        "wechat_fakeid": "fake_tennis",
                        "intro": "网球赛事报道",
                        "score": 0.92,
                        "raw_payload": {},
                    }
                ],
                {"items": []},
            )["items"][0]
            store.reject_candidates_for_import(ignored["items"][0]["id"])
            store.update_import_status(ignored["items"][0]["id"], "ignored")
            store.update_import_status(review["items"][0]["id"], "needs_review")

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "export",
                        "onboarding",
                        "--source-type",
                        "recording",
                        "--taxonomy",
                        str(FINANCE_TAXONOMY),
                    ]
                )
            rows = {row["ocr_name"]: row for row in csv.DictReader(StringIO(stdout.getvalue()))}

            self.assertEqual(exit_code, 0)
            self.assertEqual(rows["示例体育赛事"]["system_decision"], "ignored")
            self.assertEqual(rows["示例体育赛事"]["recommended_action"], "ignored_non_finance")
            self.assertEqual(rows["示例体育赛事"]["requires_user_action"], "False")
            self.assertEqual(rows["策论金工"]["system_decision"], "needs_review")
            self.assertEqual(rows["策论金工"]["requires_user_action"], "True")

    def test_cli_export_compact_onboarding_table(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            store = Store(db_path)
            store.init()
            imported = store.import_source_rows("recording", [{"raw_name": "东北金工"}])
            saved = store.save_candidates_for_import(
                imported["items"][0]["id"],
                [
                    {
                        "candidate_name": "东北金工",
                        "wechat_fakeid": "fake_quant",
                        "intro": "金融工程与量化策略研究",
                        "score": 0.92,
                        "raw_payload": {},
                    }
                ],
                {"items": []},
            )
            accepted = store.accept_candidate(saved["items"][0]["id"], tier="core")
            taxonomy = load_taxonomy(FINANCE_TAXONOMY)
            store.save_classification(classify_source(store.list_sources()[0], taxonomy))
            store.upsert_articles(
                accepted["source_id"],
                [{"title": "量化周报", "url": "https://mp.weixin.qq.com/s/demo", "digest": "量化策略跟踪"}],
            )

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "export",
                        "onboarding",
                        "--view",
                        "compact",
                        "--source-type",
                        "recording",
                        "--taxonomy",
                        str(FINANCE_TAXONOMY),
                    ]
                )
            rows = list(csv.DictReader(StringIO(stdout.getvalue())))

            self.assertEqual(exit_code, 0)
            self.assertEqual(rows[0]["ocr_account"], "东北金工")
            self.assertEqual(rows[0]["matched_account"], "东北金工")
            self.assertEqual(rows[0]["candidate_account"], "")
            self.assertEqual(rows[0]["system_decision"], "accepted_finance")
            self.assertEqual(rows[0]["requires_manual_confirmation"], "False")
            self.assertIn("金融工程", rows[0]["evidence_summary"])

    def test_onboarding_llm_jobs_include_semantic_classification_guidance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "mpfeed.sqlite")
            store.init()
            imported = store.import_source_rows("recording", [{"raw_name": "华泰证券科技研究"}])
            store.save_candidates_for_import(
                imported["items"][0]["id"],
                [
                    {
                        "candidate_name": "华泰证券科技研究",
                        "wechat_fakeid": "fake_sell_side",
                        "score": 0.92,
                        "raw_payload": {},
                    }
                ],
                {"items": []},
            )

            payload = build_onboarding_llm_jobs(store, load_taxonomy(FINANCE_TAXONOMY), source_type="recording")
            instructions = "\n".join(payload["instructions"])

            self.assertIn("sell-side", instructions)
            self.assertIn("【华泰金工】", instructions)
            self.assertIn("industry_research", instructions)
            self.assertIn("Strong non-finance accounts", instructions)
            self.assertIn("Do not use sell_side or buy_side as the primary category", instructions)
            self.assertIn("content_fetch_ok=false", instructions)

    def test_compact_onboarding_keeps_name_diff_out_of_matched_account(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            store = Store(db_path)
            store.init()
            imported = store.import_source_rows("recording", [{"raw_name": "示例金工研究"}])
            saved = store.save_candidates_for_import(
                imported["items"][0]["id"],
                [
                    {
                        "candidate_name": "示例金工研究与资产配置",
                        "wechat_fakeid": "fake_new",
                        "intro": "金融工程与资产配置研究",
                        "score": 0.92,
                        "raw_payload": {},
                    }
                ],
                {"items": []},
            )
            store.accept_candidate(saved["items"][0]["id"], tier="core")

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "export",
                        "onboarding",
                        "--view",
                        "compact",
                        "--source-type",
                        "recording",
                        "--taxonomy",
                        str(FINANCE_TAXONOMY),
                    ]
                )
            rows = list(csv.DictReader(StringIO(stdout.getvalue())))

            self.assertEqual(exit_code, 0)
            self.assertEqual(rows[0]["ocr_account"], "示例金工研究")
            self.assertEqual(rows[0]["matched_account"], "")
            self.assertEqual(rows[0]["candidate_account"], "示例金工研究与资产配置")
            self.assertEqual(rows[0]["match_type"], "different")
            self.assertEqual(rows[0]["requires_manual_confirmation"], "True")

    def test_best_candidates_for_imports_selects_one_per_import(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "mpfeed.sqlite")
            store.init()
            first = store.import_source_rows("recording", [{"raw_name": "示例金工研究"}])
            second = store.import_source_rows("recording", [{"raw_name": "招商宏观静思录"}])
            store.save_candidates_for_import(
                first["items"][0]["id"],
                [
                    {"candidate_name": "示例金工研究与资产配置", "wechat_fakeid": "fake_new", "score": 0.74, "raw_payload": {}},
                    {"candidate_name": "示例金工研究", "wechat_fakeid": "fake_old", "score": 0.92, "raw_payload": {}},
                ],
                {"items": []},
            )
            store.save_candidates_for_import(
                second["items"][0]["id"],
                [{"candidate_name": "招商宏观静思录", "wechat_fakeid": "fake_macro", "score": 0.92, "raw_payload": {}}],
                {"items": []},
            )

            selected = best_candidates_for_imports(store, store.list_candidates(decision=None, limit=10), "recording", 10)

            self.assertEqual({item["candidate_name"] for item in selected}, {"示例金工研究", "招商宏观静思录"})

    def test_cli_resolve_imports(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            store = Store(db_path)
            store.init()
            store.import_source_rows("screenshot", [{"raw_name": "示例策略研究"}])

            with patch("wechat_mp_feed.adapters.http.urlopen", side_effect=_fake_urlopen), redirect_stdout(StringIO()):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "resolve",
                        "imports",
                        "--base-url",
                        "http://example.test",
                        "--source-type",
                        "screenshot",
                        "--no-delay",
                    ]
                )
            candidates = Store(db_path).list_candidates()

            self.assertEqual(exit_code, 0)
            self.assertEqual(candidates[0]["candidate_name"], "示例策略研究")

    def test_cli_review_apply_decisions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            decisions_path = Path(temp_dir) / "decisions.csv"
            store = Store(db_path)
            store.init()
            accepted_import = store.import_source_rows("screenshot", [{"raw_name": "示例策略研究"}])
            rejected_import = store.import_source_rows("screenshot", [{"raw_name": "草根调研"}])
            accepted_candidate = store.save_candidates_for_import(
                accepted_import["items"][0]["id"],
                [{"candidate_name": "示例策略研究", "wechat_fakeid": "fake_004", "score": 0.9, "raw_payload": {}}],
                {"items": []},
            )["items"][0]
            rejected_candidate = store.save_candidates_for_import(
                rejected_import["items"][0]["id"],
                [{"candidate_name": "草根调研", "wechat_fakeid": "fake_005", "score": 0.8, "raw_payload": {}}],
                {"items": []},
            )["items"][0]
            decisions_path.write_text(
                "candidate_id,decision,tier\n"
                f"{accepted_candidate['id']},accept,core\n"
                f"{rejected_candidate['id']},reject,\n",
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()):
                exit_code = main(["--db", str(db_path), "review", "apply", str(decisions_path)])
            sources = Store(db_path).list_sources()
            rejected = Store(db_path).list_candidates(decision="reject")

            self.assertEqual(exit_code, 0)
            self.assertEqual(sources[0]["name"], "示例策略研究")
            self.assertEqual(sources[0]["tier"], "core")
            self.assertEqual(rejected[0]["candidate_name"], "草根调研")

    def test_cli_review_apply_onboarding_manual_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            decisions_path = Path(temp_dir) / "onboarding-review.csv"
            store = Store(db_path)
            store.init()
            accepted_import = store.import_source_rows("recording", [{"raw_name": "示例策略研究"}])
            url_import = store.import_source_rows("recording", [{"raw_name": "王德伦策略与投资"}])
            store.save_candidates_for_import(
                accepted_import["items"][0]["id"],
                [{"candidate_name": "示例策略研究", "wechat_fakeid": "fake_004", "score": 0.9, "raw_payload": {}}],
                {"items": []},
            )
            decisions_path.write_text(
                "OCR识别账号,人工确认账号名,人工确认文章链接,人工确认分类,人工决策,备注\n"
                "示例策略研究,,,,确认候选,候选正确\n"
                "王德伦策略与投资,,https://mp.weixin.qq.com/s/demo,策略,稍后,用文章链接解析\n",
                encoding="utf-8-sig",
            )

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "review",
                        "apply-onboarding",
                        "--source-type",
                        "recording",
                        str(decisions_path),
                    ]
                )
            payload = json.loads(stdout.getvalue())
            refreshed = Store(db_path)
            imports = {item["raw_name"]: item for item in refreshed.list_imports(source_type="recording", limit=10)}

            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["accepted_candidates"], 1)
            self.assertEqual(refreshed.list_sources()[0]["name"], "示例策略研究")
            self.assertEqual(imports["示例策略研究"]["status"], "resolved")
            self.assertEqual(imports["王德伦策略与投资"]["status"], "needs_review")
            self.assertEqual(
                imports["王德伦策略与投资"]["raw_payload"]["manual_onboarding_review"]["manual_article_url"],
                "https://mp.weixin.qq.com/s/demo",
            )

    def test_cli_resolve_manual_names_uses_manual_name_for_strict_match(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            decisions_path = Path(temp_dir) / "onboarding-review.csv"
            store = Store(db_path)
            store.init()
            store.import_source_rows("recording", [{"raw_name": "王德伦策路与投资"}])
            decisions_path.write_text(
                "OCR识别账号,人工确认账号名,人工确认文章链接,人工确认分类,人工决策,备注\n"
                "王德伦策路与投资,王德伦策略与投资,,,,手工修正 OCR 名称\n",
                encoding="utf-8-sig",
            )
            with redirect_stdout(StringIO()):
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "review",
                        "apply-onboarding",
                        "--source-type",
                        "recording",
                        str(decisions_path),
                    ]
                )
            self.assertEqual(exit_code, 0)

            with patch("wechat_mp_feed.adapters.http.urlopen", side_effect=_fake_urlopen), redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "resolve",
                        "manual-names",
                        "--base-url",
                        "http://example.test",
                        "--source-type",
                        "recording",
                        "--no-delay",
                    ]
                )
            payload = json.loads(stdout.getvalue())
            refreshed = Store(db_path)

            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["strict_matches"], 1)
            self.assertEqual(refreshed.list_candidates()[0]["candidate_name"], "王德伦策略与投资")

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "export",
                        "onboarding",
                        "--view",
                        "compact",
                        "--source-type",
                        "recording",
                        "--taxonomy",
                        str(FINANCE_TAXONOMY),
                    ]
                )
            rows = list(csv.DictReader(StringIO(stdout.getvalue())))

            self.assertEqual(exit_code, 0)
            self.assertEqual(rows[0]["ocr_account"], "王德伦策路与投资")
            self.assertEqual(rows[0]["matched_account"], "王德伦策略与投资")
            self.assertEqual(rows[0]["match_type"], "exact")
            self.assertEqual(rows[0]["manual_account_name"], "王德伦策略与投资")

    def test_best_candidates_can_filter_to_strict_identity_matches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            store = Store(db_path)
            store.init()
            strict_import = store.import_source_rows("recording", [{"raw_name": "示例策略研究"}])
            loose_import = store.import_source_rows("recording", [{"raw_name": "示例体育赛事"}])
            store.save_candidates_for_import(
                strict_import["items"][0]["id"],
                [{"candidate_name": "示例策略研究", "wechat_fakeid": "fake_strategy", "score": 0.94, "raw_payload": {}}],
                {"items": []},
            )
            store.save_candidates_for_import(
                loose_import["items"][0]["id"],
                [{"candidate_name": "中网公司", "wechat_fakeid": "fake_tennis", "score": 0.74, "raw_payload": {}}],
                {"items": []},
            )

            all_candidates = store.list_candidates(limit=10)
            strict_best = best_candidates_for_imports(
                store,
                all_candidates,
                source_type="recording",
                limit=10,
                strict_match_only=True,
            )

            self.assertEqual([item["candidate_name"] for item in strict_best], ["示例策略研究"])

    def test_cli_run_batch_auto_exact_flow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            names_path = Path(temp_dir) / "names.txt"
            digest_path = Path(temp_dir) / "digest.md"
            names_path.write_text("示例策略研究\n", encoding="utf-8")

            with patch("wechat_mp_feed.adapters.http.urlopen", side_effect=_fake_urlopen), redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "run",
                        "batch",
                        "--base-url",
                        "http://example.test",
                        "--names-file",
                        str(names_path),
                        "--source-type",
                        "screenshot",
                        "--resolve-limit",
                        "5",
                        "--max-sources",
                        "5",
                        "--article-count",
                        "2",
                        "--content-limit",
                        "2",
                        "--digest-limit",
                        "5",
                        "--digest-output",
                        str(digest_path),
                        "--no-delay",
                    ]
                )
            payload = json.loads(stdout.getvalue())
            store = Store(db_path)

            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["auto_reviewed"]["accepted"], 1)
            self.assertEqual(payload["collected"]["articles_saved"], 1)
            self.assertEqual(payload["digested"]["digests_saved"], 1)
            self.assertEqual(store.list_sources()[0]["name"], "示例策略研究")
            self.assertIn("市场观察", digest_path.read_text(encoding="utf-8"))

    def test_cli_run_onboarding_exports_jobs_and_excel_friendly_review_csv(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            names_path = Path(temp_dir) / "names.txt"
            jobs_path = Path(temp_dir) / "onboarding-jobs.json"
            review_path = Path(temp_dir) / "onboarding-review.csv"
            names_path.write_text("示例策略研究\n", encoding="utf-8")

            with patch("wechat_mp_feed.adapters.http.urlopen", side_effect=_fake_urlopen), redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "run",
                        "onboarding",
                        "--base-url",
                        "http://example.test",
                        "--names-file",
                        str(names_path),
                        "--source-type",
                        "recording",
                        "--limit",
                        "10",
                        "--llm-jobs-output",
                        str(jobs_path),
                        "--review-output",
                        str(review_path),
                        "--no-delay",
                    ]
                )
            payload = json.loads(stdout.getvalue())
            jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
            raw_review = review_path.read_bytes()
            review_rows = list(csv.DictReader(StringIO(raw_review.decode("utf-8-sig"))))
            store = Store(db_path)

            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["search_rounds"][0]["stage"], "search_original")
            self.assertEqual(payload["search_rounds"][0]["result"]["candidates_saved"], 1)
            self.assertEqual(payload["candidate_latest"]["articles_saved_to_candidate_payload"], 1)
            self.assertEqual(payload["llm_jobs_count"], 1)
            self.assertEqual(jobs["jobs"][0]["candidates"][0]["latest_articles"][0]["title"], "市场观察")
            self.assertTrue(raw_review.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(review_rows[0]["ocr_account"], "示例策略研究")
            self.assertEqual(review_rows[0]["matched_account"], "示例策略研究")
            self.assertEqual(store.list_imports(source_type="recording")[0]["status"], "searched")

    def test_reject_candidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "mpfeed.sqlite")
            store.init()
            saved = store.save_search_candidates(
                "测试号",
                [{"candidate_name": "测试号", "wechat_fakeid": "fake_002", "score": 0.92, "raw_payload": {}}],
                {"items": []},
            )

            result = store.reject_candidate(saved["items"][0]["id"])
            listed = store.list_candidates(decision="reject")

            self.assertTrue(result["ok"])
            self.assertEqual(listed[0]["decision"], "reject")

    def test_normalize_article_items(self):
        articles = normalize_article_items(
            {
                "app_msg_list": [
                    {
                        "title": "市场观察",
                        "link": "https:\\/\\/mp.weixin.qq.com\\/s?__biz=MzA123&mid=1",
                        "digest": "摘要",
                        "cover": "https://example.test/cover.jpg",
                        "update_time": 1710000000,
                    }
                ]
            }
        )

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["title"], "市场观察")
        self.assertIn("mp.weixin.qq.com", articles[0]["url"])
        self.assertEqual(articles[0]["digest"], "摘要")

    def test_normalize_article_items_reads_nested_articles(self):
        articles = normalize_article_items(
            {
                "data": {
                    "articles": [
                        {
                            "title": "四季度策略",
                            "link": "https://mp.weixin.qq.com/s/demo",
                            "digest": "策略摘要",
                            "create_time": 1758445967,
                        }
                    ]
                }
            }
        )

        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["title"], "四季度策略")
        self.assertEqual(articles[0]["digest"], "策略摘要")

    def test_store_upsert_articles(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "mpfeed.sqlite")
            store.init()
            saved = store.save_search_candidates(
                "第一财经",
                [{"candidate_name": "第一财经", "wechat_fakeid": "fake_001", "score": 0.92, "raw_payload": {}}],
                {"items": []},
            )
            accepted = store.accept_candidate(saved["items"][0]["id"], tier="core")

            result = store.upsert_articles(
                accepted["source_id"],
                [
                    {
                        "title": "市场观察",
                        "url": "https://mp.weixin.qq.com/s?__biz=MzA123&mid=1",
                        "digest": "摘要",
                        "cover_url": None,
                        "publish_time": "2024-03-09T16:00:00+00:00",
                        "raw_payload": {"title": "市场观察"},
                    }
                ],
            )
            articles = store.list_articles()

            self.assertEqual(result["count"], 1)
            self.assertEqual(articles[0]["source_id"], accepted["source_id"])
            self.assertEqual(articles[0]["title"], "市场观察")

    def test_cli_collect_history_backfills_recent_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            store = Store(db_path)
            store.init()
            saved = store.save_search_candidates(
                "示例策略研究",
                [{"candidate_name": "示例策略研究", "wechat_fakeid": "fake_004", "score": 0.9, "raw_payload": {}}],
                {"items": []},
            )
            store.accept_candidate(saved["items"][0]["id"], tier="core")

            with patch("wechat_mp_feed.adapters.http.urlopen", side_effect=_fake_urlopen), redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "collect",
                        "history",
                        "--base-url",
                        "http://example.test",
                        "--tier",
                        "core",
                        "--days",
                        "10000",
                        "--max-sources",
                        "1",
                        "--max-pages-per-source",
                        "2",
                        "--no-delay",
                    ]
                )
            payload = json.loads(stdout.getvalue())
            articles = store.list_articles()

            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["totals"]["sources_ok"], 1)
            self.assertEqual(payload["totals"]["articles_upserted"], 1)
            self.assertEqual(payload["results"][0]["stop_reason"], "short_page")
            self.assertEqual(articles[0]["title"], "市场观察")

    def test_feed_export_includes_source_and_content_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            store = Store(db_path)
            store.init()
            saved = store.save_search_candidates(
                "银行研究",
                [{"candidate_name": "银行研究", "wechat_fakeid": "fake_feed", "score": 0.92, "raw_payload": {}}],
                {"items": []},
            )
            accepted = store.accept_candidate(saved["items"][0]["id"], tier="core")
            store.save_classification(
                {
                    "entity_type": "source",
                    "entity_id": accepted["source_id"],
                    "taxonomy": "finance",
                    "category": "fixed_income",
                    "tags": ["sell_side"],
                    "confidence": 0.9,
                    "method": "manual:test",
                }
            )
            articles_result = store.upsert_articles(
                accepted["source_id"],
                [
                    {
                        "title": "银行债券观察",
                        "url": "https://mp.weixin.qq.com/s/feed-demo",
                        "digest": "摘要",
                        "publish_time": "2026-05-14T00:00:00+00:00",
                        "raw_payload": {},
                    }
                ],
            )
            article_id = articles_result["items"][0]["id"]
            store.upsert_article_content(article_id, {"content_text": "银行债券正文", "assets": [{"url": "https://example.test/a.png"}]})
            store.save_digest(
                {
                    "article_id": article_id,
                    "summary": "银行债券摘要",
                    "key_points": ["要点"],
                    "importance_score": 0.7,
                    "reason": "useful",
                    "model": "rules:test",
                }
            )

            rows = store.list_feed_items(limit=10, tier="core", crawl_status="content_ok")
            summary = store.feed_summary()

            self.assertEqual(rows[0]["source_name"], "银行研究")
            self.assertEqual(rows[0]["source_category"], "fixed_income")
            self.assertEqual(rows[0]["crawl_status"], "content_ok")
            self.assertEqual(rows[0]["asset_count"], 1)
            self.assertEqual(rows[0]["digest_summary"], "银行债券摘要")
            self.assertEqual(summary["sources"], 1)
            self.assertEqual(summary["articles"], 1)
            self.assertEqual(summary["articles_by_crawl_status"][0]["crawl_status"], "content_ok")

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(["--db", str(db_path), "export", "feed", "--format", "csv", "--limit", "10"])
            feed_rows = list(csv.DictReader(StringIO(stdout.getvalue())))

            self.assertEqual(exit_code, 0)
            self.assertEqual(feed_rows[0]["source_name"], "银行研究")
            self.assertEqual(feed_rows[0]["digest_summary"], "银行债券摘要")

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(["--db", str(db_path), "export", "feed-summary"])
            payload = json.loads(stdout.getvalue())

            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["sources_with_articles"], 1)

    def test_cli_run_feed_refreshes_and_exports_first_layer(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            feed_output = Path(temp_dir) / "feed-items.csv"
            summary_output = Path(temp_dir) / "feed-summary.json"
            store = Store(db_path)
            store.init()
            saved = store.save_search_candidates(
                "示例策略研究",
                [{"candidate_name": "示例策略研究", "wechat_fakeid": "fake_004", "score": 0.92, "raw_payload": {}}],
                {"items": []},
            )
            store.accept_candidate(saved["items"][0]["id"], tier="core")

            with patch("wechat_mp_feed.adapters.http.urlopen", side_effect=_fake_urlopen), redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "run",
                        "feed",
                        "--base-url",
                        "http://example.test",
                        "--feed-output",
                        str(feed_output),
                        "--summary-output",
                        str(summary_output),
                        "--full",
                        "--score-limit",
                        "1",
                        "--content-limit",
                        "1",
                        "--content-retention",
                        "all",
                        "--taxonomy",
                        str(FINANCE_TAXONOMY),
                        "--no-delay",
                    ]
                )
            payload = json.loads(stdout.getvalue())
            with feed_output.open(encoding="utf-8-sig") as handle:
                feed_rows = list(csv.DictReader(handle))
            summary = json.loads(summary_output.read_text(encoding="utf-8"))

            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["refreshed"]["sources_seen"], 1)
            self.assertEqual(payload["refreshed"]["articles_saved"], 1)
            self.assertEqual(payload["scored"]["digests_saved"], 1)
            self.assertEqual(payload["content"]["content_ok"], 1)
            self.assertEqual(payload["outputs"]["feed_rows"], 1)
            self.assertEqual(feed_rows[0]["source_name"], "示例策略研究")
            self.assertEqual(feed_rows[0]["title"], "市场观察")
            self.assertEqual(summary["articles"], 1)

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "run",
                        "feed",
                        "--skip-refresh",
                        "--feed-output",
                        str(feed_output),
                        "--summary-output",
                        str(summary_output),
                    ]
                )
            offline_payload = json.loads(stdout.getvalue())

            self.assertEqual(exit_code, 0)
            self.assertIsNone(offline_payload["service"])
            self.assertIsNone(offline_payload["refreshed"])
            self.assertEqual(offline_payload["outputs"]["feed_rows"], 1)

    def test_cli_run_feed_incremental_pages_until_known_article(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            feed_output = Path(temp_dir) / "feed-items.csv"
            summary_output = Path(temp_dir) / "feed-summary.json"
            store = Store(db_path)
            store.init()
            saved = store.save_search_candidates(
                "示例策略研究",
                [{"candidate_name": "示例策略研究", "wechat_fakeid": "fake_004", "score": 0.92, "raw_payload": {}}],
                {"items": []},
            )
            accepted = store.accept_candidate(saved["items"][0]["id"], tier="core")
            store.upsert_articles(
                accepted["source_id"],
                [
                    {
                        "title": "已知旧文章",
                        "url": "https://mp.weixin.qq.com/s/known",
                        "publish_time": "2026-05-18T00:00:00+00:00",
                    }
                ],
            )

            def fake_incremental_urlopen(request, timeout):
                from urllib.parse import parse_qs, urlparse

                url = request.full_url
                if url.endswith("/api/health"):
                    return _FakeResponse({"status": "ok"})
                if url.endswith("/api/admin/status"):
                    return _FakeResponse({"logged_in": True})
                if "/api/public/articles?" in url:
                    begin = int(parse_qs(urlparse(url).query).get("begin", ["0"])[0])
                    if begin == 0:
                        return _FakeResponse(
                            {
                                "data": {
                                    "articles": [
                                        {
                                            "title": "新增文章",
                                            "link": "https://mp.weixin.qq.com/s/new",
                                            "create_time": 1779076800,
                                        }
                                    ]
                                }
                            }
                        )
                    if begin == 1:
                        return _FakeResponse(
                            {
                                "data": {
                                    "articles": [
                                        {
                                            "title": "已知旧文章",
                                            "link": "https://mp.weixin.qq.com/s/known",
                                            "create_time": 1778990400,
                                        }
                                    ]
                                }
                            }
                        )
                    raise AssertionError(f"unexpected incremental page: {begin}")
                raise AssertionError(f"unexpected URL: {url}")

            with patch("wechat_mp_feed.adapters.http.urlopen", side_effect=fake_incremental_urlopen), redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "run",
                        "feed",
                        "--base-url",
                        "http://example.test",
                        "--refresh-mode",
                        "incremental",
                        "--count",
                        "1",
                        "--incremental-max-pages",
                        "5",
                        "--feed-output",
                        str(feed_output),
                        "--summary-output",
                        str(summary_output),
                        "--no-delay",
                    ]
                )
            payload = json.loads(stdout.getvalue())
            articles = Store(db_path).list_articles()

            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["refreshed"]["results"][0]["pages_fetched"], 2)
            self.assertEqual(payload["refreshed"]["results"][0]["stop_reason"], "boundary_url_reached")
            self.assertEqual(len(articles), 2)
            self.assertEqual(articles[0]["title"], "新增文章")

    def test_cli_run_feed_reads_config_and_exports_failures(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            feed_output = Path(temp_dir) / "feed-items.csv"
            summary_output = Path(temp_dir) / "feed-summary.json"
            failures_output = Path(temp_dir) / "feed-failures.csv"
            config_path = Path(temp_dir) / "feed-config.json"
            store = Store(db_path)
            store.init()
            saved = store.save_search_candidates(
                "银行研究",
                [{"candidate_name": "银行研究", "wechat_fakeid": "fake_003", "score": 0.92, "raw_payload": {}}],
                {"items": []},
            )
            accepted = store.accept_candidate(saved["items"][0]["id"], tier="core")
            article_result = store.upsert_articles(
                accepted["source_id"],
                [{"title": "银行财报点评", "url": "https://mp.weixin.qq.com/s/bank", "raw_payload": {}}],
            )
            store.upsert_article_content(article_result["items"][0]["id"], {}, fetch_error="deleted")
            config_path.write_text(
                json.dumps(
                    {
                        "storage": {"path": str(db_path)},
                        "feed": {
                            "skip_refresh": True,
                            "feed_output": str(feed_output),
                            "summary_output": str(summary_output),
                            "failures_output": str(failures_output),
                            "feed_limit": 10,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(["run", "feed", "--config", str(config_path)])
            payload = json.loads(stdout.getvalue())
            with failures_output.open(encoding="utf-8-sig") as handle:
                failed_rows = list(csv.DictReader(handle))

            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertIn("db", payload["config"]["applied_keys"])
            self.assertIn("skip_refresh", payload["config"]["applied_keys"])
            self.assertIn("failures_output", payload["config"]["applied_keys"])
            self.assertEqual(payload["outputs"]["failure_rows"], 1)
            self.assertEqual(failed_rows[0]["title"], "银行财报点评")
            self.assertEqual(failed_rows[0]["fetch_error"], "deleted")

    def test_cli_demo_seed_feed_creates_offline_feed_demo(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "demo.sqlite"
            work_dir = Path(temp_dir) / "demo-work"

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(["--db", str(db_path), "demo", "seed-feed", "--work-dir", str(work_dir)])
            seed_payload = json.loads(stdout.getvalue())

            with redirect_stdout(StringIO()) as stdout:
                exit_code_2 = main(["run", "feed", "--config", seed_payload["config"]])
            feed_payload = json.loads(stdout.getvalue())

            failures_path = Path(feed_payload["outputs"]["failures"])
            with failures_path.open(encoding="utf-8-sig") as handle:
                failed_rows = list(csv.DictReader(handle))

            self.assertEqual(exit_code, 0)
            self.assertEqual(exit_code_2, 0)
            self.assertTrue(seed_payload["ok"])
            self.assertEqual(seed_payload["sources"], 4)
            self.assertEqual(seed_payload["articles"], 5)
            self.assertEqual(feed_payload["summary"]["articles"], 5)
            self.assertEqual(feed_payload["outputs"]["failure_rows"], 1)
            self.assertEqual(failed_rows[0]["title"], "示例受限文章：无法抓取正文")

    def test_cli_run_agent_smoke_creates_agent_report_and_jobs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir) / "agent-smoke"

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(["run", "agent-smoke", "--work-dir", str(work_dir)])
            payload = json.loads(stdout.getvalue())

            report_path = Path(payload["outputs"]["report"])
            jobs_path = Path(payload["outputs"]["llm_jobs"])
            feed_path = Path(payload["outputs"]["feed"])
            failures_path = Path(payload["outputs"]["failures"])
            jobs = json.loads(jobs_path.read_text(encoding="utf-8"))
            report = report_path.read_text(encoding="utf-8")

            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["mode"], "offline_agent_smoke")
            self.assertEqual(payload["summary"]["articles"], 5)
            self.assertEqual(payload["outputs"]["feed_rows"], 5)
            self.assertEqual(payload["outputs"]["failure_rows"], 1)
            self.assertEqual(jobs["count"], 5)
            self.assertEqual(jobs["jobs"][0]["entity_type"], "article")
            self.assertTrue(feed_path.exists())
            self.assertTrue(failures_path.exists())
            self.assertIn("Agent Feed Smoke Report", report)
            self.assertIn("Finance Application V0", report)

    def test_normalize_article_content(self):
        content = normalize_article_content(
            {
                "data": {
                    "html": '<p>开头</p><p><img data-src="https://example.test/a.jpg" /></p><p>结尾</p>',
                    "text": "正文",
                    "images": [{"url": "https://example.test/a.jpg", "width": 100}],
                }
            }
        )

        self.assertEqual(content["content_html"], '<p>开头</p><p><img data-src="https://example.test/a.jpg" /></p><p>结尾</p>')
        self.assertEqual(content["content_text"], "正文")
        self.assertEqual(content["assets"][0]["url"], "https://example.test/a.jpg")
        self.assertEqual(
            content["content_structure"],
            [
                {"type": "text", "text": "开头"},
                {"type": "image", "url": "https://example.test/a.jpg", "alt": ""},
                {"type": "text", "text": "结尾"},
            ],
        )

    def test_normalize_article_content_reads_plain_content(self):
        content = normalize_article_content({"data": {"content": "<p>正文</p>", "plain_content": "纯文本正文"}})

        self.assertEqual(content["content_html"], "<p>正文</p>")
        self.assertEqual(content["content_text"], "纯文本正文")

    def test_store_upsert_article_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "mpfeed.sqlite")
            store.init()
            saved = store.save_search_candidates(
                "第一财经",
                [{"candidate_name": "第一财经", "wechat_fakeid": "fake_001", "score": 0.92, "raw_payload": {}}],
                {"items": []},
            )
            accepted = store.accept_candidate(saved["items"][0]["id"], tier="core")
            articles_result = store.upsert_articles(
                accepted["source_id"],
                [
                    {
                        "title": "市场观察",
                        "url": "https://mp.weixin.qq.com/s?__biz=MzA123&mid=1",
                        "raw_payload": {},
                    }
                ],
            )
            article_id = articles_result["items"][0]["id"]

            store.upsert_article_content(
                article_id,
                {
                    "content_html": '<p>开头</p><img data-src="https://example.test/a.jpg"/><p>结尾</p>',
                    "content_text": "正文",
                    "content_markdown": None,
                    "content_structure": [
                        {"type": "text", "text": "开头"},
                        {"type": "image", "url": "https://example.test/a.jpg", "alt": ""},
                        {"type": "text", "text": "结尾"},
                    ],
                    "assets": [{"asset_type": "image", "url": "https://example.test/a.jpg", "metadata": {"width": 100}}],
                },
            )
            contents = store.list_article_contents()
            assets = store.list_article_assets(article_id)
            articles = store.list_articles()

            self.assertEqual(contents[0]["article_id"], article_id)
            self.assertEqual(contents[0]["content_text"], "正文")
            self.assertEqual(contents[0]["content_structure"][1]["type"], "image")
            self.assertEqual(assets[0]["block_index"], 1)
            self.assertEqual(assets[0]["content_ref"], "block:1")
            self.assertEqual(articles[0]["crawl_status"], "content_ok")

    def test_cli_archive_assets_caches_full_archive_images(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            archive_dir = Path(temp_dir) / "asset-cache"
            store = Store(db_path)
            store.init()
            source = store.upsert_source(
                name="Archive Test",
                wechat_fakeid="fake_archive",
                status="active",
                tier="core",
                source_type="test",
            )
            article = store.upsert_articles(
                source["source_id"],
                [
                    {
                        "title": "深度研究",
                        "url": "https://mp.weixin.qq.com/s?__biz=MzA123&mid=91",
                        "raw_payload": {},
                    }
                ],
            )["items"][0]
            store.upsert_article_content(
                article["id"],
                {
                    "content_html": '<p>图表</p><img data-src="https://example.test/chart.png"/>',
                    "content_text": "图表正文",
                    "content_structure": [
                        {"type": "text", "text": "图表"},
                        {"type": "image", "url": "https://example.test/chart.png"},
                    ],
                    "assets": [{"asset_type": "image", "url": "https://example.test/chart.png", "metadata": {}}],
                },
            )
            store.save_digest(
                {
                    "article_id": article["id"],
                    "summary": "重要深度研究",
                    "key_points": ["图表"],
                    "importance_score": 0.9,
                    "reason": "archive",
                    "model": "llm:test",
                    "analysis_stage": "content",
                }
            )

            class _AssetResponse:
                headers = {"Content-Type": "image/png"}

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return None

                def read(self):
                    return b"png-bytes"

            with patch("wechat_mp_feed.assets.urlopen", return_value=_AssetResponse()), redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "archive",
                        "assets",
                        "--output-dir",
                        str(archive_dir),
                    ]
                )
            payload = json.loads(stdout.getvalue())
            refreshed = Store(db_path)
            assets = refreshed.list_article_assets(article["id"])
            articles = refreshed.list_articles()

            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["assets_cached"], 1)
            self.assertEqual(assets[0]["download_status"], "cached")
            self.assertTrue(Path(assets[0]["local_path"]).exists())
            self.assertEqual(articles[0]["archive_status"], "cached")

    def test_review_import_classified_sources_imports_reviewed_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            review_path = Path(temp_dir) / "reviewed.json"
            review_path.write_text(
                json.dumps(
                    {
                        "rows": [
                            {
                                "ocr_account": "示例债券观察",
                                "matched_account": "示例债券观察",
                                "inclusion_tier": "core_finance",
                                "primary_domain": "fixed_income",
                                "source_attribute": "kol",
                                "active_status": "活跃",
                                "confidence": 0.95,
                                "freshness_status": "ok",
                                "freshness_fakeid": "fake_bond",
                                "latest_article_content_fetch_ok": True,
                                "refreshed_latest_articles": [
                                    {
                                        "title": "可用文章",
                                        "url": "https://mp.weixin.qq.com/s/usable",
                                        "publish_time": "2025-06-30T09:30:12+00:00",
                                    }
                                ],
                            },
                            {
                                "ocr_account": "交易门",
                                "matched_account": "交易门",
                                "inclusion_tier": "core_finance",
                                "primary_domain": "opinion_kol",
                                "source_attribute": "media",
                                "active_status": "活跃",
                                "freshness_status": "ok_no_fetchable_articles",
                                "freshness_fakeid": "fake_unfetchable",
                                "latest_article_content_fetch_ok": False,
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "review",
                        "import-classified-sources",
                        str(review_path),
                    ]
                )
            payload = json.loads(stdout.getvalue())
            store = Store(db_path)

            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["imported"], 1)
            self.assertEqual(payload["skip_reasons"]["freshness_not_ok"], 1)
            self.assertEqual(store.list_sources()[0]["name"], "示例债券观察")
            self.assertEqual(store.list_sources()[0]["tier"], "core")
            self.assertEqual(store.list_classifications(entity_type="source")[0]["category"], "fixed_income")
            self.assertEqual(store.list_articles()[0]["title"], "可用文章")

    def test_review_import_classified_sources_can_allow_metadata_only_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            review_path = Path(temp_dir) / "reviewed.json"
            review_path.write_text(
                json.dumps(
                    {
                        "rows": [
                            {
                                "ocr_account": "交易门",
                                "matched_account": "交易门",
                                "freshness_candidate_name": "交易门",
                                "inclusion_tier": "finance_related",
                                "primary_domain": "macro_policy",
                                "source_attribute": "media",
                                "active_status": "活跃",
                                "freshness_status": "ok_no_fetchable_articles",
                                "freshness_fakeid": "fake_trade",
                                "latest_article_content_fetch_ok": False,
                                "refreshed_latest_articles": [
                                    {
                                        "title": "仅元数据文章",
                                        "url": "https://mp.weixin.qq.com/s/metadata-only",
                                        "publish_time": "2026-04-22T09:28:16+00:00",
                                        "content_fetch_ok": False,
                                    }
                                ],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "review",
                        "import-classified-sources",
                        "--allow-unfetchable",
                        str(review_path),
                    ]
                )
            payload = json.loads(stdout.getvalue())
            store = Store(db_path)

            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["imported"], 1)
            self.assertEqual(store.list_sources()[0]["name"], "交易门")
            self.assertEqual(store.list_articles()[0]["title"], "仅元数据文章")

    def test_review_import_classified_sources_keeps_each_reviewed_account_independent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            review_path = Path(temp_dir) / "reviewed.json"
            review_path.write_text(
                json.dumps(
                    {
                        "rows": [
                            {
                                "ocr_account": "国君金工",
                                "matched_account": "国君金工",
                                "inclusion_tier": "core_finance",
                                "primary_domain": "quant",
                                "source_attribute": "sell_side",
                                "active_status": "活跃",
                                "freshness_status": "ok",
                                "freshness_fakeid": "fake_conflict",
                                "latest_article_content_fetch_ok": True,
                                "refreshed_latest_articles": [
                                    {"title": "国君文章", "url": "https://mp.weixin.qq.com/s/guojun"}
                                ],
                            },
                            {
                                "ocr_account": "量化方程式",
                                "matched_account": "量化方程式",
                                "inclusion_tier": "core_finance",
                                "primary_domain": "quant",
                                "source_attribute": "sell_side",
                                "active_status": "活跃",
                                "freshness_status": "ok",
                                "freshness_fakeid": "fake_conflict",
                                "latest_article_content_fetch_ok": True,
                                "refreshed_latest_articles": [
                                    {"title": "量化文章", "url": "https://mp.weixin.qq.com/s/quant"}
                                ],
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "review",
                        "import-classified-sources",
                        str(review_path),
                    ]
                )
            payload = json.loads(stdout.getvalue())
            store = Store(db_path)
            sources = store.list_sources()

            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["imported"], 2)
            self.assertEqual({source["name"] for source in sources}, {"国君金工", "量化方程式"})
            self.assertEqual(sum(1 for source in sources if source["wechat_fakeid"] == "fake_conflict"), 2)
            self.assertEqual(len(store.list_articles()), 2)

    def test_review_import_classified_sources_requires_fakeid_to_match_confirmed_name(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            review_path = Path(temp_dir) / "reviewed.json"
            review_path.write_text(
                json.dumps(
                    {
                        "rows": [
                            {
                                "ocr_account": "国君金工",
                                "manual_account_name": "国君金工",
                                "matched_account": "国君金工",
                                "freshness_candidate_name": "量化方程式",
                                "inclusion_tier": "core_finance",
                                "primary_domain": "quant",
                                "source_attribute": "sell_side",
                                "active_status": "活跃",
                                "freshness_status": "ok",
                                "freshness_fakeid": "fake_quant",
                                "latest_article_content_fetch_ok": True,
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "review",
                        "import-classified-sources",
                        str(review_path),
                    ]
                )
            payload = json.loads(stdout.getvalue())
            store = Store(db_path)

            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["imported"], 0)
            self.assertEqual(payload["skip_reasons"]["fakeid_not_for_confirmed_name"], 1)
            self.assertEqual(store.list_sources(), [])

    def test_review_import_classified_sources_requires_final_matched_account(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            review_path = Path(temp_dir) / "reviewed.json"
            review_path.write_text(
                json.dumps(
                    {
                        "rows": [
                            {
                                "ocr_account": "国君金工",
                                "freshness_candidate_name": "量化方程式",
                                "inclusion_tier": "core_finance",
                                "primary_domain": "quant",
                                "source_attribute": "sell_side",
                                "active_status": "活跃",
                                "freshness_status": "ok",
                                "freshness_fakeid": "fake_quant",
                                "latest_article_content_fetch_ok": True,
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "review",
                        "import-classified-sources",
                        str(review_path),
                    ]
                )
            payload = json.loads(stdout.getvalue())
            store = Store(db_path)

            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["imported"], 0)
            self.assertEqual(payload["skip_reasons"]["missing_name"], 1)
            self.assertEqual(store.list_sources(), [])

    def test_review_validate_classified_sources_separates_review_and_blocking_issues(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            review_path = Path(temp_dir) / "reviewed.json"
            review_path.write_text(
                json.dumps(
                    {
                        "rows": [
                            {
                                "ocr_account": "国君金工",
                                "matched_account": "量化方程式",
                                "freshness_candidate_name": "量化方程式",
                                "inclusion_tier": "core_finance",
                                "primary_domain": "quant",
                                "freshness_status": "ok",
                                "freshness_fakeid": "fake_quant",
                                "latest_article_content_fetch_ok": True,
                            },
                            {
                                "ocr_account": "国君宏观研究",
                                "matched_account": "国君宏观研究",
                                "freshness_candidate_name": "宏观琦谈",
                                "inclusion_tier": "core_finance",
                                "primary_domain": "macro_policy",
                                "freshness_status": "ok",
                                "freshness_fakeid": "fake_macro",
                                "latest_article_content_fetch_ok": True,
                            },
                            {
                                "ocr_account": "交易门",
                                "matched_account": "交易门",
                                "freshness_candidate_name": "交易门",
                                "inclusion_tier": "finance_related",
                                "primary_domain": "macro_policy",
                                "freshness_status": "ok_no_fetchable_articles",
                                "freshness_fakeid": "fake_trade",
                                "latest_article_content_fetch_ok": False,
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "review",
                        "validate-classified-sources",
                        str(review_path),
                    ]
                )
            payload = json.loads(stdout.getvalue())

            self.assertEqual(exit_code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["ready_rows"], 1)
            self.assertEqual(payload["review_rows"], 1)
            self.assertEqual(payload["blocking_rows"], 2)
            self.assertEqual(payload["issue_counts"]["matched_differs_from_ocr"], 1)
            self.assertEqual(payload["issue_counts"]["fakeid_not_for_matched_account"], 1)

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "review",
                        "validate-classified-sources",
                        "--allow-unfetchable",
                        str(review_path),
                    ]
                )
            payload = json.loads(stdout.getvalue())

            self.assertEqual(exit_code, 1)
            self.assertEqual(payload["issue_counts"]["metadata_only_no_fetchable_content"], 1)

    def test_tier_policy_defaults(self):
        core = tier_policy("core")
        normal = tier_policy("normal")
        long_tail = tier_policy("long_tail")

        self.assertGreater(core.article_count, long_tail.article_count)
        self.assertLess(core.delay_min_seconds, long_tail.delay_min_seconds)
        self.assertGreater(normal.max_sources, core.max_sources)

    def test_retry_policy(self):
        self.assertTrue(retryable_status({"ok": False, "status": 429}))
        self.assertTrue(retryable_status({"ok": False, "status": 500}))
        self.assertTrue(retryable_status({"ok": False, "status": 0, "body": {"error": "Network is unreachable"}}))
        self.assertTrue(retryable_status({"ok": False, "status": 200, "body": {"error": "Rate limited: 文章获取过快，请3秒后重试"}}))
        self.assertFalse(retryable_status({"ok": False, "status": 404}))

    def test_with_retries(self):
        calls = []

        def call():
            calls.append(1)
            if len(calls) == 1:
                return {"ok": False, "status": 500}
            return {"ok": True, "status": 200}

        with patch("wechat_mp_feed.cli.time.sleep") as sleep:
            result = with_retries(call, retries=2, backoff_seconds=0.1)

        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once()

    def test_with_retries_converts_network_exception_to_retryable_payload(self):
        calls = []

        def call():
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionError("HTTP request failed: [Errno 101] Network is unreachable")
            return {"ok": True, "status": 200}

        with patch("wechat_mp_feed.cli.time.sleep"):
            result = with_retries(call, retries=1, backoff_seconds=0.1)

        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 2)

    def test_with_retries_returns_network_exception_payload_after_retries(self):
        def call():
            raise ConnectionError("HTTP request failed: [Errno 101] Network is unreachable")

        with patch("wechat_mp_feed.cli.time.sleep"):
            result = with_retries(call, retries=1, backoff_seconds=0.1)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 0)
        self.assertEqual(result["body"]["exception_type"], "ConnectionError")

    def test_with_retries_honors_body_retry_after_seconds(self):
        calls = []

        def call():
            calls.append(1)
            if len(calls) == 1:
                return {"ok": False, "status": 200, "body": {"error": "Rate limited: 请求过于频繁，请5秒后重试"}}
            return {"ok": True, "status": 200}

        with patch("wechat_mp_feed.cli.time.sleep") as sleep:
            result = with_retries(call, retries=2, backoff_seconds=0.1)

        self.assertTrue(result["ok"])
        sleep.assert_called_once_with(5.8)

    def test_fetch_content_queue_retries_same_articles_across_passes(self):
        class FakeAdapter:
            def __init__(self):
                self.calls = 0

            def fetch_article(self, url):
                self.calls += 1
                if self.calls == 1:
                    return {"ok": False, "status": 200, "body": {"error": "Rate limited: 请求过于频繁，请1秒后重试"}}
                return {"ok": True, "status": 200, "body": {"data": {"plain_content": "正文"}}}

        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "mpfeed.sqlite")
            store.init()
            saved = store.save_search_candidates(
                "银行研究",
                [{"candidate_name": "银行研究", "wechat_fakeid": "fake_003", "score": 0.92, "raw_payload": {}}],
                {"items": []},
            )
            accepted = store.accept_candidate(saved["items"][0]["id"], tier="core")
            store.upsert_articles(
                accepted["source_id"],
                [{"title": "银行财报点评", "url": "https://mp.weixin.qq.com/s/bank", "raw_payload": {}}],
            )

            result = fetch_content_queue(
                store=store,
                adapter=FakeAdapter(),
                limit=10,
                retention_levels=None,
                retries=0,
                backoff_seconds=0.1,
                delay_min=0,
                delay_max=0,
                passes=2,
                pass_cooldown_seconds=0,
                no_delay=True,
            )

            self.assertEqual(result["articles_seen"], 1)
            self.assertEqual(result["attempts"], 2)
            self.assertEqual(result["content_ok"], 1)
            self.assertEqual(result["content_failed"], 0)
            self.assertEqual(result["sample_results"][0]["retry_after_seconds"], 1.8)
            self.assertEqual([item["remaining"] for item in result["passes"]], [1, 0])

    def test_content_fetch_queue_prioritizes_importance_before_recency(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "mpfeed.sqlite")
            store.init()
            saved = store.save_search_candidates(
                "策略研究",
                [{"candidate_name": "策略研究", "wechat_fakeid": "fake_005", "score": 0.92, "raw_payload": {}}],
                {"items": []},
            )
            accepted = store.accept_candidate(saved["items"][0]["id"], tier="core")
            inserted = store.upsert_articles(
                accepted["source_id"],
                [
                    {
                        "title": "旧但重要的配置框架",
                        "url": "https://mp.weixin.qq.com/s/old-important",
                        "publish_time": "2026-05-10T00:00:00+00:00",
                        "raw_payload": {},
                    },
                    {
                        "title": "新但一般的日评",
                        "url": "https://mp.weixin.qq.com/s/new-normal",
                        "publish_time": "2026-05-19T00:00:00+00:00",
                        "raw_payload": {},
                    },
                ],
            )
            old_important_id = inserted["items"][0]["id"]
            new_normal_id = inserted["items"][1]["id"]
            store.save_digest(
                {
                    "article_id": old_important_id,
                    "summary": "旧文重要",
                    "key_points": ["框架"],
                    "importance_score": 0.82,
                    "reason": "framework",
                    "model": "rules:test",
                }
            )
            store.save_digest(
                {
                    "article_id": new_normal_id,
                    "summary": "新文一般",
                    "key_points": ["日评"],
                    "importance_score": 0.5,
                    "reason": "commentary",
                    "model": "rules:test",
                }
            )

            queue = store.list_articles_for_content_fetch(limit=2, retention_levels=("content", "full_archive"))

        self.assertEqual([row["id"] for row in queue], [old_important_id, new_normal_id])
        self.assertEqual(queue[0]["importance_score"], 0.82)

    def test_adapter_error_message_preserves_body_error(self):
        self.assertEqual(
            adapter_error_message({"ok": False, "status": 200, "body": {"error": "Rate limited: 文章获取过快，请3秒后重试"}}),
            "Rate limited: 文章获取过快，请3秒后重试",
        )
        self.assertEqual(adapter_error_message({"ok": False, "status": 502, "body": {}}), "adapter_status_502")

    def test_cli_source_agent_safe_helpers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            store = Store(db_path)
            store.init()
            source = store.upsert_source(
                name="Agent API Test",
                wechat_fakeid="fake_agent_api",
                status="active",
                tier="normal",
                source_type="test",
            )

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(["--db", str(db_path), "source", "status", "--name", "Agent API Test"])
            status_payload = json.loads(stdout.getvalue())

            self.assertEqual(exit_code, 0)
            self.assertEqual(status_payload["status"], "source_status")
            self.assertEqual(status_payload["source"]["id"], source["source_id"])

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(["source", "taxonomy"])
            taxonomy_payload = json.loads(stdout.getvalue())
            category_ids = {item["id"] for item in taxonomy_payload["taxonomy"]["source_categories"]}

            self.assertEqual(exit_code, 0)
            self.assertIn("industrial", category_ids)
            self.assertNotIn("technology_business", category_ids)

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "source",
                        "confirm-classification",
                        "--name",
                        "Agent API Test",
                        "--category",
                        "industrial",
                        "--source-attribute",
                        "kol",
                        "--tags",
                        "ai",
                        "--confidence",
                        "0.78",
                    ]
                )
            confirmed = json.loads(stdout.getvalue())

            self.assertEqual(exit_code, 0)
            self.assertEqual(confirmed["status"], "classification_confirmed")
            self.assertEqual(confirmed["readback"]["category"], "industrial")
            self.assertEqual(confirmed["readback"]["tags"], ["kol", "ai"])

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "source",
                        "confirm-classification",
                        "--name",
                        "Agent API Test",
                        "--category",
                        "technology_business",
                        "--source-attribute",
                        "product_observer",
                        "--tags",
                        "ai_tool",
                    ]
                )
            rejected = json.loads(stdout.getvalue())
            readback = Store(db_path).list_classifications(entity_type="source", taxonomy="finance")

            self.assertEqual(exit_code, 2)
            self.assertEqual(rejected["status"], "classification_validation_failed")
            self.assertIn("invalid_category:technology_business", rejected["validation"]["errors"])
            self.assertEqual(len(readback), 1)
            self.assertEqual(readback[0]["category"], "industrial")

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "source",
                        "refresh-latest",
                        "--name",
                        "Agent API Test",
                        "--count",
                        "0",
                    ]
                )
            refreshed = json.loads(stdout.getvalue())

            self.assertEqual(exit_code, 0)
            self.assertEqual(refreshed["latest_articles"]["status"], "skipped_by_article_count")

    def test_rule_classification_and_digest_flow(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "mpfeed.sqlite")
            store.init()
            saved = store.save_search_candidates(
                "银行研究",
                [{"candidate_name": "银行研究", "wechat_fakeid": "fake_003", "score": 0.92, "raw_payload": {}}],
                {"items": []},
            )
            accepted = store.accept_candidate(saved["items"][0]["id"], tier="core")
            articles_result = store.upsert_articles(
                accepted["source_id"],
                [
                    {
                        "title": "银行财报点评：息差企稳",
                        "url": "https://mp.weixin.qq.com/s?__biz=MzA123&mid=2",
                        "digest": "银行板块业绩和息差出现改善迹象。",
                        "raw_payload": {},
                    }
                ],
            )
            article_id = articles_result["items"][0]["id"]
            store.upsert_article_content(
                article_id,
                {
                    "content_text": "银行财报点评显示息差企稳，资产质量边际改善。关注银行板块估值修复。",
                    "assets": [],
                },
            )
            taxonomy = load_taxonomy(FINANCE_TAXONOMY)
            article = store.list_articles_with_content()[0]
            classification = classify_article(article, taxonomy)
            digest = generate_article_digest(article, classification, taxonomy)
            store.save_classification(classification)
            store.save_digest(digest)

            classifications = store.list_classifications(entity_type="article")
            digests = store.list_digests()

            self.assertEqual(classifications[0]["category"], "earnings_review")
            self.assertIn("banks", classifications[0]["tags"])
            self.assertEqual(digests[0]["article_id"], article_id)
            self.assertGreater(digests[0]["importance_score"], 0.5)
            self.assertIn("银行", digests[0]["summary"])

    def test_cli_llm_export_and_import_results(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            jobs_path = Path(temp_dir) / "llm-jobs.json"
            results_path = Path(temp_dir) / "llm-results.json"
            store = Store(db_path)
            store.init()
            saved = store.save_search_candidates(
                "银行研究",
                [{"candidate_name": "银行研究", "wechat_fakeid": "fake_llm", "score": 0.95, "raw_payload": {}}],
                {"items": []},
            )
            accepted = store.accept_candidate(saved["items"][0]["id"], tier="normal")
            articles_result = store.upsert_articles(
                accepted["source_id"],
                [
                    {
                        "title": "银行财报点评：息差企稳",
                        "url": "https://mp.weixin.qq.com/s?__biz=MzA123&mid=3",
                        "digest": "银行板块业绩和息差出现改善迹象。",
                        "raw_payload": {},
                    }
                ],
            )
            article_id = articles_result["items"][0]["id"]
            store.upsert_article_content(article_id, {"content_text": "银行资产质量边际改善。", "assets": []})

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "llm",
                        "export-jobs",
                        "--entity-type",
                        "all",
                        "--output",
                        str(jobs_path),
                    ]
                )
            exported = json.loads(stdout.getvalue())
            jobs = json.loads(jobs_path.read_text(encoding="utf-8"))

            self.assertEqual(exit_code, 0)
            self.assertEqual(exported["count"], 2)
            self.assertEqual(jobs["count"], 2)
            self.assertEqual({job["entity_type"] for job in jobs["jobs"]}, {"source", "article"})
            source_job = next(job for job in jobs["jobs"] if job["entity_type"] == "source")
            self.assertEqual(source_job["source"]["latest_articles"][0]["title"], "银行财报点评：息差企稳")

            results_path.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "job_id": f"source:{accepted['source_id']}",
                                "entity_type": "source",
                                "entity_id": accepted["source_id"],
                                "classification": {
                                    "category": "company_research",
                                    "tags": ["banks"],
                                    "confidence": 0.88,
                                    "method": "llm:test",
                                },
                                "source_attribute": "sell_side",
                                "source_update": {"status": "active", "tier": "core"},
                            },
                            {
                                "job_id": f"article:{article_id}",
                                "entity_type": "article",
                                "entity_id": article_id,
                                "classification": {
                                    "category": "earnings_review",
                                    "tags": ["banks"],
                                    "confidence": 0.9,
                                    "method": "llm:test",
                                },
                                "digest": {
                                    "summary": "银行业绩与息差改善，值得跟踪。",
                                    "key_points": ["息差企稳", "资产质量改善"],
                                    "importance_score": 0.82,
                                    "score_breakdown": {
                                        "research_depth": 0.9,
                                        "decision_value": 0.95,
                                        "strategy_reproducibility": 0.8,
                                        "timeliness": 0.9,
                                        "source_relevance": 0.95,
                                        "originality": 0.75,
                                        "evidence_quality": 0.9,
                                        "noise_penalty": 0,
                                    },
                                    "application_targets": ["daily_digest", "weekly_report", "ignore", "unknown_target"],
                                    "reason": "金融研究高相关。",
                                    "model": "llm:test",
                                },
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(["--db", str(db_path), "llm", "import-results", str(results_path)])
            imported = json.loads(stdout.getvalue())
            refreshed = Store(db_path)

            self.assertEqual(exit_code, 0)
            self.assertEqual(imported["classifications_saved"], 2)
            self.assertEqual(imported["digests_saved"], 1)
            self.assertEqual(refreshed.list_sources()[0]["tier"], "core")
            self.assertIn("sell_side", refreshed.list_classifications(entity_type="source")[0]["tags"])
            self.assertEqual(refreshed.list_digests()[0]["summary"], "银行业绩与息差改善，值得跟踪。")
            self.assertEqual(refreshed.list_digests()[0]["importance_score"], 0.872)
            self.assertEqual(refreshed.list_digests()[0]["score_breakdown"]["decision_value"], 0.95)
            self.assertEqual(refreshed.list_digests()[0]["score_breakdown"]["model_importance_score"], 0.82)
            self.assertEqual(refreshed.list_digests()[0]["score_breakdown"]["computed_importance_score"], 0.872)
            self.assertEqual(refreshed.list_digests()[0]["application_targets"], ["daily_digest", "weekly_report", "ignore"])
            self.assertEqual(refreshed.list_articles()[0]["retention_level"], "full_archive")
            self.assertEqual(refreshed.list_articles()[0]["archive_status"], "pending")

    def test_llm_article_jobs_can_export_metadata_before_content_fetch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            store = Store(db_path)
            store.init()
            saved = store.save_search_candidates(
                "量化研究",
                [{"candidate_name": "量化研究", "wechat_fakeid": "fake_quant", "score": 0.95, "raw_payload": {}}],
                {"items": []},
            )
            accepted = store.accept_candidate(saved["items"][0]["id"], tier="normal")
            store.upsert_articles(
                accepted["source_id"],
                [
                    {
                        "title": "行业轮动周报：风格继续扩散",
                        "url": "https://mp.weixin.qq.com/s?__biz=MzA123&mid=31",
                        "digest": "行业轮动和市场择时观点。",
                        "raw_payload": {},
                    }
                ],
            )

            payload = build_llm_jobs(
                store=store,
                taxonomy=load_taxonomy(FINANCE_TAXONOMY),
                entity_type="article",
                article_content_scope="metadata",
            )

            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["jobs"][0]["article"]["title"], "行业轮动周报：风格继续扩散")
            self.assertNotIn("content_excerpt", payload["jobs"][0]["article"])
            self.assertIn("score_breakdown", payload["jobs"][0]["expected_result"]["digest.required"])
            self.assertIn("article_score_rubric", payload)
            self.assertIn("article_metadata_score_rubric", payload)
            self.assertIn("article_application_targets", payload)

            future_payload = build_llm_jobs(
                store=store,
                taxonomy=load_taxonomy(FINANCE_TAXONOMY),
                entity_type="article",
                article_content_scope="metadata",
                article_updated_after="2999-01-01T00:00:00+00:00",
            )

            self.assertEqual(future_payload["count"], 0)

    def test_llm_article_jobs_filter_by_publish_time_retention_and_stage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            store = Store(db_path)
            store.init()
            saved = store.save_search_candidates(
                "量化研究",
                [{"candidate_name": "量化研究", "wechat_fakeid": "fake_quant_filter", "score": 0.95, "raw_payload": {}}],
                {"items": []},
            )
            accepted = store.accept_candidate(saved["items"][0]["id"], tier="normal")
            inserted = store.upsert_articles(
                accepted["source_id"],
                [
                    {
                        "title": "行业轮动周报：风格继续扩散",
                        "url": "https://mp.weixin.qq.com/s/filter-a",
                        "digest": "行业轮动和市场择时观点。",
                        "publish_time": "2026-05-12T00:00:00+00:00",
                        "raw_payload": {},
                    },
                    {
                        "title": "较晚发布的策略观点",
                        "url": "https://mp.weixin.qq.com/s/filter-b",
                        "digest": "策略观点。",
                        "publish_time": "2026-05-20T00:00:00+00:00",
                        "raw_payload": {},
                    },
                ],
            )
            for item in inserted["items"]:
                store.save_digest(
                    {
                        "article_id": item["id"],
                        "summary": "metadata score",
                        "key_points": ["score"],
                        "importance_score": 0.8,
                        "score_breakdown": {
                            "title_research_signal": 1.0,
                            "source_relevance": 1.0,
                            "abstract_specificity": 1.0,
                            "followup_potential": 1.0,
                            "freshness": 1.0,
                            "evidence_quality": 1.0,
                            "noise_penalty": 0.0,
                        },
                        "reason": "metadata",
                        "model": "llm:test",
                        "analysis_stage": "metadata",
                    }
                )

            payload = build_llm_jobs(
                store=store,
                taxonomy=load_taxonomy(FINANCE_TAXONOMY),
                entity_type="article",
                article_content_scope="metadata",
                article_published_after="2026-05-11T00:00:00+00:00",
                article_published_before="2026-05-18T00:00:00+00:00",
                article_retention_levels=("content",),
                article_analysis_stage="metadata",
            )

            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["jobs"][0]["analysis_stage"], "metadata")
            self.assertEqual(payload["jobs"][0]["article"]["title"], "行业轮动周报：风格继续扩散")
            self.assertEqual(payload["article_job_filter"]["retention_levels"], ["content"])
            self.assertEqual(payload["article_job_filter"]["published_before"], "2026-05-18T00:00:00+00:00")

    def test_list_digests_filters_by_application_target_score_and_stage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = Store(Path(temp_dir) / "mpfeed.sqlite")
            store.init()
            saved = store.save_search_candidates(
                "策略研究",
                [{"candidate_name": "策略研究", "wechat_fakeid": "fake_digest_filter", "score": 0.95, "raw_payload": {}}],
                {"items": []},
            )
            accepted = store.accept_candidate(saved["items"][0]["id"], tier="core")
            inserted = store.upsert_articles(
                accepted["source_id"],
                [
                    {"title": "周报材料", "url": "https://mp.weixin.qq.com/s/digest-weekly", "raw_payload": {}},
                    {"title": "低分材料", "url": "https://mp.weixin.qq.com/s/digest-low", "raw_payload": {}},
                ],
            )
            store.save_digest(
                {
                    "article_id": inserted["items"][0]["id"],
                    "summary": "高质量周报材料",
                    "key_points": ["策略"],
                    "importance_score": 0.82,
                    "application_targets": ["weekly_report", "strategy_backlog"],
                    "reason": "useful",
                    "model": "llm:test",
                    "analysis_stage": "content",
                }
            )
            store.save_digest(
                {
                    "article_id": inserted["items"][1]["id"],
                    "summary": "低分日常材料",
                    "key_points": ["日常"],
                    "importance_score": 0.3,
                    "application_targets": ["weekly_report"],
                    "reason": "routine",
                    "model": "llm:test",
                    "analysis_stage": "content",
                }
            )

            rows = store.list_digests(application_target="weekly_report", min_score=0.6, analysis_stage="content")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "周报材料")
        self.assertEqual(rows[0]["application_targets"], ["weekly_report", "strategy_backlog"])

    def test_export_digest_context_includes_article_content_and_assets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            store = Store(db_path)
            store.init()
            saved = store.save_search_candidates(
                "策略研究",
                [{"candidate_name": "策略研究", "wechat_fakeid": "fake_digest_context", "score": 0.95, "raw_payload": {}}],
                {"items": []},
            )
            accepted = store.accept_candidate(saved["items"][0]["id"], tier="core")
            inserted = store.upsert_articles(
                accepted["source_id"],
                [{"title": "行业轮动深度", "url": "https://mp.weixin.qq.com/s/context", "raw_payload": {}}],
            )
            article_id = inserted["items"][0]["id"]
            store.upsert_article_content(
                article_id,
                {
                    "content_text": "原文正文，包含行业轮动观点。",
                    "content_markdown": "原文正文，包含行业轮动观点。",
                    "content_structure": [
                        {"type": "paragraph", "text": "原文正文，包含行业轮动观点。"},
                        {"type": "image", "url": "https://img.example/chart.png"},
                    ],
                    "assets": [
                        {
                            "asset_type": "image",
                            "url": "https://img.example/chart.png",
                            "block_index": 1,
                            "content_ref": "block:1",
                        }
                    ],
                },
            )
            store.save_digest(
                {
                    "article_id": article_id,
                    "summary": "行业轮动摘要",
                    "key_points": ["轮动"],
                    "importance_score": 0.81,
                    "application_targets": ["weekly_report"],
                    "reason": "deep research",
                    "model": "llm:test",
                    "analysis_stage": "content",
                }
            )

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "export",
                        "digest-context",
                        "--application-target",
                        "weekly_report",
                        "--analysis-stage",
                        "content",
                        "--min-score",
                        "0.6",
                    ]
                )
            rows = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["digest"]["summary"], "行业轮动摘要")
        self.assertEqual(rows[0]["article"]["content_text"], "原文正文，包含行业轮动观点。")
        self.assertEqual(rows[0]["article"]["content_structure"][1]["type"], "image")
        self.assertEqual(rows[0]["assets"][0]["content_ref"], "block:1")

    def test_source_set_status_updates_source_through_cli(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            store = Store(db_path)
            store.init()
            saved = store.save_search_candidates(
                "科技观察",
                [{"candidate_name": "科技观察", "wechat_fakeid": "fake_status", "score": 0.95, "raw_payload": {}}],
                {"items": []},
            )
            accepted = store.accept_candidate(saved["items"][0]["id"], tier="normal")

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "source",
                        "set-status",
                        "--source-id",
                        accepted["source_id"],
                        "--status",
                        "inactive",
                    ]
                )
            payload = json.loads(stdout.getvalue())
            source = Store(db_path).get_source(accepted["source_id"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "source_status_updated")
        self.assertEqual(payload["source"]["status"], "inactive")
        self.assertEqual(source["status"], "inactive")

    def test_run_llm_feed_exports_metadata_jobs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            work_dir = Path(temp_dir) / "feed-llm"
            store = Store(db_path)
            store.init()
            saved = store.save_search_candidates(
                "策略研究",
                [{"candidate_name": "策略研究", "wechat_fakeid": "fake_llm_feed", "score": 0.95, "raw_payload": {}}],
                {"items": []},
            )
            accepted = store.accept_candidate(saved["items"][0]["id"], tier="core")
            store.upsert_articles(
                accepted["source_id"],
                [
                    {
                        "title": "行业轮动周报",
                        "url": "https://mp.weixin.qq.com/s/llm-feed",
                        "digest": "行业轮动观点。",
                        "publish_time": "2026-05-12T00:00:00+00:00",
                        "raw_payload": {},
                    }
                ],
            )

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "run",
                        "llm-feed",
                        "--work-dir",
                        str(work_dir),
                        "--published-after",
                        "2026-05-11T00:00:00+00:00",
                        "--published-before",
                        "2026-05-17T23:59:59+00:00",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            jobs_path = Path(payload["outputs"]["metadata_jobs"])
            jobs = json.loads(jobs_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["outputs"]["metadata_jobs_count"], 1)
        self.assertEqual(jobs["jobs"][0]["analysis_stage"], "metadata")
        self.assertIn("--metadata-results", payload["next_steps"][0])

    def test_llm_import_rejects_unknown_taxonomy_ids(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            results_path = Path(temp_dir) / "llm-results.json"
            store = Store(db_path)
            store.init()
            saved = store.save_search_candidates(
                "科技观察",
                [{"candidate_name": "科技观察", "wechat_fakeid": "fake_tech", "score": 0.95, "raw_payload": {}}],
                {"items": []},
            )
            accepted = store.accept_candidate(saved["items"][0]["id"], tier="normal")

            results_path.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "entity_type": "source",
                                "entity_id": accepted["source_id"],
                                "classification": {
                                    "category": "technology_business",
                                    "tags": ["ai_tool"],
                                    "confidence": 0.82,
                                    "method": "llm:test",
                                },
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(["--db", str(db_path), "llm", "import-results", str(results_path)])
            imported = json.loads(stdout.getvalue())

            self.assertEqual(exit_code, 0)
            self.assertEqual(imported["classifications_saved"], 0)
            self.assertEqual(imported["skipped"][0]["reason"], "invalid_category:technology_business")
            self.assertEqual(Store(db_path).list_classifications(), [])

    def test_llm_source_import_requires_user_confirmation_before_write(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            results_path = Path(temp_dir) / "llm-results.json"
            store = Store(db_path)
            store.init()
            saved = store.save_search_candidates(
                "示例量化",
                [{"candidate_name": "示例量化", "wechat_fakeid": "fake_quant_source", "score": 0.95, "raw_payload": {}}],
                {"items": []},
            )
            accepted = store.accept_candidate(saved["items"][0]["id"], tier="normal")

            results_path.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "entity_type": "source",
                                "entity_id": accepted["source_id"],
                                "classification": {
                                    "category": "quant",
                                    "tags": ["sell_side"],
                                    "confidence": 0.93,
                                    "method": "llm:test",
                                },
                                "source_update": {"status": "active", "tier": "core"},
                                "requires_user_confirmation": True,
                                "taxonomy_suggestions": [
                                    {
                                        "type": "tag",
                                        "suggested_id": "style_rotation",
                                        "name_zh": "风格轮动",
                                        "reason": "示例建议",
                                    }
                                ],
                                "reason": "首次分类需要用户确认。",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(["--db", str(db_path), "llm", "import-results", str(results_path)])
            imported = json.loads(stdout.getvalue())
            refreshed = Store(db_path)

            self.assertEqual(exit_code, 0)
            self.assertEqual(imported["classifications_saved"], 0)
            self.assertEqual(imported["source_updates"], 0)
            self.assertEqual(imported["source_classification_reviews_saved"], 1)
            self.assertEqual(refreshed.list_classifications(), [])
            reviews = refreshed.list_source_classification_reviews()
            self.assertEqual(reviews[0]["category"], "quant")
            self.assertEqual(reviews[0]["taxonomy_suggestions"][0]["suggested_id"], "style_rotation")
            self.assertEqual(refreshed.list_sources()[0]["tier"], "normal")

    def test_llm_source_jobs_can_filter_one_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            store = Store(db_path)
            store.init()
            first = store.upsert_source(name="示例量化", wechat_fakeid="fake_quant", status="active", tier="normal")
            second = store.upsert_source(name="示例宏观", wechat_fakeid="fake_macro", status="active", tier="normal")
            store.upsert_articles(
                first["source_id"],
                [{"title": "行业轮动周报", "url": "https://mp.weixin.qq.com/s/quant", "digest": "量化策略", "raw_payload": {}}],
            )
            store.upsert_articles(
                second["source_id"],
                [{"title": "宏观周报", "url": "https://mp.weixin.qq.com/s/macro", "digest": "宏观策略", "raw_payload": {}}],
            )

            payload = build_llm_jobs(
                store=store,
                taxonomy=load_taxonomy(FINANCE_TAXONOMY),
                entity_type="source",
                source_id=first["source_id"],
                source_article_limit=8,
            )

            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["jobs"][0]["entity_id"], first["source_id"])
            self.assertEqual(payload["jobs"][0]["source"]["name"], "示例量化")
            self.assertEqual(payload["jobs"][0]["source"]["latest_articles"][0]["title"], "行业轮动周报")

    def test_cli_llm_onboarding_jobs_accept_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            jobs_path = Path(temp_dir) / "onboarding-jobs.json"
            results_path = Path(temp_dir) / "onboarding-results.json"
            store = Store(db_path)
            store.init()
            imported = store.import_source_rows("recording", [{"raw_name": "示例策略研究"}])
            saved = store.save_candidates_for_import(
                imported["items"][0]["id"],
                [
                    {
                        "candidate_name": "示例策略研究",
                        "wechat_fakeid": "fake_onboarding",
                        "intro": "策略研究与市场观点",
                        "score": 0.94,
                        "raw_payload": {},
                    }
                ],
                {"items": []},
            )
            candidate_id = saved["items"][0]["id"]
            store.save_candidate_article_probe(
                candidate_id,
                [
                    {
                        "title": "市场策略周报",
                        "url": "https://mp.weixin.qq.com/s/onboarding",
                        "digest": "权益市场策略观点。",
                        "publish_time": "2026-05-04T00:00:00+00:00",
                    }
                ],
                {"items": []},
            )

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "llm",
                        "export-onboarding-jobs",
                        "--source-type",
                        "recording",
                        "--taxonomy",
                        str(FINANCE_TAXONOMY),
                        "--output",
                        str(jobs_path),
                    ]
                )
            exported = json.loads(stdout.getvalue())
            jobs = json.loads(jobs_path.read_text(encoding="utf-8"))

            self.assertEqual(exit_code, 0)
            self.assertEqual(exported["count"], 1)
            self.assertEqual(jobs["jobs"][0]["entity_type"], "source_onboarding")
            self.assertEqual(jobs["jobs"][0]["candidates"][0]["latest_articles"][0]["title"], "市场策略周报")

            results_path.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "job_id": f"onboarding:{imported['items'][0]['id']}",
                                "entity_type": "source_onboarding",
                                "entity_id": imported["items"][0]["id"],
                                "action": "accept_source",
                                "selected_candidate_id": candidate_id,
                                "review_category": "finance_research",
                                "requires_user_confirmation": False,
                                "classification": {
                                    "category": "strategy",
                                    "tags": ["a_share"],
                                    "confidence": 0.86,
                                    "method": "llm:test",
                                },
                                "source_update": {"status": "active", "tier": "core"},
                                "reason": "名称精确匹配，简介和最新文章均为策略研究。",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(["--db", str(db_path), "llm", "import-results", str(results_path)])
            imported_results = json.loads(stdout.getvalue())
            refreshed = Store(db_path)
            sources = refreshed.list_sources()
            classifications = refreshed.list_classifications(entity_type="source")
            imports = refreshed.list_imports(source_type="recording")

            self.assertEqual(exit_code, 0)
            self.assertEqual(imported_results["source_updates"], 1)
            self.assertEqual(sources[0]["name"], "示例策略研究")
            self.assertEqual(sources[0]["tier"], "core")
            self.assertEqual(classifications[0]["category"], "strategy")
            self.assertEqual(imports[0]["raw_payload"]["llm_onboarding_review"]["review_category"], "finance_research")
            self.assertEqual(imports[0]["raw_payload"]["llm_onboarding_review"]["method"], "llm:test")

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "export",
                        "onboarding",
                        "--view",
                        "compact",
                        "--source-type",
                        "recording",
                        "--taxonomy",
                        str(FINANCE_TAXONOMY),
                    ]
                )
            rows = list(csv.DictReader(StringIO(stdout.getvalue())))

            self.assertEqual(exit_code, 0)
            self.assertEqual(rows[0]["account_category"], "金融投研")
            self.assertEqual(rows[0]["system_decision"], "accept_source")
            self.assertEqual(rows[0]["requires_manual_confirmation"], "False")
            self.assertEqual(rows[0]["notes"], "名称精确匹配，简介和最新文章均为策略研究。")

    def test_llm_onboarding_ignore_archives_previously_accepted_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mpfeed.sqlite"
            results_path = Path(temp_dir) / "onboarding-results.json"
            store = Store(db_path)
            store.init()
            imported = store.import_source_rows("recording", [{"raw_name": "示例体育赛事"}])
            saved = store.save_candidates_for_import(
                imported["items"][0]["id"],
                [
                    {
                        "candidate_name": "示例体育赛事",
                        "wechat_fakeid": "fake_tennis",
                        "intro": "网球赛事报道",
                        "score": 0.94,
                        "raw_payload": {},
                    }
                ],
                {"items": []},
            )
            store.accept_candidate(saved["items"][0]["id"], tier="normal")

            results_path.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "job_id": f"onboarding:{imported['items'][0]['id']}",
                                "entity_type": "source_onboarding",
                                "entity_id": imported["items"][0]["id"],
                                "action": "ignore_non_finance",
                                "review_category": "non_finance",
                                "requires_user_confirmation": False,
                                "reason": "网球赛事账号，不属于金融内容管理。",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(["--db", str(db_path), "llm", "import-results", str(results_path)])
            imported_results = json.loads(stdout.getvalue())
            refreshed = Store(db_path)

            self.assertEqual(exit_code, 0)
            self.assertEqual(imported_results["source_updates"], 1)
            self.assertEqual(refreshed.list_imports(source_type="recording")[0]["status"], "ignored")
            self.assertEqual(refreshed.list_sources()[0]["status"], "archived")
            self.assertEqual(refreshed.list_candidates(decision=None)[0]["decision"], "reject")

            with redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "--db",
                        str(db_path),
                        "export",
                        "onboarding",
                        "--view",
                        "compact",
                        "--source-type",
                        "recording",
                        "--taxonomy",
                        str(FINANCE_TAXONOMY),
                    ]
                )
            rows = list(csv.DictReader(StringIO(stdout.getvalue())))

            self.assertEqual(exit_code, 0)
            self.assertEqual(rows[0]["system_decision"], "ignore_non_finance")
            self.assertEqual(rows[0]["account_category"], "非金融")
            self.assertEqual(rows[0]["requires_manual_confirmation"], "False")


class _FakeResponse:
    status = 200

    def __init__(self, body):
        import json

        self._body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self._body


def _fake_urlopen(request, timeout):
    url = request.full_url
    from urllib.parse import unquote

    decoded_url = unquote(url)
    if url.endswith("/api/health"):
        return _FakeResponse({"status": "ok"})
    if url.endswith("/api/admin/status"):
        return _FakeResponse({"logged_in": True})
    if "/api/public/searchbiz?" in url:
        if "示例策略研究" in decoded_url:
            return _FakeResponse(
                {
                    "items": [
                        {
                            "nickname": "示例策略研究",
                            "fakeid": "fake_004",
                            "signature": "策略研究",
                        }
                    ]
                }
            )
        if "王德伦策略与投资" in decoded_url:
            return _FakeResponse(
                {
                    "items": [
                        {
                            "nickname": "王德伦策略与投资",
                            "fakeid": "fake_manual_wdl",
                            "signature": "策略研究与投资观点",
                        }
                    ]
                }
            )
        return _FakeResponse({"items": []})
    if "/api/public/articles?" in url:
        return _FakeResponse(
            {
                "data": {
                    "articles": [
                        {
                            "title": "市场观察",
                            "link": "https://mp.weixin.qq.com/s/demo",
                            "digest": "银行板块业绩和息差出现改善迹象。",
                            "create_time": 1758445967,
                        }
                    ]
                }
            }
        )
    if url.endswith("/api/article"):
        return _FakeResponse({"data": {"content": "<p>银行正文</p>", "plain_content": "银行财报点评显示息差企稳。"}})
    raise AssertionError(f"unexpected URL: {url}")


if __name__ == "__main__":
    unittest.main()

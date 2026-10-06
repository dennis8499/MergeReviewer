from __future__ import annotations

import json
import base64
import hashlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import create_repository_fixture, load_module, remove_temporary_tree, run_git


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "merge-reviewer" / "scripts" / "review_report.py"
SESSION_SCRIPT = SCRIPT.with_name("review_session.py")
sys.path.insert(0, str(SCRIPT.parent))
import review_session


def load_report_module():
    return load_module("merge_reviewer_review_report", SCRIPT)


def load_session_module():
    return load_module("merge_reviewer_report_test_session", SESSION_SCRIPT)


class ReviewReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="merge-review-report-test-")
        self.root = Path(self.temp.name)
        self.repo, _ = create_repository_fixture(self.root)
        self.report_module = load_report_module()
        self.head = run_git(self.repo, "rev-parse", "HEAD")
        self.context_dir = self.root / "context"
        self.context_dir.mkdir()
        self.report_dir = self.root / "review-reports"
        self.manifest = {
            "schema_version": 4,
            "generated_at": "2026-10-01T03:00:00+00:00",
            "repo": str(self.repo),
            "project": "Orders",
            "base_input": "main",
            "head_input": "feature",
            "base_sha": self.head,
            "head_sha": self.head,
            "merge_base": self.head,
            "mode": "merge",
            "review_scope": "committed",
            "review_right": self.head,
            "review_tree_sha": None,
            "fetch_status": "not-needed",
            "working_tree_unchanged": True,
            "context_complete": True,
            "review_complete": True,
            "context_gaps": [],
            "review_limitations": [],
            "evidence_files": [],
            "changed_files": [{"status": "M", "old_path": None, "path": "base.txt"}],
            "merge_commits": [],
            "commit_count": 1,
            "merge_preview": {
                "status": "clean",
                "tree_sha": run_git(self.repo, "rev-parse", "HEAD^{tree}"),
                "changed_files": [],
                "manifest_info": {},
            },
        }
        (self.context_dir / "manifest.json").write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        remove_temporary_tree(self.root)
        self.temp.cleanup()

    def valid_draft(self) -> dict:
        return {
            "schema_version": 1,
            "summary": "在檢查範圍內沒有找到有證據支持的問題。",
            "coverage": [
                {
                    "path": "base.txt",
                    "status": "reviewed",
                    "evidence": [
                        {
                            "source": "head",
                            "ref": self.head,
                            "path": "base.txt",
                            "line_start": 1,
                            "line_end": 1,
                        }
                    ],
                }
            ],
            "findings": [],
            "next_steps": [{"action": "保留本次驗收紀錄，後續變更時重新檢查。", "owner": "QA"}],
            "tests_executed": [],
            "tests_not_executed": ["未執行專案測試；本次僅進行靜態審查。"],
            "limitations": [],
        }

    def publish(self, draft: dict | None = None, *, include_json: bool = False):
        draft_path = self.context_dir / "draft.json"
        draft_path.write_text(
            json.dumps(draft or self.valid_draft(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return self.report_module.publish_report(
            self.context_dir, draft_path, self.report_dir, include_json=include_json
        )

    def test_default_report_writes_markdown_only_and_keeps_context_for_cleanup_by_caller(self) -> None:
        markdown_path, json_path = self.publish()
        markdown = markdown_path.read_text(encoding="utf-8")
        self.assertIsNone(json_path)
        self.assertFalse(markdown_path.with_suffix(".json").exists())
        self.assertIn("## 問題總覽", markdown)
        self.assertIn("context manifest SHA-256", markdown)
        self.assertTrue((self.context_dir / "manifest.json").exists())
        self.assertTrue((self.context_dir / "draft.json").exists())

    def test_owned_context_is_removed_after_markdown_publication(self) -> None:
        session = load_session_module()
        context = session.create_session(self.root / "owned-context")
        self.manifest["context_cleanup_required"] = True
        (context / "manifest.json").write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        draft_path = context / "draft.json"
        draft_path.write_text(json.dumps(self.valid_draft(), ensure_ascii=False), encoding="utf-8")

        markdown_path, json_path = self.report_module.publish_report(context, draft_path, self.report_dir)

        self.assertTrue(markdown_path.is_file())
        self.assertIsNone(json_path)
        self.assertEqual([markdown_path.resolve()], [path.resolve() for path in self.report_dir.iterdir()])
        self.assertFalse(context.exists())

    def test_owned_context_is_removed_when_validation_fails(self) -> None:
        session = load_session_module()
        context = session.create_session(self.root / "invalid-context")
        self.manifest["context_cleanup_required"] = True
        (context / "manifest.json").write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        invalid = {**self.valid_draft(), "coverage": []}
        draft_path = context / "draft.json"
        draft_path.write_text(json.dumps(invalid, ensure_ascii=False), encoding="utf-8")

        with self.assertRaises(ValueError):
            self.report_module.publish_report(context, draft_path, self.report_dir)
        self.assertFalse(context.exists())
        self.assertFalse(self.report_dir.exists())

    def test_cleanup_failure_returns_failure_and_preserves_report_paths(self) -> None:
        context = review_session.create_session(self.root / "cleanup-failure-context")
        self.manifest["context_cleanup_required"] = True
        (context / "manifest.json").write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        draft_path = context / "draft.json"
        draft_path.write_text(json.dumps(self.valid_draft(), ensure_ascii=False), encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("review_session._remove_tree", side_effect=OSError("temporary volume is busy")),
            patch("sys.stdout", stdout),
            patch("sys.stderr", stderr),
        ):
            exit_code = self.report_module.main([
                "--context-dir", str(context), "--result", str(draft_path),
                "--report-dir", str(self.report_dir),
            ])

        self.assertEqual(2, exit_code)
        self.assertIn("cleanup failed", stderr.getvalue())
        self.assertTrue(context.exists())
        reports = list(self.report_dir.glob("*.md"))
        self.assertEqual(1, len(reports))
        self.assertIn(str(reports[0].resolve()), stderr.getvalue())

    def test_fixed_mr_report_defaults_to_markdown_with_import_metadata(self) -> None:
        self.manifest["diff_base"] = self.head
        self.manifest["mr_context"] = {
            "schema": "MergeReviewTask/v1", "origin": "https://gitlab.example.invalid",
            "projectId": 10, "mrIid": 4, "targetProjectId": 10, "sourceProjectId": 20,
            "sourceSha": self.head, "targetSha": self.head, "sourceBranch": "feature",
            "targetBranch": "main", "repoPath": str(self.repo), "sourceRemoteUrl": "unused",
            "targetRemoteUrl": "unused", "mode": "merge",
        }
        (self.context_dir / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

        markdown_path, json_path = self.publish()

        self.assertIsNone(json_path)
        markdown = markdown_path.read_text(encoding="utf-8")
        metadata = json.loads(base64.b64decode(markdown.split("<!-- merge-review-report:")[1].split(" -->")[0]))
        self.assertEqual("MergeReviewReport/v1", metadata["schema"])
        self.assertEqual(20, metadata["sourceProjectId"])
        self.assertEqual(self.head, metadata["targetSha"])

    def test_fixed_mr_report_automatically_preserves_portable_json_and_incomplete_state(self) -> None:
        self.manifest["diff_base"] = self.head
        self.manifest["mr_context"] = {
            "schema": "MergeReviewTask/v1", "origin": "https://gitlab.example.invalid",
            "projectId": 10, "mrIid": 4, "targetProjectId": 10, "sourceProjectId": 20,
            "sourceSha": self.head, "targetSha": self.head, "sourceBranch": "feature",
            "targetBranch": "main", "repoPath": str(self.repo), "sourceRemoteUrl": "unused",
            "targetRemoteUrl": "unused", "mode": "merge",
        }
        (self.context_dir / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        draft = self.valid_draft()
        draft["limitations"] = ["One dependency was unavailable."]
        draft["coverage"][0].update(status="metadata-only", reason="Dependency content unavailable.")
        markdown_path, json_path = self.publish(draft, include_json=True)
        self.assertIsNotNone(json_path)
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        markdown = markdown_path.read_text(encoding="utf-8")
        encoded = markdown.split("<!-- merge-review-report:")[1].split(" -->")[0]
        metadata = json.loads(base64.b64decode(encoded))
        self.assertEqual(metadata, payload["report_metadata"])
        self.assertFalse(metadata["reviewComplete"])
        self.assertEqual(20, metadata["sourceProjectId"])
        self.assertEqual(hashlib.sha256(payload["report_body"].encode("utf-8")).hexdigest(), metadata["bodySha256"])

    def test_include_json_writes_matching_json_and_markdown_reports(self) -> None:
        markdown_path, json_path = self.publish(include_json=True)
        self.assertIsNotNone(json_path)
        assert json_path is not None
        result = json.loads(json_path.read_text(encoding="utf-8"))
        markdown = markdown_path.read_text(encoding="utf-8")
        self.assertEqual(result["review_status"], "未發現具體問題")
        self.assertEqual(result["merge_recommendation"], "可以合併")
        self.assertEqual(result["priority_counts"], {"P0": 0, "P1": 0, "P2": 0, "P3": 0})
        self.assertIn("## 問題總覽", markdown)
        self.assertIn("context manifest SHA-256", markdown)
        self.assertEqual(result["report_markdown"], markdown_path.name)
        self.assertEqual(result["report_json"], json_path.name)
        self.assertEqual(json_path.stem, markdown_path.stem)

    def test_cli_default_reports_null_json_path(self) -> None:
        draft_path = self.context_dir / "draft.json"
        draft_path.write_text(
            json.dumps(self.valid_draft(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            exit_code = self.report_module.main(
                [
                    "--context-dir",
                    str(self.context_dir),
                    "--result",
                    str(draft_path),
                    "--report-dir",
                    str(self.report_dir),
                ]
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertIsNotNone(payload["markdown_report"])
        self.assertIsNone(payload["json_report"])
        self.assertTrue(Path(payload["markdown_report"]).exists())

    def test_cli_include_json_flag_writes_and_returns_json_path(self) -> None:
        draft_path = self.context_dir / "draft.json"
        draft_path.write_text(
            json.dumps(self.valid_draft(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            exit_code = self.report_module.main(
                [
                    "--context-dir",
                    str(self.context_dir),
                    "--result",
                    str(draft_path),
                    "--report-dir",
                    str(self.report_dir),
                    "--include-json",
                ]
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertIsNotNone(payload["json_report"])
        self.assertTrue(Path(payload["json_report"]).exists())

    def test_direct_review_does_not_require_a_merge_preview(self) -> None:
        self.manifest["mode"] = "direct"
        self.manifest["merge_preview"] = None
        (self.context_dir / "manifest.json").write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        _markdown_path, json_path = self.publish(include_json=True)
        assert json_path is not None
        result = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(result["review_status"], "未發現具體問題")

    def test_p1_finding_uses_valid_fixed_version_evidence_and_is_counted_once(self) -> None:
        draft = self.valid_draft()
        draft["findings"] = [
            {
                "id": "F-001",
                "priority": "P1",
                "title": "訂單重送時可能重複扣款",
                "impact": "顧客可能被多扣一次款項。",
                "trigger": "同一筆付款在逾時後立即重送。",
                "expected": "付款只執行一次。",
                "actual": "程式允許建立第二筆付款。",
                "recommendation": "讓付款流程辨識重複請求。",
                "engineering_fix": "使用具備唯一性保證的付款識別碼。",
                "evidence_summary": "程式建立付款前沒有檢查重複請求。",
                "scenario_source": "code-derived",
                "owner": "開發",
                "evidence": [draft["coverage"][0]["evidence"][0]],
            }
        ]
        markdown_path, json_path = self.publish(draft, include_json=True)
        assert json_path is not None
        result = json.loads(json_path.read_text(encoding="utf-8"))
        markdown = markdown_path.read_text(encoding="utf-8")
        self.assertEqual(result["review_status"], "發現具體問題")
        self.assertEqual(result["merge_recommendation"], "修正後再合併")
        self.assertEqual(result["priority_counts"]["P1"], 1)
        self.assertIn("F-001", markdown)
        self.assertIn("P1 合併前必修 `1`", markdown)

    def test_finding_must_cite_a_reviewed_changed_path(self) -> None:
        draft = self.valid_draft()
        draft["findings"] = [
            {
                "id": "F-001",
                "priority": "P1",
                "title": "未變更路徑中的既有缺陷",
                "impact": "此路徑的既有問題可能影響資料。",
                "trigger": "呼叫該既有流程。",
                "expected": "資料保持正確。",
                "actual": "資料可能出錯。",
                "recommendation": "先確認是否與本次變更相關。",
                "scenario_source": "code-derived",
                "owner": "開發",
                "evidence": [
                    {
                        "source": "head",
                        "ref": self.head,
                        "path": "deleted.txt",
                        "line_start": 1,
                        "line_end": 1,
                    }
                ],
            }
        ]
        with self.assertRaisesRegex(self.report_module.ReportValidationError, "已完成審查的變更路徑"):
            self.publish(draft)
        self.assertFalse(self.report_dir.exists())

    def test_missing_coverage_is_rejected_before_any_report_is_written(self) -> None:
        draft = self.valid_draft()
        draft["coverage"] = []
        with self.assertRaisesRegex(self.report_module.ReportValidationError, "coverage 遺漏必要路徑"):
            self.publish(draft)
        self.assertFalse(self.report_dir.exists())

    def test_required_schema_fields_and_non_boolean_line_numbers_are_enforced(self) -> None:
        draft = self.valid_draft()
        draft.pop("tests_not_executed")
        with self.assertRaisesRegex(self.report_module.ReportValidationError, "tests_not_executed 必須是必要的 JSON 陣列"):
            self.publish(draft)

        draft = self.valid_draft()
        draft["coverage"][0]["evidence"][0]["line_start"] = True
        draft["coverage"][0]["evidence"][0]["line_end"] = True
        with self.assertRaisesRegex(self.report_module.ReportValidationError, "有效區間"):
            self.publish(draft)

        draft = self.valid_draft()
        draft["schema_version"] = True
        with self.assertRaisesRegex(self.report_module.ReportValidationError, "schema_version 必須為 1"):
            self.publish(draft)

    def test_evidence_line_outside_the_resolved_git_blob_is_rejected(self) -> None:
        draft = self.valid_draft()
        draft["coverage"][0]["evidence"][0]["line_start"] = 99
        draft["coverage"][0]["evidence"][0]["line_end"] = 99
        with self.assertRaisesRegex(self.report_module.ReportValidationError, "超出固定版本"):
            self.publish(draft)
        self.assertFalse(self.report_dir.exists())

    def test_unresolved_issue_ids_and_priority_order_are_rejected(self) -> None:
        draft = self.valid_draft()
        draft["findings"] = [
            {
                "id": "F-003",
                "priority": "P0",
                "title": "高風險問題",
                "impact": "使用者資料可能受損。",
                "trigger": "執行受影響的操作。",
                "expected": "資料應保持正確。",
                "actual": "資料會被錯誤刪除。",
                "recommendation": "修正這個流程。",
                "scenario_source": "code-derived",
                "owner": "開發",
                "evidence": [draft["coverage"][0]["evidence"][0]],
            }
        ]
        with self.assertRaisesRegex(self.report_module.ReportValidationError, "F-001"):
            self.publish(draft)

    def test_context_or_unreviewed_files_force_an_incomplete_result(self) -> None:
        draft = self.valid_draft()
        draft["coverage"][0]["status"] = "metadata-only"
        draft["coverage"][0]["reason"] = "只確認了 Git mode，無法讀取檔案內容。"
        markdown_path, json_path = self.publish(draft, include_json=True)
        assert json_path is not None
        result = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(result["review_status"], "審查未完成")
        self.assertEqual(result["merge_recommendation"], "需補做審查")
        self.assertIn("檔案內容", markdown_path.read_text(encoding="utf-8"))

    def test_merge_conflicts_force_a_nonpassing_state_even_with_complete_file_coverage(self) -> None:
        self.manifest["merge_preview"]["status"] = "conflicts"
        self.manifest["merge_preview"]["detail"] = "CONFLICT (content): base.txt"
        self.manifest["context_complete"] = False
        self.manifest["review_complete"] = False
        self.manifest["context_gaps"] = ["合併預覽存在衝突。"]
        (self.context_dir / "manifest.json").write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        markdown_path, json_path = self.publish(include_json=True)
        assert json_path is not None
        result = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(result["review_status"], "審查未完成")
        self.assertEqual(result["merge_recommendation"], "需補做審查")
        self.assertIn("衝突", " ".join(result["limitations"]))
        self.assertTrue(markdown_path.exists())

    def test_generated_reports_get_a_unique_suffix_when_timestamp_collides(self) -> None:
        first_markdown, first_json = self.publish()
        second_markdown, second_json = self.publish(include_json=True)
        self.assertIsNone(first_json)
        assert second_json is not None
        self.assertNotEqual(first_markdown, second_markdown)
        self.assertEqual(second_markdown.stem, first_markdown.stem + "-01")
        self.assertEqual(second_json.stem, second_markdown.stem)

    def test_existing_json_report_is_not_overwritten_in_markdown_only_mode(self) -> None:
        stamp = "20261001T030000Z"
        orphan_json = self.report_dir / f"merge-review-{stamp}-{self.head[:8]}-{self.head[:8]}.json"
        orphan_json.parent.mkdir(parents=True)
        orphan_json.write_text("preserve existing report\n", encoding="utf-8")

        markdown_path, json_path = self.publish()

        self.assertIsNone(json_path)
        self.assertEqual(markdown_path.stem, orphan_json.stem + "-01")
        self.assertEqual(orphan_json.read_text(encoding="utf-8"), "preserve existing report\n")

    def test_publish_failure_rolls_back_partial_files_and_allows_retry(self) -> None:
        for include_json in (False, True):
            with self.subTest(include_json=include_json):
                report_dir = self.root / ("rollback-json" if include_json else "rollback-markdown")
                draft_path = self.context_dir / "draft.json"
                draft_path.write_text(
                    json.dumps(self.valid_draft(), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                original_link = os.link
                calls = 0

                def fail_on_last_link(source, destination):
                    nonlocal calls
                    calls += 1
                    expected_calls = 2 if include_json else 1
                    if calls == expected_calls:
                        raise OSError("simulated report write failure")
                    return original_link(source, destination)

                with patch.object(self.report_module.os, "link", side_effect=fail_on_last_link):
                    with self.assertRaisesRegex(OSError, "simulated report write failure"):
                        self.report_module.publish_report(
                            self.context_dir,
                            draft_path,
                            report_dir,
                            include_json=include_json,
                        )

                self.assertEqual(list(report_dir.iterdir()), [])
                markdown_path, json_path = self.report_module.publish_report(
                    self.context_dir,
                    draft_path,
                    report_dir,
                    include_json=include_json,
                )
                self.assertTrue(markdown_path.exists())
                if include_json:
                    self.assertIsNotNone(json_path)
                    assert json_path is not None
                    self.assertTrue(json_path.exists())
                else:
                    self.assertIsNone(json_path)

    def test_traversal_evidence_paths_are_rejected(self) -> None:
        draft = self.valid_draft()
        for unsafe_path in ("../../outside.txt", r"C:\outside.txt", r"..\outside.txt"):
            draft["coverage"][0]["evidence"][0]["path"] = unsafe_path
            with self.subTest(path=unsafe_path), self.assertRaisesRegex(
                self.report_module.ReportValidationError, "path 無效"
            ):
                self.publish(draft)
        self.assertFalse(self.report_dir.exists())

    def test_working_tree_evidence_must_match_both_git_blob_and_saved_sha256(self) -> None:
        content = b"fixed working-tree version\n"
        working_tree_dir = self.context_dir / "working-tree-files"
        working_tree_dir.mkdir()
        (working_tree_dir / "base.txt").write_bytes(content)
        self.manifest["review_scope"] = "working-tree"
        self.manifest["review_tree_sha"] = "a" * 40
        self.manifest["review_right"] = self.manifest["review_tree_sha"]
        self.manifest["evidence_files"] = [
            {
                "source": "working-tree",
                "path": "base.txt",
                "blob_sha": self.report_module._git_blob_id(self.repo, content, 180),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ]
        (self.context_dir / "manifest.json").write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        draft = self.valid_draft()
        draft["coverage"][0]["evidence"][0] = {
            "source": "working-tree",
            "ref": self.manifest["review_tree_sha"],
            "path": "base.txt",
            "line_start": 1,
            "line_end": 1,
        }

        _markdown_path, json_path = self.publish(draft, include_json=True)
        assert json_path is not None
        self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))["review_status"], "未發現具體問題")

        (working_tree_dir / "base.txt").write_bytes(b"tampered working-tree version\n")
        with self.assertRaisesRegex(self.report_module.ReportValidationError, "Git blob SHA 不相符"):
            self.publish(draft)

    def test_merge_preview_evidence_is_verified_from_its_preserved_object_store(self) -> None:
        preview = self.manifest["merge_preview"]
        content = self.report_module._git_blob(self.repo, preview["tree_sha"], "base.txt", 180)
        object_directory = self.context_dir / "merge-preview-objects"
        object_directory.mkdir()
        preview["manifest_info"] = {
            "object_directory": str(object_directory),
            "alternate_objects": str((self.repo / ".git" / "objects").resolve()),
        }
        self.manifest["evidence_files"] = [
            {
                "source": "merge-preview",
                "path": "base.txt",
                "blob_sha": self.report_module._git_blob_id(self.repo, content, 180),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ]
        (self.context_dir / "manifest.json").write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        draft = self.valid_draft()
        draft["coverage"][0]["evidence"][0] = {
            "source": "merge-preview",
            "ref": preview["tree_sha"],
            "path": "base.txt",
            "line_start": 1,
            "line_end": 1,
        }
        preview["changed_files"] = [{"status": "M", "path": "base.txt", "old_path": None}]
        (self.context_dir / "manifest.json").write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(self.report_module.ReportValidationError, "必要固定版本的證據.*head"):
            self.publish(draft)

        draft["coverage"][0]["evidence"].append(
            {
                "source": "head",
                "ref": self.head,
                "path": "base.txt",
                "line_start": 1,
                "line_end": 1,
            }
        )

        _markdown_path, json_path = self.publish(draft, include_json=True)
        assert json_path is not None
        self.assertEqual(json.loads(json_path.read_text(encoding="utf-8"))["review_status"], "未發現具體問題")

        self.manifest["evidence_files"][0]["blob_sha"] = "0" * 40
        (self.context_dir / "manifest.json").write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(self.report_module.ReportValidationError, "Git blob SHA 不相符"):
            self.publish(draft)


if __name__ == "__main__":
    unittest.main()

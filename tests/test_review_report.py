from __future__ import annotations
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
from unittest import mock

from tests.fixtures import ReviewFixture, api, workspace, workspace_report, review_report, review_session
from tests.support import run_python, run_git as git


class ReviewReportTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.commit()

    def test_single_markdown_uses_timestamp_and_branch_commits_and_cleans_context(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        seed = workspace.repository(manifest, manifest["selected_repo"])
        (context / "draft.json").write_text(json.dumps(draft), encoding="utf-8")
        markdown = review_report.publish_report(context, context / "draft.json")
        self.assertEqual(self.repo / "review-reports", markdown.parent)
        base, head = seed["sources"]["base"]["ref"], seed["sources"]["head"]["ref"]
        self.assertRegex(markdown.name, rf"^merge-review-\d{{8}}T\d{{12}}Z-{base[:8]}-{head[:8]}\.md$")
        self.assertEqual([markdown], list(markdown.parent.iterdir()))
        self.assertFalse(context.exists())
        text = markdown.read_text(encoding="utf-8")
        self.assertIn("# Merge Report: api", text)
        comparison = text.split("## 比較資訊\n", 1)[1].split("\n## ", 1)[0]
        self.assertIn("- Repo：`api`", comparison)
        self.assertIn(f"- 本機路徑：`{self.repo}`", comparison)
        self.assertIn(f"| 基礎分支 | `main` | `refs/heads/main` | `{base[:8]}` |", comparison)
        self.assertIn(f"| 比較分支 | `feature` | `refs/heads/feature` | `{head[:8]}` |", comparison)
        self.assertIn("- 合併方向：`feature` → `main`", comparison)
        self.assertIn(f"- 主要差異範圍（共同祖先 → 比較分支）：`{base[:8]}` → `{head[:8]}`", comparison)
        self.assertNotIn(base, comparison)
        self.assertNotIn(head, comparison)
        appendix = text.split("## 技術附錄\n", 1)[1]
        self.assertIn(f"`{base}`", appendix)
        self.assertIn(f"`{head}`", appendix)
        self.assertLess(text.index("問題數量："), text.index("## 比較資訊"))
        self.assertNotIn("## 問題\n", text)
        self.assertNotIn("## 下一步\n", text)

    def test_report_shows_remote_branch_inputs_and_resolved_refs(self):
        remote = self.root / "origin.git"
        git(self.root, "init", "--bare", str(remote))
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "push", "origin", "main", "feature")
        context, manifest = self.capture(base="origin/main", head="origin/feature")
        seed = workspace.repository(manifest, manifest["selected_repo"])
        base, head = seed["sources"]["base"]["ref"], seed["sources"]["head"]["ref"]
        (context / "draft.json").write_text(json.dumps(self.draft(context, manifest)), encoding="utf-8")
        markdown = review_report.publish_report(context, context / "draft.json")
        comparison = markdown.read_text(encoding="utf-8").split("## 比較資訊\n", 1)[1].split("\n## ", 1)[0]
        self.assertIn(f"| 基礎分支 | `origin/main` | `refs/remotes/origin/main` | `{base[:8]}` |", comparison)
        self.assertIn(f"| 比較分支 | `origin/feature` | `refs/remotes/origin/feature` | `{head[:8]}` |", comparison)
        self.assertIn("- 合併方向：`origin/feature` → `origin/main`", comparison)
        self.assertRegex(markdown.name, rf"^merge-review-\d{{8}}T\d{{12}}Z-{base[:8]}-{head[:8]}\.md$")

    def test_group_report_still_belongs_to_only_the_selected_repo(self):
        consumer = self.make_repo("consumer")
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        (context / "draft.json").write_text(json.dumps(draft), encoding="utf-8")
        markdown = review_report.publish_report(context, context / "draft.json")
        self.assertEqual(self.repo / "review-reports", markdown.parent)
        self.assertFalse((self.root / "review-reports").exists())
        self.assertFalse((consumer / "review-reports").exists())
        self.assertEqual([markdown], list(markdown.parent.iterdir()))
        comparison = markdown.read_text(encoding="utf-8").split("## 比較資訊\n", 1)[1].split("\n## ", 1)[0]
        self.assertIn(f"- 本機路徑：`{self.repo}`", comparison)
        self.assertNotIn(str(consumer), comparison)

    def test_markdown_preserves_all_findings_and_native_priorities(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        for index, priority in enumerate(("P0", "P1", "P2", "P3"), 1):
            item = self.finding(draft, priority)
            item.update(id=f"F-{index:03}", title=f"問題 {index}", trigger=f"觸發 {index}")
            draft["findings"].append(item)
        (context / "draft.json").write_text(json.dumps(draft), encoding="utf-8")
        markdown = review_report.publish_report(context, context / "draft.json")
        text = markdown.read_text(encoding="utf-8")
        self.assertIn("問題數量：P0 1／P1 1／P2 1／P3 1", text)
        self.assertIn("merge-report-concise/v1", text)
        self.assertLess(text.index("## 比較資訊"), text.index("## 問題\n"))
        for item in draft["findings"]:
            self.assertIn(f"{item['id']}｜[{item['priority']}] {item['title']}", text)
            self.assertIn(item["evidence"][0]["ref"], text)
        self.assertEqual([markdown], list(markdown.parent.iterdir()))

    def test_missing_coverage_and_invalid_lines_cannot_publish(self):
        for failure in ("coverage", "line", "boolean-line", "priority"):
            context, manifest = self.capture()
            draft = self.draft(context, manifest)
            if failure == "coverage":
                draft["coverage"] = []
            elif failure in ("line", "boolean-line"):
                evidence = draft["coverage"][0]["evidence"][0]
                evidence["line_start"] = 900 if failure == "line" else True
                evidence["line_end"] = 900
            else:
                item = self.finding(draft, "P9")
                draft["findings"] = [item]
            (context / "draft.json").write_text(json.dumps(draft), encoding="utf-8")
            with self.subTest(failure=failure), self.assertRaises(ValueError):
                review_report.publish_report(context, context / "draft.json")
            self.assertFalse(context.exists())
        self.assertFalse((self.repo / "review-reports").exists())

    def test_source_ref_and_path_traversal_are_rejected(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        bad = copy.deepcopy(draft)
        bad["coverage"][0]["evidence"][0]["ref"] = "a" * 40
        with self.assertRaisesRegex(ValueError, "frozen version"):
            workspace_report.validate(bad, manifest, context)
        bad = copy.deepcopy(draft)
        bad["coverage"][0]["evidence"][0]["path"] = "../app.py"
        with self.assertRaises(ValueError):
            workspace_report.validate(bad, manifest, context)

    def test_finding_requires_seed_change_and_unique_id(self):
        self.make_repo("consumer")
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        consumer = next(r for r in manifest["repositories"] if r["role"] == "candidate")
        self.attach_consumer(draft, manifest, Path(consumer["path"]))
        item = self.finding(draft)
        item["evidence"] = copy.deepcopy(draft["impact_coverage"][-1]["evidence"])
        draft["findings"] = [item]
        with self.assertRaisesRegex(ValueError, "seed change"):
            workspace_report.validate(draft, manifest, context)

    def test_duplicate_findings_and_wrong_priority_order_are_rejected(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        item = self.finding(draft, "P2")
        duplicate = copy.deepcopy(item)
        duplicate["id"] = "F-002"
        draft["findings"] = [item, duplicate]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            workspace_report.validate(draft, manifest, context)
        duplicate.update(priority="P1", title="另一個問題")
        with self.assertRaisesRegex(ValueError, "ordered"):
            workspace_report.validate(draft, manifest, context)

    def test_unreviewed_content_is_incomplete_and_retains_confirmed_findings(self):
        self.commit("second = True\n", "second.py")
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        draft["findings"] = [self.finding(draft)]
        draft["coverage"][-1].update(status="not-reviewed", reason="尚未讀取完整內容。", evidence=[])
        result = workspace_report.validate(draft, manifest, context)
        self.assertEqual("審查未完成", result["review_status"])
        self.assertEqual("需補做審查", result["recommendation"])
        self.assertEqual(1, result["priority_counts"]["P1"])
        seed = workspace.repository(manifest, manifest["selected_repo"])
        text = workspace_report.render(result, manifest)
        self.assertIn(f"| 比較分支 | `feature` | `refs/heads/feature` | `{seed['sources']['head']['ref'][:8]}` |", text)
        self.assertIn("**需補做審查**｜審查未完成", text)

    def test_no_difference_and_no_findings_are_distinct(self):
        context, manifest = self.capture(head="main")
        result = workspace_report.validate(self.draft(context, manifest), manifest, context)
        self.assertEqual("沒有差異", result["review_status"])
        seed = workspace.repository(manifest, manifest["selected_repo"])
        tip = seed["sources"]["base"]["ref"][:8]
        text = workspace_report.render(result, manifest)
        self.assertIn(f"| 基礎分支 | `main` | `refs/heads/main` | `{tip}` |", text)
        self.assertIn(f"| 比較分支 | `main` | `refs/heads/main` | `{tip}` |", text)
        self.assertIn(f"- 主要差異範圍（共同祖先 → 比較分支）：`{tip}` → `{tip}`", text)
        context, manifest = self.capture()
        result = workspace_report.validate(self.draft(context, manifest), manifest, context)
        self.assertEqual("未發現具體問題", result["review_status"])

    def test_legacy_context_and_result_schemas_are_rejected(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        for schema in ("merge-reviewer-workspace-context/v1", "merge-reviewer-workspace-context/v2"):
            bad = copy.deepcopy(manifest)
            bad["schema"] = schema
            with self.assertRaisesRegex(ValueError, "v3"):
                workspace_report.validate(draft, bad, context)
        draft["schema"] = "merge-reviewer-workspace-result/v2"
        with self.assertRaisesRegex(ValueError, "v3"):
            workspace_report.validate(draft, manifest, context)

    def test_cli_returns_only_the_markdown_path(self):
        context, manifest = self.capture()
        (context / "draft.json").write_text(json.dumps(self.draft(context, manifest)), encoding="utf-8")
        result = run_python(Path(review_report.__file__), "--context-dir", str(context),
                            "--result", str(context / "draft.json"))
        self.assertEqual(0, result.returncode, result.stderr)
        markdown = Path(result.stdout.strip())
        self.assertTrue(markdown.is_file())
        self.assertEqual(self.repo / "review-reports", markdown.parent)
        self.assertEqual([markdown], list(markdown.parent.iterdir()))
        self.assertFalse(context.exists())

    def test_removed_json_output_flag_is_rejected(self):
        result = run_python(Path(review_report.__file__), "--context-dir", "unused",
                            "--result", "unused", "--include-json")
        self.assertEqual(2, result.returncode)
        self.assertIn("unrecognized arguments: --include-json", result.stderr)
        self.assertEqual("", result.stdout)
        self.assertFalse((self.repo / "review-reports").exists())

    def test_publication_failure_leaves_no_partial_reports_and_cleans_context(self):
        context, manifest = self.capture()
        (context / "draft.json").write_text(json.dumps(self.draft(context, manifest)), encoding="utf-8")
        with mock.patch.object(workspace_report.os, "link", side_effect=OSError("publish failure")):
            with self.assertRaises(OSError):
                review_report.publish_report(context, context / "draft.json")
        self.assertFalse(context.exists())
        self.assertEqual([], list((self.repo / "review-reports").iterdir()))

    def test_filename_collision_preserves_previous_report(self):
        context, manifest = self.capture()
        seed = workspace.repository(manifest, manifest["selected_repo"])
        report_dir = self.repo / "review-reports"
        report_dir.mkdir()
        base, head = seed["sources"]["base"]["ref"], seed["sources"]["head"]["ref"]
        previous = report_dir / f"merge-review-20261014T091530123456Z-{base[:8]}-{head[:8]}.md"
        previous.write_text("keep", encoding="utf-8")
        (context / "draft.json").write_text(json.dumps(self.draft(context, manifest)), encoding="utf-8")
        with mock.patch.object(workspace_report, "datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 14, 9, 15, 30, 123456, tzinfo=timezone.utc)
            markdown = review_report.publish_report(context, context / "draft.json")
        self.assertEqual(f"merge-review-20261014T091530123457Z-{base[:8]}-{head[:8]}.md", markdown.name)
        self.assertEqual("keep", previous.read_text(encoding="utf-8"))
        self.assertEqual({previous, markdown}, set(report_dir.iterdir()))

    def test_cleanup_failure_exposes_published_path(self):
        context, manifest = self.capture()
        (context / "draft.json").write_text(json.dumps(self.draft(context, manifest)), encoding="utf-8")
        real_cleanup = review_session.cleanup_session
        with mock.patch.object(review_session, "cleanup_session", side_effect=OSError("cleanup failure")):
            with self.assertRaisesRegex(review_session.SessionError, "報告已建立") as raised:
                review_report.publish_report(context, context / "draft.json")
        reports = list((self.repo / "review-reports").glob("merge-review-*.md"))
        self.assertEqual(1, len(reports))
        self.assertIn(str(reports[0]), str(raised.exception))
        real_cleanup(context)

    def test_binary_content_is_explicitly_incomplete(self):
        (self.repo / "blob.bin").write_bytes(b"\0binary")
        from tests.support import run_git as git
        git(self.repo, "add", "blob.bin")
        git(self.repo, "commit", "-m", "binary")
        context, manifest = self.capture()
        result = workspace_report.validate(self.draft(context, manifest), manifest, context)
        self.assertFalse(result["review_complete"])

    def test_filename_uses_captured_branch_tips_not_merge_base_or_current_head(self):
        git(self.repo, "switch", "main")
        self.commit("base_ready = True\n", "base.py")
        git(self.repo, "switch", "-c", "working")
        self.commit("checkout_only = True\n", "checkout.py")
        context, manifest = self.capture()
        seed = workspace.repository(manifest, manifest["selected_repo"])
        base, head = seed["sources"]["base"]["ref"], seed["sources"]["head"]["ref"]
        merge_base = seed["native"]["merge_base"]
        checkout = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual(4, len({base, head, merge_base, checkout}))
        (context / "draft.json").write_text(json.dumps(self.draft(context, manifest)), encoding="utf-8")
        with mock.patch.object(workspace_report, "datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 14, 9, 15, 30, 123456, tzinfo=timezone.utc)
            markdown = review_report.publish_report(context, context / "draft.json")
        self.assertEqual(f"merge-review-20261014T091530123456Z-{base[:8]}-{head[:8]}.md", markdown.name)
        text = markdown.read_text(encoding="utf-8")
        comparison = text.split("## 比較資訊\n", 1)[1].split("\n## ", 1)[0]
        self.assertIn(f"| 基礎分支 | `main` | `refs/heads/main` | `{base[:8]}` |", comparison)
        self.assertIn(f"- 主要差異範圍（共同祖先 → 比較分支）：`{merge_base[:8]}` → `{head[:8]}`", comparison)
        self.assertNotIn(checkout[:8], comparison)
        self.assertIn(f"`{merge_base}`", text.split("## 技術附錄\n", 1)[1])

    def test_publication_race_preserves_other_writer_report(self):
        context, manifest = self.capture()
        (context / "draft.json").write_text(json.dumps(self.draft(context, manifest)), encoding="utf-8")
        real_link = workspace_report.os.link
        collisions = []

        def racing_link(stage, output):
            if not collisions:
                output.write_text("other writer", encoding="utf-8")
                collisions.append(output)
                raise FileExistsError(str(output))
            return real_link(stage, output)

        with mock.patch.object(workspace_report.os, "link", side_effect=racing_link):
            markdown = review_report.publish_report(context, context / "draft.json")
        self.assertNotEqual(collisions[0], markdown)
        self.assertEqual("other writer", collisions[0].read_text(encoding="utf-8"))
        self.assertEqual({collisions[0], markdown}, set(markdown.parent.iterdir()))

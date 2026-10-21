from __future__ import annotations
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
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
        self.assertTrue(text.startswith("**可以合併**｜未發現具體問題。"))
        comparison = text.split("## 比較資訊\n", 1)[1].split("\n## ", 1)[0]
        self.assertIn("- Repo：`api`", comparison)
        self.assertIn(f"- 本機路徑：`{self.repo}`", comparison)
        self.assertIn(f"| 基礎分支 | `main` | `refs/heads/main` | `{base[:8]}` |", comparison)
        self.assertIn(f"| 比較分支 | `feature` | `refs/heads/feature` | `{head[:8]}` |", comparison)
        self.assertIn("- 合併方向：`feature` → `main`", comparison)
        self.assertIn(f"- 主要差異範圍（共同祖先 → 比較分支）：`{base[:8]}` → `{head[:8]}`", comparison)
        self.assertNotIn(base, comparison)
        self.assertNotIn(head, comparison)
        self.assertNotIn("技術附錄", text)
        self.assertIn(base, text)
        self.assertIn(head, text)
        self.assertLess(text.index("問題數量："), text.index("## 比較資訊"))
        self.assertIn("## 問題\n", text)
        self.assertIn("## 下一步\n\n無額外下一步。", text)

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
            item.update(evidence_summary=f"證據摘要 {index}", engineering_fix=f"工程改善 {index}")
            item["scenario_source"] = "code-derived" if index % 2 else "reproduced"
            draft["findings"].append(item)
        draft["next_steps"] = [{"owner": "QA", "action": "補做相關回歸測試。"}]
        (context / "draft.json").write_text(json.dumps(draft), encoding="utf-8")
        markdown = review_report.publish_report(context, context / "draft.json")
        text = markdown.read_text(encoding="utf-8")
        self.assertIn("問題數量：P0 1／P1 1／P2 1／P3 1", text)
        self.assertLess(text.index("## 比較資訊"), text.index("## 問題\n"))
        self.assertLess(text.index("## 問題\n"), text.index("## 範圍與限制"))
        self.assertLess(text.index("## 範圍與限制"), text.index("## 下一步"))
        sections = re.split(r"^### F-\d{3}｜", text, flags=re.MULTILINE)[1:]
        self.assertEqual(4, len(sections))
        labels = ["問題描述", "重現原因", "預期結果", "實際結果", "證據", "建議改善方式"]
        for item, section in zip(draft["findings"], sections):
            self.assertIn(f"### {item['id']}｜[{item['priority']}] {item['title']}", text)
            self.assertEqual(labels, re.findall(r"^\*\*([^*]+)\*\*$", section, flags=re.MULTILINE))
            for label, field in (("問題描述", "impact"), ("重現原因", "trigger"), ("預期結果", "expected"), ("實際結果", "actual")):
                body = section.split(f"**{label}**", 1)[1].split("\n**", 1)[0]
                self.assertIn(item[field], body)
            reason = section.split("**重現原因**", 1)[1].split("**預期結果**", 1)[0]
            marker = "依程式推導，未實際重現" if item["scenario_source"] == "code-derived" else "已實際重現"
            self.assertIn(marker, reason)
            evidence = section.split("**證據**", 1)[1]
            self.assertIn(item["evidence_summary"], evidence)
            self.assertIn(item["evidence"][0]["ref"], evidence)
            fix = section
            for field in ("recommendation", "engineering_fix", "owner"):
                self.assertIn(item[field], fix)
        for heading in ("逐檔覆蓋", "固定版本搜尋紀錄", "功能鏈與 Repo 判定", "變更與延伸契約"):
            self.assertIn(heading, text)
        self.assertEqual([markdown], list(markdown.parent.iterdir()))

    def test_finding_excerpt_uses_only_frozen_selected_lines_and_does_not_mutate_draft(self):
        content = "outside_before = True\n    名稱 = '固定內容'\n\n\t```內嵌```\n    note = '甲\u2028乙'  \noutside_after = True\n"
        git(self.repo, "config", "core.autocrlf", "false")
        (self.repo / "snippet.py").write_bytes(content.encode("utf-8"))
        git(self.repo, "add", "snippet.py")
        git(self.repo, "commit", "-m", "selected excerpt")
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        item = self.finding(draft)
        evidence = copy.deepcopy(next(row for row in draft["coverage"] if row["path"] == "snippet.py")["evidence"][0])
        evidence.update(line_start=2, line_end=5, excerpt="untrusted draft excerpt")
        item["evidence"] = [evidence]
        draft["findings"] = [item]
        original = copy.deepcopy(draft)
        (self.repo / "snippet.py").write_text("dirty worktree content\n", encoding="utf-8")
        result = workspace_report.validate(draft, manifest, context)
        expected = "    名稱 = '固定內容'\n\n\t```內嵌```\n    note = '甲\u2028乙'  "
        self.assertEqual(original, draft)
        self.assertEqual(expected, result["findings"][0]["evidence"][0]["excerpt"])
        self.assertEqual(original["coverage"], result["coverage"])
        self.assertTrue(all("excerpt" not in e for row in result["coverage"] for e in row["evidence"]))
        text = workspace_report.render(result, manifest)
        self.assertIn("`api/snippet.py:2–5`", text)
        self.assertIn(f"（來源：`head`；固定版本：`{evidence['ref']}`）", text)
        self.assertIn(f"\n````\n{expected}\n````\n", text)
        for excluded in ("outside_before", "outside_after", "dirty worktree content", "untrusted draft excerpt"):
            self.assertNotIn(excluded, text)

    def test_each_cross_repo_file_and_source_has_its_own_excerpt_and_full_version(self):
        consumer = self.make_repo("consumer")
        self.commit("bridge = 'new'\n", "helper.py")
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        node = self.attach_consumer(draft, manifest, consumer)
        seed = workspace.repository(manifest, manifest["selected_repo"])
        item = self.finding(draft)
        item["evidence"] = [copy.deepcopy(row["evidence"][0]) for row in draft["coverage"]]
        item["evidence"].append({"repo": seed["id"], "source": "base", "path": "app.py",
                                 "ref": seed["sources"]["base"]["ref"], "line_start": 1, "line_end": 1})
        item["evidence"].extend(copy.deepcopy(node["evidence"]))
        draft["findings"] = [item]
        result = workspace_report.validate(draft, manifest, context)
        text = workspace_report.render(result, manifest)
        expected = {("api", "head", "app.py"): "contract = 'new'",
                    ("api", "head", "helper.py"): "bridge = 'new'",
                    ("api", "base", "app.py"): "contract = 'old'",
                    ("consumer", "head", "app.py"): "contract = 'old'"}
        for evidence in result["findings"][0]["evidence"]:
            name = workspace.repository(manifest, evidence["repo"])["name"]
            with self.subTest(repo=name, source=evidence["source"], path=evidence["path"]):
                self.assertEqual(expected[(name, evidence["source"], evidence["path"])], evidence["excerpt"])
                metadata = f"`{name}/{evidence['path']}:1–1`（來源：`{evidence['source']}`；固定版本：`{evidence['ref']}`）"
                self.assertIn(f"{metadata}\n\n```\n{evidence['excerpt']}\n```", text)
        self.assertEqual(4, len(result["findings"][0]["evidence"]))
        self.assertNotIn("技術附錄", text)

    def test_only_finding_evidence_requires_explicit_nonempty_line_ranges(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        item = self.finding(draft)
        for evidence in draft["coverage"][0]["evidence"]:
            evidence.pop("line_start")
            evidence.pop("line_end")
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])
        draft["findings"] = [item]
        for bounds in ({}, {"line_start": 1}, {"line_end": 1}, {"line_start": True, "line_end": 1},
                       {"line_start": 0, "line_end": 1}, {"line_start": 2, "line_end": 1},
                       {"line_start": 1, "line_end": 900}, {"line_start": "1", "line_end": 1}):
            bad = copy.deepcopy(draft)
            evidence = bad["findings"][0]["evidence"][0]
            evidence.pop("line_start")
            evidence.pop("line_end")
            evidence.update(bounds)
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                workspace_report.validate(bad, manifest, context)

    def test_empty_finding_evidence_has_no_invented_lines_or_code(self):
        self.commit("")
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        item = self.finding(draft)
        item["evidence"][0]["excerpt"] = "untrusted nonempty excerpt"
        draft["findings"] = [item]
        original = copy.deepcopy(draft)
        result = workspace_report.validate(draft, manifest, context)
        self.assertEqual(original, draft)
        self.assertIsNone(result["findings"][0]["evidence"][0]["excerpt"])
        text = workspace_report.render(result, manifest)
        self.assertIn("`api/app.py`", text)
        self.assertIn("固定版本內容為空（0 行）", text)
        self.assertNotIn("```", text)
        self.assertNotIn("untrusted nonempty excerpt", text)
        self.assertNotIn("app.py:1", text)

    def test_deleted_and_renamed_paths_render_the_actual_comparison_left_source(self):
        self.commit("deleted = True\n", "gone.py")
        git(self.repo, "branch", "-f", "main", "feature")
        git(self.repo, "mv", "app.py", "renamed.py")
        git(self.repo, "rm", "gone.py")
        git(self.repo, "commit", "-m", "rename and delete")
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        item = self.finding(draft)
        item["evidence"] = [copy.deepcopy(row["evidence"][0]) for row in draft["coverage"]]
        draft["findings"] = [item]
        result = workspace_report.validate(draft, manifest, context)
        seed = workspace.repository(manifest, manifest["selected_repo"])
        text = workspace_report.render(result, manifest)
        by_path = {e["path"]: e for e in result["findings"][0]["evidence"]}
        self.assertEqual({"app.py", "renamed.py", "gone.py"}, set(by_path))
        for path, expected in (("app.py", "contract = 'new'"), ("gone.py", "deleted = True")):
            evidence = by_path[path]
            self.assertIn(evidence["source"], ("base", "merge-base"))
            self.assertEqual(seed["diff_base"], evidence["ref"])
            self.assertEqual(expected, evidence["excerpt"])
            self.assertIn(f"`api/{path}:1–1`", text)
            self.assertIn(f"固定版本：`{seed['diff_base']}`", text)
        self.assertEqual("head", by_path["renamed.py"]["source"])
        self.assertEqual("contract = 'new'", by_path["renamed.py"]["excerpt"])

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
        self.assertIn("尚未讀取完整內容。", text)
        self.assertIn(draft["tests_not_executed"][0], text)
        self.assertNotIn("技術附錄", text)

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
        self.assertNotIn("技術附錄", text)
        self.assertIn(merge_base, text)

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

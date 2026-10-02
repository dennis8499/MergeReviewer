from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from tests.support import create_repository_fixture, run_git, run_python, remove_temporary_tree

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/merge-reviewer/scripts"
sys.path.insert(0, str(SCRIPTS))
import group_review
import mr_contract


class GroupReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo, _ = create_repository_fixture(self.root, name="服務 Alpha")
        self.context = self.root / "context"

    def capture(self):
        result = run_python(SCRIPTS / "git_review_context.py", "--group-root", str(self.root),
                            "--quick", "--context-dir", str(self.context))
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def draft(self, manifest):
        coverage = []
        for repo in manifest["repositories"]:
            for source, changes in repo["changed_files"].items():
                paths = {p for c in changes for p in (c.get("old_path"), c["path"]) if p}
                for path in paths:
                    records = [e for e in repo["evidence_files"] if e["path"] == path and e["source"] == source]
                    text = next((e for e in records if b"\0" not in (self.context / e["file"]).read_bytes()
                                 and (self.context / e["file"]).read_bytes().splitlines()), None)
                    item = {"repo": repo["repo"], "source": source, "path": path}
                    if text:
                        item.update(status="reviewed", evidence=[{**item, "ref": text["ref"], "side": text["side"],
                                                                  "line_start": 1, "line_end": 1}])
                    else:
                        item.update(status="metadata-only", reason="Binary or empty content.")
                    coverage.append(item)
        return {"schema": group_review.RESULT, "summary": "Group review", "coverage": coverage,
                "findings": [], "limitations": []}

    def test_renames_deletions_untracked_ignored_binary_and_worktree_are_preserved(self):
        (self.repo / ".gitignore").write_text("*.ignored\n", encoding="utf-8")
        run_git(self.repo, "add", ".gitignore")
        run_git(self.repo, "commit", "-m", "ignore rules")
        worktree = self.root / "獨立 Worktree"
        run_git(self.repo, "worktree", "add", "-b", "other", str(worktree))
        (worktree / "new.txt").write_text("worktree new\n", encoding="utf-8")
        run_git(self.repo, "mv", "base.txt", "改名.txt")
        run_git(self.repo, "rm", "deleted.txt")
        (self.repo / "new.txt").write_text("new\n", encoding="utf-8")
        (self.repo / "skip.ignored").write_text("ignored\n", encoding="utf-8")
        (self.repo / "forced.ignored").write_text("included\n", encoding="utf-8")
        run_git(self.repo, "add", "-f", "forced.ignored")
        (self.repo / "data.bin").write_bytes(b"\0\x01\x02")
        before = {str(p.relative_to(self.repo / ".git")): p.read_bytes() for p in (self.repo / ".git").rglob("*") if p.is_file()}
        manifest = self.capture()
        repo = next(r for r in manifest["repositories"] if r["repo"] == self.repo.name)
        self.assertEqual({"R", "D", "A"}, {c["status"][0] for c in repo["changed_files"]["index"]})
        paths = {c["path"] for c in repo["changed_files"]["working-tree"]}
        self.assertTrue({"改名.txt", "deleted.txt", "new.txt", "forced.ignored", "data.bin"}.issubset(paths))
        self.assertNotIn("skip.ignored", paths)
        self.assertTrue(all(r["unchanged"] for r in manifest["repositories"]))
        self.assertEqual(before, {str(p.relative_to(self.repo / ".git")): p.read_bytes() for p in (self.repo / ".git").rglob("*") if p.is_file()})
        result = group_review.validate(self.draft(manifest), manifest, self.context)
        self.assertFalse(result["review_complete"])

    def test_same_finding_deduplicates_sources_and_report_failure_can_retry(self):
        (self.repo / "new.txt").write_text("same problem\n", encoding="utf-8")
        run_git(self.repo, "add", "new.txt")
        manifest = self.capture()
        draft = self.draft(manifest)
        draft["findings"] = [{"priority": "P1", "title": "Problem", "impact": "Bad result", "trigger": "Use",
                              "expected": "Good", "actual": "Bad", "recommendation": "Fix", "evidence": c["evidence"]}
                             for c in draft["coverage"]]
        result = group_review.validate(draft, manifest, self.context)
        self.assertEqual(1, len(result["findings"]))
        self.assertEqual({"index", "working-tree"}, {e["source"] for e in result["findings"][0]["evidence"]})
        output = self.root / "reports"
        with mock.patch.object(group_review, "save", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                group_review.publish(self.context, draft, manifest, output, True)
        self.assertFalse(output.exists())
        self.assertTrue((self.context / "manifest.json").exists())
        markdown, result_file = group_review.publish(self.context, draft, manifest, output, True)
        self.assertIn("P1: 1", markdown.read_text(encoding="utf-8"))
        self.assertTrue(result_file.exists())
        self.assertTrue((output / "repo-000.md").exists())

    def test_changes_after_capture_mark_report_incomplete(self):
        (self.repo / "new.txt").write_text("reviewed\n", encoding="utf-8")
        manifest = self.capture()
        draft = self.draft(manifest)
        (self.repo / "new.txt").write_text("changed after capture\n", encoding="utf-8")
        _, output = group_review.publish(self.context, draft, manifest, None, True)
        self.assertFalse(json.loads(output.read_text(encoding="utf-8"))["review_complete"])

    def test_split_index_and_intent_to_add_preserve_real_staging_semantics(self):
        (self.repo / "intent.txt").write_text("working content\n", encoding="utf-8")
        run_git(self.repo, "add", "-N", "intent.txt")
        run_git(self.repo, "update-index", "--split-index")
        before = {str(p.relative_to(self.repo / ".git")): p.read_bytes() for p in (self.repo / ".git").rglob("*") if p.is_file()}
        manifest = self.capture()
        captured = manifest["repositories"][0]
        self.assertEqual("captured", captured["state"], captured.get("limitations"))
        self.assertEqual([], captured["changed_files"]["index"])
        self.assertEqual(["intent.txt"], [c["path"] for c in captured["changed_files"]["working-tree"]])
        self.assertEqual(before, {str(p.relative_to(self.repo / ".git")): p.read_bytes() for p in (self.repo / ".git").rglob("*") if p.is_file()})

    def test_fork_fetches_exact_missing_sha_and_missing_versions_never_fallback(self):
        source = self.root / "fork"
        run_git(self.root, "clone", str(self.repo), str(source))
        run_git(source, "config", "user.name", "Reviewer")
        run_git(source, "config", "user.email", "test@example.invalid")
        (source / "fork.txt").write_text("fork content\n", encoding="utf-8")
        run_git(source, "add", "fork.txt")
        run_git(source, "commit", "-m", "fork change")
        task = {"schema": mr_contract.TASK, "origin": "https://gitlab.example.invalid", "projectId": 11,
                "mrIid": 8, "sourceProjectId": 22, "targetProjectId": 11, "sourceBranch": "feature",
                "targetBranch": "main", "sourceSha": run_git(source, "rev-parse", "HEAD"),
                "targetSha": run_git(self.repo, "rev-parse", "main"), "repoPath": str(self.repo),
                "sourceRemoteUrl": str(source), "targetRemoteUrl": str(self.repo), "mode": "merge"}
        task_file = self.root / "task.json"
        task_file.write_text(json.dumps(task), encoding="utf-8")
        result = run_python(SCRIPTS / "git_review_context.py", "--mr-context", str(task_file), "--context-dir", str(self.context))
        self.assertEqual(0, result.returncode, result.stderr)
        manifest = json.loads(result.stdout)
        self.assertEqual(task["sourceSha"], manifest["head_sha"])
        task["sourceSha"] = "b" * 40
        task_file.write_text(json.dumps(task), encoding="utf-8")
        result = run_python(SCRIPTS / "git_review_context.py", "--mr-context", str(task_file), "--context-dir", str(self.root / "failed-context"))
        self.assertNotEqual(0, result.returncode)
        self.assertFalse((self.root / "failed-context/manifest.json").exists())

    def test_staged_bug_fixed_in_worktree_remains_in_staged_evidence(self):
        (self.repo / "app.py").write_text("bug = True\n", encoding="utf-8")
        run_git(self.repo, "add", "app.py")
        (self.repo / "app.py").write_text("bug = False\n", encoding="utf-8")
        git_dir = self.repo / ".git"
        before = {str(p.relative_to(git_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in git_dir.rglob("*") if p.is_file()}
        manifest = self.capture()
        repo = manifest["repositories"][0]
        self.assertEqual("captured", repo["state"])
        self.assertTrue(repo["unchanged"])
        self.assertFalse(manifest["network_used"])
        evidence = [r for r in repo["evidence_files"] if r["path"] == "app.py" and r["side"] == "result"]
        self.assertEqual({"index", "working-tree"}, {r["source"] for r in evidence})
        contents = {r["source"]: (self.context / r["file"]).read_text(encoding="utf-8") for r in evidence}
        self.assertIn("True", contents["index"])
        self.assertIn("False", contents["working-tree"])
        after = {str(p.relative_to(git_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in git_dir.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        draft = {"schema": group_review.RESULT, "summary": "暫存內容仍有問題。",
                 "coverage": [], "findings": [], "limitations": []}
        for record in evidence:
            item = {"repo": repo["repo"], "source": record["source"], "path": "app.py",
                    "ref": record["ref"], "line_start": 1, "line_end": 1}
            draft["coverage"].append({"repo": repo["repo"], "source": record["source"], "path": "app.py",
                                      "status": "reviewed", "evidence": [item]})
        found = {"priority": "P1", "title": "錯誤值", "impact": "錯誤結果", "trigger": "執行功能",
                 "expected": "False", "actual": "True", "recommendation": "修正暫存內容",
                 "evidence": draft["coverage"][0]["evidence"]}
        draft["findings"] = [found]
        result = group_review.validate(draft, manifest, self.context)
        self.assertEqual("先修正後提交", result["submission_recommendation"])
        self.assertEqual(1, result["priority_counts"]["P1"])
        (self.context / evidence[0]["file"]).write_text("modified", encoding="utf-8")
        with self.assertRaises(ValueError):
            group_review.validate(draft, manifest, self.context)

    def test_unborn_repo_and_invalid_repo_do_not_abort_other_repositories(self):
        unborn = self.root / "new repo"
        unborn.mkdir()
        run_git(unborn, "init")
        (unborn / "new.txt").write_text("new\n", encoding="utf-8")
        broken = self.root / "broken"
        broken.mkdir()
        (broken / ".git").write_text("gitdir: missing\n", encoding="utf-8")
        manifest = self.capture()
        states = {r["repo"]: r["state"] for r in manifest["repositories"]}
        self.assertEqual("error", states["broken"])
        self.assertEqual("captured", states["new repo"])
        self.assertEqual("clean", states[self.repo.name])

    def test_fixed_mr_task_and_report_binding(self):
        source = run_git(self.repo, "rev-parse", "HEAD")
        task = {"schema": mr_contract.TASK, "origin": "https://gitlab.example.invalid",
                "projectId": 11, "mrIid": 7, "sourceProjectId": 22, "targetProjectId": 11,
                "sourceBranch": "feature", "targetBranch": "main", "sourceSha": source,
                "targetSha": source, "repoPath": str(self.repo), "sourceRemoteUrl": "unused",
                "targetRemoteUrl": "unused", "mode": "merge"}
        task_file = self.root / "task.json"
        task_file.write_text(json.dumps(task), encoding="utf-8")
        result = run_python(SCRIPTS / "git_review_context.py", "--mr-context", str(task_file),
                            "--context-dir", str(self.context))
        self.assertEqual(0, result.returncode, result.stderr)
        manifest = json.loads(result.stdout)
        body, meta = mr_contract.bind_report("Review\r\n", manifest,
                                            {"review_status": "審查未完成", "context_sha256": "a" * 64,
                                             "priority_counts": {"P0": 0, "P1": 1, "P2": 0, "P3": 0}})
        self.assertEqual(task["targetSha"], meta["targetSha"])
        self.assertFalse(meta["reviewComplete"])
        self.assertIn("merge-review-report:", body)
        self.assertEqual(hashlib.sha256(b"Review").hexdigest(), meta["bodySha256"])
        manifest["head_sha"] = "b" * 40
        with self.assertRaises(ValueError):
            mr_contract.bind_report("Review", manifest, {})


if __name__ == "__main__":
    unittest.main()

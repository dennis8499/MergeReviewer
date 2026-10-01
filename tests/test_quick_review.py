from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from tests.support import (
    create_repository_fixture,
    load_module,
    remove_temporary_tree,
    run_git as git,
    run_python,
)


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "merge-reviewer" / "scripts" / "git_review_context.py"


def load_helper_module():
    return load_module("merge_reviewer_git_review_context", SCRIPT)


class QuickReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="merge-reviewer-test-")
        self.root = Path(self.temp.name)
        self.repo, self.remote = create_repository_fixture(self.root, name="repo with spaces")
        self.context_dirs: list[Path] = []

    def tearDown(self) -> None:
        for context_dir in self.context_dirs:
            remove_temporary_tree(context_dir)
        self.temp.cleanup()
        self.temp = None

    def run_helper(self, *args: str) -> tuple[subprocess.CompletedProcess[str], dict | None]:
        environment = os.environ.copy()
        result = run_python(SCRIPT, "--workspace", str(self.repo), *args, env=environment)
        payload = json.loads(result.stdout) if result.returncode == 0 else None
        if payload and payload.get("context_dir"):
            self.context_dirs.append(Path(payload["context_dir"]).parent)
        return result, payload

    def test_quick_uses_current_head_and_remote_default_branch(self) -> None:
        (self.repo / "local.txt").write_text("local\n", encoding="utf-8")
        git(self.repo, "add", "local.txt")
        git(self.repo, "commit", "-m", "local")
        result, payload = self.run_helper("--quick", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert payload is not None
        self.assertEqual(payload["base_input"], "origin/main")
        self.assertEqual(payload["head_input"], "HEAD")
        self.assertEqual(payload["remote_branch"], "main")
        self.assertIn("local.txt", {item["path"] for item in payload["changed_files"]})

    def test_quick_fetches_only_the_selected_remote_base(self) -> None:
        result, payload = self.run_helper("--quick", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert payload is not None
        self.assertEqual(payload["fetches"], [{"remote": "origin", "branch": "main", "input_ref": "origin/main"}])

    def test_quick_honors_a_custom_remote_default_branch(self) -> None:
        git(self.repo, "switch", "main")
        git(self.repo, "switch", "-c", "stable")
        (self.repo / "stable.txt").write_text("stable\n", encoding="utf-8")
        git(self.repo, "add", "stable.txt")
        git(self.repo, "commit", "-m", "stable")
        git(self.repo, "push", "origin", "stable")
        git(self.root, "--git-dir", str(self.remote), "symbolic-ref", "HEAD", "refs/heads/stable")
        git(self.repo, "switch", "feature")
        result, payload = self.run_helper("--quick", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert payload is not None
        self.assertEqual(payload["base_input"], "origin/stable")
        self.assertEqual(payload["remote_branch"], "stable")

    def test_multiple_remotes_require_selection(self) -> None:
        upstream = self.root / "upstream.git"
        git(self.root, "init", "--bare", str(upstream))
        git(self.root, "--git-dir", str(upstream), "symbolic-ref", "HEAD", "refs/heads/main")
        git(self.repo, "remote", "add", "upstream", str(upstream))
        git(self.repo, "push", "upstream", "main")
        result, _ = self.run_helper("--quick", "--format", "json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("origin", result.stderr)
        self.assertIn("建議：", result.stderr)
        self.assertIn("upstream", result.stderr)
        result, payload = self.run_helper("--quick", "--remote", "upstream", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert payload is not None
        self.assertEqual(payload["remote"], "upstream")

    def test_remote_only_changes_are_not_in_merge_scope(self) -> None:
        clone = self.root / "remote-work"
        git(self.root, "clone", str(self.remote), str(clone))
        git(clone, "config", "user.email", "test@example.invalid")
        git(clone, "config", "user.name", "Merge Reviewer Test")
        (clone / "remote.txt").write_text("remote\n", encoding="utf-8")
        git(clone, "add", ".")
        git(clone, "commit", "-m", "remote")
        git(clone, "push", "origin", "main")
        (self.repo / "local.txt").write_text("local\n", encoding="utf-8")
        git(self.repo, "add", "local.txt")
        git(self.repo, "commit", "-m", "local")
        git(self.repo, "fetch", "origin", "main")
        result, payload = self.run_helper("--quick", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert payload is not None
        self.assertEqual({item["path"] for item in payload["changed_files"]}, {"local.txt"})

    def test_working_tree_snapshot_preserves_files_and_index(self) -> None:
        (self.repo / "staged.txt").write_text("staged\n", encoding="utf-8")
        git(self.repo, "add", "staged.txt")
        (self.repo / "staged.txt").write_text("unstaged\n", encoding="utf-8")
        (self.repo / "deleted.txt").write_text("deleted\n", encoding="utf-8")
        git(self.repo, "add", "deleted.txt")
        (self.repo / "deleted.txt").unlink()
        (self.repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        status_before = git(self.repo, "status", "--porcelain=v1", "--untracked-files=all")
        index = Path(git(self.repo, "rev-parse", "--git-path", "index"))
        if not index.is_absolute():
            index = self.repo / index
        index_before = index.read_bytes()
        result, payload = self.run_helper("--quick", "--include-working-tree", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert payload is not None
        self.assertEqual(payload["review_scope"], "working-tree")
        self.assertTrue(Path(payload["context_dir"]).exists())
        self.assertTrue(payload["working_tree_unchanged"])
        self.assertEqual(status_before, git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"))
        self.assertEqual(index_before, index.read_bytes())
        paths = {item["path"] for item in payload["changed_files"]}
        self.assertTrue({"staged.txt", "deleted.txt", "untracked.txt"}.issubset(paths))
        bundle = Path(payload["context_dir"])
        patch = (bundle / "diff.patch").read_bytes()
        self.assertEqual(patch, (bundle / "working-tree.patch").read_bytes())
        self.assertEqual(payload["diff_patch_bytes"], len(patch))

    def test_diff_bundle_ignores_configured_textconv_and_reuses_patch(self) -> None:
        helper = load_helper_module()
        (self.repo / ".gitattributes").write_text("base.txt diff=masked\n", encoding="utf-8")
        git(self.repo, "add", ".gitattributes")
        git(self.repo, "commit", "-m", "configure diff driver")
        git(self.repo, "branch", "comparison-base")
        (self.repo / "base.txt").write_text("changed logic\n", encoding="utf-8")
        git(self.repo, "add", "base.txt")
        git(self.repo, "commit", "-m", "change source")
        git(self.repo, "config", "diff.masked.textconv", "git --version")
        context_dir = self.root / "textconv-context"
        args = helper.parse_args(
            ["--workspace", str(self.repo), "--base", "comparison-base", "--head", "HEAD",
             "--no-fetch", "--context-dir", str(context_dir)]
        )
        with mock.patch.object(helper, "run_git", wraps=helper.run_git) as run_git_spy:
            payload = helper.build_manifest(args)

        patch = (context_dir / "diff.patch").read_bytes()
        self.assertIn(b"+changed logic", patch)
        self.assertEqual(payload["diff_patch_bytes"], len(patch))
        main_patch_calls = [
            call for call in run_git_spy.call_args_list
            if call.args[1][: 1 + len(helper.DIFF_FLAGS)] == ["diff", *helper.DIFF_FLAGS]
            and "--binary" in call.args[1]
            and "--stat" not in call.args[1]
        ]
        self.assertEqual(len(main_patch_calls), 1)

    def test_read_only_git_commands_preserve_index_bytes(self) -> None:
        helper = load_helper_module()
        file_path = self.repo / "base.txt"
        file_stat = file_path.stat()
        os.utime(file_path, ns=(file_stat.st_atime_ns + 10_000_000_000, file_stat.st_mtime_ns + 10_000_000_000))
        index = Path(git(self.repo, "rev-parse", "--git-path", "index"))
        if not index.is_absolute():
            index = self.repo / index
        index_before = index.read_bytes()
        args = helper.parse_args(
            ["--workspace", str(self.repo), "--base", "main", "--head", "HEAD", "--no-fetch"]
        )
        with mock.patch.dict(os.environ, {"GIT_OPTIONAL_LOCKS": "1"}), mock.patch.object(
            helper.subprocess, "run", wraps=subprocess.run
        ) as git_run_spy:
            payload = helper.build_manifest(args)

        self.assertTrue(payload["working_tree_unchanged"])
        self.assertEqual(index_before, index.read_bytes())
        git_calls = [call for call in git_run_spy.call_args_list if call.args[0][0] == "git"]
        self.assertTrue(git_calls)
        self.assertTrue(all(call.kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0" for call in git_calls))

    def test_untracked_review_reports_are_excluded_from_working_tree_scope(self) -> None:
        reports = self.repo / "review-reports"
        reports.mkdir()
        (reports / "old.md").write_text("generated report\n", encoding="utf-8")
        git(self.repo, "add", "review-reports/old.md")
        result, payload = self.run_helper("--quick", "--include-working-tree", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert payload is not None
        self.assertNotIn("review-reports/old.md", {item["path"] for item in payload["changed_files"]})

    def test_dirty_submodule_with_non_ascii_path_is_reported_as_limitation(self) -> None:
        nested = self.root / "nested"
        git(self.root, "init", str(nested))
        git(nested, "config", "user.email", "test@example.invalid")
        git(nested, "config", "user.name", "Merge Reviewer Test")
        (nested / "nested.txt").write_text("nested\n", encoding="utf-8")
        git(nested, "add", ".")
        git(nested, "commit", "-m", "nested")
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                str(nested),
                "子模組",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "add submodule")
        (self.repo / "子模組" / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        result, payload = self.run_helper("--quick", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert payload is not None
        self.assertEqual(payload["dirty_submodule_paths"], ["子模組"])
        self.assertFalse(payload["review_complete"])

    def test_changed_submodule_gitlinks_are_classified_for_each_change_kind(self) -> None:
        helper = load_helper_module()
        submodule_path = "nested"
        git(
            self.repo,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(self.remote),
            submodule_path,
        )
        git(self.repo, "commit", "-m", "add submodule")
        git(self.repo, "branch", "submodule-base")

        add_result, add_payload = self.run_helper(
            "--base", "main", "--head", "HEAD", "--no-fetch", "--format", "json"
        )
        self.assertEqual(add_result.returncode, 0, add_result.stderr)
        assert add_payload is not None
        self.assertIn(submodule_path, add_payload["binary_or_submodule_paths"])
        self.assertTrue(any("未檢查內部程式碼" in note for note in add_payload["review_limitations"]))

        write_path = self.repo / submodule_path / "nested-change.txt"
        write_path.write_text("new nested commit\n", encoding="utf-8")
        git(self.repo / submodule_path, "add", "nested-change.txt")
        git(self.repo / submodule_path, "commit", "-m", "update submodule content")
        git(self.repo, "add", submodule_path)
        git(self.repo, "commit", "-m", "update submodule pointer")
        update_result, update_payload = self.run_helper(
            "--base", "submodule-base", "--head", "HEAD", "--no-fetch", "--format", "json"
        )
        self.assertEqual(update_result.returncode, 0, update_result.stderr)
        assert update_payload is not None
        self.assertIn(submodule_path, update_payload["binary_or_submodule_paths"])

        git(self.repo, "rm", "-f", submodule_path)
        git(self.repo, "commit", "-m", "remove submodule")
        delete_result, delete_payload = self.run_helper(
            "--base", "submodule-base", "--head", "HEAD", "--no-fetch", "--format", "json"
        )
        self.assertEqual(delete_result.returncode, 0, delete_result.stderr)
        assert delete_payload is not None
        self.assertIn(submodule_path, delete_payload["binary_or_submodule_paths"])

        (self.repo / submodule_path).write_text("regular file\n", encoding="utf-8")
        git(self.repo, "add", submodule_path)
        git(self.repo, "commit", "-m", "replace gitlink with file")
        type_result, type_payload = self.run_helper(
            "--base", "submodule-base", "--head", "HEAD", "--no-fetch", "--format", "json"
        )
        self.assertEqual(type_result.returncode, 0, type_result.stderr)
        assert type_payload is not None
        self.assertIn(submodule_path, type_payload["binary_or_submodule_paths"])

    def test_uninitialized_submodule_is_reported_as_incomplete(self) -> None:
        submodule_path = "nested"
        git(
            self.repo,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(self.remote),
            submodule_path,
        )
        git(self.repo, "commit", "-m", "add submodule")
        checkout = self.repo / submodule_path
        moved_checkout = self.root / "uninitialized-submodule-checkout"
        checkout.rename(moved_checkout)
        try:
            result, payload = self.run_helper(
                "--base", "main", "--head", "HEAD", "--no-fetch", "--format", "json"
            )
        finally:
            moved_checkout.rename(checkout)

        self.assertEqual(result.returncode, 0, result.stderr)
        assert payload is not None
        self.assertEqual(payload["dirty_submodule_paths"], [submodule_path])
        self.assertFalse(payload["review_complete"])
        self.assertIn(submodule_path, " ".join(payload["review_limitations"]))

    def test_modified_tracked_unicode_report_is_included_in_working_tree_scope(self) -> None:
        report = self.repo / "review-reports" / "審查 文件.md"
        report.parent.mkdir()
        report.write_text("original report\n", encoding="utf-8")
        git(self.repo, "add", "review-reports")
        git(self.repo, "commit", "-m", "add tracked review report")
        git(self.repo, "branch", "report-base")
        report.write_text("updated report\n", encoding="utf-8")

        result, payload = self.run_helper(
            "--quick",
            "--base",
            "report-base",
            "--no-fetch",
            "--include-working-tree",
            "--format",
            "json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        assert payload is not None
        self.assertIn("review-reports/審查 文件.md", {item["path"] for item in payload["changed_files"]})

    def test_binary_and_rename_changes_are_preserved_in_manifest(self) -> None:
        (self.repo / "binary.bin").write_bytes(b"\x00\x01\x02\xff")
        git(self.repo, "add", "binary.bin")
        git(self.repo, "commit", "-m", "binary")
        (self.repo / "renamed.bin").write_bytes((self.repo / "binary.bin").read_bytes())
        (self.repo / "binary.bin").unlink()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "rename")
        result, payload = self.run_helper("--quick", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert payload is not None
        paths = {item["path"] for item in payload["changed_files"]}
        self.assertIn("renamed.bin", paths)
        self.assertTrue(any(item["binary"] for item in payload["changed_files"]))

    def test_git_worktree_is_discovered_and_reviewable(self) -> None:
        worktree = self.root / "review worktree"
        git(self.repo, "worktree", "add", "-b", "review-worktree", str(worktree), "HEAD")
        result = run_python(SCRIPT, "--workspace", str(worktree), "--quick", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["quick"])

    def test_detached_head_is_rejected_by_quick_mode(self) -> None:
        git(self.repo, "switch", "--detach", "HEAD")
        result, _ = self.run_helper("--quick", "--format", "json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("detached HEAD", result.stderr)
        self.assertIn("建議：", result.stderr)

    def test_explicit_refs_and_direct_mode_remain_supported(self) -> None:
        (self.repo / "local.txt").write_text("local\n", encoding="utf-8")
        git(self.repo, "add", "local.txt")
        git(self.repo, "commit", "-m", "local")
        result, payload = self.run_helper(
            "--base", "origin/main", "--head", "HEAD", "--mode", "direct", "--format", "json"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        assert payload is not None
        self.assertFalse(payload["quick"])
        self.assertEqual(payload["mode"], "direct")
        self.assertIn("local.txt", {item["path"] for item in payload["changed_files"]})

    def test_explicit_head_symbol_does_not_trigger_a_fake_remote_fetch(self) -> None:
        result, payload = self.run_helper("--base", "origin/main", "--head", "HEAD", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert payload is not None
        self.assertEqual(payload["head_resolved_ref"], "HEAD")

    def test_local_branch_does_not_fetch_its_upstream(self) -> None:
        git(self.repo, "branch", "--set-upstream-to=origin/main", "feature")
        git(self.repo, "remote", "set-url", "origin", str(self.root / "missing-origin.git"))
        result, payload = self.run_helper(
            "--base", "feature", "--head", "HEAD", "--format", "json"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        assert payload is not None
        self.assertEqual(payload["base_resolved_ref"], "refs/heads/feature")
        self.assertEqual(payload["fetches"], [])

    def test_missing_local_branch_does_not_fall_back_to_remote_branch(self) -> None:
        git(self.repo, "push", "origin", "HEAD:refs/heads/release")
        git(self.repo, "fetch", "origin", "release")
        result, payload = self.run_helper(
            "--base", "release", "--head", "HEAD", "--no-fetch", "--format", "json"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(payload)
        self.assertIn("找不到基礎 ref", result.stderr)
        self.assertIn("本機分支", result.stderr)
        self.assertIn("建議：", result.stderr)

    def test_explicit_remote_branch_fetches_and_ignores_same_named_local_branch(self) -> None:
        git(self.repo, "push", "origin", "HEAD:refs/heads/release")
        git(self.repo, "switch", "-c", "release")
        (self.repo / "local-release.txt").write_text("local\n", encoding="utf-8")
        git(self.repo, "add", "local-release.txt")
        git(self.repo, "commit", "-m", "local release")
        local_sha = git(self.repo, "rev-parse", "refs/heads/release")
        git(self.repo, "switch", "feature")
        git(self.repo, "fetch", "origin", "release")
        remote_sha = git(self.repo, "rev-parse", "refs/remotes/origin/release")
        self.assertNotEqual(local_sha, remote_sha)
        result, payload = self.run_helper(
            "--base", "origin/release", "--head", "HEAD", "--format", "json"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        assert payload is not None
        self.assertEqual(payload["base_sha"], remote_sha)
        self.assertEqual(payload["base_resolved_ref"], "refs/remotes/origin/release")

    def test_deleted_remote_branch_is_not_satisfied_by_cached_tracking_ref(self) -> None:
        git(self.repo, "push", "origin", "HEAD:refs/heads/release")
        git(self.repo, "fetch", "origin", "release")
        git(self.root, "--git-dir", str(self.remote), "branch", "-D", "release")
        context_dir = self.root / "deleted-remote-context"
        result, payload = self.run_helper(
            "--base", "origin/release", "--head", "HEAD", "--context-dir", str(context_dir), "--format", "json"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(payload)
        self.assertIn("fetch origin/release 失敗", result.stderr)
        self.assertIn("建議：", result.stderr)
        self.assertFalse(context_dir.exists())

    def test_remote_ref_cannot_be_used_with_no_fetch(self) -> None:
        result, payload = self.run_helper(
            "--base", "origin/main", "--head", "HEAD", "--no-fetch", "--format", "json"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(payload)
        self.assertIn("--no-fetch", result.stderr)
        self.assertIn("建議：", result.stderr)

    def test_quick_auto_remote_ref_cannot_be_used_with_no_fetch(self) -> None:
        result, payload = self.run_helper("--quick", "--no-fetch", "--format", "json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(payload)
        self.assertIn("--no-fetch", result.stderr)
        self.assertIn("建議：", result.stderr)

    def test_missing_local_head_does_not_fall_back_to_remote_branch(self) -> None:
        git(self.repo, "push", "origin", "HEAD:refs/heads/release")
        git(self.repo, "fetch", "origin", "release")
        result, payload = self.run_helper(
            "--base", "HEAD", "--head", "release", "--no-fetch", "--format", "json"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(payload)
        self.assertIn("找不到比較 ref", result.stderr)
        self.assertIn("本機分支", result.stderr)

    def test_explicit_branch_namespace_does_not_fall_back_to_tag(self) -> None:
        git(self.repo, "tag", "release")
        result, payload = self.run_helper(
            "--base", "refs/heads/release", "--head", "HEAD", "--no-fetch", "--format", "json"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(payload)
        self.assertIn("找不到基礎 ref", result.stderr)
        self.assertIn("本機分支", result.stderr)

    def test_explicit_remote_namespace_is_supported_for_head(self) -> None:
        git(self.repo, "push", "origin", "HEAD:refs/heads/release")
        result, payload = self.run_helper(
            "--base", "HEAD", "--head", "refs/remotes/origin/release", "--format", "json"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        assert payload is not None
        self.assertEqual(payload["head_resolved_ref"], "refs/remotes/origin/release")

    def test_explicit_tag_sha_and_slash_branch_remain_supported(self) -> None:
        git(self.repo, "switch", "-c", "topic/login")
        (self.repo / "login.txt").write_text("login\n", encoding="utf-8")
        git(self.repo, "add", "login.txt")
        git(self.repo, "commit", "-m", "login")
        commit_sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "tag", "v1")
        for base in ("refs/tags/v1", commit_sha, "HEAD"):
            result, payload = self.run_helper(
                "--base", base, "--head", "HEAD", "--mode", "direct", "--format", "json"
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            assert payload is not None
            self.assertEqual(payload["mode"], "direct")

    def test_fetch_failure_stops_before_writing_context(self) -> None:
        context_dir = self.root / "failed-fetch-context"
        git(self.repo, "remote", "set-url", "origin", str(self.root / "missing-origin.git"))
        result, payload = self.run_helper(
            "--quick",
            "--base",
            "origin/main",
            "--include-working-tree",
            "--context-dir",
            str(context_dir),
            "--format",
            "json",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(payload)
        self.assertIn("fetch origin/main 失敗", result.stderr)
        self.assertFalse(context_dir.exists())

    def test_unqualified_missing_branch_stops_before_writing_context(self) -> None:
        upstream = self.root / "upstream.git"
        git(self.root, "init", "--bare", str(upstream))
        git(self.root, "--git-dir", str(upstream), "symbolic-ref", "HEAD", "refs/heads/main")
        git(self.repo, "remote", "add", "upstream", str(upstream))
        for remote in ("origin", "upstream"):
            git(self.repo, "push", remote, "HEAD:refs/heads/release")
            git(self.repo, "fetch", remote, "release")
        context_dir = self.root / "ambiguous-context"
        result, payload = self.run_helper(
            "--quick",
            "--base",
            "release",
            "--include-working-tree",
            "--no-fetch",
            "--context-dir",
            str(context_dir),
            "--format",
            "json",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(payload)
        self.assertIn("找不到基礎 ref", result.stderr)
        self.assertIn("本機分支", result.stderr)
        self.assertFalse(context_dir.exists())

    def test_unresolved_conflict_stops_before_writing_context(self) -> None:
        clone = self.root / "conflict-remote-work"
        git(self.root, "clone", str(self.remote), str(clone))
        git(clone, "config", "user.email", "test@example.invalid")
        git(clone, "config", "user.name", "Merge Reviewer Test")
        (clone / "base.txt").write_text("remote conflict\n", encoding="utf-8")
        git(clone, "add", "base.txt")
        git(clone, "commit", "-m", "remote conflict")
        git(clone, "push", "origin", "main")
        git(self.repo, "fetch", "origin", "main")
        (self.repo / "base.txt").write_text("local conflict\n", encoding="utf-8")
        git(self.repo, "add", "base.txt")
        git(self.repo, "commit", "-m", "local conflict")
        git(self.repo, "merge", "--no-edit", "origin/main", check=False)
        self.assertIn("UU base.txt", git(self.repo, "status", "--porcelain=v1"))
        context_dir = self.root / "conflict-context"
        result, payload = self.run_helper(
            "--quick",
            "--include-working-tree",
            "--context-dir",
            str(context_dir),
            "--format",
            "json",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(payload)
        self.assertIn("merge conflict", result.stderr)
        self.assertIn("建議：", result.stderr)
        self.assertFalse(context_dir.exists())

    def test_criss_cross_history_stops_before_writing_context(self) -> None:
        (self.repo / "a.txt").write_text("a\n", encoding="utf-8")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-m", "A1")
        a1 = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "branch", "side-b", "HEAD~1")
        git(self.repo, "switch", "side-b")
        (self.repo / "b.txt").write_text("b\n", encoding="utf-8")
        git(self.repo, "add", "b.txt")
        git(self.repo, "commit", "-m", "B1")
        git(self.repo, "switch", "feature")
        git(self.repo, "merge", "--no-ff", "side-b", "-m", "A2")
        git(self.repo, "switch", "side-b")
        git(self.repo, "merge", "--no-ff", a1, "-m", "B2")
        context_dir = self.root / "criss-cross-context"
        result, payload = self.run_helper(
            "--quick",
            "--base",
            "feature",
            "--no-fetch",
            "--include-working-tree",
            "--context-dir",
            str(context_dir),
            "--format",
            "json",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(payload)
        self.assertIn("多個共同祖先", result.stderr)
        self.assertIn("建議：", result.stderr)
        self.assertFalse(context_dir.exists())
        direct_result, direct_payload = self.run_helper(
            "--base",
            "feature",
            "--head",
            "side-b",
            "--mode",
            "direct",
            "--no-fetch",
            "--context-dir",
            str(self.root / "direct-criss-cross-context"),
            "--format",
            "json",
        )
        self.assertEqual(direct_result.returncode, 0, direct_result.stderr)
        assert direct_payload is not None
        self.assertIsNone(direct_payload["merge_base"])
        saved_manifest = json.loads(
            (self.root / "direct-criss-cross-context" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved_manifest, direct_payload)

    def test_unrelated_history_stops_before_writing_context(self) -> None:
        git(self.repo, "switch", "--orphan", "unrelated")
        (self.repo / "unrelated.txt").write_text("unrelated\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "unrelated root")
        git(self.repo, "switch", "feature")
        context_dir = self.root / "unrelated-context"
        result, payload = self.run_helper(
            "--quick",
            "--base",
            "unrelated",
            "--no-fetch",
            "--include-working-tree",
            "--context-dir",
            str(context_dir),
            "--format",
            "json",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(payload)
        self.assertIn("沒有共同祖先", result.stderr)
        self.assertIn("建議：", result.stderr)
        self.assertFalse(context_dir.exists())
        direct_result, direct_payload = self.run_helper(
            "--base",
            "unrelated",
            "--head",
            "feature",
            "--mode",
            "direct",
            "--no-fetch",
            "--context-dir",
            str(self.root / "direct-unrelated-context"),
            "--format",
            "json",
        )
        self.assertEqual(direct_result.returncode, 0, direct_result.stderr)
        assert direct_payload is not None
        self.assertEqual(direct_payload["schema_version"], 3)
        self.assertIsNone(direct_payload["merge_base"])
        self.assertEqual(direct_payload["diff_base"], direct_payload["base_sha"])
        saved_manifest = json.loads(
            (self.root / "direct-unrelated-context" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved_manifest, direct_payload)

    def test_shallow_repository_stops_before_writing_context(self) -> None:
        shallow = self.root / "shallow repo"
        git(self.root, "clone", "--depth", "1", "--no-local", str(self.remote), str(shallow))
        git(shallow, "switch", "-c", "feature")
        context_dir = self.root / "shallow-context"
        result = run_python(
            SCRIPT,
            "--workspace",
            str(shallow),
            "--quick",
            "--include-working-tree",
            "--context-dir",
            str(context_dir),
            "--format",
            "json",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("shallow clone", result.stderr)
        self.assertFalse(context_dir.exists())

    def test_snapshot_race_stops_and_cleans_temporary_snapshot(self) -> None:
        helper = load_helper_module()
        args = helper.parse_args(
            [
                "--workspace",
                str(self.repo),
                "--quick",
                "--include-working-tree",
                "--format",
                "json",
            ]
        )
        before = helper.snapshot(self.repo)
        after = dict(before)
        after["status"] = "race detected\n"
        temporary_dir = self.root / "race-snapshot"
        temporary_dir.mkdir()
        tree_sha = git(self.repo, "rev-parse", "HEAD^{tree}")
        with mock.patch.object(helper, "snapshot", side_effect=[before, after]), mock.patch.object(
            helper, "create_worktree_snapshot", return_value=(temporary_dir, {}, tree_sha)
        ):
            with self.assertRaises(helper.ReviewContextError) as raised:
                helper.build_manifest(args)
        self.assertIn("發生變更", str(raised.exception))
        self.assertFalse(temporary_dir.exists())

    def test_context_write_failure_cleans_new_context_directory(self) -> None:
        helper = load_helper_module()
        context_dir = self.root / "partial-context"
        args = helper.parse_args(
            [
                "--workspace",
                str(self.repo),
                "--quick",
                "--include-working-tree",
                "--context-dir",
                str(context_dir),
                "--format",
                "json",
            ]
        )

        original_write_bytes = Path.write_bytes

        def fail_while_writing_context(path, data):
            if path == context_dir / "diff.patch":
                raise OSError("simulated context write failure")
            return original_write_bytes(path, data)

        with mock.patch.object(Path, "write_bytes", new=fail_while_writing_context):
            with self.assertRaises(OSError):
                helper.build_manifest(args)
        self.assertFalse(context_dir.exists())

    def test_context_creation_race_preserves_other_process_directory(self) -> None:
        helper = load_helper_module()
        context_dir = self.root / "raced-context"
        args = helper.parse_args(
            ["--workspace", str(self.repo), "--base", "main", "--head", "HEAD",
             "--no-fetch", "--context-dir", str(context_dir)]
        )
        original_mkdir = Path.mkdir

        def race_mkdir(path, *args, **kwargs):
            if path == context_dir and not path.exists():
                original_mkdir(path, parents=True, exist_ok=False)
                (path / "belongs-to-other-process.txt").write_text("preserve me\n", encoding="utf-8")
            return original_mkdir(path, *args, **kwargs)

        with mock.patch.object(Path, "mkdir", new=race_mkdir):
            with self.assertRaises(FileExistsError):
                helper.build_manifest(args)
        self.assertEqual((context_dir / "belongs-to-other-process.txt").read_text(encoding="utf-8"), "preserve me\n")


if __name__ == "__main__":
    unittest.main()

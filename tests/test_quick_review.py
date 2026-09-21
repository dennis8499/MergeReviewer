from __future__ import annotations

import json
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "skills" / "merge-reviewer" / "scripts" / "git_review_context.py"


def load_helper_module():
    spec = importlib.util.spec_from_file_location("merge_reviewer_git_review_context", SCRIPT)
    if spec is None or spec.loader is None:
        raise AssertionError(f"unable to load helper module: {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def remove_tree(path: Path) -> None:
    if not path.exists():
        return

    def make_writable(function, target, _error):
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        function(target)

    shutil.rmtree(path, onerror=make_writable)


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


class QuickReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="merge-reviewer-test-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo with spaces"
        self.remote = self.root / "origin.git"
        self.context_dirs: list[Path] = []
        git(self.root, "init", "--bare", str(self.remote))
        git(self.root, "init", "-b", "main", str(self.repo))
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "user.name", "Merge Reviewer Test")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        (self.repo / "deleted.txt").write_text("kept until the scenario deletes it\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "base")
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "-u", "origin", "main")
        git(self.root, "--git-dir", str(self.remote), "symbolic-ref", "HEAD", "refs/heads/main")
        git(self.repo, "switch", "-c", "feature")

    def tearDown(self) -> None:
        for context_dir in self.context_dirs:
            remove_tree(context_dir)
        remove_tree(self.root)
        self.temp.cleanup()
        self.temp = None

    def run_helper(self, *args: str) -> tuple[subprocess.CompletedProcess[str], dict | None]:
        result = subprocess.run(
            ["python", str(SCRIPT), "--workspace", str(self.repo), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        payload = json.loads(result.stdout) if result.returncode == 0 else None
        if payload and payload.get("context_dir"):
            self.context_dirs.append(Path(payload["context_dir"]).parent)
        return result, payload

    def test_quick_uses_current_head_and_remote_default_branch(self) -> None:
        (self.repo / "local.txt").write_text("local\n", encoding="utf-8")
        git(self.repo, "add", "local.txt")
        git(self.repo, "commit", "-m", "local")
        result, payload = self.run_helper("--quick", "--no-fetch", "--format", "json")
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
        result, _ = self.run_helper("--quick", "--no-fetch", "--format", "json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("origin", result.stderr)
        self.assertIn("upstream", result.stderr)
        result, payload = self.run_helper("--quick", "--remote", "upstream", "--no-fetch", "--format", "json")
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
        result, payload = self.run_helper("--quick", "--no-fetch", "--format", "json")
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
        result, payload = self.run_helper("--quick", "--no-fetch", "--include-working-tree", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert payload is not None
        self.assertEqual(payload["review_scope"], "working-tree")
        self.assertTrue(Path(payload["context_dir"]).exists())
        self.assertTrue(payload["working_tree_unchanged"])
        self.assertEqual(status_before, git(self.repo, "status", "--porcelain=v1", "--untracked-files=all"))
        self.assertEqual(index_before, index.read_bytes())
        paths = {item["path"] for item in payload["changed_files"]}
        self.assertTrue({"staged.txt", "deleted.txt", "untracked.txt"}.issubset(paths))

    def test_untracked_review_reports_are_excluded_from_working_tree_scope(self) -> None:
        reports = self.repo / "review-reports"
        reports.mkdir()
        (reports / "old.md").write_text("generated report\n", encoding="utf-8")
        git(self.repo, "add", "review-reports/old.md")
        result, payload = self.run_helper("--quick", "--no-fetch", "--include-working-tree", "--format", "json")
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
        result, payload = self.run_helper("--quick", "--no-fetch", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert payload is not None
        self.assertEqual(payload["dirty_submodule_paths"], ["子模組"])
        self.assertFalse(payload["review_complete"])

    def test_binary_and_rename_changes_are_preserved_in_manifest(self) -> None:
        (self.repo / "binary.bin").write_bytes(b"\x00\x01\x02\xff")
        git(self.repo, "add", "binary.bin")
        git(self.repo, "commit", "-m", "binary")
        (self.repo / "renamed.bin").write_bytes((self.repo / "binary.bin").read_bytes())
        (self.repo / "binary.bin").unlink()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-m", "rename")
        result, payload = self.run_helper("--quick", "--no-fetch", "--format", "json")
        self.assertEqual(result.returncode, 0, result.stderr)
        assert payload is not None
        paths = {item["path"] for item in payload["changed_files"]}
        self.assertIn("renamed.bin", paths)
        self.assertTrue(any(item["binary"] for item in payload["changed_files"]))

    def test_git_worktree_is_discovered_and_reviewable(self) -> None:
        worktree = self.root / "review worktree"
        git(self.repo, "worktree", "add", "-b", "review-worktree", str(worktree), "HEAD")
        result = subprocess.run(
            ["python", str(SCRIPT), "--workspace", str(worktree), "--quick", "--no-fetch", "--format", "json"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["quick"])

    def test_detached_head_is_rejected_by_quick_mode(self) -> None:
        git(self.repo, "switch", "--detach", "HEAD")
        result, _ = self.run_helper("--quick", "--no-fetch", "--format", "json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("detached HEAD", result.stderr)

    def test_explicit_refs_and_direct_mode_remain_supported(self) -> None:
        (self.repo / "local.txt").write_text("local\n", encoding="utf-8")
        git(self.repo, "add", "local.txt")
        git(self.repo, "commit", "-m", "local")
        result, payload = self.run_helper(
            "--base", "origin/main", "--head", "HEAD", "--mode", "direct", "--no-fetch", "--format", "json"
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

    def test_ambiguous_base_ref_stops_before_writing_context(self) -> None:
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
        self.assertIn("存在多個 remote 候選", result.stderr)
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
            "--no-fetch",
            "--include-working-tree",
            "--context-dir",
            str(context_dir),
            "--format",
            "json",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(payload)
        self.assertIn("merge conflict", result.stderr)
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
        self.assertFalse(context_dir.exists())

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
        self.assertFalse(context_dir.exists())

    def test_shallow_repository_stops_before_writing_context(self) -> None:
        shallow = self.root / "shallow repo"
        git(self.root, "clone", "--depth", "1", "--no-local", str(self.remote), str(shallow))
        git(shallow, "switch", "-c", "feature")
        context_dir = self.root / "shallow-context"
        result = subprocess.run(
            [
                "python",
                str(SCRIPT),
                "--workspace",
                str(shallow),
                "--quick",
                "--no-fetch",
                "--include-working-tree",
                "--context-dir",
                str(context_dir),
                "--format",
                "json",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
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
                "--no-fetch",
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
                "--no-fetch",
                "--include-working-tree",
                "--context-dir",
                str(context_dir),
                "--format",
                "json",
            ]
        )

        def fail_after_creating_context(path, *_args, **_kwargs):
            path.mkdir(parents=True, exist_ok=False)
            raise OSError("simulated context write failure")

        with mock.patch.object(helper, "write_context_bundle", side_effect=fail_after_creating_context):
            with self.assertRaises(OSError):
                helper.build_manifest(args)
        self.assertFalse(context_dir.exists())


if __name__ == "__main__":
    unittest.main()

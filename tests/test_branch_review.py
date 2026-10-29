from __future__ import annotations
import contextlib
import copy
import io
import json
from pathlib import Path
from unittest import mock
import zipfile

from tests.fixtures import ReviewFixture, SCRIPTS, api, workspace, workspace_report, review_session
from tests.support import run_git as git, run_python, write_text


class BranchReviewTests(ReviewFixture):
    def test_three_business_inputs_are_required(self):
        for arguments in ([], ["--project", str(self.repo)], ["--project", str(self.repo), "--base", "main"]):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                api.parse_args(arguments)

    def test_removed_public_arguments_are_rejected(self):
        for extra in (["--quick"], ["--scope", "repo"], ["--target", "staged"], ["--target", "uncommitted"],
                      ["--group-root", str(self.root)], ["--include-working-tree"], ["--remote", "origin"],
                      ["--mode", "direct"], ["--mr-context", "task.json"]):
            with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                api.parse_args(["--project", str(self.repo), "--base", "main", "--head", "feature", *extra])

    def test_head_tags_and_commit_ids_are_not_branches(self):
        git(self.repo, "tag", "v1")
        commit = git(self.repo, "rev-parse", "HEAD")
        for value in ("HEAD", "v1", "refs/tags/v1", commit, "main~1"):
            with self.subTest(value=value), self.assertRaises(api.ReviewContextError):
                self.capture(head=value)

    def test_single_repo_container_disables_extension_and_requires_no_impact_graph(self):
        self.commit()
        context, manifest = self.capture()
        self.assertFalse(manifest["extension_enabled"])
        self.assertEqual([manifest["selected_repo"]], manifest["seed_repos"])
        draft = self.draft(context, manifest)
        draft.pop("impact_coverage")
        draft.pop("repository_coverage")
        result = workspace_report.validate(draft, manifest, context)
        self.assertTrue(result["review_complete"])
        self.assertEqual("disabled", result["extension_status"])

    def test_direct_git_root_and_single_folder_workspace_disable_extension(self):
        self.commit()
        workspace_file = self.root / "single.code-workspace"
        workspace_file.write_text(json.dumps({"folders": [{"path": "api"}]}), encoding="utf-8")
        for extra in ([], ["--workspace-file", str(workspace_file)]):
            _, manifest = self.capture(*extra, workspace_root=self.repo)
            self.assertFalse(manifest["extension_enabled"])

    def test_multi_repo_has_one_seed_and_related_repo_uses_current_head(self):
        consumer = self.make_repo("consumer")
        git(consumer, "branch", "feature")
        self.commit()
        write_text(consumer / "app.py", "local = 'excluded'\n")
        context, manifest = self.capture()
        self.assertTrue(manifest["extension_enabled"])
        self.assertEqual(1, len(manifest["seed_repos"]))
        record = next(r for r in manifest["repositories"] if r["path"] == str(consumer))
        self.assertEqual(git(consumer, "rev-parse", "HEAD"), record["sources"]["head"]["ref"])
        self.assertEqual(b"contract = 'old'\n", workspace.fixed_bytes(context, record, "head", "app.py", api))
        self.assertEqual([], record["changed_files"])

    def test_workspace_jsonc_is_the_boundary_and_deduplicates_roots(self):
        consumer = self.make_repo("nested/consumer")
        self.make_repo("outside")
        self.commit()
        workspace_file = self.root / "visible.code-workspace"
        workspace_file.write_text('{\n// visible folders\n"folders":[{"path":"api"},{"path":"nested/consumer"},{"path":"api"},],\n}', encoding="utf-8")
        _, manifest = self.capture("--workspace-file", str(workspace_file))
        self.assertEqual({str(self.repo), str(consumer)}, {r["path"] for r in manifest["repositories"]})
        self.assertTrue(manifest["extension_enabled"])

    def test_same_named_repositories_require_an_exact_path(self):
        self.make_repo("nested/api")
        self.commit()
        with self.assertRaises(api.ReviewContextError):
            self.capture(project="api")
        _, manifest = self.capture(project=str(self.repo))
        self.assertEqual(str(self.repo), workspace.repository(manifest, manifest["selected_repo"])["path"])

    def test_missing_workspace_folder_is_not_silently_ignored(self):
        workspace_file = self.root / "missing.code-workspace"
        workspace_file.write_text(json.dumps({"folders": [{"path": "api"}, {"path": "missing"}]}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "不存在"):
            self.capture("--workspace-file", str(workspace_file))

    def test_dirty_index_and_working_files_are_excluded_and_preserved(self):
        self.commit()
        write_text(self.repo / "app.py", "staged = True\n")
        git(self.repo, "add", "app.py")
        write_text(self.repo / "app.py", "working = True\n")
        write_text(self.repo / "untracked.py", "new = True\n")
        index = api.git_path(self.repo, "index")
        before = (index.read_bytes(), git(self.repo, "rev-parse", "HEAD"), git(self.repo, "status", "--porcelain"))
        context, manifest = self.capture()
        seed = workspace.repository(manifest, manifest["selected_repo"])
        self.assertEqual(b"contract = 'new'\n", workspace.fixed_bytes(context, seed, "head", "app.py", api))
        self.assertNotIn("untracked.py", {r["path"] for r in manifest["required_coverage"]})
        self.assertEqual(before, (index.read_bytes(), git(self.repo, "rev-parse", "HEAD"), git(self.repo, "status", "--porcelain")))

    def test_local_branch_does_not_fetch_upstream_or_fall_back_to_tag(self):
        git(self.repo, "remote", "add", "origin", str(self.root / "missing.git"))
        git(self.repo, "config", "branch.main.remote", "origin")
        git(self.repo, "config", "branch.main.merge", "refs/heads/main")
        git(self.repo, "tag", "release")
        self.commit()
        _, manifest = self.capture()
        self.assertEqual([], workspace.repository(manifest, manifest["selected_repo"])["native"]["fetches"])
        with self.assertRaises(api.ReviewContextError):
            self.capture(base="release")

    def test_explicit_remote_is_fetched_and_cannot_use_no_fetch_or_stale_cache(self):
        remote = self.root / "origin.git"
        git(self.root, "init", "--bare", str(remote))
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "push", "origin", "main")
        self.commit()
        _, manifest = self.capture(base="origin/main")
        native = workspace.repository(manifest, manifest["selected_repo"])["native"]
        self.assertEqual("refs/remotes/origin/main", native["base_resolved_ref"])
        self.assertEqual(["main"], [f["branch"] for f in native["fetches"]])
        with self.assertRaises(api.ReviewContextError):
            self.capture("--no-fetch", base="origin/main")
        git(self.root, "--git-dir", str(remote), "update-ref", "-d", "refs/heads/main")
        with self.assertRaisesRegex(api.ReviewContextError, "fetch"):
            self.capture(base="origin/main")

    def test_git_worktree_file_is_a_declared_git_root(self):
        worktree = self.root / "linked"
        git(self.repo, "worktree", "add", "-b", "linked", str(worktree), "main")
        self.commit()
        _, manifest = self.capture()
        self.assertTrue(manifest["extension_enabled"])
        self.assertIn(str(worktree), {r["path"] for r in manifest["repositories"]})

    def test_merge_preview_contains_base_changes_without_writing_repo_objects(self):
        self.commit()
        git(self.repo, "switch", "main")
        self.commit("base = True\n", "base.py")
        git(self.repo, "switch", "feature")
        objects = {p.relative_to(self.repo / ".git/objects").as_posix() for p in (self.repo / ".git/objects").rglob("*") if p.is_file()}
        context, manifest = self.capture()
        seed = workspace.repository(manifest, manifest["selected_repo"])
        self.assertEqual(b"base = True\n", workspace.fixed_bytes(context, seed, "merge-preview", "base.py", api))
        self.assertEqual(objects, {p.relative_to(self.repo / ".git/objects").as_posix() for p in (self.repo / ".git/objects").rglob("*") if p.is_file()})

    def test_conflicting_preview_is_incomplete(self):
        self.commit("contract = 'feature'\n")
        git(self.repo, "switch", "main")
        self.commit("contract = 'base'\n")
        git(self.repo, "switch", "feature")
        context, manifest = self.capture()
        self.assertFalse(manifest["context_complete"])
        self.assertTrue(manifest["context_gaps"])

    def test_rename_deletion_and_mode_changes_keep_fixed_old_paths(self):
        write_text(self.repo / "gone.py", "deleted = True\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "add source")
        git(self.repo, "branch", "-f", "main", "feature")
        git(self.repo, "mv", "app.py", "renamed.py")
        (self.repo / "gone.py").unlink()
        git(self.repo, "add", "-A")
        git(self.repo, "update-index", "--chmod=+x", "renamed.py")
        git(self.repo, "commit", "-m", "rename and delete")
        context, manifest = self.capture()
        paths = {r["path"] for r in manifest["required_coverage"]}
        self.assertTrue({"app.py", "renamed.py", "gone.py"} <= paths)
        draft = self.draft(context, manifest)
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])

    def test_multiple_seeds_or_extension_override_are_rejected(self):
        self.make_repo("consumer")
        self.commit()
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        tampered = copy.deepcopy(manifest)
        tampered["seed_repos"] = [r["id"] for r in manifest["repositories"]]
        with self.assertRaisesRegex(ValueError, "one selected"):
            workspace_report.validate(draft, tampered, context)
        tampered = copy.deepcopy(manifest)
        tampered["extension_enabled"] = False
        with self.assertRaisesRegex(ValueError, "Repo count"):
            workspace_report.validate(draft, tampered, context)

    def test_new_repo_after_capture_does_not_change_the_frozen_boundary(self):
        self.commit()
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        self.make_repo("later")
        result = workspace_report.validate(draft, manifest, context)
        self.assertTrue(result["review_complete"])
        self.assertEqual("disabled", result["extension_status"])

    def test_unavailable_candidate_cannot_be_claimed_excluded(self):
        candidate = self.root / "unborn"
        git(self.root, "init", "-b", "main", str(candidate))
        self.commit()
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        self.assertFalse(workspace_report.validate(draft, manifest, context)["review_complete"])

    def test_git_timeout_diagnostic_preserves_command_and_next_step(self):
        with mock.patch.object(api.subprocess, "run", side_effect=api.subprocess.TimeoutExpired(["git"], 1)):
            with self.assertRaisesRegex(api.ReviewContextError, "逾時") as caught:
                api.run_git(self.repo, ["status"])
        self.assertIn("--git-timeout", caught.exception.hint)

    def test_comparison_branch_drift_does_not_invalidate_the_captured_snapshot(self):
        self.commit()
        git(self.repo, "switch", "main")
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        git(self.repo, "branch", "-f", "feature", "main")
        result = workspace_report.validate(draft, manifest, context)
        self.assertTrue(result["review_complete"])
        self.assertFalse(any("head 分支" in gap for gap in result["limitations"]))

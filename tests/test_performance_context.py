"""Compatibility and output-size checks for capture performance changes."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
from unittest import mock

from tests.fixtures import ReviewFixture, api, workspace
from tests.support import remove_temporary_tree, run_git as git, write_text


def legacy_snapshot(repo):
    """The pre-optimization capture, kept as a differential compatibility oracle."""
    head = str(api.run_git(repo, ["rev-parse", "--verify", "HEAD"], check=False)).strip()
    branch = str(api.run_git(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)).strip()
    status = str(api.run_git(repo, ["status", "--porcelain=v1", "--untracked-files=all"], check=False))
    index_digest = api.file_digest(api.git_path(repo, "index"))
    tracked = set(str(api.run_git(repo, ["ls-files", "-z"])).split("\0"))
    paths = set(str(api.run_git(repo, ["ls-files", "-co", "--exclude-standard", "-z"])).split("\0"))
    digest = hashlib.sha256()
    for relative in sorted(paths - {""}):
        if relative.startswith("review-reports/") and relative not in tracked:
            continue
        path = repo / relative
        value = ((str(api.run_git(repo, ["ls-files", "-s", "--", relative], check=False)).strip() or "directory")
                 if path.is_dir() else api.file_digest(path))
        digest.update(relative.encode("utf-8", errors="surrogateescape") + b"\0")
        digest.update(value.encode("ascii", errors="replace") + b"\0")
    candidates = set()
    for entry in str(api.run_git(repo, ["ls-files", "-s", "-z"], check=False)).split("\0"):
        if entry and "\t" in entry:
            metadata, path = entry.split("\t", 1)
            if metadata.split(" ", 1)[0] == "160000":
                candidates.add(path)
    dirty = set()
    if candidates:
        sub_status = str(api.run_git(repo, ["status", "--porcelain=v1", "-z", "--untracked-files=all",
                                              "--ignore-submodules=none"], check=False))
        for record in sub_status.split("\0"):
            if len(record) < 4:
                continue
            path = record[3:].split(" -> ")[-1]
            if path in candidates and record[:2] != "  ":
                dirty.add(path)
        dirty.update(path for path in candidates if not (repo / path).exists() or not (repo / path / ".git").exists())
    return {"head": head, "branch": branch, "status": status, "index_digest": index_digest,
            "working_tree_digest": digest.hexdigest(), "dirty_submodules": sorted(dirty)}


class SnapshotPerformanceTests(ReviewFixture):
    def assert_compatible_snapshot(self):
        expected = legacy_snapshot(self.repo)
        with mock.patch.object(api, "run_git", wraps=api.run_git) as calls:
            actual = api.snapshot(self.repo)
        self.assertEqual(expected, actual)
        index_reads = [call.args[1] for call in calls.call_args_list
                       if call.args[1] in (["ls-files", "-s", "-z"], ["ls-files", "-z"])]
        self.assertEqual([["ls-files", "-s", "-z"]], index_reads)
        self.assertEqual(expected["working_tree_digest"], api.working_tree_digest(self.repo))
        self.assertEqual(expected["dirty_submodules"], api.dirty_submodule_paths(self.repo))
        return actual

    def test_clean_dirty_special_paths_missing_files_and_report_exclusions(self):
        names = ["space name.txt", "中文-é.txt", "[literal].txt", "-leading.txt", "review-reports/tracked.md"]
        if os.name != "nt":
            names += ['quote".txt', "tab\tname.txt", "line\nname.txt", "back\\slash.txt"]
        for name in names:
            write_text(self.repo / name, "tracked\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "special paths")
        clean = self.assert_compatible_snapshot()
        write_text(self.repo / "review-reports/untracked.md", "ignored review output\n")
        self.assertEqual(clean["working_tree_digest"], self.assert_compatible_snapshot()["working_tree_digest"])
        write_text(self.repo / "app.py", "staged\n")
        git(self.repo, "add", "app.py")
        write_text(self.repo / "app.py", "working\n")
        write_text(self.repo / "review-reports/tracked.md", "changed tracked report\n")
        write_text(self.repo / "untracked.txt", "included untracked\n")
        (self.repo / "space name.txt").unlink()
        dirty = self.assert_compatible_snapshot()
        self.assertNotEqual(clean["working_tree_digest"], dirty["working_tree_digest"])

    def test_conflicted_index_retains_every_stage_and_directory_marker(self):
        self.commit("feature\n")
        git(self.repo, "switch", "main")
        self.commit("main\n")
        git(self.repo, "merge", "feature", check=False)
        entries = api.tracked_index_entries(self.repo)
        self.assertEqual({"1", "2", "3"}, {metadata.split()[-1] for metadata, path in entries if path == "app.py"})
        self.assert_compatible_snapshot()
        (self.repo / "app.py").unlink()
        write_text(self.repo / "app.py/nested.txt", "directory replacement\n")
        self.assert_compatible_snapshot()

    def test_clean_dirty_and_missing_submodules_keep_snapshot_semantics(self):
        self.addCleanup(remove_temporary_tree, self.root)
        source = self.make_repo("module-source")
        git(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", str(source), "module space 中文")
        git(self.repo, "commit", "-am", "add module")
        self.assertEqual([], self.assert_compatible_snapshot()["dirty_submodules"])
        write_text(self.repo / "module space 中文/app.py", "dirty module\n")
        self.assertEqual(["module space 中文"], self.assert_compatible_snapshot()["dirty_submodules"])
        git(self.repo, "submodule", "deinit", "--force", "module space 中文")
        self.assertEqual(["module space 中文"], self.assert_compatible_snapshot()["dirty_submodules"])
        (self.repo / "module space 中文").rmdir()
        self.assertEqual(["module space 中文"], self.assert_compatible_snapshot()["dirty_submodules"])


class CaptureSummaryTests(ReviewFixture):
    def test_summary_indexes_complete_persisted_sources_and_patch_files(self):
        self.make_repo("consumer")
        self.commit()
        git(self.repo, "switch", "-c", "side", "main")
        self.commit("side = True\n", relative="side.py")
        git(self.repo, "switch", "feature")
        git(self.repo, "merge", "--no-ff", "side", "-m", "merge side")
        git(self.repo, "switch", "main")
        self.commit("base = True\n", relative="base.py")
        git(self.repo, "switch", "feature")
        context, manifest = self.capture()
        original = copy.deepcopy(manifest)
        summary = api.context_summary(manifest)
        self.assertEqual(original, manifest)
        self.assertEqual(manifest, json.loads((context / "manifest.json").read_text(encoding="utf-8")))
        self.assertEqual("merge-reviewer-workspace-summary/v1", summary["schema"])
        self.assertEqual(manifest["schema"], summary["context_schema"])
        self.assertEqual(str(context / "manifest.json"), summary["manifest_path"])
        for key in ("selected_repo", "extension_enabled", "required_coverage", "context_gaps", "context_complete"):
            self.assertEqual(manifest[key], summary[key])
        for compact in summary["repositories"]:
            full = workspace.repository(manifest, compact["id"])
            for key in ("name", "path", "role", "state", "review_source"):
                self.assertEqual(full[key], compact[key])
            for name, source in compact["sources"].items():
                self.assertEqual({"ref": full["sources"][name]["ref"], "tree_sha": full["sources"][name]["tree_sha"],
                                  "file_count": len(full["sources"][name]["files"])}, source)
            self.assertNotIn("changed_files", compact)
            for patch in compact["patches"]:
                self.assertTrue((context / patch).is_file())
        seed = next(r for r in summary["repositories"] if r["id"] == summary["selected_repo"])
        self.assertEqual(workspace.repository(manifest, manifest["selected_repo"])["native"]["merge_base"],
                         seed["native"]["merge_base"])
        self.assertTrue(any("merge-preview.patch" in path for path in seed["patches"]))
        self.assertEqual(2, len(seed["native"]["merge_commits"][0]["parent_views"]))
        for view in seed["native"]["merge_commits"][0]["parent_views"]:
            self.assertIn(view["patch"], seed["patches"])
            self.assertTrue((context / view["patch"]).is_file())
        self.assertNotIn("changed_files", seed["native"]["merge_preview"])
        self.assertLess(len(json.dumps(summary)), len(json.dumps(manifest)))

    def test_cli_default_is_full_manifest_and_summary_is_opt_in(self):
        self.commit()
        context, manifest = self.capture()
        arguments = ["--project", str(self.repo), "--base", "main", "--head", "feature"]
        for extra, expected in (([], manifest), (["--summary"], api.context_summary(manifest))):
            output = io.StringIO()
            with mock.patch.object(api, "build_manifest", return_value=manifest), contextlib.redirect_stdout(output):
                self.assertEqual(0, api.main([*arguments, *extra]))
            self.assertEqual(expected, json.loads(output.getvalue()))
        self.assertEqual(manifest, workspace.load(context))

    def test_summary_preserves_unavailable_candidate_and_conflicting_preview_gaps(self):
        unborn = self.root / "unborn"
        git(self.root, "init", "-b", "main", str(unborn))
        self.commit("feature\n")
        git(self.repo, "switch", "main")
        self.commit("base\n")
        git(self.repo, "switch", "feature")
        _, manifest = self.capture()
        summary = api.context_summary(manifest)
        candidate = next(r for r in summary["repositories"] if r["role"] == "candidate")
        self.assertEqual("error", candidate["state"])
        self.assertTrue(candidate["limitations"])
        self.assertFalse(summary["context_complete"])
        self.assertEqual(manifest["context_gaps"], summary["context_gaps"])
        seed = next(r for r in summary["repositories"] if r["role"] == "seed")
        self.assertEqual("conflicts", seed["native"]["merge_preview"]["status"])

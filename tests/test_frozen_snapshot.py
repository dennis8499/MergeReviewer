from __future__ import annotations

import copy
import os
import stat
from unittest import mock

from tests.fixtures import ReviewFixture, api, workspace, workspace_report, review_session
from tests.support import run_git as git, write_text


class FrozenSnapshotTests(ReviewFixture):
    def test_remote_updates_with_or_without_fetch_do_not_restart_snapshot_review(self):
        self.commit()
        remote = self.root / "origin.git"
        git(self.root, "init", "--bare", str(remote))
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "push", "origin", "main", "feature")
        context, manifest = self.capture(base="origin/main", head="origin/feature")
        draft = self.draft(context, manifest)
        seed = workspace.repository(manifest, manifest["selected_repo"])
        original = workspace_report.validate(draft, manifest, context)
        self.commit("contract = 'newer'\n")
        git(self.repo, "push", "origin", "feature")
        with mock.patch.object(api, "run_git", wraps=api.run_git) as calls:
            self.assertEqual(original, workspace_report.validate(draft, manifest, context))
        self.assertFalse(any(call.args[1][0] in ("fetch", "ls-remote") for call in calls.call_args_list))
        git(self.repo, "fetch", "origin", "+refs/heads/feature:refs/remotes/origin/feature")
        self.assertNotEqual(seed["sources"]["head"]["ref"], api.verified_commit(self.repo, "refs/remotes/origin/feature"))
        self.assertEqual(original, workspace_report.validate(draft, manifest, context))
        self.assertEqual(b"contract = 'new'\n", workspace.fixed_bytes(context, seed, "head", "app.py", api))

    def test_inherited_live_git_environment_cannot_override_the_owned_store(self):
        self.commit()
        context, manifest = self.capture()
        seed = workspace.repository(manifest, manifest["selected_repo"])
        other = self.root / "other-objects"
        other.mkdir()
        inherited = {"GIT_DIR": str(self.repo / ".git"), "GIT_COMMON_DIR": str(self.repo / ".git"),
                     "GIT_OBJECT_DIRECTORY": str(other),
                     "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(self.repo / ".git/objects")}
        with mock.patch.dict(os.environ, inherited):
            self.assertEqual(b"contract = 'new'\n", workspace.fixed_bytes(context, seed, "head", "app.py", api))

    def test_gc_and_removed_source_git_do_not_change_reads_searches_or_report(self):
        self.commit()
        git(self.repo, "switch", "main")
        self.commit("base = True\n", "base.py")
        git(self.repo, "switch", "feature")
        context, manifest = self.capture()
        seed = workspace.repository(manifest, manifest["selected_repo"])
        draft = self.draft(context, manifest)
        draft["findings"] = [self.finding(draft)]
        expected = workspace_report.validate(draft, manifest, context)
        versions = {(name, entry["path"]): workspace.fixed_bytes(context, seed, name, entry["path"], api)
                    for name, source in seed["sources"].items() for entry in source["files"] if entry["kind"] == "blob"}
        old_head = seed["sources"]["head"]["ref"]
        git(self.repo, "switch", "main")
        git(self.repo, "branch", "-D", "feature")
        git(self.repo, "reflog", "expire", "--expire=now", "--all")
        git(self.repo, "gc", "--prune=now")
        self.assertIsNone(api.verified_commit(self.repo, old_head))
        # Preserve the source metadata for fixture cleanup, but make it unavailable.
        (self.repo / ".git").rename(self.root / "removed-source-git")
        self.assertEqual(expected, workspace_report.validate(draft, manifest, context))
        for (name, path), content in versions.items():
            self.assertEqual(content, workspace.fixed_bytes(context, seed, name, path, api))
        result = workspace.search(context, manifest, "contract", [seed["id"]], api)
        self.assertEqual("complete", result["searches"][0]["status"])
        self.assertIn("contract = 'new'", result["searches"][0]["output"])
        text = workspace_report.render(workspace_report.validate(draft, manifest, context), manifest)
        self.assertIn("快照擷取", text)
        self.assertIn("僅適用於上述固定 SHA", text)

    def test_owned_store_damage_never_falls_back_to_available_source_objects(self):
        self.commit()
        context, manifest = self.capture()
        seed = workspace.repository(manifest, manifest["selected_repo"])
        draft = self.draft(context, manifest)
        source = seed["sources"]["head"]
        self.assertIsNotNone(api.verified_commit(self.repo, source["ref"]))
        for packed in workspace.evidence_repository(context, seed).joinpath("objects/pack").glob("*.pack"):
            packed.chmod(stat.S_IREAD | stat.S_IWRITE)
            packed.unlink()
        with self.assertRaises(api.ReviewContextError):
            workspace.fixed_bytes(context, seed, "head", "app.py", api)
        with self.assertRaises(api.ReviewContextError):
            workspace_report.validate(draft, manifest, context)

    def test_missing_store_and_borrowed_object_store_are_rejected(self):
        self.commit()
        context, manifest = self.capture()
        seed = workspace.repository(manifest, manifest["selected_repo"])
        draft = self.draft(context, manifest)
        bad = copy.deepcopy(manifest)
        workspace.repository(bad, manifest["selected_repo"])["snapshot"]["git_dir"] = "missing.git"
        with self.assertRaisesRegex(ValueError, "snapshot repository identity"):
            workspace_report.validate(draft, bad, context)
        original = seed["sources"]["head"]["objects"]
        seed["sources"]["head"]["objects"] = None
        with self.assertRaisesRegex(ValueError, "outside its frozen"):
            workspace.fixed_bytes(context, seed, "head", "app.py", api)
        seed["sources"]["head"]["objects"] = original
        alternate = workspace.evidence_repository(context, seed) / "objects/info/alternates"
        alternate.write_text(str(self.repo / ".git/objects"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "cannot borrow"):
            workspace_report.validate(draft, manifest, context)

    def test_snapshot_pack_failure_cleans_the_entire_owned_session(self):
        self.commit()
        created = []
        create = review_session.create_session

        def record(*args, **kwargs):
            result = create(*args, **kwargs)
            created.append(result)
            return result

        with mock.patch.object(review_session, "create_session", side_effect=record), \
                mock.patch.object(workspace, "seal_snapshot", side_effect=ValueError("pack failed")):
            with self.assertRaisesRegex(ValueError, "pack failed"):
                self.capture()
        self.assertEqual(1, len(created))
        self.assertFalse(created[0].exists())

    def test_failed_candidate_has_no_live_backed_partial_sources(self):
        consumer = self.make_repo("consumer")
        self.commit()
        seal = workspace.seal_snapshot

        def fail_candidate(context, repo, git_api):
            if repo["path"] == str(consumer):
                raise ValueError("candidate pack failed")
            return seal(context, repo, git_api)

        with mock.patch.object(workspace, "seal_snapshot", side_effect=fail_candidate):
            context, manifest = self.capture()
        candidate = next(repo for repo in manifest["repositories"] if repo["path"] == str(consumer))
        self.assertEqual("error", candidate["state"])
        self.assertEqual({}, candidate["sources"])
        with self.assertRaisesRegex(ValueError, "unknown fixed source"):
            workspace.read(context, manifest, candidate["id"], "app.py", api)
        draft = self.draft(context, manifest)
        contract = manifest["changed_contracts"][0]
        result = workspace.search(context, manifest, contract["search_terms"][0]["query"], [candidate["id"]],
                                  api, contract_id=contract["id"])
        self.assertEqual("unresolved", result["searches"][0]["status"])
        self.assertFalse(workspace_report.validate(draft, manifest, context)["review_complete"])

    def test_applicable_workspace_and_nested_instructions_are_frozen(self):
        write_text(self.root / "AGENTS.md", "Workspace initial instructions\n")
        self.commit("Repo initial instructions\n", "AGENTS.md")
        self.commit("Nested initial instructions\n", "nested/AGENTS.md")
        self.commit("nested = True\n", "nested/app.py")
        context, manifest = self.capture()
        repo_id = manifest["selected_repo"]
        write_text(self.root / "AGENTS.md", "Workspace updated instructions\n")
        write_text(self.repo / "AGENTS.md", "Repo updated instructions\n")
        write_text(self.repo / "nested/AGENTS.md", "Nested updated instructions\n")
        root = workspace.instructions(context, manifest, repo_id, "app.py", api)["instructions"]
        nested = workspace.instructions(context, manifest, repo_id, "nested/app.py", api)["instructions"]
        self.assertEqual(["Workspace initial instructions\n", "Repo initial instructions\n"],
                         [i["content"].replace("\r\n", "\n") for i in root])
        self.assertEqual(["Workspace initial instructions\n", "Repo initial instructions\n", "Nested initial instructions\n"],
                         [i["content"].replace("\r\n", "\n") for i in nested])
        stored = context / manifest["workspace_instructions"][0]["stored_path"]
        stored.write_text("changed stored instructions", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "instruction identity"):
            workspace.instructions(context, manifest, repo_id, "app.py", api)

    def test_legacy_v3_context_still_checks_mutable_branch_drift(self):
        self.commit()
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        manifest.pop("snapshot_policy")
        for repo in manifest["repositories"]:
            repo.pop("snapshot", None)
            for name, source in repo["sources"].items():
                source["objects"] = f"repos/{repo['id']}/native/merge-preview-objects" if name == "merge-preview" else None
        git(self.repo, "switch", "main")
        git(self.repo, "branch", "-f", "feature", "main")
        result = workspace_report.validate(draft, manifest, context)
        self.assertFalse(result["review_complete"])
        self.assertTrue(any("head 分支" in gap for gap in result["limitations"]))

    def test_removing_snapshot_metadata_cannot_enable_legacy_fallback(self):
        self.commit()
        context, manifest = self.capture()
        seed = workspace.repository(manifest, manifest["selected_repo"])
        seed.pop("snapshot")
        workspace.save(context, manifest)
        with self.assertRaisesRegex(ValueError, "owned snapshot"):
            workspace.load(context)
        with self.assertRaisesRegex(ValueError, "snapshot metadata is missing"):
            workspace.read(context, manifest, manifest["selected_repo"], "app.py", api)
        with self.assertRaisesRegex(ValueError, "snapshot metadata is missing"):
            workspace.fixed_bytes(context, seed, "head", "app.py", api)

    def test_sha256_snapshot_uses_the_original_object_format(self):
        target = self.root / "sha256"
        git(self.root, "init", "--bare", "--object-format=sha256", str(target))
        # Use a normal working repository for the existing preparation entry point.
        working = self.root / "sha256-work"
        git(self.root, "init", "--object-format=sha256", "-b", "main", str(working))
        git(working, "config", "user.name", "Snapshot Test")
        git(working, "config", "user.email", "snapshot@example.invalid")
        write_text(working / "app.py", "initial = True\n")
        git(working, "add", ".")
        git(working, "commit", "-m", "initial")
        git(working, "switch", "-c", "feature")
        self.commit("updated = True\n", repository=working)
        context, manifest = self.capture(workspace_root=working, project=working)
        seed = workspace.repository(manifest, manifest["selected_repo"])
        self.assertEqual("sha256", seed["snapshot"]["object_format"])
        self.assertEqual(64, len(seed["sources"]["head"]["ref"]))
        self.assertEqual(b"updated = True\n", workspace.fixed_bytes(context, seed, "head", "app.py", api))
        self.assertTrue(workspace_report.validate(self.draft(context, manifest), manifest, context)["review_complete"])

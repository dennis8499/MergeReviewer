"""Already-contained heads keep frozen evidence without reviewing base-only changes."""
from __future__ import annotations

import copy
import json
import stat
from unittest import mock

from tests.fixtures import ReviewFixture, api, workspace, workspace_report, review_report
from tests.support import run_git as git, write_text


class AncestorNoopTests(ReviewFixture):
    def ancestor_with_base_changes(self):
        for path in ("gone.py", "old.py", "mode.py"):
            self.commit(f"{path.split('.')[0]} = True\n", path)
        git(self.repo, "branch", "-f", "main", "feature")
        git(self.repo, "switch", "main")
        self.commit("historical_tree_only = True\n", "intermediate-only.py")
        self.intermediate_commit = git(self.repo, "rev-parse", "HEAD")
        self.intermediate_tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        git(self.repo, "rm", "intermediate-only.py")
        write_text(self.repo / "new.py", "new = True\n")
        git(self.repo, "rm", "gone.py")
        git(self.repo, "mv", "old.py", "renamed.py")
        mode_file = self.repo / "mode.py"
        mode_file.chmod(mode_file.stat().st_mode | 0o111)
        git(self.repo, "add", "-A")
        git(self.repo, "update-index", "--chmod=+x", "mode.py")
        git(self.repo, "commit", "-m", "advance base with file changes")
        git(self.repo, "switch", "feature")

    def capture_policy(self, **kwargs):
        context, manifest = self.capture(**kwargs)
        self.assertEqual(workspace.COVERAGE_POLICY, manifest["coverage_policy"])
        self.assertEqual(workspace.required_coverage(manifest), manifest["required_coverage"])
        return context, manifest

    def test_ancestor_base_changes_keep_raw_preview_and_snapshots_without_contract_searches(self):
        self.ancestor_with_base_changes()
        self.make_repo("consumer")
        with mock.patch.object(api, "run_git", wraps=api.run_git) as calls:
            context, manifest = self.capture_policy()
            seed = workspace.repository(manifest, manifest["selected_repo"])
            raw_changes = copy.deepcopy(seed["changed_files"])
            raw_preview = copy.deepcopy(seed["native"]["merge_preview"])
            draft = self.draft(context, manifest)
            result = workspace_report.validate(draft, manifest, context)
            workspace_report.validate_captured_evidence(manifest, context)
        self.assertTrue(workspace.ancestor_noop(manifest))
        self.assertEqual([], manifest["required_coverage"])
        self.assertEqual([], manifest["required_merge_views"])
        self.assertEqual([], manifest["changed_contracts"])
        self.assertEqual([], manifest["searches"])
        self.assertEqual([], draft["coverage"])
        self.assertEqual([], draft["impact_coverage"])
        self.assertEqual(raw_changes, seed["changed_files"])
        self.assertEqual(raw_preview, seed["native"]["merge_preview"])
        preview_paths = {path for change in raw_changes for path in (change["path"], change.get("old_path"))}
        self.assertTrue({"new.py", "gone.py", "old.py", "renamed.py", "mode.py"} <= preview_paths)
        self.assertTrue(all(repo.get("snapshot") for repo in manifest["repositories"]))
        self.assertEqual("沒有差異", result["review_status"])
        self.assertTrue(result["review_complete"])
        self.assertIn("比較分支已包含於基礎分支", workspace_report.render(result, manifest))
        self.assertFalse(any(call.args[1][0] in ("fetch", "ls-remote") for call in calls.call_args_list))

    def test_same_sha_is_an_ancestor_noop(self):
        context, manifest = self.capture_policy(base="main", head="main")
        seed = workspace.repository(manifest, manifest["selected_repo"])
        self.assertEqual([seed["native"]["base_sha"]], seed["ancestor_path"])
        self.assertTrue(workspace.ancestor_noop(manifest))
        self.assertTrue(workspace_report.validate(self.draft(context, manifest), manifest, context)["review_complete"])

    def test_multihop_proof_keeps_only_commit_objects_and_survives_removed_source(self):
        self.ancestor_with_base_changes()
        context, manifest = self.capture_policy()
        seed = workspace.repository(manifest, manifest["selected_repo"])
        self.assertEqual([seed["native"]["base_sha"], self.intermediate_commit, seed["native"]["head_sha"]],
                         seed["ancestor_path"])
        owned = workspace.evidence_repository(context, seed)
        environment = workspace.source_environment(context, seed, seed["sources"]["base"], api)
        self.assertEqual("commit", str(api.run_git(owned, ["cat-file", "-t", self.intermediate_commit],
                                                  env=environment)).strip())
        self.assertNotEqual(0, api.run_git_capture(owned, ["cat-file", "-e", self.intermediate_tree],
                                                 env=environment)[0])
        self.assertFalse(any(entry["path"] == "intermediate-only.py"
                             for source in seed["sources"].values() for entry in source["files"]))
        self.assertEqual([], manifest["required_coverage"])
        self.assertEqual([], manifest["required_merge_views"])
        draft = self.draft(context, manifest)
        git(self.repo, "reflog", "expire", "--expire=now", "--all")
        git(self.repo, "gc", "--prune=now")
        (self.repo / ".git").rename(self.root / "removed-source-git")
        with mock.patch.object(api, "run_git", wraps=api.run_git) as calls:
            self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])
            workspace_report.validate_captured_evidence(manifest, context)
            draft_path = context / "draft.json"
            draft_path.write_text('{"schema":', encoding="utf-8")
            with self.assertRaises(workspace_report.DraftValidationError):
                review_report.publish_report(context, draft_path, retain_on_validation_error=True)
            self.assertTrue(context.is_dir())
            draft_path.write_text(json.dumps(draft), encoding="utf-8")
            report = review_report.publish_report(context, draft_path, retain_on_validation_error=True)
        self.assertTrue(report.is_file())
        self.assertFalse(context.exists())
        self.assertFalse(any(call.args[1][0] in ("fetch", "ls-remote") for call in calls.call_args_list))

    def test_ancestor_path_follows_a_merge_second_parent(self):
        self.commit("contract = 'feature'\n")
        head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "main")
        self.commit("base_only = True\n", "base.py")
        git(self.repo, "merge", "feature", "--no-ff", "-m", "include feature as second parent")
        context, manifest = self.capture_policy()
        seed = workspace.repository(manifest, manifest["selected_repo"])
        self.assertEqual([seed["native"]["base_sha"], head], seed["ancestor_path"])
        self.assertTrue(workspace_report.validate(self.draft(context, manifest), manifest, context)["review_complete"])
        workspace_report.validate_captured_evidence(manifest, context)

    def test_coordinated_nonancestor_metadata_forgery_cannot_skip_review(self):
        self.commit("contract = 'new'\n")
        context, manifest = self.capture_policy()
        seed = workspace.repository(manifest, manifest["selected_repo"])
        native, sources = seed["native"], seed["sources"]
        self.assertFalse(workspace.ancestor_noop(manifest))
        native["merge_base"] = native["diff_base"] = seed["diff_base"] = native["head_sha"]
        sources["merge-base"] = copy.deepcopy(sources["head"])
        sources["merge-preview"] = copy.deepcopy(sources["base"])
        sources["merge-preview"]["ref"] = sources["base"]["tree_sha"]
        native["merge_preview"].update(status="clean", exit_code=0,
            tree_sha=sources["base"]["tree_sha"], changed_files=[])
        native["changed_files"] = seed["changed_files"] = []
        manifest["required_coverage"] = manifest["required_merge_views"] = []
        seed["ancestor_path"] = [native["base_sha"], native["head_sha"]]
        self.assertTrue(workspace.ancestor_noop(manifest))
        draft = self.draft(context, manifest)
        for validate in (lambda: workspace_report.validate(draft, manifest, context),
                         lambda: workspace_report.validate_captured_evidence(manifest, context)):
            with self.assertRaisesRegex(ValueError, "ancestor proof parent link") as raised:
                validate()
            self.assertNotIsInstance(raised.exception, workspace_report.DraftValidationError)

    def test_invalid_or_incomplete_paths_are_fatal_on_both_validation_paths(self):
        self.ancestor_with_base_changes()
        context, manifest = self.capture_policy()
        seed = workspace.repository(manifest, manifest["selected_repo"])
        path = seed["ancestor_path"]
        draft = self.draft(context, manifest)
        for bad in (None, [], "invalid", [path[0]], list(reversed(path)), [path[0], path[-1]],
                    path + [path[-1]], [path[0], seed["sources"]["base"]["tree_sha"], path[-1]]):
            with self.subTest(path=bad):
                changed = copy.deepcopy(manifest)
                workspace.repository(changed, changed["selected_repo"])["ancestor_path"] = bad
                for validate in (lambda: workspace_report.validate(draft, changed, context),
                                 lambda: workspace_report.validate_captured_evidence(changed, context)):
                    with self.assertRaises(ValueError) as raised:
                        validate()
                    self.assertNotIsInstance(raised.exception, workspace_report.DraftValidationError)

    def test_commit_hash_damage_and_missing_owned_proof_do_not_fall_back(self):
        self.ancestor_with_base_changes()
        context, manifest = self.capture_policy()
        seed = workspace.repository(manifest, manifest["selected_repo"])
        draft = self.draft(context, manifest)
        original = api.run_git

        def corrupt_proof(repo, arguments, **kwargs):
            raw = original(repo, arguments, **kwargs)
            if arguments == ["cat-file", "--batch"]:
                return raw.replace(b"advance base with file changes", b"Advance base with file changes", 1)
            return raw

        with mock.patch.object(api, "run_git", side_effect=corrupt_proof):
            for validate in (lambda: workspace_report.validate(draft, manifest, context),
                             lambda: workspace_report.validate_captured_evidence(manifest, context)):
                with self.assertRaisesRegex(ValueError, "ancestor proof commit checksum") as raised:
                    validate()
                self.assertNotIsInstance(raised.exception, workspace_report.DraftValidationError)
        for packed in workspace.evidence_repository(context, seed).joinpath("objects/pack").glob("*.pack"):
            packed.chmod(stat.S_IREAD | stat.S_IWRITE)
            packed.unlink()
        self.assertEqual("commit", str(original(self.repo, ["cat-file", "-t", self.intermediate_commit])).strip())
        for validate in (lambda: workspace_report.validate(draft, manifest, context),
                         lambda: workspace_report.validate_captured_evidence(manifest, context)):
            with self.assertRaises((ValueError, api.ReviewContextError)) as raised:
                validate()
            self.assertNotIsInstance(raised.exception, workspace_report.DraftValidationError)

    def test_ancestor_noop_does_not_hide_an_unavailable_candidate(self):
        self.ancestor_with_base_changes()
        git(self.root, "init", "-b", "main", str(self.root / "unborn"))
        context, manifest = self.capture_policy()
        self.assertTrue(workspace.ancestor_noop(manifest))
        result = workspace_report.validate(self.draft(context, manifest), manifest, context)
        self.assertFalse(result["review_complete"])
        self.assertEqual("incomplete", result["extension_status"])

    def test_nonancestor_same_final_tree_keeps_normal_coverage(self):
        self.commit("contract = 'same'\n")
        git(self.repo, "switch", "main")
        write_text(self.repo / "app.py", "contract = 'same'\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "same tree on a separate base history")
        git(self.repo, "switch", "feature")
        context, manifest = self.capture_policy()
        seed = workspace.repository(manifest, manifest["selected_repo"])
        self.assertEqual(seed["sources"]["base"]["tree_sha"], seed["sources"]["merge-preview"]["tree_sha"])
        self.assertNotEqual(seed["native"]["head_sha"], seed["native"]["merge_base"])
        self.assertFalse(workspace.ancestor_noop(manifest))
        self.assertTrue(manifest["required_coverage"])
        self.assertTrue(workspace_report.validate(self.draft(context, manifest), manifest, context)["review_complete"])

    def test_gaps_and_nonclean_preview_do_not_skip_base_preview_coverage(self):
        self.ancestor_with_base_changes()
        _, manifest = self.capture_policy()
        for kind in ("selected-gap", "native-gap", "workspace-gap", "incomplete", "conflict", "unavailable"):
            with self.subTest(kind=kind):
                changed = copy.deepcopy(manifest)
                seed = next(repo for repo in changed["repositories"] if repo["id"] == changed["selected_repo"])
                if kind == "selected-gap":
                    seed["limitations"].append("required source unavailable")
                elif kind == "native-gap":
                    seed["native"]["context_gaps"].append("required source unavailable")
                elif kind == "workspace-gap":
                    changed["context_gaps"].append("required source unavailable")
                elif kind == "incomplete":
                    seed["native"]["context_complete"] = False
                elif kind == "conflict":
                    seed["native"]["merge_preview"].update(status="conflicts", exit_code=1)
                else:
                    seed["native"]["merge_preview"].update(status="unavailable", tree_sha=None)
                    seed["sources"].pop("merge-preview")
                self.assertFalse(workspace.ancestor_noop(changed))
                self.assertTrue(workspace.required_coverage(changed))

    def test_real_conflict_and_unavailable_ancestor_preview_remain_incomplete(self):
        self.commit("contract = 'feature'\n")
        git(self.repo, "switch", "main")
        self.commit("contract = 'base'\n")
        git(self.repo, "switch", "feature")
        context, manifest = self.capture_policy()
        self.assertFalse(workspace.ancestor_noop(manifest))
        self.assertTrue(manifest["required_coverage"])
        self.assertFalse(workspace_report.validate(self.draft(context, manifest), manifest, context)["review_complete"])
        unavailable = {"status": "unavailable", "exit_code": 128, "detail": "fixture cannot produce preview",
                       "tree_sha": None, "changed_files": []}
        with mock.patch.object(api, "create_merge_preview", return_value=(unavailable, {})):
            context, manifest = self.capture_policy(base="main", head="main")
        self.assertFalse(workspace.ancestor_noop(manifest))
        self.assertFalse(workspace_report.validate(self.draft(context, manifest), manifest, context)["review_complete"])

    def test_unknown_policy_and_identity_tampering_are_fatal_on_both_validation_paths(self):
        self.ancestor_with_base_changes()
        context, manifest = self.capture_policy()
        draft = self.draft(context, manifest)
        for kind in ("unknown", "null", "removed", "head", "base", "merge-base", "diff-base", "preview-exit",
                     "preview-tree", "preview-source", "base-tree", "head-changes", "raw-head-changes"):
            with self.subTest(kind=kind):
                changed = copy.deepcopy(manifest)
                seed = next(repo for repo in changed["repositories"] if repo["id"] == changed["selected_repo"])
                native = seed["native"]
                if kind == "unknown":
                    changed["coverage_policy"] = "ancestor-noop/future"
                elif kind == "null":
                    changed["coverage_policy"] = None
                elif kind == "removed":
                    changed.pop("coverage_policy")
                elif kind == "head":
                    native["head_sha"] = native["base_sha"]
                elif kind == "base":
                    native["base_sha"] = native["head_sha"]
                elif kind == "merge-base":
                    native["merge_base"] = native["base_sha"]
                elif kind == "diff-base":
                    native["diff_base"] = native["base_sha"]
                elif kind == "preview-exit":
                    native["merge_preview"]["exit_code"] = 1
                elif kind == "preview-tree":
                    native["merge_preview"]["tree_sha"] = "0" * 40
                elif kind == "preview-source":
                    seed["sources"]["merge-preview"]["tree_sha"] = "0" * 40
                elif kind == "base-tree":
                    seed["sources"]["base"]["tree_sha"] = "0" * 40
                elif kind == "head-changes":
                    native["changed_files"] = copy.deepcopy(native["merge_preview"]["changed_files"])
                else:
                    seed["changed_files"][0]["source"] = "head"
                with self.assertRaises((ValueError, api.ReviewContextError)) as normal:
                    workspace_report.validate(draft, changed, context)
                self.assertNotIsInstance(normal.exception, workspace_report.DraftValidationError)
                with self.assertRaises((ValueError, api.ReviewContextError)) as retained:
                    workspace_report.validate_captured_evidence(changed, context)
                self.assertNotIsInstance(retained.exception, workspace_report.DraftValidationError)

    def test_unknown_policy_is_rejected_when_loading_the_context(self):
        context, manifest = self.capture_policy(base="main", head="main")
        manifest["coverage_policy"] = "ancestor-noop/future"
        workspace.save(context, manifest)
        with self.assertRaisesRegex(ValueError, "unsupported coverage policy"):
            workspace.load(context)

    def test_consistently_forged_tree_metadata_still_checks_the_owned_base_commit(self):
        self.ancestor_with_base_changes()
        context, manifest = self.capture_policy()
        draft = self.draft(context, manifest)
        seed = workspace.repository(manifest, manifest["selected_repo"])
        forged = seed["sources"]["head"]["tree_sha"]
        seed["sources"]["base"]["tree_sha"] = forged
        seed["sources"]["merge-preview"].update(ref=forged, tree_sha=forged)
        seed["native"]["merge_preview"]["tree_sha"] = forged
        self.assertTrue(workspace.ancestor_noop(manifest))
        with self.assertRaisesRegex(ValueError, "fixed tree identity"):
            workspace_report.validate(draft, manifest, context)
        with self.assertRaisesRegex(ValueError, "fixed tree identity"):
            workspace_report.validate_captured_evidence(manifest, context)

    def test_legacy_v3_keeps_base_preview_coverage(self):
        self.ancestor_with_base_changes()
        context, manifest = self.capture_policy()
        manifest.pop("coverage_policy")
        manifest["required_coverage"] = workspace.required_coverage(manifest)
        workspace.save(context, manifest)
        self.assertFalse(workspace.ancestor_noop(manifest))
        self.assertTrue(manifest["required_coverage"])
        draft = self.draft(context, manifest)
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])
        workspace_report.validate_captured_evidence(manifest, context)

    def test_repairable_draft_retains_valid_noop_evidence_then_publishes_without_fetch(self):
        self.ancestor_with_base_changes()
        context, manifest = self.capture_policy()
        draft_path = context / "draft.json"
        draft_path.write_text('{"schema":', encoding="utf-8")
        with self.assertRaises(workspace_report.DraftValidationError):
            review_report.publish_report(context, draft_path, retain_on_validation_error=True)
        self.assertTrue(context.is_dir())
        draft_path.write_text(json.dumps(self.draft(context, manifest)), encoding="utf-8")
        with mock.patch.object(api, "run_git", wraps=api.run_git) as calls:
            report = review_report.publish_report(context, draft_path, retain_on_validation_error=True)
        self.assertTrue(report.is_file())
        self.assertFalse(context.exists())
        self.assertFalse(any(call.args[1][0] in ("fetch", "ls-remote") for call in calls.call_args_list))

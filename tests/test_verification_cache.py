"""The validation cache saves repeated reads without trusting earlier operations."""
from __future__ import annotations

import copy
from unittest import mock

from tests.fixtures import ReviewFixture, api, workspace, workspace_report


class VerificationCacheTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.consumer = self.make_repo("consumer", {"app.py": "contract = 'consumer'\n"})
        self.commit()

    def test_shared_contract_and_evidence_reads_preserve_validated_result(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        original = manifest["changed_contracts"][0]
        second = copy.deepcopy(original)
        second["id"] = "C-second"
        second.pop("anchor_sha256")
        workspace.register_contract(context, manifest, second, api)
        draft["impact_coverage"][0]["contract_ids"].append(second["id"])
        for row in draft["repository_coverage"]:
            if row["status"] == "seed":
                continue
            for term in second["search_terms"]:
                result = workspace.search(context, manifest, term["query"], [row["repo"]], api,
                                          contract_id=second["id"])
                row["search_ids"].extend(s["id"] for s in result["searches"])

        class Uncached(workspace.FixedSourceCache):
            def read(self, repo, source_name, path):
                return workspace.fixed_bytes(self.context, repo, source_name, path, self.api)

            def changed_patch(self, repo, source_name, baseline, path):
                # A fresh instance for each request is equivalent to no patch cache.
                return workspace.FixedSourceCache(self.context, self.api).changed_patch(
                    repo, source_name, baseline, path)

        with mock.patch.object(api, "run_git", wraps=api.run_git) as calls:
            cached = workspace_report.validate(draft, manifest, context)
            cached_commands = [c.args[1] for c in calls.call_args_list]
        with mock.patch.object(workspace_report, "FixedSourceCache", Uncached):
            with mock.patch.object(api, "run_git", wraps=api.run_git) as calls:
                uncached = workspace_report.validate(draft, manifest, context)
                uncached_commands = [c.args[1] for c in calls.call_args_list]
        self.assertEqual(uncached, cached)
        self.assertTrue(cached["review_complete"])
        for command in ("cat-file", "diff"):
            self.assertLess(sum(c[0] == command for c in cached_commands),
                            sum(c[0] == command for c in uncached_commands))

    def test_same_path_is_isolated_by_repo_version_and_object_store(self):
        context, manifest = self.capture()
        cache = workspace.FixedSourceCache(context, api)
        seed = workspace.repository(manifest, manifest["selected_repo"])
        consumer = next(r for r in manifest["repositories"] if r["path"] == str(self.consumer))
        self.assertEqual(b"contract = 'new'\n", cache.read(seed, "head", "app.py"))
        self.assertEqual(b"contract = 'old'\n", cache.read(seed, "base", "app.py"))
        self.assertEqual(b"contract = 'consumer'\n", cache.read(consumer, "head", "app.py"))
        with mock.patch.object(workspace, "fixed_bytes", wraps=workspace.fixed_bytes) as calls:
            cache.read(seed, "head", "app.py")
            self.assertEqual(0, calls.call_count)
            (context / "alternate-objects").mkdir()
            seed["sources"]["head"]["objects"] = "alternate-objects"
            self.assertEqual(b"contract = 'new'\n", cache.read(seed, "head", "app.py"))
            self.assertEqual(1, calls.call_count)
        (context / "alternate-objects").rmdir()
        with self.assertRaisesRegex(ValueError, "storage is missing"):
            cache.read(seed, "head", "app.py")

    def test_changed_entry_or_unsafe_path_cannot_reuse_cached_bytes(self):
        context, manifest = self.capture()
        seed = workspace.repository(manifest, manifest["selected_repo"])
        cache = workspace.FixedSourceCache(context, api)
        cache.read(seed, "head", "app.py")
        entry = next(f for f in seed["sources"]["head"]["files"] if f["path"] == "app.py")
        entry["blob_sha"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "blob identity mismatch"):
            cache.read(seed, "head", "app.py")
        with self.assertRaisesRegex(ValueError, "invalid relative path"):
            cache.read(seed, "head", "../app.py")

    def test_next_validation_reads_objects_again_and_still_checks_tree_identity(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])
        real_git = api.run_git

        def changed_object(repo, args, **kwargs):
            if args[0] == "cat-file":
                return b"object changed after the previous validation\n"
            return real_git(repo, args, **kwargs)

        with mock.patch.object(api, "run_git", side_effect=changed_object):
            with self.assertRaisesRegex(ValueError, "blob identity mismatch"):
                workspace_report.validate(draft, manifest, context)
        seed = workspace.repository(manifest, manifest["selected_repo"])
        seed["sources"]["head"]["tree_sha"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "tree identity mismatch"):
            workspace_report.validate(draft, manifest, context)

    def test_patch_cache_keeps_baseline_source_and_path_separate(self):
        context, manifest = self.capture()
        seed = workspace.repository(manifest, manifest["selected_repo"])
        cache = workspace.FixedSourceCache(context, api)
        head = seed["sources"]["head"]["ref"]
        baseline = seed["diff_base"]
        changed = cache.changed_patch(seed, "head", baseline, "app.py")
        self.assertIn("+contract = 'new'", changed)
        self.assertEqual("", cache.changed_patch(seed, "head", head, "app.py"))
        self.assertEqual("", cache.changed_patch(seed, "head", baseline, "absent.py"))
        with mock.patch.object(api, "run_git", wraps=api.run_git) as calls:
            self.assertEqual(changed, cache.changed_patch(seed, "head", baseline, "app.py"))
            self.assertFalse(any(c.args[1][0] == "diff" for c in calls.call_args_list))

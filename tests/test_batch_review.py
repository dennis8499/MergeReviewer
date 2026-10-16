from __future__ import annotations

import copy
import json
import os
from unittest import mock

from tests.fixtures import ReviewFixture, SCRIPTS, api, workspace, workspace_report
from tests.support import run_python, run_git as git


class BatchReviewTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.commit()

    def requests_file(self, requests):
        path = self.root / "batch requests.json"
        path.write_text(json.dumps(requests, ensure_ascii=False), encoding="utf-8")
        return path

    def command(self, context, operation, requests):
        return run_python(SCRIPTS / "review_workspace.py", operation, "--context-dir", str(context),
                          "--requests-file", str(self.requests_file(requests)))

    def test_batch_read_equals_single_commands_with_default_and_explicit_sources(self):
        special = "有 空格/程式.py"
        self.commit("contract = 'unicode'\nsecond = '值'\n", relative=special)
        context, manifest = self.capture()
        repo_id = manifest["selected_repo"]
        requests = [{"repo": repo_id, "path": special},
                    {"repo": repo_id, "source": "base", "path": "app.py"},
                    {"repo": repo_id, "source": "head", "path": "app.py"}]
        expected = []
        for request in requests:
            arguments = ["read", "--context-dir", str(context), "--repo", repo_id, "--path", request["path"]]
            if "source" in request:
                arguments.extend(["--source", request["source"]])
            result = run_python(SCRIPTS / "review_workspace.py", *arguments)
            self.assertEqual(0, result.returncode, result.stderr)
            expected.append(json.loads(result.stdout))
        result = self.command(context, "batch-read", requests)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({"reads": expected}, json.loads(result.stdout))
        self.assertEqual(manifest, workspace.load(context))

    def test_batch_read_preserves_single_read_behavior_for_newline_paths(self):
        # A Git tree can contain a newline even on hosts whose worktree cannot.
        relative = "line\nbreak.py"
        context, manifest = self.capture()
        repo = workspace.repository(manifest, manifest["selected_repo"])
        git_repo = workspace.evidence_repository(context, repo)
        env = workspace.source_environment(context, repo, repo["sources"]["head"], api)
        blob = str(api.run_git(git_repo, ["hash-object", "-w", "--stdin"],
                               input_bytes=b"newline = 'contract'\n", env=env)).strip()
        tree = str(api.run_git(git_repo, ["mktree", "-z"],
                               input_bytes=f"100644 blob {blob}\t{relative}\0".encode("utf-8"), env=env)).strip()
        repo["sources"]["newline-source"] = {
            "ref": tree, "tree_sha": tree, "objects": repo["sources"]["head"]["objects"],
            "files": [{"path": relative, "mode": "100644", "kind": "blob", "blob_sha": blob}],
        }
        workspace.save(context, manifest)
        request = {"repo": repo["id"], "source": "newline-source", "path": relative}
        single = run_python(SCRIPTS / "review_workspace.py", "read", "--context-dir", str(context),
                            "--repo", repo["id"], "--source", "newline-source", "--path", relative)
        result = self.command(context, "batch-read", [request])
        if os.name == "nt":
            # Existing safe-path filesystem checks reject newline names on Windows.
            self.assertEqual(2, single.returncode)
            self.assertEqual(single.returncode, result.returncode)
            self.assertEqual(single.stderr, result.stderr)
            return
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(0, single.returncode, single.stderr)
        expected = json.loads(single.stdout)
        self.assertEqual({"reads": [expected]}, json.loads(result.stdout))
        self.assertEqual(relative, expected["path"])

    def test_batch_search_equals_sequential_ledger_and_checkpoints_each_query(self):
        self.make_repo("consumer")
        context, manifest = self.capture()
        self.draft(context, manifest)  # Register the actual frozen changed contract.
        contract = manifest["changed_contracts"][0]
        requests = [
            {"query": contract["search_terms"][0]["query"], "contract": contract["id"]},
            {"repo": [manifest["selected_repo"]], "source": "base",
             "query": contract["search_terms"][1]["query"], "contract": contract["id"]},
        ]
        before = copy.deepcopy(manifest)
        expected = []
        for request in requests:
            value = workspace.search(context, manifest, request["query"], request.get("repo"), api,
                                     request.get("source"), request["contract"])
            expected.extend(value["searches"])
        sequential_manifest = copy.deepcopy(manifest)
        workspace.save(context, before)
        result = self.command(context, "batch-search", requests)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({"searches": expected}, json.loads(result.stdout))
        self.assertEqual(sequential_manifest, workspace.load(context))
        with mock.patch.object(workspace, "save", wraps=workspace.save) as save:
            workspace.batch_search(context, before, requests, api)
        self.assertEqual(len(requests), save.call_count)

    def test_invalid_later_search_keeps_completed_query_and_stops_before_following_query(self):
        context, manifest = self.capture()
        repo_id = manifest["selected_repo"]
        requests = [{"repo": [repo_id], "query": "contract"}, {"query": ""}, {"query": "old"}]
        expected = workspace.search(context, copy.deepcopy(manifest), "contract", [repo_id], api)
        workspace.save(context, manifest)
        result = self.command(context, "batch-search", requests)
        self.assertEqual(2, result.returncode)
        self.assertIn("search query must be nonempty", result.stderr)
        self.assertEqual("", result.stdout)
        self.assertEqual(expected["searches"], workspace.load(context)["searches"])

    def test_unresolved_search_source_retains_existing_record_behavior_and_continues(self):
        context, manifest = self.capture()
        requests = [{"query": "contract", "source": "missing"}, {"query": "contract"}]
        result = self.command(context, "batch-search", requests)
        self.assertEqual(0, result.returncode, result.stderr)
        records = json.loads(result.stdout)["searches"]
        self.assertEqual(["unresolved", "complete"], [r["status"] for r in records])
        self.assertEqual(["S-0001", "S-0002"], [r["id"] for r in records])
        self.assertEqual(records, workspace.load(context)["searches"])

    def test_empty_and_invalid_batch_requests_use_exit_two_without_ledger_changes(self):
        context, manifest = self.capture()
        for operation, key in (("batch-read", "reads"), ("batch-search", "searches")):
            with self.subTest(operation=operation, requests=[]):
                result = self.command(context, operation, [])
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual({key: []}, json.loads(result.stdout))
            for requests in ({}, [None], [{}], [{"unknown": "value"}], [{"source": []}]):
                with self.subTest(operation=operation, requests=requests):
                    result = self.command(context, operation, requests)
                    self.assertEqual(2, result.returncode, result.stderr)
                    self.assertIn("workspace-review:", result.stderr)
                    self.assertEqual("", result.stdout)
            missing = run_python(SCRIPTS / "review_workspace.py", operation, "--context-dir", str(context))
            self.assertEqual(2, missing.returncode)
            self.assertIn("needs --requests-file", missing.stderr)
            malformed_path = self.requests_file([])
            malformed_path.write_text("[", encoding="utf-8")
            malformed = run_python(SCRIPTS / "review_workspace.py", operation, "--context-dir", str(context),
                                   "--requests-file", str(malformed_path))
            self.assertEqual(2, malformed.returncode)
        for requests in ([{"query": "contract", "repo": "repo-000"}],
                         [{"query": ["contract"]}], [{"query": "contract", "contract": []}]):
            result = self.command(context, "batch-search", requests)
            self.assertEqual(2, result.returncode, result.stderr)
        self.assertEqual(manifest, workspace.load(context))

    def test_binary_read_fails_identically_and_does_not_run_later_reads(self):
        (self.repo / "blob.bin").write_bytes(b"\0binary")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "binary")
        context, manifest = self.capture()
        request = {"repo": manifest["selected_repo"], "path": "blob.bin"}
        single = run_python(SCRIPTS / "review_workspace.py", "read", "--context-dir", str(context),
                            "--repo", request["repo"], "--path", request["path"])
        batch = self.command(context, "batch-read", [request, {"repo": request["repo"], "path": "missing"}])
        self.assertEqual(2, single.returncode)
        self.assertEqual(single.returncode, batch.returncode)
        self.assertEqual(single.stderr, batch.stderr)

    def test_read_error_stops_before_later_reads(self):
        context, manifest = self.capture()
        repo_id = manifest["selected_repo"]
        requests = [{"repo": repo_id, "path": "app.py"},
                    {"repo": repo_id, "path": "missing"},
                    {"repo": repo_id, "path": "app.py"}]
        with mock.patch.object(workspace, "read", wraps=workspace.read) as read:
            with self.assertRaisesRegex(ValueError, "fixed blob"):
                workspace.batch_read(context, manifest, requests, api)
        self.assertEqual(2, read.call_count)


class FixedSourceCacheTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.commit()

    def test_read_cache_reuses_verified_bytes_and_rechecks_entry_and_path(self):
        context, manifest = self.capture()
        repo = workspace.repository(manifest, manifest["selected_repo"])
        cache = workspace.FixedSourceCache(context, api)
        with mock.patch.object(api, "run_git", wraps=api.run_git) as run:
            first = cache.read(repo, "head", "app.py")
            self.assertEqual(2, run.call_count)
            self.assertEqual(first, cache.read(repo, "head", "app.py"))
            self.assertEqual(2, run.call_count)
            repo["sources"]["head"]["files"][0]["blob_sha"] = "0" * 40
            with self.assertRaisesRegex(ValueError, "blob identity"):
                cache.read(repo, "head", "app.py")
        with self.assertRaisesRegex(ValueError, "relative path"):
            cache.read(repo, "head", "../app.py")
        repo["sources"]["head"]["files"].append(copy.deepcopy(repo["sources"]["head"]["files"][0]))
        with self.assertRaisesRegex(ValueError, "fixed blob"):
            cache.read(repo, "head", "app.py")

    def test_cache_distinguishes_inherited_git_object_environment(self):
        context, manifest = self.capture()
        repo = workspace.repository(manifest, manifest["selected_repo"])
        cache = workspace.FixedSourceCache(context, api)
        alternate = context / "alternate-objects"
        alternate.mkdir()
        with mock.patch.object(workspace, "fixed_bytes", wraps=workspace.fixed_bytes) as read:
            expected = cache.read(repo, "head", "app.py")
            with mock.patch.dict(os.environ, {"GIT_ALTERNATE_OBJECT_DIRECTORIES": str(alternate)}):
                self.assertEqual(expected, cache.read(repo, "head", "app.py"))
            self.assertEqual(2, read.call_count)

    def test_json_metadata_keeps_uncached_blob_and_mode_validation_behavior(self):
        context, manifest = self.capture()
        repo = workspace.repository(manifest, manifest["selected_repo"])
        cache = workspace.FixedSourceCache(context, api)
        expected = workspace.fixed_bytes(context, repo, "head", "app.py", api)
        for value in ([], {"unexpected": "metadata"}):
            for field in ("tree_sha", "mode", "blob_sha"):
                with self.subTest(field=field, value=value):
                    changed = copy.deepcopy(repo)
                    source = changed["sources"]["head"]
                    entry = next(f for f in source["files"] if f["path"] == "app.py")
                    if field == "tree_sha":
                        source[field] = value
                    else:
                        entry[field] = value
                    if field == "blob_sha":
                        with self.assertRaisesRegex(ValueError, "fixed blob identity mismatch"):
                            workspace.fixed_bytes(context, changed, "head", "app.py", api)
                        with self.assertRaisesRegex(ValueError, "fixed blob identity mismatch"):
                            cache.read(changed, "head", "app.py")
                    else:
                        # Content reads ignore mode/tree metadata; report validation
                        # independently verifies the actual frozen tree identity.
                        self.assertEqual(expected, workspace.fixed_bytes(context, changed, "head", "app.py", api))
                        self.assertEqual(expected, cache.read(changed, "head", "app.py"))
                        self.assertEqual(expected, cache.read(changed, "head", "app.py"))
                    with mock.patch.object(api, "run_git", wraps=api.run_git) as run:
                        patch = cache.changed_patch(changed, "head", changed["diff_base"], "app.py")
                        self.assertIn("+contract = 'new'", patch)
                        self.assertEqual(patch, cache.changed_patch(changed, "head", changed["diff_base"], "app.py"))
                        self.assertEqual(1, run.call_count)

    def test_malformed_tree_metadata_reaches_the_original_report_validation(self):
        self.make_repo("consumer")
        context, manifest = self.capture()
        draft = self.draft(context, manifest)

        class Uncached(workspace.FixedSourceCache):
            def read(self, repo, source_name, path):
                return workspace.fixed_bytes(self.context, repo, source_name, path, self.api)

        for value in ([], {"unexpected": "tree"}):
            changed = copy.deepcopy(manifest)
            workspace.repository(changed, changed["selected_repo"])["sources"]["head"]["tree_sha"] = value
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "fixed tree identity mismatch"):
                    workspace_report.validate(draft, changed, context)
                with mock.patch.object(workspace_report, "FixedSourceCache", Uncached):
                    with self.assertRaisesRegex(ValueError, "fixed tree identity mismatch"):
                        workspace_report.validate(draft, changed, context)

    def test_cache_isolates_repo_source_ref_and_object_storage(self):
        consumer = self.make_repo("consumer", {"app.py": "contract = 'consumer'\n"})
        context, manifest = self.capture()
        repo = workspace.repository(manifest, manifest["selected_repo"])
        other = next(r for r in manifest["repositories"] if r["path"] == str(consumer))
        cache = workspace.FixedSourceCache(context, api)
        self.assertNotEqual(cache.read(repo, "head", "app.py"), cache.read(other, "head", "app.py"))
        head = cache.read(repo, "head", "app.py")
        base = cache.read(repo, "base", "app.py")
        changed = copy.deepcopy(repo)
        changed["sources"]["head"] = copy.deepcopy(repo["sources"]["base"])
        self.assertEqual(base, cache.read(changed, "head", "app.py"))
        for name in ("cache-objects-a", "cache-objects-b"):
            (context / name).mkdir()
        # Borrowed object-store switching remains a legacy-v3 capability.
        repo = copy.deepcopy(repo)
        repo.pop("snapshot")
        for name, source in repo["sources"].items():
            source["objects"] = f"repos/{repo['id']}/native/merge-preview-objects" if name == "merge-preview" else None
        with mock.patch.object(workspace, "fixed_bytes", wraps=workspace.fixed_bytes) as read:
            repo["sources"]["head"]["objects"] = "cache-objects-a"
            self.assertEqual(head, cache.read(repo, "head", "app.py"))
            repo["sources"]["head"]["objects"] = "cache-objects-b"
            self.assertEqual(head, cache.read(repo, "head", "app.py"))
            self.assertEqual(2, read.call_count)
            (context / "cache-objects-b").rmdir()
            with self.assertRaisesRegex(ValueError, "storage is missing"):
                cache.read(repo, "head", "app.py")
        with mock.patch.object(workspace, "fixed_bytes", wraps=workspace.fixed_bytes) as read:
            workspace.FixedSourceCache(context, api).read(changed, "head", "app.py")
            self.assertEqual(1, read.call_count)

    def test_changed_patch_cache_keys_baseline_flags_and_source(self):
        context, manifest = self.capture()
        repo = workspace.repository(manifest, manifest["selected_repo"])
        cache = workspace.FixedSourceCache(context, api)
        with mock.patch.object(api, "run_git", wraps=api.run_git) as run:
            patch = cache.changed_patch(repo, "head", repo["diff_base"], "app.py")
            self.assertIn("+contract = 'new'", patch)
            self.assertEqual(patch, cache.changed_patch(repo, "head", repo["diff_base"], "app.py"))
            self.assertEqual(1, run.call_count)
            self.assertEqual("", cache.changed_patch(repo, "head", repo["sources"]["head"]["ref"], "app.py"))
            self.assertEqual(2, run.call_count)
            self.assertEqual("", cache.changed_patch(repo, "base", repo["diff_base"], "app.py"))
            self.assertEqual(3, run.call_count)
            with mock.patch.object(api, "DIFF_FLAGS", (*api.DIFF_FLAGS, "--minimal")):
                self.assertEqual(patch, cache.changed_patch(repo, "head", repo["diff_base"], "app.py"))
            self.assertEqual(4, run.call_count)

    def test_contract_validation_shares_reads_and_changed_patch_without_changing_value(self):
        self.make_repo("consumer")
        context, manifest = self.capture()
        self.draft(context, manifest)
        contract = copy.deepcopy(manifest["changed_contracts"][0])
        empty_contracts = {**manifest, "changed_contracts": []}
        cache = workspace.FixedSourceCache(context, api)
        expected = workspace.validate_contract(context, empty_contracts, contract, api)
        with mock.patch.object(api, "run_git", wraps=api.run_git) as run:
            self.assertEqual(expected, workspace.validate_contract(context, empty_contracts, contract, api, cache=cache))
            self.assertEqual(3, run.call_count)
            other = {**contract, "id": "C-second"}
            expected_other = {**expected, "id": "C-second"}
            self.assertEqual(expected_other, workspace.validate_contract(context, empty_contracts, other, api, cache=cache))
            self.assertEqual(3, run.call_count)

    def test_validate_root_queries_git_root_once(self):
        with mock.patch.object(api, "run_git", wraps=api.run_git) as run:
            self.assertEqual(self.repo, workspace.validate_root(self.repo, api))
        self.assertEqual(1, run.call_count)

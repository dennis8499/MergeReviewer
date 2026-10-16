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

    def test_batch_read_reuses_verified_content_only_within_the_batch(self):
        context, manifest = self.capture()
        request = {"repo": manifest["selected_repo"], "path": "app.py"}
        with mock.patch.object(api, "run_git", wraps=api.run_git) as calls:
            first = workspace.batch_read(context, manifest, [request, request], api)
            self.assertEqual(2, calls.call_count)
            self.assertEqual(first["reads"][0], first["reads"][1])
            self.assertEqual(first, workspace.batch_read(context, manifest, [request, request], api))
            self.assertEqual(4, calls.call_count)
        self.assertEqual(manifest, workspace.load(context))

    def test_cached_batch_read_rechecks_changed_metadata_before_next_request(self):
        context, manifest = self.capture()
        repo = workspace.repository(manifest, manifest["selected_repo"])
        request = {"repo": repo["id"], "path": "app.py"}
        original = workspace.read

        def read_then_change(*args, **kwargs):
            result = original(*args, **kwargs)
            repo["sources"]["head"]["files"][0]["blob_sha"] = "0" * 40
            return result

        with mock.patch.object(workspace, "read", side_effect=read_then_change) as reads:
            with self.assertRaisesRegex(ValueError, "blob identity mismatch"):
                workspace.batch_read(context, manifest, [request, request, request], api)
        self.assertEqual(2, reads.call_count)

    def contract_batch(self, context, manifest):
        self.draft(context, manifest)
        first = copy.deepcopy(manifest["changed_contracts"][0])
        first.pop("anchor_sha256")
        first["extra_metadata"] = {"preserved": True}
        second = {**copy.deepcopy(first), "id": "C-second", "parent_ids": [first["id"]]}
        manifest["changed_contracts"] = []
        manifest["searches"] = []
        workspace.save(context, manifest)
        return [first, second]

    def test_batch_register_contract_matches_single_registration_and_each_checkpoint(self):
        self.make_repo("consumer")
        context, manifest = self.capture()
        requests = self.contract_batch(context, manifest)
        before = copy.deepcopy(manifest)
        expected = [workspace.register_contract(context, manifest, item, api) for item in requests]
        expected_manifest = copy.deepcopy(manifest)
        workspace.save(context, before)
        result = self.command(context, "batch-register-contract", requests)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({"contracts": expected}, json.loads(result.stdout))
        self.assertEqual(expected_manifest, workspace.load(context))
        self.assertTrue(expected[0]["extra_metadata"]["preserved"])
        with mock.patch.object(workspace, "save", wraps=workspace.save) as saves:
            self.assertEqual({"contracts": expected}, workspace.batch_register_contract(context, before, requests, api))
        self.assertEqual(len(requests), saves.call_count)

    def test_batch_register_failure_keeps_prior_contract_and_stops_before_later_items(self):
        self.make_repo("consumer")
        context, manifest = self.capture()
        first, second = self.contract_batch(context, manifest)
        invalid = {**second, "parent_ids": ["C-missing"]}
        result = self.command(context, "batch-register-contract", [first, invalid, second])
        self.assertEqual(2, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertIn("parent contracts must already be registered", result.stderr)
        persisted = workspace.load(context)
        self.assertEqual([first["id"]], [row["id"] for row in persisted["changed_contracts"]])
        self.assertEqual({"contracts": []}, json.loads(
            self.command(context, "batch-register-contract", []).stdout))
        for requests in ({}, [None], [second]):
            failure = self.command(context, "batch-register-contract", requests)
            self.assertEqual(2 if requests != [second] else 0, failure.returncode, failure.stderr)

    def test_batch_search_reuses_same_query_across_contracts_but_keeps_each_record(self):
        self.make_repo("consumer")
        context, manifest = self.capture()
        contracts = self.contract_batch(context, manifest)
        workspace.batch_register_contract(context, manifest, contracts, api)
        requests = [{"query": contracts[0]["symbol"], "contract": item["id"]} for item in contracts]
        before = copy.deepcopy(manifest)
        expected = []
        for request in requests:
            expected.extend(workspace.search(context, manifest, request["query"], None, api,
                                              contract_id=request["contract"])["searches"])
        workspace.save(context, before)
        with mock.patch.object(api, "run_git_capture", wraps=api.run_git_capture) as calls, \
                mock.patch.object(workspace, "save", wraps=workspace.save) as saves:
            actual = workspace.batch_search(context, before, requests, api)
        self.assertEqual({"searches": expected}, actual)
        self.assertEqual(len(before["repositories"]), calls.call_count)
        self.assertEqual(len(requests), saves.call_count)
        self.assertEqual(["S-0001", "S-0002", "S-0003", "S-0004"],
                         [row["id"] for row in actual["searches"]])
        with mock.patch.object(api, "run_git_capture", wraps=api.run_git_capture) as calls:
            workspace.batch_search(context, before, requests, api)
        self.assertEqual(len(before["repositories"]), calls.call_count)

    def test_batch_search_checks_contract_even_when_the_query_is_cached(self):
        self.make_repo("consumer")
        context, manifest = self.capture()
        contract = self.contract_batch(context, manifest)[0]
        workspace.register_contract(context, manifest, contract, api)
        requests = [{"query": contract["symbol"], "contract": contract["id"]},
                    {"query": contract["symbol"], "contract": "C-missing"}]
        with mock.patch.object(api, "run_git_capture", wraps=api.run_git_capture) as calls:
            with self.assertRaisesRegex(ValueError, "registered --contract"):
                workspace.batch_search(context, manifest, requests, api)
        self.assertEqual(len(manifest["repositories"]), calls.call_count)
        self.assertEqual(len(manifest["repositories"]), len(workspace.load(context)["searches"]))

    def test_batch_search_does_not_cache_failures_or_live_backed_sources(self):
        context, manifest = self.capture()
        requests = [{"query": "contract"}, {"query": "contract"}]
        original = api.run_git_capture
        attempted = []

        def fail_first(*args, **kwargs):
            attempted.append(True)
            return (128, "", "transient error") if len(attempted) == 1 else original(*args, **kwargs)

        with mock.patch.object(api, "run_git_capture", side_effect=fail_first):
            result = workspace.batch_search(context, manifest, requests, api)
        self.assertEqual(2, len(attempted))
        self.assertEqual(["unresolved", "complete"], [row["status"] for row in result["searches"]])
        repo = workspace.repository(manifest, manifest["selected_repo"])
        repo.pop("snapshot")
        for source in repo["sources"].values():
            source["objects"] = None
        cache = workspace.FixedSearchCache(context, api)
        with mock.patch.object(api, "run_git_capture", wraps=api.run_git_capture) as calls:
            self.assertEqual(cache.run(repo, "head", "contract"), cache.run(repo, "head", "contract"))
        self.assertEqual(2, calls.call_count)

    def test_search_cache_rechecks_owned_storage_and_isolates_source_and_query(self):
        context, manifest = self.capture()
        repo = workspace.repository(manifest, manifest["selected_repo"])
        cache = workspace.FixedSearchCache(context, api)
        with mock.patch.object(api, "run_git_capture", wraps=api.run_git_capture) as calls:
            first = cache.run(repo, "head", "contract")
            self.assertEqual(first, cache.run(repo, "head", "contract"))
            cache.run(repo, "base", "contract")
            cache.run(repo, "head", "no-match")
            self.assertEqual(3, calls.call_count)
            repo["sources"]["head"]["objects"] = "missing"
            with self.assertRaisesRegex(ValueError, "outside its frozen"):
                cache.run(repo, "head", "contract")
            self.assertEqual(3, calls.call_count)


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

    def test_read_cache_hit_still_rejects_borrowed_owned_object_storage(self):
        context, manifest = self.capture()
        repo = workspace.repository(manifest, manifest["selected_repo"])
        cache = workspace.FixedSourceCache(context, api)
        cache.read(repo, "head", "app.py")
        alternate = workspace.evidence_repository(context, repo) / "objects/info/alternates"
        alternate.write_text(str(self.repo / ".git/objects"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "cannot borrow"):
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

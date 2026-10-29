"""Regression tests for ref reuse, v2 boundary decisions and concise evidence."""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from tests.fixtures import ReviewFixture, api, workspace, workspace_report, review_session
import contract_boundary
import review_authoring
import review_coordinator
import review_flow
import review_runtime


class BoundaryV2Tests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.make_repo("consumer")
        self.commit()

    def test_coordinated_peer_snapshots_run_in_two_isolated_workers(self):
        self.make_repo("consumer-two")
        current, peak = 0, 0
        lock = threading.Lock()
        original = workspace.add_source

        def delayed(context, repo, name, reference, runner, **kwargs):
            nonlocal current, peak
            if repo["path"] != str(self.repo) and name == "head":
                with lock:
                    current += 1
                    peak = max(peak, current)
                threading.Event().wait(.1)
                try:
                    return original(context, repo, name, reference, runner, **kwargs)
                finally:
                    with lock:
                        current -= 1
            return original(context, repo, name, reference, runner, **kwargs)

        arguments = review_flow.parser().parse_args(["start", "--workspace", str(self.root),
            "--project", str(self.repo), "--base", "main", "--head", "feature"])
        with mock.patch.object(workspace, "add_source", side_effect=delayed):
            result = review_flow.start(arguments)
        self.addCleanup(review_session.cleanup_if_owned, result["context_dir"])
        self.assertEqual(2, peak)
        self.assertEqual(3, len(result["repositories"]))

    def test_unavailable_peer_snapshot_keeps_multi_repo_context_incomplete(self):
        original = workspace.add_source

        def fail_peer(context, repo, name, reference, runner, **kwargs):
            if Path(repo["path"]).resolve() != self.repo.resolve() and name == "head":
                raise api.ReviewContextError("synthetic missing peer object")
            return original(context, repo, name, reference, runner, **kwargs)

        with mock.patch.object(workspace, "add_source", side_effect=fail_peer):
            _context, manifest = self.capture()
        peer = next(row for row in manifest["repositories"] if row["path"] != str(self.repo.resolve()))
        self.assertEqual("error", peer["state"])
        self.assertFalse(manifest["context_complete"])
        self.assertTrue(any(peer["id"] in gap for gap in manifest["context_gaps"]))

    def test_object_format_is_probed_once_when_each_snapshot_is_sealed(self):
        with mock.patch.object(api, "run_git", wraps=api.run_git) as run:
            _context, manifest = self.capture()
        probes = [call for call in run.call_args_list
                  if call.args[1] == ["rev-parse", "--show-object-format"]]
        captured = sum(row["state"] == "captured" for row in manifest["repositories"])
        self.assertEqual(captured, len(probes))

    def contained(self):
        context, manifest = self.capture(policy=contract_boundary.V2_POLICY)
        root = manifest["required_coverage"][0]
        seed = workspace.repository(manifest, root["repo"])
        evidence = {**root, "ref": seed["sources"][root["source"]]["ref"], "line_start": 1, "line_end": 1}
        draft = {"schema": workspace.RESULT, "summary": "內部資料流已審查。",
                 "coverage": [{**root, "status": "reviewed", "evidence": [evidence]}],
                 "merge_parent_review": [], "impact_coverage": [],
                 "boundary_reviews": [{"change_id": manifest["boundary_changes"][0]["id"],
                     "outcome": "contained", "reason": "已讀取變更與本操作的固定輸出，資料留在本 Repo。",
                     "evidence": [evidence], "search_ids": [], "boundary_contract_ids": []}],
                 "repository_coverage": [{"repo": repo["id"], "status": "seed" if repo["id"] == seed["id"] else "not-related",
                     "reason": "固定資料流沒有輸出至其他 Repo。", "search_ids": []} for repo in manifest["repositories"]],
                 "findings": [], "next_steps": [], "tests_executed": [],
                 "tests_not_executed": ["未執行應用程式測試。"], "limitations": []}
        return context, manifest, draft

    def test_contained_change_requires_evidence_but_no_contract_or_graph(self):
        context, manifest, draft = self.contained()
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])
        broken = copy.deepcopy(draft)
        broken["boundary_reviews"][0]["evidence"] = []
        with self.assertRaises(workspace_report.DraftValidationError):
            workspace_report.validate(broken, manifest, context)
        unresolved = copy.deepcopy(draft)
        unresolved["boundary_reviews"][0].update(outcome="unresolved", evidence=[], reason="輸出尚未確認。")
        self.assertFalse(workspace_report.validate(unresolved, manifest, context)["review_complete"])
        propagated = copy.deepcopy(draft)
        propagated["boundary_reviews"][0]["outcome"] = "propagates"
        with self.assertRaises(workspace_report.DraftValidationError):
            workspace_report.validate(propagated, manifest, context)

    def test_change_search_is_local_and_is_replayed_at_finish(self):
        context, manifest, draft = self.contained()
        change = manifest["boundary_changes"][0]["id"]
        result = workspace.search(context, manifest, "contract", None, api, change_id=change)
        self.assertEqual([manifest["selected_repo"]], [row["repo"] for row in result["searches"]])
        draft["boundary_reviews"][0]["search_ids"] = [result["searches"][0]["id"]]
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])
        peer = next(row["id"] for row in manifest["repositories"] if row["id"] != manifest["selected_repo"])
        with self.assertRaisesRegex(ValueError, "own Repo"):
            workspace.search(context, manifest, "contract", [peer], api, change_id=change)
        manifest["searches"][0]["output"] = "forged\n"
        with self.assertRaisesRegex(ValueError, "search ledger"):
            workspace_report.validate(draft, manifest, context)

    def test_propagation_requires_contract_graph_and_retains_consumer_gaps(self):
        context, manifest = self.capture(policy=contract_boundary.V2_POLICY)
        draft = self.draft(context, manifest)
        contract = manifest["changed_contracts"][0]
        evidence = copy.deepcopy(draft["coverage"][0]["evidence"])
        draft["boundary_reviews"] = [{"change_id": manifest["boundary_changes"][0]["id"],
            "outcome": "propagates", "reason": "固定值流入工作區契約，已追查消費端。",
            "evidence": evidence, "search_ids": [], "boundary_contract_ids": [contract["id"]]}]
        consumer = next(Path(row["path"]) for row in manifest["repositories"] if row["role"] == "candidate")
        node = self.attach_consumer(draft, manifest, consumer)
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])
        node.update(status="not-reviewed", evidence=[], reason="必要 consumer 尚未檢查。")
        self.assertFalse(workspace_report.validate(draft, manifest, context)["review_complete"])
        hidden = copy.deepcopy(draft)
        hidden["boundary_reviews"][0].update(outcome="contained", boundary_contract_ids=[])
        with self.assertRaisesRegex(workspace_report.DraftValidationError, "hide workspace"):
            workspace_report.validate(hidden, manifest, context)

    def test_local_contract_cannot_relabel_a_cross_repo_search(self):
        context, manifest = self.capture(policy=contract_boundary.V2_POLICY)
        draft = self.draft(context, manifest)
        manifest["changed_contracts"][0]["search_scope"] = "repo-local"
        with self.assertRaisesRegex(ValueError, "repo-local search ledger"):
            workspace_report.validate(draft, manifest, context)
        with self.assertRaisesRegex(ValueError, "repo-local search ledger"):
            workspace_report.validate_captured_evidence(manifest, context)

    def test_compact_draft_expands_only_recorded_read_lines(self):
        context, manifest, draft = self.contained()
        compact_template = review_flow.draft_template(manifest, "compact")
        self.assertEqual(review_authoring.SCHEMA, compact_template["schema"])
        self.assertNotIn("report_layout", compact_template)
        self.assertTrue(all("change_id" in row and "path" not in row for row in compact_template["coverage"]))
        expanded_template = review_authoring.expand(compact_template, manifest, context)
        self.assertEqual([tuple(row[key] for key in ("repo", "source", "path"))
                          for row in manifest["required_coverage"]],
                         [tuple(row[key] for key in ("repo", "source", "path"))
                          for row in expanded_template["coverage"]])
        initial = review_flow.initial_read_requests(manifest)
        reads = [row for row in initial if row["op"] == "read"]
        self.assertTrue(any(row["source"] == "base" for row in reads))
        self.assertTrue(any(row["source"] == "head" for row in reads))
        self.assertTrue(any(row["op"] == "patch" for row in initial))
        self.assertTrue(any(row["op"] == "instructions" for row in initial))
        self.assertEqual(len(reads), len({(row["repo"], row["source"], row["path"]) for row in reads}))
        root = manifest["required_coverage"][0]
        operation = {"id": "read-change", "op": "read", "repo": root["repo"],
                     "source": root["source"], "path": root["path"], "line_start": 1, "line_end": 1}
        def copy_link(source, target):
            Path(target).write_bytes(Path(source).read_bytes())
        with mock.patch.object(review_runtime.os, "link", side_effect=copy_link):
            initial_result = workspace.ordered_step(context, manifest, initial, api)
            self.assertTrue(initial_result["complete"])
            outcome = workspace.ordered_step(context, manifest, [operation], api)
        self.assertTrue(outcome["complete"])
        compact = copy.deepcopy(draft)
        compact["schema"] = review_authoring.SCHEMA
        compact["coverage"][0] = {"change_id": manifest["boundary_changes"][0]["id"], "status": "reviewed",
                                  "evidence": [{"read_id": "read-change", "line_start": 1, "line_end": 1}]}
        compact["boundary_reviews"][0]["evidence"] = [{"read_id": "read-change", "line_start": 1, "line_end": 1}]
        compact["findings"] = [{"id": "F-001", "priority": "P2", "title": "變更產生錯誤輸出",
            "scenario": "處理該項目", "trigger": "輸入該值", "explanation": "固定程式使用錯誤的值。",
            "expected": "產生原值", "actual": "產生新值", "impact": "影響此處理結果", "fix": "修正指定值",
            "owner": "開發", "scenario_source": "code-derived", "evidence": compact["coverage"][0]["evidence"]}]
        expanded = review_authoring.expand(compact, manifest, context)
        self.assertEqual(root["repo"], expanded["coverage"][0]["evidence"][0]["repo"])
        self.assertTrue(workspace_report.validate(compact, manifest, context)["review_complete"])
        invalid = copy.deepcopy(compact)
        invalid["findings"][0]["evidence"][0]["line_end"] = 2
        with self.assertRaisesRegex(workspace_report.DraftValidationError, "not included"):
            workspace_report.validate(invalid, manifest, context)
        conflicting = copy.deepcopy(compact)
        conflicting["findings"][0]["technical_trigger"] = "另一個觸發條件"
        with self.assertRaisesRegex(workspace_report.DraftValidationError, "conflicts with technical_trigger"):
            workspace_report.validate(conflicting, manifest, context)

    def test_initial_read_requests_reuse_one_fixed_path_index(self):
        _context, manifest, _draft = self.contained()

        class CountedFiles(list):
            iterations = 0

            def __iter__(self):
                self.iterations += 1
                return super().__iter__()

        selected = workspace.repository(manifest, manifest["selected_repo"])
        counted = {}
        for name, source in selected["sources"].items():
            source["files"] = counted[name] = CountedFiles(source["files"])
        requests = review_flow.initial_read_requests(manifest)
        used = {row["source"] for row in requests if row["op"] == "read"}
        self.assertTrue(requests)
        self.assertTrue({"base", "head"} <= used)
        self.assertTrue(all(counted[name].iterations == 1 for name in used))

    def test_compact_multi_repo_draft_checks_and_publishes_through_facade(self):
        args = review_flow.parser().parse_args(["start", "--workspace", str(self.root), "--project", str(self.repo),
                                               "--base", "main", "--head", "feature", "--draft-format", "compact"])
        start = review_flow.start(args)
        context, runtime, identifier = Path(start["context_dir"]), Path(start["runtime_root"]), start["request_id"]
        self.addCleanup(review_session.cleanup_if_owned, context)
        self.assertEqual(review_authoring.SCHEMA, json.loads((context / "draft.json").read_text(encoding="utf-8"))["schema"])
        self.assertTrue(Path(start["initial_reads_file"]).is_file())
        self.assertGreater(start["initial_read_count"], 0)
        manifest = workspace.load(context)
        root = manifest["required_coverage"][0]
        outcome = review_flow.step(runtime, identifier, [{"id": "read-change", "op": "read", "repo": root["repo"],
             "source": root["source"], "path": root["path"], "line_start": 1, "line_end": 1}])
        self.assertTrue(outcome["complete"])
        citation = {"read_id": "read-change", "line_start": 1, "line_end": 1}
        draft = review_flow.draft_template(manifest)
        draft.update(schema=review_authoring.SCHEMA, summary="已審查固定的內部變更。",
                     coverage=[{"change_id": manifest["boundary_changes"][0]["id"],
                                "status": "reviewed", "evidence": [citation]}],
                     tests_not_executed=["未執行應用程式測試。"])
        draft["boundary_reviews"][0].update(outcome="contained", reason="使用處及輸出均留在本 Repo。",
                                            evidence=[citation])
        for row in draft["repository_coverage"]:
            if row["status"] != "seed":
                row.update(status="not-related", reason="已確認固定輸出不跨越 Repo。")
        (context / "draft.json").write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(api, "run_git", side_effect=AssertionError("check must not run Git")):
            checked, code = review_flow.check(runtime, identifier)
        self.assertEqual(0, code, checked)
        published, code = review_flow.finish(runtime, identifier)
        self.assertEqual(0, code, published)
        self.assertTrue(published["review_outcome"]["review_complete"])
        self.assertEqual(5, sum(line.startswith("## ") for line in
                                Path(published["report_path"]).read_text(encoding="utf-8").splitlines()))

    def test_batched_search_matches_independent_git_and_replays(self):
        context, manifest, _ = self.contained()
        repo = workspace.repository(manifest, manifest["selected_repo"])
        cache = workspace.FixedSearchCache(context, api, persistent=True)
        queries = ["contract", "new", "absent"]
        requests = [{"repo": repo["id"], "source": "head", "query": query} for query in queries]
        with mock.patch.object(api, "run_git_capture", wraps=api.run_git_capture) as calls:
            cache.prefetch(manifest, requests)
            combined = [cache.run(repo, "head", query) for query in queries]
        self.assertEqual(1, sum(call.args[1][0] == "grep" for call in calls.call_args_list))
        separate = [workspace.FixedSearchCache(context, api).run(repo, "head", query) for query in queries]
        self.assertEqual(separate, combined)
        fresh = workspace.FixedSearchCache(context, api, persistent=True)
        original = api.run_git_capture

        def reject_search(repo_path, arguments, **kwargs):
            if arguments and arguments[0] == "grep":
                raise AssertionError("persistent result was not reused")
            return original(repo_path, arguments, **kwargs)

        with mock.patch.object(api, "run_git_capture", side_effect=reject_search):
            self.assertEqual(combined, [fresh.run(repo, "head", query) for query in queries])

    def test_batched_search_supports_utf8_paths_and_queries(self):
        self.commit("付款欄位 = '資料值'\n", "中文-é.txt")
        context, manifest = self.capture()
        repo = workspace.repository(manifest, manifest["selected_repo"])
        queries = ["付款欄位", "資料值"]
        requests = [{"repo": repo["id"], "source": "head", "query": query} for query in queries]
        cache = workspace.FixedSearchCache(context, api)
        with mock.patch.object(api, "run_git_capture", wraps=api.run_git_capture) as calls:
            cache.prefetch(manifest, requests)
            combined = [cache.run(repo, "head", query) for query in queries]
        independent = [workspace.FixedSearchCache(context, api).run(repo, "head", query) for query in queries]
        self.assertEqual(independent, combined)
        self.assertEqual(1, sum(call.args[1][0] == "grep" for call in calls.call_args_list))
        self.assertTrue(all("付款欄位" in result[1] for result in combined))

    def test_unicode_separator_does_not_create_phantom_evidence_lines(self):
        self.commit("contract = 'new\u0085value'\nsecond = 2\n")
        context, manifest, draft = self.contained()
        root = manifest["required_coverage"][0]
        result = workspace.read(context, manifest, root["repo"], root["path"], api, root["source"])
        self.assertEqual([1, 2], [line["line"] for line in result["lines"]])
        self.assertIn("\u0085", result["lines"][0]["text"])
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])

    def test_ordinary_utf8_path_uses_batched_search(self):
        self.commit("contract = 'new'\n", relative="特殊 路徑.py")
        context, manifest, _ = self.contained()
        repo = workspace.repository(manifest, manifest["selected_repo"])
        cache = workspace.FixedSearchCache(context, api)
        queries = ["contract", "new"]
        requests = [{"repo": repo["id"], "source": "head", "query": query} for query in queries]
        with mock.patch.object(api, "run_git_capture", wraps=api.run_git_capture) as calls:
            cache.prefetch(manifest, requests)
            values = [cache.run(repo, "head", query) for query in queries]
        self.assertEqual(1, sum(call.args[1][0] == "grep" for call in calls.call_args_list),
                         [call.args[1] for call in calls.call_args_list])
        self.assertEqual(values, [workspace.FixedSearchCache(context, api).run(repo, "head", query) for query in queries])

    def test_special_path_uses_independent_searches(self):
        self.commit("contract = 'new'\n", relative="unusual (file).py")
        context, manifest, _ = self.contained()
        repo = workspace.repository(manifest, manifest["selected_repo"])
        cache = workspace.FixedSearchCache(context, api)
        queries = ["contract", "new"]
        requests = [{"repo": repo["id"], "source": "head", "query": query} for query in queries]
        with mock.patch.object(api, "run_git_capture", wraps=api.run_git_capture) as calls:
            cache.prefetch(manifest, requests)
            values = [cache.run(repo, "head", query) for query in queries]
        self.assertEqual(2, sum(call.args[1][0] == "grep" for call in calls.call_args_list))
        self.assertEqual(values, [workspace.FixedSearchCache(context, api).run(repo, "head", query)
                                  for query in queries])


class ProjectionTests(unittest.TestCase):
    def test_small_step_distinguishes_saved_returned_and_agent_review(self):
        with tempfile.TemporaryDirectory() as directory:
            result = review_flow.compact({"complete": True, "operations": [{"id": "one", "result": {"value": 1}}]},
                                         Path(directory))
            self.assertTrue(result["evidence_complete"])
            self.assertTrue(result["operations"][0]["saved"])
            self.assertTrue(result["operations"][0]["result_returned"])
            self.assertEqual("agent-determined", result["review_status"])

    def test_large_operation_does_not_hide_small_results(self):
        with tempfile.TemporaryDirectory() as directory:
            context = Path(directory)
            value = {"complete": True, "next_index": 2, "operations": [
                {"id": "large", "result": {"text": "x" * 40000}},
                {"id": "small", "result": {"answer": "available"}}]}
            compact = review_flow.compact(value, context)
            self.assertLessEqual(len(review_runtime.canonical(compact)), review_flow.LIMIT)
            self.assertEqual("MergeReviewEvidenceStep/v2", compact["schema"])
            self.assertFalse(compact["operations"][0]["result_returned"])
            self.assertEqual("available", compact["operations"][1]["result"]["answer"])
            self.assertEqual(value, review_runtime.load_view(context, compact["artifact"]))

    def test_many_operations_keep_a_page_of_small_results_and_a_continuation(self):
        with tempfile.TemporaryDirectory() as directory:
            value = {"complete": True, "next_index": 200, "operations":
                     [{"id": f"op-{i}", "result": {"value": "x" * (40000 if i == 0 else 2)}}
                      for i in range(200)]}
            compact = review_flow.compact(value, Path(directory))
            self.assertLess(len(review_runtime.canonical(compact)), review_flow.LIMIT)
            self.assertEqual(200, compact["operations_total"])
            self.assertEqual(len(compact["operations"]), compact["operations_next_offset"])
            self.assertFalse(compact["operations"][0]["result_returned"])
            self.assertTrue(compact["operations"][1]["result_returned"])
            self.assertEqual("/operations", compact["continue_operations"]["pointer"])

    def test_large_terminal_status_remains_bounded_and_can_be_read_after_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = review_coordinator.runtime(Path(directory) / "runtime", create=True)
            (root / "jobs").mkdir()
            identifier = uuid.uuid4().hex
            review_coordinator.atomic(root / "jobs" / (identifier + ".json"), {
                "schema": review_flow.SCHEMA, "request_id": identifier, "runtime_root": str(root),
                "state": "failed", "error": "x" * 40000})
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = review_flow.main(["status", "--runtime-root", str(root), "--request-id", identifier])
            self.assertEqual(0, code)
            self.assertLessEqual(len(output.getvalue().encode("utf-8")), review_flow.LIMIT)
            summary = json.loads(output.getvalue())
            self.assertEqual("runtime", summary["response_scope"])
            self.assertEqual("status", summary["continue"]["command"])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = review_flow.main(["status", "--runtime-root", str(root), "--request-id", identifier,
                                         "--artifact", summary["artifact"], "--pointer", "/error"])
            self.assertEqual(0, code)
            page = json.loads(output.getvalue())
            self.assertEqual("x" * 2048, page["text"])
            self.assertEqual(2048, page["next_string_offset"])

    def test_expired_response_cleanup_is_scoped_to_its_own_request(self):
        with tempfile.TemporaryDirectory() as directory:
            root = review_coordinator.runtime(Path(directory) / "runtime", create=True)
            (root / "jobs").mkdir()
            expired, live = uuid.uuid4().hex, uuid.uuid4().hex
            for identifier, state in ((expired, "failed"), (live, "ready")):
                review_coordinator.atomic(root / "jobs" / (identifier + ".json"), {
                    "schema": review_flow.SCHEMA, "request_id": identifier, "runtime_root": str(root),
                    "state": state, "finished_at_epoch": 0})
                review_runtime.store_view(review_flow.response_context(root, identifier, create=True), {"value": identifier})
            review_flow.prune_jobs(root)
            self.assertFalse((root / "responses" / expired).exists())
            self.assertTrue((root / "responses" / live).is_dir())
            self.assertTrue((root / "jobs" / (live + ".json")).is_file())


class RefCoordinationTests(unittest.TestCase):
    def setUp(self):
        # These cases exercise the local lock/sync logic. The sandbox places
        # temp roots below this checkout, while production correctly rejects
        # runtimes inside any Git Repo.
        def local_runtime(path, *args, **kwargs):
            value = Path(path)
            if kwargs.get("create"):
                value.mkdir(parents=True, exist_ok=True)
            return value

        for patcher in (
                mock.patch.object(review_coordinator, "runtime", side_effect=local_runtime),
                mock.patch.object(review_coordinator, "plain", side_effect=lambda path: Path(path)),
                mock.patch.object(review_coordinator.git_network_lease, "_plain_directory",
                                  side_effect=lambda path: Path(path))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_pack_slots_allow_two_workers_and_nested_pack_is_reentrant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = review_coordinator.runtime(Path(directory) / "runtime", create=True)
            owner = review_coordinator.Coordination(root, uuid.uuid4().hex)
            barrier = threading.Barrier(2)
            lock = threading.Lock()
            active, peak = 0, 0

            def pack(index):
                nonlocal active, peak
                with owner.packing():
                    with owner.packing():
                        with lock:
                            active += 1
                            peak = max(peak, active)
                        if index < 2:
                            barrier.wait(timeout=5)
                        threading.Event().wait(.05)
                        with lock:
                            active -= 1

            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(pack, range(3)))
            self.assertEqual(2, peak)
            self.assertGreaterEqual(owner.timings["packing_wait_seconds"], 0)

    def test_ttl_exact_boundary_and_invalidation_during_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = review_coordinator.runtime(Path(directory) / "runtime", create=True)
            repo, refs, calls = Path(directory) / "repo", {}, []
            common = (repo / ".git").resolve()
            def run_git(_repo, args):
                if args[0] == "rev-parse":
                    return str(common)
                if args[:2] == ["remote", "get-url"]:
                    return "https://example.invalid/repo"
                if args[:2] == ["update-ref", "-d"]:
                    refs.pop(args[2], None)
                    return ""
                raise AssertionError(args)
            fake = SimpleNamespace(run_git=run_git, run_git_capture=lambda *_: (1, "", ""),
                                   verified_commit=lambda _repo, ref: refs.get(ref),
                                   ReviewContextError=api.ReviewContextError)
            target = [api.RemoteTarget("origin", "main", "origin/main")]
            def fetch(specs):
                calls.append(specs)
                for spec in specs:
                    refs[spec.split(":", 1)[1]] = "a" * 40
            for tick in (1000.0, 1299.999, 1300.0):
                owner = review_coordinator.Coordination(root, uuid.uuid4().hex)
                with mock.patch.object(review_coordinator.time, "time", return_value=tick):
                    owner.sync(repo, "origin", target, fake, fetch)
                owner.release_syncs()
            self.assertEqual(2, len(calls))
            invalidation = root / "invalidations" / (review_coordinator.digest(str(common)) + ".json")
            invalidation.parent.mkdir(exist_ok=True)
            def racing_fetch(specs):
                fetch(specs)
                review_coordinator.atomic(invalidation, {"nonce": "changed-during-fetch"})
            raced = review_coordinator.Coordination(root, uuid.uuid4().hex, refresh=True)
            with self.assertRaisesRegex(api.ReviewContextError, "invalidated while fetching"):
                raced.sync(repo, "origin", target, fake, racing_fetch)
            self.assertEqual({}, raced.pins)
            retried = review_coordinator.Coordination(root, uuid.uuid4().hex)
            retried.sync(repo, "origin", target, fake, fetch)
            self.assertEqual("owner", retried.syncs[0]["mode"])
            retried.release_syncs()

    def test_four_different_heads_share_main_and_failed_head_keeps_main(self):
        with tempfile.TemporaryDirectory() as directory:
            root = review_coordinator.runtime(Path(directory) / "runtime", create=True)
            repo = Path(directory) / "repo"
            refs, calls = {}, []
            barrier = threading.Barrier(4)
            def run_git(_repo, arguments):
                if arguments[0] == "rev-parse":
                    return str(repo / ".git")
                if arguments[:2] == ["remote", "get-url"]:
                    return "https://example.invalid/repo"
                raise AssertionError(arguments)
            fake = SimpleNamespace(run_git=run_git, run_git_capture=lambda *_: (1, "", ""),
                                   verified_commit=lambda _repo, ref: refs.get(ref),
                                   ReviewContextError=api.ReviewContextError)
            def fetch(specs):
                calls.append(list(specs))
                for spec in specs:
                    branch, private = spec.split(":", 1)
                    refs[private] = (branch.rsplit("/", 1)[-1] + "0" * 40)[:40]
            owners = [review_coordinator.Coordination(root, uuid.uuid4().hex) for _ in range(4)]
            def start(index):
                barrier.wait()
                targets = [api.RemoteTarget("origin", "main", "origin/main"),
                           api.RemoteTarget("origin", f"feature-{index}", f"origin/feature-{index}")]
                owners[index].sync(repo, "origin", targets, fake, fetch)
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(start, range(4)))
            self.assertEqual(4, len(calls))
            self.assertEqual(5, sum(len(specs) for specs in calls))
            self.assertEqual(1, sum("refs/heads/main:" in spec for specs in calls for spec in specs))
            self.assertEqual(4, len({owner.pins["origin/feature-" + str(i)]["sha"] for i, owner in enumerate(owners)}))
            failed = review_coordinator.Coordination(root, uuid.uuid4().hex)
            targets = [api.RemoteTarget("origin", "main", "origin/main"),
                       api.RemoteTarget("origin", "missing", "origin/missing")]
            with self.assertRaises(api.ReviewContextError):
                failed.sync(repo, "origin", targets, fake,
                            lambda specs: (_ for _ in ()).throw(api.ReviewContextError("missing branch")))
            again = review_coordinator.Coordination(root, uuid.uuid4().hex)
            again.sync(repo, "origin", targets[:1], fake, lambda _: self.fail("main should stay cached"))
            self.assertEqual("cached", again.syncs[0]["mode"])
            for owner in [*owners, failed, again]:
                owner.release_syncs()

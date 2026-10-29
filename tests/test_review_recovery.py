"""Cross-step transport reuse, fresh publication replay and durable report recovery."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

from tests.fixtures import ReviewFixture, api, workspace, workspace_report, review_session
import review_coordinator as coordination
import review_flow as flow
import report_receipt


class SearchReuseTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.make_repo("consumer")
        self.commit()

    def test_two_steps_reuse_raw_query_but_finish_replays_it(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        contract = manifest["changed_contracts"][0]
        peer = next(r["id"] for r in manifest["repositories"] if r["id"] != manifest["selected_repo"])
        operation = {"id": "first", "op": "search", "query": contract["symbol"], "contract": contract["id"], "repo": [peer]}
        with mock.patch.object(api, "run_git_capture", wraps=api.run_git_capture) as run:
            self.assertTrue(workspace.ordered_step(context, manifest, [operation], api)["complete"])
            self.assertTrue(workspace.ordered_step(context, manifest, [dict(operation, id="second")], api)["complete"])
        self.assertEqual(1, sum(call.args[1][0] == "grep" for call in run.call_args_list))
        self.assertEqual(2, len({row["id"] for row in manifest["searches"][-2:]}))
        with mock.patch.object(api, "run_git_capture", side_effect=AssertionError("completed checkpoint was replayed")):
            reused = workspace.ordered_step(context, manifest, [operation], api)
        self.assertTrue(reused["operations"][0]["reused"])
        with mock.patch.object(api, "run_git_capture", wraps=api.run_git_capture) as run:
            workspace_report.validate(draft, manifest, context)
        self.assertTrue(any(call.args[1][0] == "grep" for call in run.call_args_list))

    def test_local_refs_do_not_fetch_or_follow_configured_upstream(self):
        api.run_git(self.repo, ["remote", "add", "origin", str(self.root / "missing-remote.git")])
        api.run_git(self.repo, ["config", "branch.feature.remote", "origin"])
        api.run_git(self.repo, ["config", "branch.feature.merge", "refs/heads/feature"])
        with mock.patch.object(api, "run_git", wraps=api.run_git) as run:
            context, manifest = self.capture(base="main", head="feature")
        self.assertEqual(0, manifest["network_activity"]["fetch_attempt_count"])
        self.assertFalse(any(call.args[1][0] == "fetch" for call in run.call_args_list))

    def test_steps_and_publication_replay_only_frozen_local_evidence(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        contract = manifest["changed_contracts"][0]
        operation = {"id": "frozen-search", "op": "search", "query": contract["symbol"],
                     "contract": contract["id"],
                     "repo": [manifest["selected_repo"]]}
        with mock.patch.object(api, "run_git", wraps=api.run_git) as run:
            self.assertTrue(workspace.ordered_step(context, manifest, [operation], api)["complete"])
            workspace_report.validate(draft, manifest, context)
        self.assertFalse(any(call.args[1][0] == "fetch" for call in run.call_args_list))

    def test_corrupt_cache_is_discarded_and_tampered_ledger_is_rejected(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        contract = manifest["changed_contracts"][0]
        owner = contract["evidence"]["repo"]
        query = contract["symbol"]
        first = {"id": "first", "op": "search", "query": query, "contract": contract["id"], "repo": [owner]}
        workspace.ordered_step(context, manifest, [first], api)
        cache = next((context / "search-cache").glob("*.json"))
        cache.write_text('{"broken":true}', encoding="utf-8")
        with mock.patch.object(api, "run_git_capture", wraps=api.run_git_capture) as run:
            workspace.ordered_step(context, manifest, [dict(first, id="second")], api)
        self.assertEqual(1, sum(call.args[1][0] == "grep" for call in run.call_args_list))
        manifest["searches"][-1].update(output="invented", matches=["invented"])
        with self.assertRaisesRegex(ValueError, "ledger"):
            workspace_report.validate(draft, manifest, context)

    def test_persistent_cache_hashes_git_environment_values(self):
        context, manifest = self.capture()
        self.draft(context, manifest)
        contract = manifest["changed_contracts"][0]
        operation = {"id": "private-env", "op": "search", "query": contract["symbol"],
                     "contract": contract["id"], "repo": [contract["evidence"]["repo"]]}
        sentinel = "synthetic-private-git-environment-value"
        with mock.patch.dict(os.environ, {"GIT_REVIEW_PRIVATE_TEST_VALUE": sentinel}):
            self.assertTrue(workspace.ordered_step(context, manifest, [operation], api)["complete"])
        self.assertTrue(list((context / "search-cache").glob("*.json")))
        for path in (context / "search-cache").glob("*.json"):
            self.assertNotIn(sentinel, path.read_text(encoding="utf-8"))

    def test_cache_write_failure_keeps_fresh_authoritative_search(self):
        context, manifest = self.capture()
        self.draft(context, manifest)
        contract = manifest["changed_contracts"][0]
        operation = {"id": "cache-write-failure", "op": "search", "query": contract["symbol"],
                     "contract": contract["id"], "repo": [contract["evidence"]["repo"]]}
        original = coordination.atomic
        def fail_cache(path, value):
            if Path(path).parent.name == "search-cache":
                raise PermissionError("synthetic cache ACL")
            return original(path, value)
        before = len(manifest["searches"])
        with mock.patch.object(coordination, "atomic", side_effect=fail_cache):
            result = workspace.ordered_step(context, manifest, [operation], api)
        self.assertTrue(result["complete"])
        self.assertEqual(before + 1, len(manifest["searches"]))
        self.assertEqual("complete", manifest["searches"][-1]["status"])

    def test_final_validation_never_accesses_transport_cache_paths(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        original = workspace.safe_path
        def no_transport(root, value):
            if value.startswith("search-cache/"):
                raise AssertionError("finish accessed transport cache")
            return original(root, value)
        with mock.patch.object(workspace, "safe_path", side_effect=no_transport):
            self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])


class RecoveryTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.commit()

    def start(self):
        args = flow.parser().parse_args(["start", "--workspace", str(self.root), "--project", str(self.repo),
                                        "--base", "main", "--head", "feature"])
        result = flow.start(args)
        self.addCleanup(review_session.cleanup_session, Path(result["context_dir"]))
        return result

    def author(self, result):
        context = Path(result["context_dir"])
        manifest = workspace.load(context)
        draft = self.draft(context, manifest)
        draft["report_layout"] = "engineering-review/v1"
        coordination.atomic(context / "draft.json", draft)
        return context

    def test_check_and_status_are_zero_git_and_collect_field_errors(self):
        result = self.start()
        root, identifier = Path(result["runtime_root"]), result["request_id"]
        context = Path(result["context_dir"])
        draft = coordination.read_json(context / "draft.json")
        draft["findings"] = [{"id": "F-001"}]
        coordination.atomic(context / "draft.json", draft)
        with mock.patch.object(api, "run_git", side_effect=AssertionError("Git")), \
             mock.patch.object(api, "run_git_capture", side_effect=AssertionError("Git")):
            checked, code = flow.check(root, identifier)
            resumed = flow.status(root, identifier)
        self.assertEqual(3, code)
        self.assertGreaterEqual(len(checked["errors"]), 6)
        self.assertEqual(0, resumed["resume"]["diagnostics"]["git_commands"])

    def test_cleanup_failure_preserves_publication_and_retry_never_revalidates(self):
        result = self.start()
        root, identifier = Path(result["runtime_root"]), result["request_id"]
        context = self.author(result)
        with mock.patch.object(review_session, "cleanup_session", side_effect=OSError("synthetic cleanup")):
            published, code = flow.finish(root, identifier)
        self.assertEqual(0, code)
        self.assertTrue(published["cleanup_pending"])
        self.assertTrue(context.exists())
        with mock.patch.object(workspace_report, "validate", side_effect=AssertionError("duplicate validation")):
            repeated, code = flow.finish(root, identifier)
        self.assertEqual(0, code)
        self.assertFalse(repeated["cleanup_pending"])
        self.assertFalse(context.exists())
        self.assertEqual(published["report_path"], repeated["report_path"])
        checked, code = flow.check(root, identifier)
        self.assertEqual(0, code)
        self.assertFalse(checked["evidence_verified"])
        self.assertEqual(0, checked["git_commands"])

    def test_published_missing_or_changed_body_is_not_reported_as_success(self):
        result = self.start()
        root, identifier = Path(result["runtime_root"]), result["request_id"]
        self.author(result)
        published, _ = flow.finish(root, identifier)
        report = Path(published["report_path"])
        original = report.read_bytes()
        report.write_bytes(original + b"altered")
        with self.assertRaisesRegex(ValueError, "fixed body changed"):
            flow.status(root, identifier)
        with self.assertRaisesRegex(ValueError, "fixed body changed"):
            flow.finish(root, identifier)
        report.unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            flow.finish(root, identifier)

    def test_receipt_allows_only_marked_manual_record_edits(self):
        path = self.repo / "review-reports" / "manual.md"
        path.parent.mkdir(exist_ok=True)
        body = "Fixed assessment.\n" + report_receipt.START + "\n- [ ] Q-001\n" + report_receipt.END + "\nFixed evidence.\n"
        path.write_text(body, encoding="utf-8", newline="\n")
        job = {"report_path": str(path), "inputs": {"project": str(self.repo)}, "report_receipt": report_receipt.create(body)}
        path.write_text(body.replace("- [ ]", "- [x] reviewed by QA"), encoding="utf-8", newline="\n")
        self.assertTrue(report_receipt.verify(job)["manual_record_changed"])
        path.write_text(body.replace("Fixed assessment", "Changed assessment"), encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(ValueError, "fixed body changed"):
            report_receipt.verify(job)
        path.write_text(body.replace(report_receipt.END, ""), encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(ValueError, "markers"):
            report_receipt.verify(job)

"""Sanitized November 10 regressions across launch, authoring and publication."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
from unittest import mock

from tests.fixtures import ReviewFixture, api, workspace, workspace_report, review_session
from tests.test_r3_authoring_diagnostics import ImpactOwnEvidenceDiagnosticsTests
import review_coordinator as coordination
import review_flow as flow
import review_runtime
from review_validation import completion_preview


class CompletionShapeTests(ImpactOwnEvidenceDiagnosticsTests):
    def test_gap_identity_and_excluded_peer_limitations_are_preserved(self):
        draft, manifest = self.fixture()
        manifest["repositories"].append({"id": "excluded", "limitations": ["非必要範圍。"]})
        preview = completion_preview(draft, manifest)
        self.assertTrue(preview["expected_outcome"]["review_complete"])
        gaps = [{"kind": "change", "id": name, "reason": "尚未讀完。"} for name in ("one", "two")]
        preview = completion_preview(draft, manifest, gaps)
        self.assertEqual(["one", "two"], [row["id"] for row in preview["blocking_gaps"]])
        draft["limitations"] = ["必要來源缺失。"]
        manifest["context_gaps"] = ["擷取缺口。"]
        preview = completion_preview(draft, manifest, ["必要來源缺失。", "擷取缺口。"], complete_gaps=True)
        self.assertEqual(2, len(preview["blocking_gaps"]))

    def test_reviewed_reason_accepts_missing_or_null_but_not_other_json_types(self):
        for value in (None, "", "  ", "已核對"):
            draft, manifest = self.fixture()
            draft["coverage"][0]["reason"] = value
            self.assertTrue(self.inspect(draft, manifest)["ready_for_finish"])
        for value in (False, 7, [], {}):
            draft, manifest = self.fixture()
            draft["coverage"][0]["reason"] = value
            checked = self.inspect(draft, manifest)
            self.assertFalse(checked["ready_for_finish"])
            self.assertIn("/coverage/0/reason", [row["pointer"] for row in checked["errors"]])
        for value in (None, "", "  "):
            draft, manifest = self.fixture()
            draft["coverage"][0].update(reason=value, status="not-reviewed", evidence=[])
            checked = self.inspect(draft, manifest)
            self.assertIn("/coverage/0/reason", [row["pointer"] for row in checked["errors"]])

    def test_notes_tests_and_blocking_limitations_are_distinct(self):
        draft, manifest = self.fixture()
        draft.update(scope_notes=["只涵蓋宣告工作區。"], tests_not_executed=["未執行測試。"])
        checked = self.inspect(draft, manifest)
        self.assertTrue(checked["expected_outcome"]["review_complete"])
        self.assertEqual([], checked["blocking_gaps"])
        draft["limitations"] = ["必要消費端來源無法取得。"]
        checked = self.inspect(draft, manifest)
        self.assertFalse(checked["expected_outcome"]["review_complete"])
        self.assertFalse(checked["ready_for_finish"])
        self.assertEqual("limitation", checked["blocking_gaps"][0]["kind"])
        draft["limitations"] = []
        manifest["context_gaps"] = ["固定 peer HEAD 無法取得。"]
        self.assertFalse(self.inspect(draft, manifest)["expected_outcome"]["review_complete"])


class SessionContractTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.commit()
        patcher = mock.patch.dict(os.environ, {"GITLAB_WORKSPACE_RUNTIME_DIR": str(self.root / "network")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def arguments(self, **changes):
        args = flow.parser().parse_args(["start", "--workspace", str(self.root), "--project", str(self.repo),
                                        "--base", "main", "--head", "feature",
                                        "--runtime-root", str(self.root / "runtime")])
        for key, value in changes.items():
            setattr(args, key, value)
        return args

    def ready(self):
        result = flow.start(self.arguments())
        root, identifier, context = Path(result["runtime_root"]), result["request_id"], Path(result["context_dir"])
        self.addCleanup(review_session.cleanup_session, context)
        manifest = workspace.load(context)
        draft = self.draft(context, manifest)
        draft["report_layout"] = "engineering-review/v1"
        coordination.atomic(context / "draft.json", draft)
        return root, identifier, context, manifest, draft

    def cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = flow.main(list(args))
        return code, json.loads(output.getvalue()) if output.getvalue() else json.loads(errors.getvalue())

    def inventory(self, root):
        return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in root.rglob("*") if path.is_file()}

    def test_preflight_and_capabilities_write_nothing_and_bad_workspace_does_not_consume_id(self):
        args = self.arguments(request_id="1" * 32)
        before = self.inventory(self.root)
        with mock.patch.object(coordination, "atomic", side_effect=AssertionError("write")), \
             mock.patch.object(coordination.FileLock, "acquire", side_effect=AssertionError("lock write")), \
             mock.patch.object(api, "fetch_inputs", side_effect=AssertionError("fetch")):
            result = flow.launch_preflight(args)
            self.assertTrue(result["valid"], result)
            self.assertTrue(flow.capabilities()["features"]["launch_preflight"])
        self.assertEqual(before, self.inventory(self.root))
        args.workspace = str(self.root / "missing")
        with self.assertRaises(flow.LaunchPreflightError):
            flow.start(args)
        self.assertFalse(Path(args.runtime_root).exists())
        args.workspace = str(self.root)
        result = flow.start(args)
        self.addCleanup(review_session.cleanup_session, Path(result["context_dir"]))
        self.assertEqual(args.request_id, result["request_id"])

    def test_existing_start_does_not_preflight_live_workspace_again(self):
        args = self.arguments(request_id="2" * 32)
        started = flow.start(args)
        self.addCleanup(review_session.cleanup_session, Path(started["context_dir"]))
        with mock.patch.object(flow, "launch_preflight", side_effect=AssertionError("recapture preflight")):
            self.assertEqual(args.request_id, flow.start(args)["request_id"])
            args.head = "other"
            with self.assertRaisesRegex(ValueError, "different inputs"):
                flow.start(args)

    def test_preflight_rejects_scope_runtime_and_nonbranches(self):
        for changes, pointer in (({"project": str(self.root / "outside")}, "/project"),
                                 ({"runtime_root": str(self.repo / "runtime")}, "/runtime_root"),
                                 ({"head": "HEAD"}, "/head"),
                                 ({"head": "missing"}, "/head")):
            with self.subTest(changes=changes):
                result = flow.launch_preflight(self.arguments(**changes))
                self.assertFalse(result["valid"])
                self.assertIn(pointer, [row["pointer"] for row in result["errors"]])

    def test_check_and_finish_agree_on_scope_notes_and_true_limitations(self):
        for limitations, expected in (([], True), (["必要來源仍被拒絕存取。"], False)):
            root, identifier, context, _, draft = self.ready()
            draft.update(scope_notes=["僅涵蓋本次宣告的工作區。"], tests_not_executed=["未執行測試。"],
                         limitations=limitations)
            draft["coverage"][0]["reason"] = None
            coordination.atomic(context / "draft.json", draft)
            checked, _ = flow.check(root, identifier)
            self.assertEqual(expected, checked["expected_outcome"]["review_complete"])
            saved = coordination.read_json(flow.job_path(root, identifier))
            self.assertIn("last_check_at", saved)
            self.assertEqual(0, saved["remaining_counts"]["changes"])
            published, code = flow.finish(root, identifier)
            self.assertEqual(0, code)
            outcome = published["review_outcome"]
            for key in ("review_complete", "review_status", "recommendation", "extension_status"):
                self.assertEqual(checked["expected_outcome"][key], outcome[key], key)
            text = Path(published["report_path"]).read_text(encoding="utf-8")
            self.assertIn("範圍說明：僅涵蓋本次宣告的工作區。", text)
            self.assertNotIn("None", text)

    def test_reverse_index_pack_receipts_include_and_verify_generated_files(self):
        # Override only this process: new Git defaults may emit .rev alongside
        # each pack. Older snapshots without .rev retain the same receipt schema.
        for reverse_index in ("true", "false"):
            with self.subTest(reverse_index=reverse_index):
                config_index = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
                config = {"GIT_CONFIG_COUNT": str(config_index + 1),
                          f"GIT_CONFIG_KEY_{config_index}": "pack.writeReverseIndex",
                          f"GIT_CONFIG_VALUE_{config_index}": reverse_index}
                with mock.patch.dict(os.environ, config):
                    root, identifier, context, manifest, _ = self.ready()
                    job = coordination.read_json(flow.job_path(root, identifier))
                    receipt = flow._pack_receipt(context, manifest)
                    reverse_files = [row for row in receipt if row["path"].endswith(".rev")]
                    self.assertEqual(reverse_index == "true", bool(reverse_files))
                    self.assertEqual(job["evidence_receipt"]["packs"], receipt)
                    flow.verify_step_packs(context, manifest, job)
                    if reverse_files:
                        target = context / reverse_files[0]["path"]
                        original = target.read_bytes()
                        target.chmod(0o666)
                        target.write_bytes(original + b"corruption")
                        with self.assertRaises((ValueError, api.ReviewContextError)):
                            flow.verify_step_packs(context, manifest, job)
                        target.write_bytes(original)
                    result, code = flow.finish(root, identifier)
                    self.assertEqual(0, code, result)

    def test_read_only_summary_and_views_have_no_hidden_writes(self):
        root, identifier, context, _, _ = self.ready()
        artifact = review_runtime.store_view(context, {"lines": ["a" * 3000, "b" * 3000]})
        before = self.inventory(root)
        with mock.patch.object(coordination, "atomic", side_effect=AssertionError("write")), \
             mock.patch.object(coordination.FileLock, "acquire", side_effect=AssertionError("lock write")), \
             mock.patch.object(api, "run_git", side_effect=AssertionError("Git")), \
             mock.patch.object(api, "run_git_capture", side_effect=AssertionError("Git")):
            code, result = self.cli("status", "--runtime-root", str(root), "--request-id", identifier, "--summary")
            self.assertEqual(0, code)
            self.assertEqual("MergeReviewStatusSummary/v1", result["schema"])
            code, result = self.cli("view", "--runtime-root", str(root), "--request-id", identifier,
                                    "--artifact", artifact, "--pointer", "/lines/0", "--read-only")
            self.assertEqual(0, code)
            self.assertTrue(result["continue"]["read_only"])
        self.assertEqual(before, self.inventory(root))

    def test_step_completion_reuse_and_partial_failure_are_persisted(self):
        root, identifier, context, manifest, _ = self.ready()
        row = manifest["required_coverage"][0]
        ops = [{"id": f"read-{i}", "op": "read", **row} for i in range(2)]
        for _ in range(2):
            result = flow.step(root, identifier, ops)
            self.assertTrue(result["complete"])
            progress = flow.status(root, identifier)["progress"]
            self.assertEqual((2, 2, None), (progress["completed"], progress["total"], progress["operation_id"]))
        result = flow.step(root, identifier, ops + [{"id": "bad", "op": "read", **row, "line_end": 999}])
        self.assertFalse(result["complete"])
        progress = flow.status(root, identifier)["progress"]
        self.assertEqual((2, 3), (progress["completed"], progress["total"]))
        self.assertEqual("operation-failed", progress["phase"])

    def test_operation_timing_stops_at_each_completion(self):
        root, identifier, _, _, _ = self.ready()
        clock = [0.0]
        def batch(_context, _manifest, _operations, _api, *, on_progress):
            for index, kind in enumerate(("read", "search")):
                operation = {"id": kind, "op": kind}
                on_progress("operation", index, 2, operation)
                clock[0] += 2.0 if index == 0 else 5.0
                on_progress("operation-complete", index + 1, 2, operation)
            return {"operations": [], "complete": True, "next_index": 2}
        with mock.patch.object(workspace, "ordered_step", side_effect=batch), \
             mock.patch.object(flow, "operation_preflight", return_value=None), \
             mock.patch.object(flow.time, "perf_counter", side_effect=lambda: clock[0]):
            flow.step(root, identifier, [{"id": "read", "op": "read"}, {"id": "search", "op": "search"}])
        seconds = flow.status(root, identifier)["metrics"]["operation_seconds"]
        self.assertEqual(2.0, seconds["read"])
        self.assertEqual(5.0, seconds["search"])

    def render_failure(self):
        root, identifier, context, _, _ = self.ready()
        before = coordination.read_json(flow.job_path(root, identifier))["tool_identity"]
        with mock.patch.object(workspace_report, "render", side_effect=TypeError("synthetic formatter defect")):
            result, code = flow.finish(root, identifier)
        self.assertEqual(2, code)
        self.assertTrue(result["context_retained"])
        self.assertTrue((context / "manifest.json").exists())
        job = coordination.read_json(flow.job_path(root, identifier))
        self.assertEqual(before, job["tool_identity"])
        self.assertEqual("failed", job["state"])
        self.assertNotIn("report_receipt", job)
        with mock.patch.object(workspace_report, "validate", side_effect=AssertionError("retried validation")), \
             self.assertRaisesRegex(ValueError, "cannot be published"):
            flow.finish(root, identifier)
        return root, identifier, context, job

    def test_render_failure_is_terminal_retained_and_explicitly_cleanable(self):
        root, identifier, context, _ = self.render_failure()
        flow.cancel(root, identifier)
        self.assertFalse(context.exists())
        self.assertFalse(flow.status(root, identifier)["context_retained"])

    def test_render_failure_expires_lazily_after_24_hours(self):
        root, identifier, context, job = self.render_failure()
        with mock.patch.object(flow.time, "time", return_value=job["finished_at_epoch"] + 86399):
            flow.prune_jobs(root)
        self.assertTrue(context.exists())
        with mock.patch.object(flow.time, "time", return_value=job["finished_at_epoch"] + 86401):
            flow.prune_jobs(root)
        self.assertFalse(context.exists())
        self.assertFalse(flow.job_path(root, identifier).exists())

"""Sanitized November 10 regressions across launch, authoring and publication."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
from unittest import mock

from tests.fixtures import ReviewFixture, SCRIPTS, api, workspace, workspace_report, review_session
from tests.support import run_git, run_python
from tests.test_r3_authoring_diagnostics import ImpactOwnEvidenceDiagnosticsTests
import report_publication
import review_authoring
import review_coordinator as coordination
import review_flow as flow
import review_manifest_recovery as recovery
import review_report
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

    def ready(self, *, draft_format="v3"):
        result = flow.start(self.arguments(draft_format=draft_format))
        root, identifier, context = Path(result["runtime_root"]), result["request_id"], Path(result["context_dir"])
        self.addCleanup(review_session.cleanup_session, context)
        manifest = workspace.load(context)
        draft = self.draft(context, manifest)
        draft["report_layout"] = "engineering-review/v1"
        if draft_format == "compact":
            draft["schema"] = review_authoring.SCHEMA
        coordination.atomic(context / "draft.json", draft)
        return root, identifier, context, manifest, draft

    def cli(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = flow.main(list(args))
        return code, json.loads(output.getvalue()) if output.getvalue() else json.loads(errors.getvalue())

    def cli_crlf(self, *args):
        output, errors = io.BytesIO(), io.StringIO()
        with io.TextIOWrapper(output, encoding="utf-8", newline="\r\n", write_through=True) as stream, \
             redirect_stdout(stream), redirect_stderr(errors):
            code = flow.main(list(args))
            raw = output.getvalue()
        self.assertEqual(0, code, errors.getvalue())
        self.assertTrue(raw.endswith(b"\r\n"))
        self.assertLessEqual(len(raw), flow.LIMIT)
        return raw

    def inventory(self, root):
        return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in root.rglob("*") if path.is_file()}

    def stage_inventory(self, root, identifier, context, selected_repo):
        replace = os.replace

        def deny_manifest(source, destination):
            if Path(destination) == context / "manifest.json":
                error = PermissionError("Synthetic manifest sharing denial")
                error.winerror = 32
                raise error
            return replace(source, destination)

        with mock.patch.object(recovery.os, "replace", side_effect=deny_manifest), \
             mock.patch.object(recovery.time, "sleep"), redirect_stderr(io.StringIO()):
            blocked = flow.step(root, identifier, [
                {"id": "inventory", "op": "inventory", "repo": selected_repo},
            ])
        self.assertEqual("retry-step", blocked["next_action"])
        pending = recovery.read_pending(context)
        self.assertEqual("prepared", pending["state"])
        self.assertTrue((context / pending["staged_manifest"]).is_file())

    def checkpoint_requests(self, context):
        requests = self.root / "checkpoint-requests.json"
        coordination.atomic(requests, [
            {"id": "checkpoint", "op": "author", "updates": {},
             "expected_draft_sha256": coordination.file_digest(context / "draft.json"),
             "expected_checkpoint_revision": 0, "checkpoint": {"next_action": "Inspect evidence."}},
        ])
        return requests

    def deny_first_author_marker(self, root, identifier, context):
        requests = self.checkpoint_requests(context)
        atomic = coordination.atomic

        def deny_marker(path, value):
            if path == context / recovery.NAME:
                error = PermissionError("Synthetic author marker sharing denial")
                error.winerror = 32
                raise error
            return atomic(path, value)

        with mock.patch.object(coordination, "atomic", side_effect=deny_marker), redirect_stderr(io.StringIO()):
            result = flow.step_file(root, identifier, requests)
        self.assertEqual(recovery.RETRY_WITH_REQUESTS_FILE, result["next_action"])
        self.assertIsNone(recovery.read_pending(context))
        self.assertFalse((context / "checkpoint.json").exists())
        return requests

    def assert_published_action(self, root, identifier, published, action="open-report"):
        for result in (published, flow.status(root, identifier), flow.status(root, identifier, summary=True)):
            self.assertEqual("published", result["state"])
            self.assertEqual(action, result["next_action"])
            self.assertEqual(published["report_path"], result["report_path"])
            self.assertFalse(result["recoverable"])
            self.assertEqual([], result["errors"])
            self.assertFalse(result.get("error"))
        self.assertTrue(published["publication"]["verified"])

    def test_direct_publish_waits_for_pending_manifest_and_native_finish_publishes_once(self):
        root, identifier, context, manifest, _ = self.ready()
        draft = context / "draft.json"
        output = self.repo / "review-reports"
        self.stage_inventory(root, identifier, context, manifest["selected_repo"])

        before = self.inventory(context)
        output_before = self.inventory(output)
        args = ["--context-dir", str(context), "--result", str(draft), "--report-dir", str(output)]
        for flag in ([], ["--retain-on-validation-error"]):
            with self.subTest(retain_draft_error=bool(flag)):
                result = run_python(SCRIPTS / "review_report.py", *args, *flag)
                self.assertEqual(3, result.returncode, result.stderr)
                self.assertEqual("", result.stdout)
                diagnostic = json.loads(result.stderr)
                self.assertEqual(identifier, diagnostic["request_id"])
                self.assertEqual("evidence.manifest-write-repair", diagnostic["code"])
                self.assertEqual("retry-step", diagnostic["next_action"])
                self.assertTrue(diagnostic["context_retained"])
                self.assertEqual(before, self.inventory(context))
                self.assertEqual(output_before, self.inventory(output))

        with redirect_stderr(io.StringIO()):
            self.assertTrue(flow.step(root, identifier, None)["complete"])
            published, code = flow.finish(root, identifier)
            repeated, repeated_code = flow.finish(root, identifier)
        self.assertEqual(0, code, published)
        self.assertEqual(0, repeated_code, repeated)
        self.assertEqual("published", published["state"])
        self.assertEqual(published["report_path"], repeated["report_path"])
        self.assertEqual([Path(published["report_path"])], list(output.glob("merge-review-*.md")))
        self.assertFalse(context.exists())

    def test_publishers_preserve_context_when_recovery_record_is_invalid(self):
        root, identifier, context, _, _ = self.ready()
        (context / recovery.NAME).write_text("{", encoding="utf-8")
        before = self.inventory(context)
        output = self.repo / "review-reports"
        output_before = self.inventory(output)
        result = run_python(SCRIPTS / "review_report.py", "--context-dir", str(context),
                            "--result", str(context / "draft.json"))
        self.assertEqual(2, result.returncode, result.stderr)
        self.assertEqual("", result.stdout)
        native, native_code = flow.finish(root, identifier)
        self.assertEqual(2, native_code)
        for diagnostic in (json.loads(result.stderr), native):
            self.assertEqual("evidence.manifest-recovery-record", diagnostic["code"])
            self.assertTrue(diagnostic["context_retained"])
            self.assertFalse(diagnostic["recoverable"])
        self.assertEqual(before, self.inventory(context))
        self.assertEqual(output_before, self.inventory(output))

    def test_direct_managed_publish_keeps_output_override_and_cleans_owned_context(self):
        _, _, context, _, _ = self.ready()
        output = self.root / "direct-output"
        output.mkdir()
        result = run_python(SCRIPTS / "review_report.py", "--context-dir", str(context),
                            "--result", str(context / "draft.json"), "--report-dir", str(output))
        self.assertEqual(0, result.returncode, result.stderr)
        report = Path(result.stdout.strip())
        self.assertEqual([report], list(output.glob("merge-review-*.md")))
        self.assertFalse(context.exists())

    def test_unreadable_manifest_marker_defers_native_and_direct_publication(self):
        root, identifier, context, _, _ = self.ready(draft_format="compact")
        path = flow.job_path(root, identifier)
        denied = recovery.Retryable("Synthetic recovery record sharing denial")
        initial = coordination.read_json(path)
        with mock.patch.object(recovery, "read_pending", side_effect=denied):
            blocked, code = flow.finish(root, identifier)
        self.assertEqual(3, code, blocked)
        self.assertEqual("finish", blocked["next_action"])
        after = coordination.read_json(path)
        for field in ("state", "phase", "next_action", "error"):
            self.assertEqual(initial.get(field), after.get(field), field)

        requests = self.deny_first_author_marker(root, identifier, context)
        saved = coordination.read_json(path)
        before = self.inventory(context)
        output = self.repo / "review-reports"
        output_before = self.inventory(output)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(recovery, "read_pending", side_effect=denied), \
             mock.patch.object(workspace, "load", side_effect=AssertionError("Read manifest after blocked guard")), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            native, native_code = flow.finish(root, identifier)
            direct_code = review_report.main(["--context-dir", str(context), "--result", str(context / "draft.json")])
        self.assertEqual("", stdout.getvalue())
        self.assertEqual(3, direct_code)
        self.assertEqual(3, native_code)
        self.assertEqual(native, json.loads(stderr.getvalue()))
        self.assertEqual(recovery.RETRY_WITH_REQUESTS_FILE, native["next_action"])
        self.assertIn("--requests-file", native["hint"])
        self.assertTrue(native["context_retained"])
        self.assertEqual(before, self.inventory(context))
        self.assertEqual(output_before, self.inventory(output))
        current = coordination.read_json(path)
        for field in ("state", "phase", "next_action", "error"):
            self.assertEqual(saved[field], current[field], field)

        generation = workspace.load(context)["generation"]
        with mock.patch.object(recovery, "read_pending", side_effect=[None, denied]), redirect_stderr(io.StringIO()):
            late, code = flow.finish(root, identifier)
        self.assertEqual(3, code, late)
        self.assertEqual(recovery.RETRY_WITH_REQUESTS_FILE, late["next_action"])
        self.assertEqual(before, self.inventory(context))
        self.assertEqual(output_before, self.inventory(output))
        self.assertEqual(late["next_action"], flow.status(root, identifier, summary=True)["next_action"])
        step_args = ["step", "--runtime-root", str(root), "--request-id", identifier]
        code, bare = self.cli(*step_args)
        self.assertEqual(3, code, bare)
        self.assertEqual(late["next_action"], bare["next_action"])
        code, resumed = self.cli(*step_args, "--requests-file", str(requests))
        self.assertEqual(0, code, resumed)
        self.assertTrue(resumed["complete"])
        self.assertEqual(generation + 1, workspace.load(context)["generation"])
        self.assertEqual(1, flow.review_checkpoint.revision(flow.review_checkpoint.load(context)))

    def test_native_publish_guard_under_writer_retains_context_after_early_check(self):
        root, identifier, context, manifest, _ = self.ready()
        self.stage_inventory(root, identifier, context, manifest["selected_repo"])
        before = self.inventory(context)
        checks = []

        def read_pending(location):
            checks.append(str(location) in review_runtime._WRITERS.get())
            if len(checks) == 2:
                raise recovery.Retryable("Synthetic recovery record sharing denial")
            return None

        with mock.patch.object(recovery, "read_pending", side_effect=read_pending), \
             mock.patch.object(workspace_report, "publish", side_effect=AssertionError("Unexpected publish")), \
             redirect_stderr(io.StringIO()):
            diagnostic, code = flow.finish(root, identifier)
        self.assertEqual([False, True], checks)
        self.assertEqual(3, code, diagnostic)
        self.assertTrue(diagnostic["context_retained"])
        self.assertEqual("evidence.manifest-write-repair", diagnostic["code"])
        self.assertEqual("finish", diagnostic["next_action"])
        self.assertEqual(before, self.inventory(context))
        self.assertEqual("retry-step", flow.status(root, identifier, summary=True)["next_action"])
        job = coordination.read_json(flow.job_path(root, identifier))
        self.assertEqual("finish", job["next_action"])
        code, recovered = self.cli("step", "--runtime-root", str(root), "--request-id", identifier)
        self.assertEqual(0, code, recovered)
        self.assertTrue(recovered["complete"], recovered)
        self.assertIsNone(recovery.read_pending(context))
        self.assertEqual(manifest["generation"] + 1, workspace.load(context)["generation"])

    def test_late_manifest_guard_preserves_checkpoint_and_retries_finish_without_a_marker(self):
        root, identifier, context, _, _ = self.ready(draft_format="compact")
        requests = self.checkpoint_requests(context)
        with redirect_stderr(io.StringIO()):
            saved = flow.step_file(root, identifier, requests)
        self.assertTrue(saved["complete"], saved)
        checkpoint = flow.review_checkpoint.load(context)
        self.assertEqual(1, flow.review_checkpoint.revision(checkpoint))
        before = self.inventory(context)
        output = self.repo / "review-reports"
        output_before = self.inventory(output)
        denial = recovery.Retryable("Synthetic locked guard sharing denial")
        with mock.patch.object(recovery, "read_pending", side_effect=[None, denial]), \
             mock.patch.object(workspace_report, "publish", side_effect=AssertionError("Unexpected publish")), \
             redirect_stderr(io.StringIO()):
            diagnostic, code = flow.finish(root, identifier)
        self.assertEqual(3, code, diagnostic)
        self.assertEqual("finish", diagnostic["next_action"])
        self.assertEqual(before, self.inventory(context))
        self.assertIsNone(recovery.read_pending(context))
        self.assertEqual(output_before, self.inventory(output))
        job_path = flow.job_path(root, identifier)
        job = coordination.read_json(job_path)
        self.assertEqual(("reviewing", "review", "finish"), (job["state"], job["phase"], job["next_action"]))
        self.assertIsNone(job["error"])
        saved_job = job_path.read_bytes()
        with mock.patch.object(recovery, "read_pending", side_effect=denial), \
             mock.patch.object(workspace, "load", side_effect=AssertionError("Loaded blocked manifest")), \
             mock.patch.object(flow.review_checkpoint, "load", side_effect=AssertionError("Loaded blocked checkpoint")):
            code, blocked = self.cli("step", "--runtime-root", str(root), "--request-id", identifier)
            self.assertEqual(3, code, blocked)
            self.assertEqual("finish", blocked["next_action"])
            self.assertEqual("evidence.manifest-write-repair", blocked["code"])
            self.assertFalse(blocked["complete"])
            self.assertTrue(blocked["recoverable"])
            self.assertTrue(blocked["context_retained"])
            self.assertEqual([], blocked["operations"])
            self.assertEqual(saved_job, job_path.read_bytes())
            self.assertEqual(before, self.inventory(context))
            for summary in (False, True):
                blocked = flow.status(root, identifier, summary=summary)
                self.assertEqual("finish", blocked["next_action"])
                self.assertNotIn("resume_from", blocked)
            checked, code = flow.check(root, identifier)
            self.assertEqual(3, code, checked)
            self.assertEqual("finish", checked["next_action"])

        full = flow.status(root, identifier)
        summary = flow.status(root, identifier, summary=True)
        self.assertEqual("finish", full["next_action"])
        self.assertEqual(checkpoint, full["execution_checkpoint"])
        self.assertEqual("finish", summary["next_action"])
        self.assertEqual("checkpoint", summary["resume_from"])
        self.assertEqual(1, summary["checkpoint_revision"])
        self.assertTrue(summary["checkpoint_current"])

        draft = context / "draft.json"
        draft_before = draft.read_bytes()
        draft.write_bytes(b"{")
        checked, code = flow.check(root, identifier)
        self.assertEqual(3, code, checked)
        self.assertTrue(checked["errors"])
        for summary in (False, True):
            self.assertEqual("repair-draft-and-check", flow.status(root, identifier, summary=summary)["next_action"])
        with redirect_stderr(io.StringIO()):
            repaired, code = flow.finish(root, identifier)
        self.assertEqual(3, code, repaired)
        full = flow.status(root, identifier)
        self.assertEqual("draft-repair", full["phase"])
        self.assertIsNone(full.get("next_action"))
        self.assertIsNone(full["error"])
        draft.write_bytes(draft_before)
        with redirect_stderr(io.StringIO()):
            published, code = flow.finish(root, identifier)
        self.assertEqual(0, code, published)
        self.assert_published_action(root, identifier, published)
        self.assertEqual([Path(published["report_path"])], list(output.glob("merge-review-*.md")))

    def test_unreadable_marker_with_explicit_step_input_keeps_batch_retry(self):
        root, identifier, context, _, _ = self.ready(draft_format="compact")
        requests = self.checkpoint_requests(context)
        step_args = ["step", "--runtime-root", str(root), "--request-id", identifier]
        for supplied in ("requests-file", "empty-operations"):
            with self.subTest(supplied=supplied):
                flow.update(root, identifier, state="reviewing", phase="review", next_action="finish")
                denial = recovery.Retryable("Synthetic marker sharing denial")
                with mock.patch.object(recovery, "read_pending", side_effect=denial), \
                     mock.patch.object(workspace, "load", side_effect=AssertionError("Loaded blocked manifest")):
                    if supplied == "requests-file":
                        code, blocked = self.cli(*step_args, "--requests-file", str(requests))
                        self.assertEqual(3, code, blocked)
                    else:
                        blocked = flow.step(root, identifier, [])
                self.assertFalse(blocked["complete"])
                self.assertEqual(recovery.RETRY_WITH_REQUESTS_FILE, blocked["next_action"])
                job = coordination.read_json(flow.job_path(root, identifier))
                self.assertEqual(recovery.PHASE, job["phase"])
                self.assertEqual(recovery.RETRY_WITH_REQUESTS_FILE, job["next_action"])

    def test_cancel_during_unreadable_marker_wins_over_saved_finish_retry(self):
        root, identifier, context, _, _ = self.ready()
        job_path = flow.job_path(root, identifier)
        flow.update(root, identifier, state="reviewing", phase="review", next_action="finish")
        cleanup = review_session.cleanup_session

        def cancel_and_deny(location):
            self.assertEqual(context, location)
            self.assertIn(str(context), review_runtime._WRITERS.get())
            coordination.atomic(job_path.with_suffix(".cancel"), {"cancelled": True})
            raise recovery.Retryable("Synthetic marker sharing denial")

        def cleanup_after_release(location):
            self.assertEqual(context, location)
            self.assertNotIn(str(context), review_runtime._WRITERS.get())
            return cleanup(location)

        with mock.patch.object(recovery, "read_pending", side_effect=cancel_and_deny), \
             mock.patch.object(review_session, "cleanup_session", side_effect=cleanup_after_release) as cleaned:
            code, cancelled = self.cli("step", "--runtime-root", str(root), "--request-id", identifier)
        self.assertEqual(2, code, cancelled)
        self.assertTrue(cancelled["cancelled"])
        self.assertFalse(cancelled["cleanup_pending"])
        cleaned.assert_called_once_with(context)
        self.assertFalse(context.exists())
        job = coordination.read_json(job_path)
        self.assertEqual(("cancelled", "terminal", None), (job["state"], job["phase"], job["next_action"]))

    def test_cancel_when_manifest_lock_is_released_wins_over_saved_finish_retry(self):
        root, identifier, context, _, _ = self.ready()
        job_path = flow.job_path(root, identifier)
        manifest_lock = job_path.with_suffix(".manifest.lock")
        cancel_marker = job_path.with_suffix(".cancel")
        flow.update(root, identifier, state="reviewing", phase="review", next_action="finish")
        release = coordination.FileLock.release
        args = ["--runtime-root", str(root), "--request-id", identifier]

        def cancel_after_unlock(lock):
            release(lock)
            if lock.path == manifest_lock:
                cancelled = run_python(SCRIPTS / "review_session.py", "cancel", *args)
                self.assertEqual(0, cancelled.returncode, cancelled.stderr)
                self.assertTrue(cancel_marker.is_file())
                self.assertEqual("reviewing", json.loads(cancelled.stdout)["state"])
                self.assertTrue(context.is_dir())

        denial = recovery.Retryable("Synthetic marker sharing denial")
        with mock.patch.object(recovery, "read_pending", side_effect=denial), \
             mock.patch.object(coordination.FileLock, "release", new=cancel_after_unlock), \
             mock.patch.object(review_session, "cleanup_session", wraps=review_session.cleanup_session) as cleaned:
            code, cancelled = self.cli("step", *args)
        self.assertEqual(2, code, cancelled)
        self.assertTrue(cancelled["cancelled"])
        self.assertFalse(cancelled["cleanup_pending"])
        cleaned.assert_called_once_with(context)
        self.assertFalse(context.exists())
        job = coordination.read_json(job_path)
        self.assertEqual(("cancelled", "terminal", None), (job["state"], job["phase"], job["next_action"]))

    def test_manifest_recovery_directs_finish_if_job_cannot_return_to_reviewing(self):
        for action in ("retry-step", recovery.RETRY_WITH_REQUESTS_FILE, "finish"):
            with self.subTest(action=action):
                root, identifier, context, manifest, _ = self.ready(draft_format="compact")
                if action == "retry-step":
                    self.stage_inventory(root, identifier, context, manifest["selected_repo"])
                if action == recovery.RETRY_WITH_REQUESTS_FILE:
                    self.deny_first_author_marker(root, identifier, context)
                flow.update(root, identifier, state="validating", phase="validation")
                update = flow.update
                read_pending = recovery.read_pending

                def deny_reviewing(*args, **fields):
                    if fields.get("state") == "reviewing":
                        raise OSError("Synthetic job state write denial")
                    return update(*args, **fields)

                def read_marker(location):
                    if action != "retry-step":
                        raise recovery.Retryable("Synthetic recovery record sharing denial")
                    return read_pending(location)

                with mock.patch.object(recovery, "read_pending", side_effect=read_marker):
                    with mock.patch.object(flow, "update", side_effect=deny_reviewing):
                        blocked, code = flow.finish(root, identifier)
                    self.assertEqual(3, code, blocked)
                    self.assertEqual("finish", blocked["next_action"])
                    self.assertIn("review job", blocked["hint"])
                    self.assertEqual("validating", coordination.read_json(flow.job_path(root, identifier))["state"])
                    restored, code = flow.finish(root, identifier)
                self.assertEqual(3, code, restored)
                self.assertEqual(action, restored["next_action"])
                saved = coordination.read_json(flow.job_path(root, identifier))
                self.assertEqual("reviewing", saved["state"])
                self.assertEqual(action, saved["next_action"])
                if action == "finish":
                    summary = flow.status(root, identifier, summary=True)
                    self.assertEqual("finish", summary["next_action"])
                    self.assertEqual("initial-evidence", summary["resume_from"])
                    step_args = ["step", "--runtime-root", str(root), "--request-id", identifier]
                    code, rejected = self.cli(*step_args)
                    self.assertEqual(3, code, rejected)
                    self.assertEqual("authoring.invalid-requests-file", rejected["code"])
                    self.assertEqual("finish", flow.status(root, identifier, summary=True)["next_action"])
                    with redirect_stderr(io.StringIO()):
                        stepped = flow.step(root, identifier, [
                            {"id": "inventory", "op": "inventory", "repo": manifest["selected_repo"]},
                        ])
                    self.assertTrue(stepped["complete"], stepped)
                    self.assertIsNone(coordination.read_json(flow.job_path(root, identifier))["next_action"])

    def test_publication_clears_original_file_retry_even_when_cleanup_is_pending(self):
        root, identifier, context, _, _ = self.ready(draft_format="compact")
        self.deny_first_author_marker(root, identifier, context)

        def deny_cleanup(location):
            self.assertEqual(context, location)
            committed = coordination.read_json(flow.job_path(root, identifier))
            self.assertEqual("published", committed["state"])
            self.assertTrue(committed["cleanup_pending"])
            self.assertIsNone(committed["next_action"])
            raise PermissionError("Synthetic cleanup denial")

        with mock.patch.object(review_session, "cleanup_session", side_effect=deny_cleanup), \
             redirect_stderr(io.StringIO()):
            published, code = flow.finish(root, identifier)
        self.assertEqual(0, code, published)
        self.assertTrue(published["cleanup_pending"])
        self.assertTrue(context.is_dir())
        with mock.patch.object(flow, "_file_digest_if_present", side_effect=PermissionError("Synthetic draft sharing denial")), \
             mock.patch.object(recovery, "read_pending", side_effect=AssertionError("Read marker after publication")), \
             mock.patch.object(flow.review_checkpoint, "load", side_effect=AssertionError("Read published checkpoint")):
            self.assert_published_action(root, identifier, published, "finish")
        job = coordination.read_json(flow.job_path(root, identifier))
        self.assertIsNone(job["next_action"])
        self.assertIsNone(job["error"])
        self.assertFalse(job["recoverable"])
        with mock.patch.object(workspace_report, "validate", side_effect=AssertionError("Duplicate validation")), \
             redirect_stderr(io.StringIO()):
            repeated, code = flow.finish(root, identifier)
        self.assertEqual(0, code, repeated)
        self.assertFalse(repeated["cleanup_pending"])
        self.assertFalse(context.exists())
        self.assert_published_action(root, identifier, repeated)
        self.assertEqual(published["review_outcome"], repeated["review_outcome"])
        report = Path(published["report_path"])
        self.assertEqual([report], list(report.parent.glob("merge-review-*.md")))

    def test_large_publication_cli_preserves_action_and_paging_in_both_scopes(self):
        for fallback in (False, True):
            with self.subTest(field_list_fallback=fallback):
                root, identifier, context, _, _ = self.ready()
                args = ["--runtime-root", str(root), "--request-id", identifier]
                reports = self.repo / "review-reports"
                reports_before = set(reports.glob("merge-review-*.md"))
                metadata = ({f"field-{index}": "證" * 220 for index in range(48)}
                            if fallback else {"detail": "證" * 15000})
                flow.update(root, identifier, display_metadata=metadata)

                def assert_response(command, scope, action):
                    result = json.loads(self.cli_crlf(command, *args))
                    self.assertEqual("MergeReviewEvidenceSummary/v1", result["schema"])
                    self.assertEqual(action, result["next_action"])
                    self.assertEqual(scope, result["continue"]["artifact_scope"])
                    self.assertEqual(fallback, "fields" in result["summary"])
                    if scope == "runtime":
                        self.assertEqual("runtime", result["response_scope"])
                        self.assertEqual("status", result["continue"]["command"])
                    else:
                        self.assertEqual("review_session.py view", result["continue"]["command"])
                    code, page = self.cli("view", *args, "--read-only", "--artifact-scope", scope,
                                          "--artifact", result["artifact"], "--pointer", "/next_action")
                    self.assertEqual(0, code, page)
                    self.assertEqual(action, page["text"])

                with mock.patch.object(review_session, "cleanup_session",
                                       side_effect=PermissionError("Synthetic cleanup denial")):
                    assert_response("finish", "context", "finish")
                    assert_response("status", "context", "finish")
                self.assertTrue(context.exists())
                with mock.patch.object(workspace_report, "validate", side_effect=AssertionError("Duplicate validation")):
                    assert_response("finish", "runtime", "open-report")
                    assert_response("status", "runtime", "open-report")
                self.assertFalse(context.exists())
                self.assertEqual(1, len(set(reports.glob("merge-review-*.md")) - reports_before))
                # Cleanup can remove the directory before its flag is cleared.
                flow.update(root, identifier, cleanup_pending=True)
                assert_response("status", "runtime", "finish")

    def test_publication_cli_line_budget_includes_crlf_and_runtime_scope(self):
        root, identifier, _, _, _ = self.ready()
        args = ["status", "--runtime-root", str(root), "--request-id", identifier]
        with redirect_stderr(io.StringIO()):
            published, code = flow.finish(root, identifier)
        self.assertEqual(0, code, published)
        value = {**flow.status(root, identifier), "padding": ""}
        padding = flow.LIMIT - 2 - len(review_runtime.canonical(value))
        value["padding"] = "x" * padding
        with mock.patch.object(flow, "status", return_value=value):
            raw = self.cli_crlf(*args)
            self.assertEqual(flow.LIMIT, len(raw))
            self.assertEqual(flow.SCHEMA, json.loads(raw)["schema"])
            value["padding"] += "x"
            overflow = json.loads(self.cli_crlf(*args))
        self.assertEqual("MergeReviewEvidenceSummary/v1", overflow["schema"])
        self.assertEqual("open-report", overflow["next_action"])
        self.assertEqual("runtime", overflow["response_scope"])

        value["padding"] = "x" * (flow.LIMIT * 2)
        value["preview_fields"] = {f"field-{index}": "" for index in range(36)}
        response = flow.response_context(root, identifier)
        with mock.patch.object(review_runtime, "store_view", return_value="a" * 64):
            initial = flow.compact(value, response, limit=flow.LIMIT * 2)
            available = flow.LIMIT - 2 - len(review_runtime.canonical(initial))
            for key in value["preview_fields"]:
                count = min(700, available)
                value["preview_fields"][key] = "x" * count
                available -= count
            self.assertEqual(0, available)
            preview = flow.compact(value, response, limit=flow.LIMIT * 2)
        self.assertEqual(flow.LIMIT - 2, len(review_runtime.canonical(preview)))
        self.assertNotIn("fields", preview["summary"])
        with mock.patch.object(flow, "status", return_value=value):
            budgeted = json.loads(self.cli_crlf(*args))
        self.assertIn("fields", budgeted["summary"])
        self.assertEqual("open-report", budgeted["next_action"])
        self.assertEqual("runtime", budgeted["response_scope"])

    def test_compacted_publication_keeps_paging_when_runtime_hint_exceeds_budget(self):
        root = self.root / ("證" * flow.LIMIT)
        identifier = "a" * 32
        value = {"next_action": "open-report", "publication": {"verified": True}, "report_path": "x" * flow.LIMIT}
        with mock.patch.object(review_runtime, "store_view", return_value="b" * 64):
            result = flow.compact(value, root / "responses" / identifier)
        self.assertLess(len(review_runtime.canonical(result)), flow.LIMIT)
        self.assertEqual("open-report", result["next_action"])
        self.assertEqual("b" * 64, result["artifact"])
        self.assertEqual(identifier, result["continue"]["request_id"])
        self.assertEqual("runtime", result["continue"]["artifact_scope"])
        self.assertNotIn("runtime_root", result["continue"])

    def test_finish_recovers_verified_link_before_reading_manifest_marker(self):
        root, identifier, context, _, _ = self.ready(draft_format="compact")
        self.deny_first_author_marker(root, identifier, context)
        update = flow.update

        def lose_completion(*args, **fields):
            if fields.get("state") == "published":
                raise OSError("Synthetic lost publication completion")
            return update(*args, **fields)

        with mock.patch.object(flow, "update", side_effect=lose_completion), redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(OSError, "lost publication completion"):
                flow.finish(root, identifier)
        path = flow.job_path(root, identifier)
        job = coordination.read_json(path)
        self.assertEqual("validating", job["state"])
        self.assertEqual(recovery.RETRY_WITH_REQUESTS_FILE, job["next_action"])
        report = Path(job["planned_report"])
        body = report.read_bytes()
        saved = path.read_bytes()
        with mock.patch.object(recovery, "read_pending", side_effect=AssertionError("Read marker after report linked")), \
             mock.patch.object(flow, "_file_digest_if_present", side_effect=AssertionError("Read draft after report linked")), \
             mock.patch.object(workspace_report, "validate", side_effect=AssertionError("Duplicate validation")), \
             redirect_stderr(io.StringIO()):
            for summary in (False, True):
                result = flow.status(root, identifier, summary=summary)
                self.assertEqual("validating", result["state"])
                self.assertEqual("finish", result["next_action"])
                self.assertEqual(str(report), result["report_path"])
            self.assertEqual(saved, path.read_bytes())
            report.write_bytes(body + b"\nChanged fixed report content.\n")
            with self.assertRaisesRegex(ValueError, "fixed body changed"):
                flow.status(root, identifier, summary=True)
            with self.assertRaisesRegex(ValueError, "fixed body changed"):
                flow.finish(root, identifier)
            self.assertTrue(context.exists())
            self.assertEqual("validating", coordination.read_json(path)["state"])
            report.write_bytes(body)
            recovered, code = flow.finish(root, identifier)
        self.assertEqual(0, code, recovered)
        self.assert_published_action(root, identifier, recovered)
        self.assertEqual(job["review_outcome"], recovered["review_outcome"])
        self.assertEqual([report], list(report.parent.glob("merge-review-*.md")))
        self.assertEqual(body, report.read_bytes())
        self.assertIsNone(coordination.read_json(path)["next_action"])

    def test_post_link_publication_error_clears_persisted_step_retry(self):
        root, identifier, context, _, _ = self.ready(draft_format="compact")
        self.deny_first_author_marker(root, identifier, context)
        commit = report_publication.commit_report

        def link_then_raise(stage, output):
            commit(stage, output)
            raise report_publication.PublicationError("staging cleanup", stage, PermissionError("Synthetic sharing denial"))

        with mock.patch.object(report_publication, "commit_report", side_effect=link_then_raise), \
             redirect_stderr(io.StringIO()):
            recovered, code = flow.finish(root, identifier)
        self.assertEqual(0, code, recovered)
        self.assert_published_action(root, identifier, recovered)
        self.assertIsNone(coordination.read_json(flow.job_path(root, identifier))["next_action"])
        report = Path(recovered["report_path"])
        self.assertEqual([report], list(report.parent.glob("merge-review-*.md")))

    def test_legacy_publication_summary_retains_unknown_outcome_without_step_retry(self):
        root, identifier, _, _, _ = self.ready()
        with redirect_stderr(io.StringIO()):
            published, code = flow.finish(root, identifier)
        self.assertEqual(0, code, published)
        path = flow.job_path(root, identifier)
        job = coordination.read_json(path)
        job.pop("review_outcome")
        job["report_receipt"].pop("review_outcome")
        job.update(next_action=recovery.RETRY_WITH_REQUESTS_FILE, error="Old step failure.",
                   recoverable=True, context_retained=True)
        coordination.atomic(path, job)
        before = path.read_bytes()
        full = flow.status(root, identifier)
        self.assert_published_action(root, identifier, full)
        for result in (full, flow.status(root, identifier, summary=True)):
            self.assertFalse(result["review_outcome"]["outcome_available"])
            self.assertIsNone(result["review_complete"])
            self.assertIsNone(result["remaining"])
            self.assertFalse(result["context_retained"])
        self.assertEqual({}, flow.status(root, identifier, summary=True)["remaining_counts"])
        self.assertEqual(before, path.read_bytes())

    def test_lost_start_response_recovers_initial_evidence_read_only(self):
        args = self.arguments(request_id="a" * 32)
        flow.start(args)
        root = Path(args.runtime_root)
        context = root / "sessions" / args.request_id
        before = self.inventory(root)
        with mock.patch.object(api, "run_git", side_effect=AssertionError("inspection called Git")), \
             mock.patch.object(coordination, "atomic", side_effect=AssertionError("inspection wrote state")):
            summary = flow.status(root, args.request_id, summary=True)
            handoff = summary["initial_evidence"]
            guide = review_runtime.load_view(context, handoff["start_ref"]["artifact"])
        self.assertEqual(before, self.inventory(root))
        self.assertEqual("initial-evidence", summary["resume_from"])
        self.assertTrue(handoff["start_ref"]["read_only"])
        self.assertEqual(guide["initial_reads_file"], handoff["initial_reads_file"])
        operations = coordination.read_json(Path(handoff["initial_reads_file"]))
        self.assertEqual(len(operations), handoff["initial_read_count"])
        self.assertTrue(flow.step(root, args.request_id, operations)["complete"])
        (context / "checkpoint.json").write_text("{", encoding="utf-8")
        fallback = flow.status(root, args.request_id, summary=True)
        self.assertEqual("initial-evidence", fallback["resume_from"])
        self.assertEqual(handoff, fallback["initial_evidence"])

    def test_corrupt_or_missing_checkpoint_falls_back_from_the_saved_author_checkpoint(self):
        with redirect_stderr(io.StringIO()):
            started = flow.start(self.arguments(draft_format="compact"))
        root, identifier = Path(started["runtime_root"]), started["request_id"]
        context = Path(started["context_dir"])
        self.addCleanup(review_session.cleanup_session, context)
        checkpoint = context / "checkpoint.json"
        operation = {"id": "navigation", "op": "author", "updates": {},
                     "expected_draft_sha256": coordination.file_digest(context / "draft.json"),
                     "expected_checkpoint_revision": 0, "checkpoint": {"next_action": "Read the fixed evidence."}}
        with redirect_stderr(io.StringIO()):
            saved = flow.step(root, identifier, [operation])
        self.assertTrue(saved["complete"], saved)
        initial = flow.status(root, identifier, summary=True)
        self.assertEqual("checkpoint", initial["resume_from"])
        self.assertEqual(1, initial["checkpoint_revision"])
        self.assertEqual(1, coordination.read_json(flow.job_path(root, identifier))["execution_checkpoint"]["checkpoint_revision"])
        for name, content in (("corrupt", b"{"), ("missing", None)):
            with self.subTest(case=name):
                if content is None:
                    checkpoint.unlink()
                else:
                    checkpoint.write_bytes(content)
                with mock.patch.object(api, "run_git", side_effect=AssertionError("status ran Git")), \
                     mock.patch.object(coordination, "atomic", side_effect=AssertionError("status wrote state")):
                    fallback = flow.status(root, identifier, summary=True)
                    full_status = flow.status(root, identifier)
                self.assertEqual("initial-evidence", fallback["resume_from"])
                self.assertFalse(fallback["checkpoint_current"])
                self.assertEqual(0, fallback["checkpoint_revision"])
                self.assertNotIn("execution_checkpoint", full_status)
                self.assertEqual(initial["initial_evidence"], fallback["initial_evidence"])
                reads = fallback["initial_evidence"]["initial_reads_file"]
                code, step = self.cli("step", "--runtime-root", str(root), "--request-id", identifier,
                                      "--requests-file", reads)
                self.assertEqual(0, code, step)
                self.assertTrue(step["complete"])
                if content is None:
                    self.assertFalse(checkpoint.exists())
                else:
                    self.assertEqual(content, checkpoint.read_bytes())

    def test_status_reports_original_requests_file_retry_before_hashing_and_prefers_a_durable_record(self):
        root, identifier, context, manifest, _ = self.ready()
        initial = flow.status(root, identifier, summary=True)["initial_evidence"]
        retry = {"phase": recovery.PHASE, "next_action": recovery.RETRY_WITH_REQUESTS_FILE,
                 "recoverable": True, "error": "Use the original --requests-file."}
        flow.update(root, identifier, state="reviewing", **retry)
        before = self.inventory(context)
        with mock.patch.object(flow, "_file_digest_if_present", side_effect=AssertionError("status hashed draft")), \
             mock.patch.object(flow.review_checkpoint, "load", side_effect=AssertionError("status read checkpoint")), \
             mock.patch.object(coordination, "atomic", side_effect=AssertionError("status wrote state")):
            for summary in (False, True):
                with self.subTest(summary=summary):
                    status = flow.status(root, identifier, summary=summary)
                    self.assertEqual(retry["next_action"], status["next_action"])
                    self.assertNotIn("resume_from", status)
                    self.assertEqual(initial, status["initial_evidence"])
            with mock.patch.object(recovery, "read_pending", side_effect=recovery.Retryable("Marker sharing denial")):
                status = flow.status(root, identifier, summary=True)
                self.assertEqual(recovery.RETRY_WITH_REQUESTS_FILE, status["next_action"])
                self.assertEqual(initial, status["initial_evidence"])
        self.assertEqual(before, self.inventory(context))
        with mock.patch.object(recovery, "read_pending", side_effect=recovery.Retryable("Marker sharing denial")), \
             mock.patch.object(workspace, "load", side_effect=AssertionError("check loaded the manifest")):
            check, code = flow.check(root, identifier)
        self.assertEqual(3, code, check)
        self.assertEqual(recovery.RETRY_WITH_REQUESTS_FILE, check["next_action"])
        self.assertEqual("unknown", check["publication_readiness"])

        self.stage_inventory(root, identifier, context, manifest["selected_repo"])
        flow.update(root, identifier, **retry)
        for summary in (False, True):
            status = flow.status(root, identifier, summary=summary)
            self.assertEqual("retry-step", status["next_action"])
            self.assertNotIn("initial_evidence", status)

    def test_check_cache_binds_diagnostics_to_bytes_inspected(self):
        for name, field in (("draft.json", "draft_sha256"), ("manifest.json", "manifest_sha256")):
            with self.subTest(file=name):
                root, identifier, context, _, _ = self.ready()
                path = context / name
                before = coordination.file_digest(path)
                inspect = flow.diagnostics

                def change_after_read(*args, **kwargs):
                    checked = inspect(*args, **kwargs)
                    path.write_bytes(path.read_bytes() + b"\n")
                    return checked

                with mock.patch.object(flow, "diagnostics", side_effect=change_after_read):
                    checked, code = flow.check(root, identifier)
                self.assertEqual(0, code)
                self.assertEqual(before, checked[field])
                self.assertNotEqual(before, coordination.file_digest(path))
                summary = flow.status(root, identifier, summary=True)
                self.assertFalse(summary["check_current"])
                self.assertEqual("unknown", summary["publication_readiness"])
                self.assertNotEqual("finish", summary["next_action"])

    def test_pending_report_remains_publishable_and_check_keeps_exit_code(self):
        root, identifier, context, _, draft = self.ready()
        change = {key: draft["coverage"][0][key] for key in ("repo", "source", "path")}
        pending = {"id": "PC-001", "title": "確認資料契約", "reason": "缺業務定義",
                   "missing_evidence": "資料契約文件", "verification": "核對欄位允許值",
                   "owner": "QA", "related_changes": [change], "evidence": []}
        draft["pending_confirmations"] = [pending]
        coordination.atomic(context / "draft.json", draft)
        checked, code = flow.check(root, identifier)
        self.assertEqual(3, code)
        self.assertEqual("repair-draft", checked["publication_readiness"])
        self.assertIn("/pending_confirmations/0/id", {row["pointer"] for row in checked["errors"]})
        rejected, code = flow.finish(root, identifier)
        self.assertEqual(3, code)
        self.assertTrue(rejected["context_retained"])
        self.assertIn("Q-001", str(rejected["errors"]))
        pending["id"] = "Q-001"
        coordination.atomic(context / "draft.json", draft)
        checked, code = flow.check(root, identifier)
        self.assertEqual(3, code)
        self.assertFalse(checked["ready_for_finish"])
        self.assertEqual("ready", checked["publication_readiness"])
        summary = flow.status(root, identifier, summary=True)
        self.assertEqual("finish", summary["next_action"])
        self.assertEqual([], summary["errors"])
        published, code = flow.finish(root, identifier)
        self.assertEqual(0, code)
        self.assertFalse(published["review_outcome"]["review_complete"])
        self.assertEqual("需人工確認", flow.status(root, identifier, summary=True)["review_outcome"]["recommendation"])

    def test_start_replays_saved_no_fetch_and_rejects_conflicts_with_owner_lock(self):
        for offline, identifier in ((False, "1" * 32), (True, "2" * 32)):
            with self.subTest(offline=offline):
                identity = ("--runtime-root", str(self.root / "runtime"), "--request-id", identifier)
                start_args = ("start", "--workspace", str(self.root), "--project", str(self.repo),
                              "--base", "main", "--head", "feature", *identity)
                offline_args = start_args + ("--no-fetch",)
                accepted_args, conflicting_args = (offline_args, start_args) if offline else (start_args, offline_args)
                code, started = self.cli(*accepted_args)
                self.assertEqual(0, code, started)
                context = Path(started["context_dir"])
                self.addCleanup(review_session.cleanup_session, context)
                self.assertIs(started["no_fetch"], offline)
                job_path = flow.job_path(Path(started["runtime_root"]), identifier)
                with mock.patch.object(api, "build_manifest", side_effect=AssertionError("recapture")):
                    code, replay = self.cli(*accepted_args)
                    self.assertEqual(0, code, replay)
                    self.assertEqual(str(context), replay["context_dir"])
                    self.assertIs(replay["inputs"]["no_fetch"], offline)
                    with coordination.FileLock(job_path.with_suffix(".owner.lock")):
                        code, replay = self.cli(*accepted_args)
                        self.assertEqual(0, code, replay)
                        self.assertEqual(str(context), replay["context_dir"])
                        self.assertIs(replay["inputs"]["no_fetch"], offline)
                        code, rejected = self.cli(*conflicting_args)
                        self.assertEqual(2, code)
                        self.assertIn("different inputs", rejected["error"])
                        self.assertEqual(identifier, rejected["request_id"])
                        self.assertNotIn("inputs", rejected)
                    with mock.patch.object(flow, "LIMIT", 512):
                        code, compacted = self.cli(*accepted_args)
                    self.assertEqual(0, code, compacted)
                    self.assertEqual("MergeReviewEvidenceSummary/v1", compacted["schema"])
                continuation = compacted["continue"]
                code, summary = self.cli("status", "--summary", "--runtime-root", continuation["runtime_root"],
                                         "--request-id", continuation["request_id"])
                self.assertEqual(0, code, summary)
                self.assertEqual(identifier, summary["request_id"])
                self.assertIs(summary["no_fetch"], offline)

    def test_start_with_owner_lock_before_job_exists_returns_error(self):
        root = coordination.runtime(self.root / "runtime", create=True)
        identifier = "3" * 32
        job_path = flow.job_path(root, identifier)
        with coordination.FileLock(job_path.with_suffix(".owner.lock")), \
             mock.patch.object(api, "build_manifest", side_effect=AssertionError("capture")):
            code, rejected = self.cli("start", "--workspace", str(self.root), "--project", str(self.repo),
                                      "--base", "main", "--head", "feature", "--no-fetch",
                                      "--runtime-root", str(root), "--request-id", identifier)
            self.assertFalse(job_path.exists())
        self.assertEqual(2, code)
        self.assertEqual(identifier, rejected["request_id"])
        self.assertTrue(rejected["error"])
        self.assertNotIn("inputs", rejected)

    def test_no_fetch_preflight_rejects_remote_ref_before_capture(self):
        run_git(self.repo, "remote", "add", "origin", str(self.root / "unused.git"))
        with mock.patch.object(api, "fetch_inputs", side_effect=AssertionError("fetch")):
            result = flow.launch_preflight(self.arguments(base="origin/main", no_fetch=True))
        self.assertFalse(result["valid"])
        self.assertTrue(any(row["pointer"] == "/base" and "--no-fetch" in row["message"]
                            for row in result["errors"]), result)
        self.assertEqual(0, result["network_calls"])

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
        def batch(_context, _manifest, _operations, _api, *, on_progress, commit):
            self.assertTrue(callable(commit))
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

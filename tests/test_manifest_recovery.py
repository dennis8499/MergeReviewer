"""Fault injection at the managed manifest commit boundary."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
from unittest import mock

from tests.fixtures import ReviewFixture, review_session, workspace
import review_checkpoint
import review_coordinator as coordination
import review_flow as flow
import review_manifest_recovery as recovery


def sharing_error(code):
    error = PermissionError("Synthetic manifest sharing denial")
    error.winerror = code
    return error


class ManifestRecoveryTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.commit()
        self.started = self.start()
        self.root_runtime = Path(self.started["runtime_root"])
        self.identifier = self.started["request_id"]
        self.context = Path(self.started["context_dir"])
        self.target = self.context / "manifest.json"

    def start(self):
        args = flow.parser().parse_args([
            "start", "--workspace", str(self.root), "--project", str(self.repo),
            "--base", "main", "--head", "feature", "--draft-format", "compact",
        ])
        with redirect_stderr(io.StringIO()):
            started = flow.start(args)
        self.addCleanup(review_session.cleanup_session, Path(started["context_dir"]))
        return started

    def step(self, operations):
        with redirect_stderr(io.StringIO()):
            return flow.step(self.root_runtime, self.identifier, operations)

    def requests_file(self, operations):
        path = self.root / "operations.json"
        coordination.atomic(path, operations)
        return path

    def cli_step(self, path=None):
        args = ["step", "--runtime-root", str(self.root_runtime), "--request-id", self.identifier]
        if path is not None:
            args.extend(("--requests-file", str(path)))
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = review_session.main(args)
        self.assertTrue(stdout.getvalue(), stderr.getvalue())
        return code, json.loads(stdout.getvalue())

    def inventory(self, identifier="inventory"):
        return {"id": identifier, "op": "inventory", "repo": self.started["selected_repo"]}

    def author(self):
        return {"id": "first", "op": "author", "updates": {"summary": "First judgment"},
                "expected_draft_sha256": coordination.file_digest(self.context / "draft.json"),
                "expected_checkpoint_revision": 0}

    def replace_failures(self, failures):
        original = os.replace
        errors = iter(failures)
        attempts = []

        def replace(source, target):
            if Path(target) == self.target:
                self.assertIsNotNone(recovery.read_pending(self.context))
                attempts.append(source)
                error = next(errors, None)
                if error is not None:
                    raise error
            return original(source, target)

        return mock.patch.object(recovery.os, "replace", side_effect=replace), attempts

    def deny_author_draft_replace(self, *, deny_manifest=False):
        replace = os.replace

        def denied(source, target):
            path = Path(target)
            if path == self.context / "draft.json":
                self.assertTrue((self.context / "author-first.json").is_file())
                raise sharing_error(5)
            if deny_manifest and path == self.target:
                raise sharing_error(32)
            return replace(source, target)

        return mock.patch.object(recovery.os, "replace", side_effect=denied)

    def test_transient_windows_denial_commits_one_generation(self):
        before = workspace.load(self.context)["generation"]
        failure, attempts = self.replace_failures([sharing_error(32)])
        with failure, mock.patch.object(recovery.time, "sleep") as sleep:
            result = self.step([self.inventory()])
        self.assertTrue(result["complete"])
        self.assertEqual([mock.call(.05)], sleep.call_args_list)
        self.assertEqual(2, len(attempts))
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(before + 1, workspace.load(self.context)["generation"])
        self.assertIsNone(recovery.read_pending(self.context))

    def test_progress_sharing_denial_keeps_completed_operations_in_ledger(self):
        before = workspace.load(self.context)["generation"]
        job_path = flow.job_path(self.root_runtime, self.identifier)
        atomic = coordination.atomic
        attempted = []

        def deny_second_completion(path, value):
            progress = value.get("progress", {}) if path == job_path else {}
            if ("last_step" not in value and progress.get("phase") == "operation-complete"
                    and progress["completed"] == 2):
                attempted.append(progress["completed"])
                raise sharing_error(32)
            return atomic(path, value)

        operations = [self.inventory(), self.inventory("second")]
        requests = self.requests_file(operations)
        with mock.patch.object(coordination, "atomic", side_effect=deny_second_completion):
            code, result = self.cli_step(requests)
        self.assertEqual(0, code, result)
        self.assertEqual([2], attempted)
        manifest = workspace.load(self.context)
        self.assertEqual(["inventory", "second"], list(manifest["operations"]))
        self.assertEqual(list(manifest["operations"]), [row["id"] for row in result["operations"]])
        self.assertEqual(before + 1, manifest["generation"])
        self.assertIsNone(recovery.read_pending(self.context))

    def test_final_job_update_denial_resumes_committed_generation_without_file(self):
        before = workspace.load(self.context)["generation"]
        job_path = flow.job_path(self.root_runtime, self.identifier)
        atomic = coordination.atomic

        def deny_final_step(path, value):
            if path == job_path and value.get("last_step", {}).get("complete"):
                raise sharing_error(5)
            return atomic(path, value)

        requests = self.requests_file([self.inventory()])
        with mock.patch.object(coordination, "atomic", side_effect=deny_final_step):
            code, rejected = self.cli_step(requests)
        self.assertEqual(3, code)
        self.assertEqual("retry-step", rejected["next_action"])
        self.assertEqual("prepared", recovery.read_pending(self.context)["state"])
        self.assertEqual(before + 1, workspace.load(self.context)["generation"])
        with mock.patch.object(workspace, "ordered_step", side_effect=AssertionError("Replayed submitted operations")):
            code, resumed = self.cli_step()
        self.assertEqual(0, code, resumed)
        self.assertEqual(["inventory"], [row["id"] for row in resumed["operations"]])
        self.assertEqual(before + 1, workspace.load(self.context)["generation"])
        self.assertIsNone(recovery.read_pending(self.context))

    def test_evidence_step_and_prepared_retry_ignore_locked_author_state(self):
        before = workspace.load(self.context)
        draft_path = self.context / "draft.json"
        draft_bytes = draft_path.read_bytes()
        read_bytes = Path.read_bytes

        def deny_draft_read(path):
            if path == draft_path:
                raise sharing_error(32)
            return read_bytes(path)

        first = self.requests_file([self.inventory()])
        with mock.patch.object(Path, "read_bytes", autospec=True, side_effect=deny_draft_read), \
             mock.patch.object(review_checkpoint, "load", side_effect=AssertionError("Read checkpoint")):
            code, result = self.cli_step(first)
            self.assertEqual(0, code, result)
            failure, _ = self.replace_failures([sharing_error(33)] * 5)
            second = self.requests_file([self.inventory("second")])
            with failure, mock.patch.object(recovery.time, "sleep"):
                code, denied = self.cli_step(second)
            self.assertEqual(3, code)
            self.assertEqual("retry-step", denied["next_action"])
            pending = recovery.read_pending(self.context)
            self.assertIs(False, pending["author_state_required"])
            for name in ("previous_draft_sha256", "previous_checkpoint", "draft_sha256", "checkpoint"):
                self.assertNotIn(name, pending)
            code, resumed = self.cli_step()
            self.assertEqual(0, code, resumed)
        after = workspace.load(self.context)
        self.assertEqual(before["generation"] + 2, after["generation"])
        self.assertEqual(before["repositories"], after["repositories"])
        self.assertEqual(["inventory", "second"], list(after["operations"]))
        self.assertEqual(draft_bytes, draft_path.read_bytes())

    def test_current_checkpoint_damage_blocks_author_and_keeps_original_bytes(self):
        self.assertTrue(self.step([self.author()])["complete"])
        checkpoint = self.context / "checkpoint.json"
        saved = checkpoint.read_bytes()
        draft = self.context / "draft.json"
        authored = draft.read_bytes()
        before = workspace.load(self.context)
        operation = {**self.author(), "id": "second", "updates": {"summary": "Second judgment"},
                     "expected_checkpoint_revision": 1}
        requests = self.requests_file([operation])
        checkpoint.write_bytes(b"{")
        with mock.patch.object(workspace, "ordered_step", side_effect=AssertionError("Ran author")):
            code, denied = self.cli_step(requests)
        self.assertEqual(3, code, denied)
        self.assertTrue(denied["context_retained"])
        self.assertEqual(recovery.RETRY_WITH_REQUESTS_FILE, denied["next_action"])
        self.assertIn("Repair or restore checkpoint.json", denied["error"])
        self.assertIsNone(recovery.read_pending(self.context))
        self.assertFalse((self.context / "author-second.json").exists())
        self.assertEqual(b"{", checkpoint.read_bytes())
        self.assertEqual(authored, draft.read_bytes())
        self.assertEqual(before, workspace.load(self.context))
        checkpoint.write_bytes(saved)
        code, resumed = self.cli_step(requests)
        self.assertEqual(0, code, resumed)
        self.assertEqual(2, review_checkpoint.revision(review_checkpoint.load(self.context)))
        self.assertEqual(before["generation"] + 1, workspace.load(self.context)["generation"])

    def test_first_author_marker_denial_requires_original_requests_file(self):
        job_path = flow.job_path(self.root_runtime, self.identifier)
        atomic = coordination.atomic
        operations = [self.author()]
        requests = self.requests_file(operations)
        draft = self.context / "draft.json"
        before = draft.read_bytes()

        def deny_marker(path, value):
            if path == self.context / recovery.NAME:
                raise sharing_error(32)
            return atomic(path, value)

        with mock.patch.object(coordination, "atomic", side_effect=deny_marker):
            code, denied = self.cli_step(requests)
        self.assertEqual(3, code)
        self.assertEqual(recovery.RETRY_WITH_REQUESTS_FILE, denied["next_action"])
        self.assertIn("--requests-file", denied["hint"])
        self.assertEqual(before, draft.read_bytes())
        self.assertIsNone(recovery.read_pending(self.context))
        self.assertEqual(recovery.RETRY_WITH_REQUESTS_FILE, flow.status(
            self.root_runtime, self.identifier, summary=True)["next_action"])
        _, missing = self.cli_step()
        self.assertEqual(recovery.RETRY_WITH_REQUESTS_FILE, missing["next_action"])
        code, unreadable = self.cli_step(self.root / "missing-operations.json")
        self.assertEqual(3, code, unreadable)
        self.assertEqual(recovery.RETRY_WITH_REQUESTS_FILE, unreadable["next_action"])
        self.assertEqual(recovery.RETRY_WITH_REQUESTS_FILE, flow.status(
            self.root_runtime, self.identifier, summary=True)["next_action"])
        code, resumed = self.cli_step(requests)
        self.assertEqual(0, code, resumed)
        job = coordination.read_json(job_path)
        self.assertIsNone(job.get("next_action"))
        self.assertFalse(job["recoverable"])
        self.assertFalse(job["context_retained"])
        self.assertEqual(1, review_checkpoint.revision(review_checkpoint.load(self.context)))

    def test_first_marker_post_replace_error_allows_retry_without_file(self):
        operations = [self.author()]
        requests = self.requests_file(operations)
        marker = self.context / recovery.NAME
        atomic = coordination.atomic

        def report_error_after_author_marker(path, value):
            atomic(path, value)
            if path == marker and value["state"] == "author-in-progress":
                raise sharing_error(32)

        before = workspace.load(self.context)["generation"]
        with mock.patch.object(coordination, "atomic", side_effect=report_error_after_author_marker):
            code, denied = self.cli_step(requests)
        self.assertEqual(3, code)
        self.assertEqual("retry-step", denied["next_action"])
        self.assertEqual(operations, recovery.read_pending(self.context)["batch"])
        self.assertIsNone(review_checkpoint.load(self.context))
        code, resumed = self.cli_step()
        self.assertEqual(0, code, resumed)
        self.assertEqual(1, review_checkpoint.revision(review_checkpoint.load(self.context)))
        self.assertEqual(before + 1, workspace.load(self.context)["generation"])

    def test_earlier_execution_error_wins_live_and_recovered_over_preflight_tail(self):
        before = workspace.load(self.context)["generation"]
        missing = {"id": "broken-read", "op": "read", "repo": self.started["selected_repo"],
                   "source": "head", "path": "app.py"}
        invalid = {"id": "invalid-op", "op": "unsupported"}
        diagnostic_fields = ("code", "error", "next_index", "operation_id", "pointer")
        with mock.patch.object(workspace, "read", side_effect=OSError("Synthetic execution failure")):
            code, live = self.cli_step(self.requests_file([self.inventory(), missing, invalid]))
            self.assertEqual(3, code)
            failure, _ = self.replace_failures([sharing_error(33)] * 5)
            requests = self.requests_file([self.inventory("second"), missing, invalid])
            with failure, mock.patch.object(recovery.time, "sleep"):
                code, blocked = self.cli_step(requests)
            self.assertEqual(3, code)
            self.assertEqual("retry-step", blocked["next_action"])
        code, resumed = self.cli_step()
        self.assertEqual(3, code)
        self.assertEqual([live[key] for key in diagnostic_fields], [resumed[key] for key in diagnostic_fields])
        self.assertEqual(["evidence.operation-failed", "Synthetic execution failure", 1, "broken-read",
                          "/operations/1"], [resumed[key] for key in diagnostic_fields])
        self.assertEqual(before + 2, workspace.load(self.context)["generation"])

    def test_legacy_prepared_records_still_verify_author_snapshot(self):
        failure, _ = self.replace_failures([sharing_error(32)] * 5)
        with failure, mock.patch.object(recovery.time, "sleep"):
            self.assertEqual("retry-step", self.step([self.inventory()])["next_action"])
        pending = recovery.read_pending(self.context)
        del pending["author_state_required"]
        draft_sha, checkpoint = recovery.draft_state(self.context)
        pending.update(previous_draft_sha256=draft_sha, previous_checkpoint=checkpoint,
                       draft_sha256=draft_sha, checkpoint=checkpoint)
        coordination.atomic(self.context / recovery.NAME, pending)
        with mock.patch.object(recovery, "draft_state", wraps=recovery.draft_state) as author_state:
            code, resumed = self.cli_step()
        self.assertEqual(0, code, resumed)
        author_state.assert_called_once_with(self.context)

    def test_reused_author_keeps_mixed_prepared_record_bound_to_snapshot(self):
        author = self.author()
        self.assertTrue(self.step([author])["complete"])
        draft_sha, checkpoint = recovery.draft_state(self.context)
        failure, _ = self.replace_failures([sharing_error(32)] * 5)
        with failure, mock.patch.object(recovery.time, "sleep"):
            self.assertEqual("retry-step", self.step([author, self.inventory()])["next_action"])
        pending = recovery.read_pending(self.context)
        self.assertNotIn("author_state_required", pending)
        self.assertEqual((draft_sha, checkpoint), (pending["draft_sha256"], pending["checkpoint"]))
        code, resumed = self.cli_step()
        self.assertEqual(0, code, resumed)
        self.assertTrue(resumed["operations"][0]["reused"])
        self.assertEqual(checkpoint, review_checkpoint.load(self.context))

    def test_persistent_denial_resumes_two_saved_authors_without_replay(self):
        before = workspace.load(self.context)
        draft = self.context / "draft.json"
        first = self.author()
        candidate = coordination.read_json(draft)
        candidate["summary"] = "First judgment"
        after_first = hashlib.sha256((json.dumps(candidate, separators=(",", ":")) + os.linesep).encode()).hexdigest()
        second = {"id": "second", "op": "author", "updates": {"summary": "Second judgment"},
                  "expected_draft_sha256": after_first, "expected_checkpoint_revision": 1}
        failure, attempts = self.replace_failures([sharing_error(5)] * 5)
        with failure, mock.patch.object(recovery.time, "sleep") as sleep:
            denied = self.step([first, second])
        self.assertEqual("evidence.manifest-write-repair", denied["code"])
        self.assertTrue(denied["context_retained"])
        self.assertEqual([mock.call(.05), mock.call(.1), mock.call(.2), mock.call(.4)], sleep.call_args_list)
        self.assertEqual(5, len(attempts))
        self.assertEqual(before, workspace.load(self.context))
        self.assertEqual(2, review_checkpoint.revision(review_checkpoint.load(self.context)))
        status = flow.status(self.root_runtime, self.identifier, summary=True)
        self.assertEqual("retry-step", status["next_action"])
        self.assertFalse(status["ready_for_finish"])
        self.assertEqual(0, status["checkpoint_revision"])
        self.assertNotIn("batch", status)
        for inspect in (flow.check, flow.finish):
            preview, code = inspect(self.root_runtime, self.identifier)
            self.assertEqual(3, code)
            self.assertEqual("evidence.manifest-write-repair", preview["code"])
        self.assertFalse(self.step([self.inventory()])["complete"])
        incorrect = self.root / "incorrect-operations.json"
        incorrect.write_text("null", encoding="utf-8")
        self.assertFalse(flow.step_file(self.root_runtime, self.identifier, incorrect)["complete"])
        committed = self.step(None)
        self.assertTrue(committed["complete"])
        self.assertEqual(["first", "second"], [row["id"] for row in committed["operations"]])
        self.assertEqual(2, review_checkpoint.revision(review_checkpoint.load(self.context)))
        self.assertEqual("Second judgment", coordination.read_json(draft)["summary"])
        after = workspace.load(self.context)
        self.assertEqual(before["generation"] + 1, after["generation"])
        self.assertEqual(before["repositories"], after["repositories"])
        self.assertIsNone(recovery.read_pending(self.context))

    def test_resume_after_replace_reuses_saved_result_and_keeps_one_generation(self):
        before = workspace.load(self.context)["generation"]
        with mock.patch.object(flow, "finish_manifest_commit", side_effect=SystemExit("Synthetic process loss")):
            with self.assertRaises(SystemExit):
                self.step([self.inventory()])
        self.assertEqual(before + 1, workspace.load(self.context)["generation"])
        pending = recovery.read_pending(self.context)
        self.assertFalse((self.context / pending["staged_manifest"]).exists())
        with mock.patch.object(recovery, "replace_staged", side_effect=AssertionError("Already replaced")):
            resumed = self.step(None)
        self.assertTrue(resumed["complete"])
        self.assertEqual("inventory", resumed["operations"][0]["id"])
        self.assertEqual(before + 1, workspace.load(self.context)["generation"])
        self.assertIsNone(recovery.read_pending(self.context))

    def test_interruption_before_authoring_restarts_the_saved_batch_without_a_file(self):
        original = [self.author()]
        with mock.patch.object(workspace, "ordered_step", side_effect=SystemExit("Synthetic process loss")):
            with self.assertRaises(SystemExit):
                self.step(original)
        pending = recovery.read_pending(self.context)
        self.assertEqual("author-in-progress", pending["state"])
        self.assertEqual(original, pending["batch"])
        marker = self.context / recovery.NAME
        coordination.atomic(marker, {**pending, "batch": [self.inventory()]})
        with self.assertRaisesRegex(ValueError, "Invalid manifest recovery record"):
            recovery.read_pending(self.context)
        coordination.atomic(marker, pending)
        self.assertFalse(self.step([self.inventory()])["complete"])
        result = self.step(None)
        self.assertTrue(result["complete"])
        self.assertNotIn("batch", result)
        self.assertIsNone(recovery.read_pending(self.context))

    def test_interrupted_authoring_without_staged_manifest_preserves_state_for_inspection(self):
        original = [self.author()]
        before = workspace.load(self.context)
        with mock.patch.object(recovery, "stage", side_effect=SystemExit("Synthetic process loss")):
            with self.assertRaises(SystemExit):
                self.step(original)
        self.assertEqual("author-in-progress", recovery.read_pending(self.context)["state"])
        with mock.patch.object(workspace, "ordered_step", side_effect=AssertionError("Author replay")):
            result = self.step(original)
        self.assertEqual("inspect-interrupted-step", result["next_action"])
        self.assertFalse(result["recoverable"])
        self.assertEqual("First judgment", coordination.read_json(self.context / "draft.json")["summary"])
        self.assertEqual(before, workspace.load(self.context))
        self.assertEqual(1, review_checkpoint.revision(review_checkpoint.load(self.context)))

    def test_author_validation_failure_without_writes_allows_correction(self):
        invalid = {**self.author(), "updates": {"next_steps": ["Follow up"]}}
        result = self.step([invalid])
        self.assertIn("summary is required", result["error"])
        self.assertIsNone(recovery.read_pending(self.context))
        self.assertTrue(self.step([self.author()])["complete"])

    def test_author_write_failure_without_a_new_ledger_retains_advanced_draft(self):
        atomic = coordination.atomic

        def allowed_except_checkpoint(path, value):
            if path == self.context / "checkpoint.json":
                raise sharing_error(32)
            return atomic(path, value)

        operations = [self.author()]
        with mock.patch.object(coordination, "atomic", side_effect=allowed_except_checkpoint):
            result = self.step(operations)
        self.assertEqual("evidence.manifest-preparation-interrupted", result["code"])
        self.assertEqual("author-interrupted", recovery.read_pending(self.context)["state"])
        self.assertEqual({}, workspace.load(self.context).get("operations", {}))
        self.assertIsNone(review_checkpoint.load(self.context))
        self.assertEqual("First judgment", coordination.read_json(self.context / "draft.json")["summary"])
        with mock.patch.object(workspace, "ordered_step", side_effect=AssertionError("Author replay")):
            self.assertEqual("inspect-interrupted-step", self.step(operations)["next_action"])

    def test_journal_only_author_resumes_after_repeated_draft_replacement_denial(self):
        first_author = self.author()
        generation = workspace.load(self.context)["generation"]
        draft = self.context / "draft.json"
        before = draft.read_bytes()
        candidate = coordination.read_json(draft)
        candidate["summary"] = first_author["updates"]["summary"]
        first_sha = hashlib.sha256((json.dumps(candidate, separators=(",", ":")) + os.linesep).encode()).hexdigest()
        second_author = {**first_author, "id": "second", "updates": {"summary": "Second judgment"},
                         "expected_draft_sha256": first_sha, "expected_checkpoint_revision": 1}
        operations = [first_author, second_author]
        journal = self.context / "author-first.json"
        with self.deny_author_draft_replace():
            first = self.step(operations)
            pending = recovery.read_pending(self.context)
            saved_journal = journal.read_bytes()
            second = self.step(None)
        self.assertEqual("retry-step", first["next_action"])
        self.assertEqual("retry-step", second["next_action"])
        self.assertEqual(pending, recovery.read_pending(self.context))
        self.assertEqual(saved_journal, journal.read_bytes())
        self.assertFalse((self.context / "author-second.json").exists())
        self.assertEqual(before, draft.read_bytes())
        self.assertIsNone(review_checkpoint.load(self.context))
        self.assertFalse(self.step([self.inventory()])["complete"])
        resumed = self.step(None)
        self.assertTrue(resumed["complete"])
        self.assertEqual(["first", "second"], [row["id"] for row in resumed["operations"]])
        self.assertEqual("Second judgment", coordination.read_json(draft)["summary"])
        self.assertEqual(2, review_checkpoint.revision(review_checkpoint.load(self.context)))
        self.assertEqual(generation + 1, workspace.load(self.context)["generation"])
        self.assertIsNone(recovery.read_pending(self.context))

    def test_journal_only_author_rejects_a_coherently_altered_draft(self):
        operation = self.author()
        with self.deny_author_draft_replace():
            self.assertEqual("retry-step", self.step([operation])["next_action"])
        journal = self.context / "author-first.json"
        saved = coordination.read_json(journal)
        saved["draft"]["summary"] = "An altered judgment"
        altered = (json.dumps(saved["draft"], separators=(",", ":")) + os.linesep).encode()
        saved["after"] = hashlib.sha256(altered).hexdigest()
        saved["result"]["draft_sha256"] = saved["after"]
        saved["result"]["checkpoint"]["draft_sha256"] = saved["after"]
        coordination.atomic(journal, saved)
        with mock.patch.object(workspace, "ordered_step", side_effect=AssertionError("Unverified author replay")):
            self.assertEqual("inspect-interrupted-step", self.step(None)["next_action"])
        self.assertEqual(operation["expected_draft_sha256"], coordination.file_digest(self.context / "draft.json"))
        self.assertIsNone(review_checkpoint.load(self.context))

    def test_journal_only_author_rejects_altered_checkpoint_navigation(self):
        operation = {**self.author(), "updates": {}, "checkpoint": {"next_action": "Inspect source"}}
        atomic = coordination.atomic

        def denied(path, value):
            if path == self.context / "checkpoint.json":
                raise sharing_error(5)
            return atomic(path, value)

        with mock.patch.object(coordination, "atomic", side_effect=denied):
            self.assertEqual("retry-step", self.step([operation])["next_action"])
        journal = self.context / "author-first.json"
        saved = coordination.read_json(journal)
        saved["result"]["checkpoint"]["next_action"] = "Different navigation"
        coordination.atomic(journal, saved)
        with mock.patch.object(workspace, "ordered_step", side_effect=AssertionError("Unverified checkpoint replay")):
            self.assertEqual("inspect-interrupted-step", self.step(None)["next_action"])
        self.assertEqual(operation["expected_draft_sha256"], coordination.file_digest(self.context / "draft.json"))
        self.assertIsNone(review_checkpoint.load(self.context))

    def test_committed_prefix_preserves_author_suffix_retry_through_manifest_recovery(self):
        author = self.author()
        generation = workspace.load(self.context)["generation"]
        with self.deny_author_draft_replace(deny_manifest=True):
            denied = self.step([self.inventory(), author])
        self.assertEqual("retry-step", denied["next_action"])
        self.assertEqual("prepared", recovery.read_pending(self.context)["state"])
        prefix = self.step(None)
        self.assertEqual(1, prefix["next_index"])
        self.assertEqual("evidence.operation-failed", prefix["code"])
        self.assertEqual(["inventory"], list(workspace.load(self.context)["operations"]))
        self.assertIsNone(recovery.read_pending(self.context))
        self.assertTrue(self.step([author])["complete"])
        self.assertEqual(1, review_checkpoint.revision(review_checkpoint.load(self.context)))
        self.assertEqual(generation + 2, workspace.load(self.context)["generation"])

    def test_recovery_preserves_valid_prefix_and_editable_tail_diagnostic(self):
        operations = [self.inventory(), {"id": "bad", "op": "unsupported", "note": "\ud800"}]
        failure, _ = self.replace_failures([sharing_error(33)] * 5)
        with failure, mock.patch.object(recovery.time, "sleep"):
            self.assertTrue(self.step(operations)["recoverable"])
        resumed = self.step(operations)
        self.assertEqual(1, resumed["next_index"])
        self.assertEqual("authoring.unsupported-operation", resumed["code"])
        self.assertEqual(["inventory"], list(workspace.load(self.context)["operations"]))
        self.assertIsNone(recovery.read_pending(self.context))

    def test_staged_manifest_tamper_is_terminal_on_retry(self):
        failure, _ = self.replace_failures([sharing_error(5)] * 5)
        with failure, mock.patch.object(recovery.time, "sleep"):
            self.assertTrue(self.step([self.inventory()])["recoverable"])
        pending = recovery.read_pending(self.context)
        staged = self.context / pending["staged_manifest"]
        with staged.open("ab") as stream:
            stream.write(b" ")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = flow.main(["step", "--runtime-root", str(self.root_runtime), "--request-id", self.identifier])
        self.assertEqual(2, code)
        self.assertTrue(json.loads(stderr.getvalue())["terminal"])
        self.assertFalse(self.context.exists())

    def test_cancel_retains_cleanup_state_until_blocked_files_are_released(self):
        from tests.fixtures import api

        failure, _ = self.replace_failures([sharing_error(5)] * 5)
        with failure, mock.patch.object(recovery.time, "sleep"):
            self.assertTrue(self.step([self.inventory()])["recoverable"])
        with mock.patch.object(review_session, "cleanup_session", side_effect=sharing_error(5)):
            cancelled = flow.cancel(self.root_runtime, self.identifier)
            with self.assertRaises(PermissionError):
                flow.cancel(self.root_runtime, self.identifier)
        self.assertEqual("cancelled", cancelled["state"])
        self.assertTrue(cancelled["cleanup_pending"])
        self.assertEqual("retry-cancel", cancelled["next_action"])

        def runtime_files():
            return {path: (path.stat().st_mtime_ns, coordination.file_digest(path))
                    for path in self.root_runtime.rglob("*") if path.is_file()}

        before = runtime_files()
        with mock.patch.object(api, "run_git", side_effect=AssertionError("status ran Git")), \
                mock.patch.object(review_session, "cleanup_session", side_effect=AssertionError("status cleaned files")):
            for summary in (False, True):
                status = flow.status(self.root_runtime, self.identifier, summary=summary)
                self.assertEqual(self.identifier, status["request_id"])
                self.assertEqual("retry-cancel", status["next_action"])
                self.assertTrue(status["cleanup_pending"])
        self.assertEqual(before, runtime_files())
        cleared = flow.cancel(self.root_runtime, self.identifier)
        self.assertEqual("cancelled", cleared["state"])
        self.assertFalse(cleared["cleanup_pending"])
        self.assertEqual("stop", cleared["next_action"])
        self.assertEqual("stop", flow.status(self.root_runtime, self.identifier, summary=True)["next_action"])
        self.assertFalse(self.context.exists())

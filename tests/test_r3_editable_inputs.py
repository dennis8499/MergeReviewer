"""Editable input errors retain one request; damaged owned evidence stays fatal."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
from unittest import mock

from tests.fixtures import ReviewFixture, api, review_session, workspace, workspace_report
import review_coordinator as coordination
import review_flow as flow


class EditableInputRecoveryTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.commit()
        self.environment = mock.patch.dict(os.environ, {
            "GITLAB_WORKSPACE_RUNTIME_DIR": str(self.root / "owned-network")})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.started = flow.start(flow.parser().parse_args([
            "start", "--workspace", str(self.root), "--project", str(self.repo),
            "--base", "main", "--head", "feature"]))
        self.runtime = Path(self.started["runtime_root"])
        self.identifier = self.started["request_id"]
        self.context = Path(self.started["context_dir"])
        self.addCleanup(review_session.cleanup_session, self.context)

    def cli_step(self, requested):
        values = ["step", "--runtime-root", str(self.runtime), "--request-id", self.identifier]
        if requested is not None:
            values.extend(["--requests-file", str(requested)])
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = flow.main(values)
        return code, json.loads(output.getvalue()) if output.getvalue() else None, errors.getvalue()

    def assert_repairable(self, requested):
        code, result, errors = self.cli_step(requested)
        self.assertEqual(3, code, errors)
        self.assertEqual("", errors)
        self.assertFalse(result["terminal"])
        self.assertTrue(result["recoverable"])
        self.assertTrue(result["context_retained"])
        self.assertFalse(result["complete"])
        self.assertEqual("authoring.invalid-requests-file", result["code"])
        self.assertEqual("/requests_file", result["pointer"])
        saved = coordination.read_json(flow.job_path(self.runtime, self.identifier))
        self.assertEqual(("reviewing", "input-repair"), (saved["state"], saved["phase"]))
        self.assertTrue(self.context.is_dir())
        self.assertEqual(self.context, review_session.validate_session(self.context))

    def test_missing_operations_can_be_written_and_retried_without_losing_prefix(self):
        operation = {"id": "read-once", "op": "read", "repo": self.started["selected_repo"], "path": "app.py"}
        self.assertTrue(flow.step(self.runtime, self.identifier, [operation])["complete"])
        before = workspace.load(self.context)
        requested = self.context / "操作請求.json"
        self.assert_repairable(requested)
        after = workspace.load(self.context)
        self.assertEqual(before["operations"], after["operations"])
        self.assertEqual(before["generation"], after["generation"])
        coordination.atomic(requested, [operation])
        code, result, errors = self.cli_step(requested)
        self.assertEqual(0, code, errors)
        self.assertTrue(result["complete"])
        self.assertTrue(result["operations"][0]["reused"])
        self.assertEqual(before["operations"], workspace.load(self.context)["operations"])
        self.assertEqual("review", flow.status(self.runtime, self.identifier)["phase"])

    def test_missing_argument_malformed_json_and_invalid_encoding_are_repairable(self):
        requested = self.context / "editable-operations.json"
        self.assert_repairable(None)
        requested.write_text('{"unfinished":', encoding="utf-8")
        self.assert_repairable(requested)
        requested.write_bytes(b"\xff\xfe\x81")
        self.assert_repairable(requested)
        self.assertEqual({}, workspace.load(self.context).get("operations", {}))

    def test_unreadable_operations_are_repairable(self):
        requested = self.context / "operations.json"
        original = coordination.read_json

        def read(path):
            if Path(path) == requested:
                raise PermissionError("synthetic editable-input ACL")
            return original(path)

        with mock.patch.object(coordination, "read_json", side_effect=read):
            self.assert_repairable(requested)

    def damage_pack(self):
        manifest = workspace.load(self.context)
        seed = workspace.repository(manifest, self.started["selected_repo"])
        packs = list(workspace.evidence_repository(self.context, seed).joinpath("objects/pack").glob("*.pack"))
        self.assertTrue(packs)
        for pack in packs:
            pack.chmod(0o666)
            pack.unlink()

    def assert_terminal(self, action):
        with self.assertRaises((ValueError, OSError, RuntimeError)):
            action()
        saved = coordination.read_json(flow.job_path(self.runtime, self.identifier))
        self.assertEqual(("failed", "terminal"), (saved["state"], saved["phase"]))
        self.assertFalse(self.context.exists())
        # Output permission preflight may create the empty destination directory.
        self.assertEqual([], list((self.repo / "review-reports").glob("*.md")))

    def test_missing_operations_do_not_hide_damaged_owned_pack(self):
        self.damage_pack()
        self.assert_terminal(lambda: flow.step_file(self.runtime, self.identifier, self.context / "missing.json"))

    def test_malformed_owned_manifest_is_not_an_editable_input_error(self):
        (self.context / "manifest.json").write_text('{"broken":', encoding="utf-8")
        self.assert_terminal(lambda: flow.step_file(self.runtime, self.identifier, self.context / "missing.json"))

    def authored_draft(self):
        manifest = workspace.load(self.context)
        draft = self.draft(self.context, manifest)
        draft["report_layout"] = "engineering-review/v1"
        coordination.atomic(self.context / "draft.json", draft)
        return draft

    def test_missing_draft_retains_context_then_restored_draft_publishes_once(self):
        draft = self.authored_draft()
        (self.context / "draft.json").unlink()
        with mock.patch.object(workspace_report, "validate", wraps=workspace_report.validate) as validate:
            result, code = flow.finish(self.runtime, self.identifier)
            self.assertEqual(3, code)
            self.assertTrue(result["context_retained"])
            self.assertEqual(0, validate.call_count)
            self.assertEqual("draft-repair", flow.status(self.runtime, self.identifier)["phase"])
            coordination.atomic(self.context / "draft.json", draft)
            result, code = flow.finish(self.runtime, self.identifier)
            self.assertEqual(0, code)
            self.assertEqual(1, validate.call_count)
        self.assertTrue(Path(result["report_path"]).is_file())
        self.assertFalse(self.context.exists())

    def test_unreadable_draft_is_repairable_but_owned_load_errors_are_not_swallowed(self):
        self.authored_draft()
        original = coordination.read_json

        def read(path):
            if Path(path) == self.context / "draft.json":
                raise PermissionError("synthetic draft ACL")
            return original(path)

        with mock.patch.object(coordination, "read_json", side_effect=read):
            result, code = flow.finish(self.runtime, self.identifier)
        self.assertEqual(3, code)
        self.assertTrue(result["context_retained"])
        self.assertIn("synthetic draft ACL", result["errors"][0])
        self.damage_pack()
        (self.context / "draft.json").unlink()
        self.assert_terminal(lambda: flow.finish(self.runtime, self.identifier))


class OwnImpactFacadeTests(ReviewFixture):
    def test_zero_git_check_catches_source_mismatch_before_one_final_validation(self):
        self.make_repo("consumer")
        self.commit()
        with mock.patch.dict(os.environ, {"GITLAB_WORKSPACE_RUNTIME_DIR": str(self.root / "owned-network")}):
            started = flow.start(flow.parser().parse_args([
                "start", "--workspace", str(self.root), "--project", str(self.repo),
                "--base", "main", "--head", "feature"]))
        root, identifier = Path(started["runtime_root"]), started["request_id"]
        context = Path(started["context_dir"])
        self.addCleanup(review_session.cleanup_session, context)
        manifest = workspace.load(context)
        draft = self.draft(context, manifest)
        draft["report_layout"] = "engineering-review/v1"
        original_source = draft["impact_coverage"][0]["source"]
        draft["impact_coverage"][0]["source"] = "merge-base"
        coordination.atomic(context / "draft.json", draft)
        with mock.patch.object(api, "run_git", side_effect=AssertionError("check ran Git")), \
             mock.patch.object(api, "run_git_capture", side_effect=AssertionError("check ran Git")):
            result, code = flow.check(root, identifier)
        self.assertEqual(3, code)
        self.assertFalse(result["evidence_verified"])
        self.assertEqual(["/impact_coverage/0/evidence"], [row["pointer"] for row in result["errors"]
                         if row["code"] == "authoring.impact-evidence-must-cover-its-own-fixed-path-source"])
        with mock.patch.object(workspace_report, "validate", wraps=workspace_report.validate) as validate:
            self.assertEqual(3, flow.finish(root, identifier)[1])
            self.assertEqual(0, validate.call_count)
            draft["impact_coverage"][0]["source"] = original_source
            coordination.atomic(context / "draft.json", draft)
            published, code = flow.finish(root, identifier)
            self.assertEqual(0, code)
            self.assertEqual(1, validate.call_count)
        self.assertTrue(Path(published["report_path"]).is_file())
        self.assertFalse(context.exists())

"""Publication success must retain the validated semantic review state after cleanup."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from unittest import mock

from tests.fixtures import ReviewFixture, api, workspace, workspace_report, review_session
from tests.support import run_git as git
import review_coordinator as coordination
import review_flow as flow


class PublishedOutcomeTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.commit()
        environment = mock.patch.dict(os.environ, {"GITLAB_WORKSPACE_RUNTIME_DIR": str(self.root / "network")})
        environment.start()
        self.addCleanup(environment.stop)

    def start(self):
        args = flow.parser().parse_args(["start", "--workspace", str(self.root), "--project", str(self.repo),
                                        "--base", "main", "--head", "feature"])
        result = flow.start(args)
        context = Path(result["context_dir"])
        self.addCleanup(review_session.cleanup_session, context)
        manifest = workspace.load(context)
        draft = self.draft(context, manifest)
        draft["report_layout"] = "engineering-review/v1"
        coordination.atomic(context / "draft.json", draft)
        return Path(result["runtime_root"]), result["request_id"], context, manifest, draft

    def save(self, context, draft):
        coordination.atomic(context / "draft.json", draft)

    def saved_status(self, root, identifier):
        with mock.patch.object(api, "run_git", side_effect=AssertionError("status/check ran Git")), \
             mock.patch.object(api, "run_git_capture", side_effect=AssertionError("status/check ran Git")), \
             mock.patch.object(workspace_report, "validate", side_effect=AssertionError("duplicate validation")):
            status = flow.status(root, identifier)
            checked, code = flow.check(root, identifier)
        self.assertFalse(checked["evidence_verified"])
        self.assertFalse(checked["ready_for_finish"], "a published request has no pending finish operation")
        self.assertTrue(checked["publication_complete"])
        self.assertEqual(0, checked["git_commands"])
        self.assertEqual(status["review_outcome"], checked["review_outcome"])
        return status, checked, code

    def test_complete_outcome_is_atomic_before_link_and_validated_only_once(self):
        root, identifier, context, _, _ = self.start()
        original_link = workspace_report.os.link
        observed = []
        def link_after_saved(source, destination):
            job = coordination.read_json(flow.job_path(root, identifier))
            self.assertEqual(job["review_outcome"], job["report_receipt"]["review_outcome"])
            self.assertTrue(job["review_outcome"]["review_complete"])
            self.assertFalse(Path(destination).exists())
            observed.append(copy.deepcopy(job["review_outcome"]))
            return original_link(source, destination)
        with mock.patch.object(workspace_report.os, "link", side_effect=link_after_saved), \
             mock.patch.object(workspace_report, "validate", wraps=workspace_report.validate) as validate:
            published, code = flow.finish(root, identifier)
        self.assertEqual(0, code)
        self.assertEqual(1, validate.call_count)
        self.assertEqual(1, len(observed))
        self.assertFalse(context.exists())
        status, checked, code = self.saved_status(root, identifier)
        self.assertEqual(0, code)
        self.assertTrue(status["review_complete"])
        self.assertEqual([], checked["gaps"])
        self.assertEqual(observed[0], published["review_outcome"])

    def test_missing_peer_head_gap_survives_context_cleanup(self):
        git(self.root, "init", "-b", "main", str(self.root / "billing-consumer"))
        root, identifier, context, _, _ = self.start()
        published, code = flow.finish(root, identifier)
        self.assertEqual(0, code, "an honestly incomplete report may be published")
        self.assertFalse(context.exists())
        self.assertFalse(published["review_outcome"]["review_complete"])
        status, checked, code = self.saved_status(root, identifier)
        self.assertEqual(3, code)
        self.assertFalse(status["review_complete"])
        self.assertEqual("審查未完成", status["review_status"])
        self.assertTrue(checked["gaps"])
        self.assertTrue(status["remaining"]["repositories"])
        self.assertIn("incomplete", status["review_outcome"]["extension_status"])

    def test_pending_only_and_manual_edit_preserve_initial_semantics(self):
        root, identifier, context, _, draft = self.start()
        change = {key: draft["coverage"][0][key] for key in ("repo", "source", "path")}
        draft["pending_confirmations"] = [{"id": "Q-001", "title": "核對必要消費端部署版本",
            "reason": "既有註冊消費端版本尚待負責人核對。", "missing_evidence": "部署版本紀錄。",
            "verification": "由負責人核對固定契約相容性。", "owner": "QA",
            "related_changes": [change], "evidence": []}]
        self.save(context, draft)
        published, _ = flow.finish(root, identifier)
        initial = copy.deepcopy(published["review_outcome"])
        report = Path(published["report_path"])
        report.write_text(report.read_text(encoding="utf-8").replace("- [ ] Q-001", "- [x] Q-001"),
                          encoding="utf-8", newline="\n")
        status, checked, code = self.saved_status(root, identifier)
        self.assertEqual(3, code)
        self.assertEqual("待人工確認", status["review_status"])
        self.assertEqual(1, status["review_outcome"]["pending_confirmation_count"])
        self.assertEqual(initial, status["review_outcome"])
        self.assertTrue(status["publication"]["manual_record_changed"])
        self.assertTrue(checked["gaps"])
        with mock.patch.object(workspace_report, "validate", side_effect=AssertionError("duplicate validation")):
            repeated, code = flow.finish(root, identifier)
        self.assertEqual(0, code, "finish retries verify publication, rather than redoing manual confirmation")
        self.assertEqual(initial, repeated["review_outcome"])

    def test_unreviewed_coverage_keeps_structured_gap_and_redacts_saved_summary(self):
        root, identifier, context, _, draft = self.start()
        secret = "synthetic-outcome-secret"
        key_body = "synthetic-private-body"
        draft["coverage"][0].update(status="not-reviewed", evidence=[],
            reason=f"必要來源尚缺 password={secret}\n-----BEGIN PRIVATE KEY-----\n{key_body}\n-----END PRIVATE KEY-----")
        self.save(context, draft)
        published, _ = flow.finish(root, identifier)
        status, checked, code = self.saved_status(root, identifier)
        self.assertEqual(3, code)
        self.assertTrue(status["remaining"]["changes"])
        self.assertTrue(checked["gaps"])
        serialized = json.dumps(coordination.read_json(flow.job_path(root, identifier)), ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertNotIn(key_body, serialized)
        self.assertNotIn("evidence", json.dumps(published["review_outcome"]["remaining"]))

    def test_unresolved_impact_and_boundary_remain_structured_after_publication(self):
        self.make_repo("consumer")
        root, identifier, context, manifest, draft = self.start()
        contract = manifest["changed_contracts"][0]
        contract["search_scope"] = "repo-local"
        manifest["searches"] = []
        for disposition in draft["repository_coverage"]:
            disposition["search_ids"] = []
        workspace.save(context, manifest)
        draft["impact_coverage"][0].update(status="not-reviewed", evidence=[], reason="必要輸出映射尚未查清。")
        draft["boundary_reviews"] = [{"change_id": manifest["boundary_changes"][0]["id"], "outcome": "unresolved",
            "reason": "尚缺 enclosing operation 輸出證據。", "evidence": [], "search_ids": [], "boundary_contract_ids": []}]
        self.save(context, draft)
        flow.finish(root, identifier)
        status, checked, code = self.saved_status(root, identifier)
        self.assertEqual(3, code)
        self.assertTrue(status["remaining"]["impacts"])
        self.assertTrue(status["remaining"]["boundaries"])
        self.assertIn("必要輸出映射", "\n".join(checked["gaps"]))
        self.assertIn("enclosing operation", "\n".join(checked["gaps"]))

    def test_cleanup_retry_keeps_incomplete_outcome_without_revalidation(self):
        root, identifier, context, _, draft = self.start()
        draft["coverage"][0].update(status="not-reviewed", evidence=[], reason="必要來源尚未查清。")
        self.save(context, draft)
        with mock.patch.object(review_session, "cleanup_session", side_effect=OSError("synthetic cleanup")):
            first, _ = flow.finish(root, identifier)
        self.assertTrue(first["cleanup_pending"])
        with mock.patch.object(workspace_report, "validate", side_effect=AssertionError("duplicate validation")):
            repeated, code = flow.finish(root, identifier)
        self.assertEqual(0, code)
        self.assertFalse(context.exists())
        self.assertEqual(first["review_outcome"], repeated["review_outcome"])
        self.assertFalse(repeated["review_complete"])

    def test_post_link_completion_interruption_recovers_same_saved_outcome(self):
        root, identifier, context, _, draft = self.start()
        draft["coverage"][0].update(status="not-reviewed", evidence=[], reason="必要來源尚未查清。")
        self.save(context, draft)
        original_update = flow.update
        def interrupted(*args, **kwargs):
            if kwargs.get("state") == "published":
                raise OSError("synthetic interrupted completion")
            return original_update(*args, **kwargs)
        with mock.patch.object(flow, "update", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "interrupted completion"):
                flow.finish(root, identifier)
        staged = coordination.read_json(flow.job_path(root, identifier))["review_outcome"]
        self.assertFalse(staged["review_complete"])
        with mock.patch.object(workspace_report, "validate", side_effect=AssertionError("duplicate validation")):
            recovered, code = flow.finish(root, identifier)
        self.assertEqual(0, code)
        self.assertFalse(context.exists())
        self.assertEqual(staged, recovered["review_outcome"])
        self.assertEqual(1, len(list((self.repo / "review-reports").glob("*.md"))))

    def test_legacy_missing_outcome_is_unknown_not_complete(self):
        root, identifier, _, _, _ = self.start()
        flow.finish(root, identifier)
        job = coordination.read_json(flow.job_path(root, identifier))
        job.pop("review_outcome")
        job["report_receipt"].pop("review_outcome")
        coordination.atomic(flow.job_path(root, identifier), job)
        status, checked, code = self.saved_status(root, identifier)
        self.assertEqual(3, code)
        self.assertIsNone(status["review_complete"])
        self.assertFalse(status["review_outcome"]["outcome_available"])
        self.assertEqual("unknown", status["review_status"])
        self.assertTrue(checked["gaps"])
        job.pop("report_receipt")
        coordination.atomic(flow.job_path(root, identifier), job)
        status, checked, code = self.saved_status(root, identifier)
        self.assertEqual(3, code)
        self.assertIsNone(status["review_complete"])
        self.assertTrue(checked["gaps"])

    def test_staged_receipt_can_supply_saved_outcome_after_missing_job_projection(self):
        root, identifier, _, _, _ = self.start()
        published, _ = flow.finish(root, identifier)
        job = coordination.read_json(flow.job_path(root, identifier))
        job.pop("review_outcome")
        coordination.atomic(flow.job_path(root, identifier), job)
        status, _, code = self.saved_status(root, identifier)
        self.assertEqual(0, code)
        self.assertEqual(published["review_outcome"], status["review_outcome"])

    def test_changed_saved_semantic_outcome_is_rejected(self):
        root, identifier, _, _, _ = self.start()
        flow.finish(root, identifier)
        job = coordination.read_json(flow.job_path(root, identifier))
        job["review_outcome"]["review_complete"] = False
        coordination.atomic(flow.job_path(root, identifier), job)
        with self.assertRaisesRegex(ValueError, "differs from its staged receipt"):
            flow.status(root, identifier)

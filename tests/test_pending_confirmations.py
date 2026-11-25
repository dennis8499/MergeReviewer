from __future__ import annotations

import copy
from unittest import mock

from tests.fixtures import ReviewFixture, api, workspace_report
import review_diagnostics


class PendingConfirmationTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.commit()

    def pending(self, draft):
        change = {key: draft["coverage"][0][key] for key in ("repo", "source", "path")}
        return {"id": "Q-001", "title": "確認外部消費端是否支援新格式",
                "reason": "工作區沒有部署中的外部消費端，無法驗證相容性。",
                "missing_evidence": "外部消費端目前版本與相容性測試紀錄。",
                "verification": "由服務負責人核對版本並提供契約測試結果。",
                "owner": "QA", "related_changes": [change], "evidence": []}

    def test_pending_items_do_not_become_confirmed_findings_or_unconditional_pass(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        draft["pending_confirmations"] = [self.pending(draft)]
        original = copy.deepcopy(draft)
        result = workspace_report.validate(draft, manifest, context)
        self.assertEqual(original, draft)
        self.assertFalse(result["review_complete"])
        self.assertEqual("待人工確認", result["review_status"])
        self.assertEqual("需人工確認", result["recommendation"])
        self.assertEqual(1, result["pending_confirmation_count"])
        self.assertEqual(0, sum(result["priority_counts"].values()))
        text = workspace_report.render(result, manifest)
        self.assertIn("### Q-001", text)
        self.assertIn("- [ ] Q-001 已完成核對", text)
        self.assertIn("核對人：", text)
        self.assertIn("日期：", text)
        self.assertIn("確認結果與證據：", text)
        self.assertEqual(1, text.count("<!-- merge-confirmations:start -->"))
        self.assertEqual(1, text.count("<!-- merge-confirmations:end -->"))
        self.assertNotIn("可以合併", text)
        expanded, audit = text.split("<details>", 1)
        self.assertIn("待人工確認：1 項。", expanded)
        self.assertIn("<!-- merge-confirmations:start -->", expanded)
        self.assertIn("<!-- merge-confirmations:end -->", expanded)
        self.assertNotIn("merge-confirmations:", audit)
        self.assertLess(text.index("<!-- merge-confirmations:end -->"), text.index("## 下一步"))

    def test_confirmed_findings_keep_priority_and_evidence_when_pending_is_present(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        draft["findings"] = [self.finding(draft, "P0")]
        draft["pending_confirmations"] = [self.pending(draft)]
        result = workspace_report.validate(draft, manifest, context)
        self.assertEqual("暫緩合併", result["recommendation"])
        self.assertEqual(1, result["priority_counts"]["P0"])
        self.assertEqual(1, result["pending_confirmation_count"])
        text = workspace_report.render(result, manifest)
        self.assertIn("### F-001｜[P0]", text)
        self.assertIn("contract = 'new'", text)
        self.assertIn("### Q-001", text)

    def test_omitted_or_empty_pending_array_remains_compatible(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        omitted = workspace_report.validate(draft, manifest, context)
        draft["pending_confirmations"] = []
        explicit = workspace_report.validate(draft, manifest, context)
        self.assertEqual(omitted, explicit)
        self.assertTrue(explicit["review_complete"])
        self.assertNotIn("### 待人工確認", workspace_report.render(explicit, manifest))
        self.assertNotIn("merge-confirmations:start", workspace_report.render(explicit, manifest))

    def test_incomplete_coverage_is_not_hidden_by_pending_confirmations(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        draft["pending_confirmations"] = [self.pending(draft)]
        draft["coverage"][0].update(status="not-reviewed", reason="尚未查清必要內容。", evidence=[])
        result = workspace_report.validate(draft, manifest, context)
        self.assertEqual("審查未完成", result["review_status"])
        self.assertEqual("需補做審查", result["recommendation"])

    def test_malformed_pending_items_and_fabricated_evidence_are_rejected(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        item = self.pending(draft)
        variants = [None, {}, {**item, "id": "Q-002"}, {**item, "owner": "unknown"},
                    {**item, "title": " "}, {**item, "verification": []},
                    {**item, "related_changes": []}, {**item, "related_changes": [{"repo": "missing", "source": "head", "path": "app.py"}]},
                    {**item, "related_changes": [{"repo": [], "source": "head", "path": "app.py"}]},
                    {**item, "confirmed": True}, {**item, "priority": "P1"},
                    {**item, "evidence": [{**draft["coverage"][0]["evidence"][0], "ref": "0" * 40}]},
                    {**item, "evidence": [{**draft["coverage"][0]["evidence"][0], "line_end": 9999}]}]
        for variant in variants:
            bad = {**draft, "pending_confirmations": [variant]}
            with self.subTest(variant=variant), self.assertRaises(ValueError):
                workspace_report.validate(bad, manifest, context)
        duplicate = {**item, "id": "Q-002"}
        with self.assertRaisesRegex(ValueError, "duplicate pending"):
            workspace_report.validate({**draft, "pending_confirmations": [item, duplicate]}, manifest, context)

    def test_partial_valid_evidence_is_preserved_without_claiming_verification(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        item = self.pending(draft)
        item["evidence"] = copy.deepcopy(draft["coverage"][0]["evidence"])
        draft["pending_confirmations"] = [item]
        result = workspace_report.validate(draft, manifest, context)
        self.assertEqual(item["evidence"], result["pending_confirmations"][0]["evidence"])
        self.assertIn(item["evidence"][0]["ref"], workspace_report.render(result, manifest))
        self.assertFalse(result["review_complete"])

    def test_pending_diagnostics_identify_invalid_fields_without_git(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        draft["report_layout"] = "engineering-review/v1"
        item = self.pending(draft)
        cases = [
            ("wrong_prefix", [{**item, "id": "PC-001"}], "/pending_confirmations/0/id", "expected Q-001"),
            ("missing_title", [{**item, "title": ""}], "/pending_confirmations/0/title", "needs title"),
            ("invalid_owner", [{**item, "owner": []}], "/pending_confirmations/0/owner", "valid owner"),
            ("unbound_change", [{**item, "related_changes": [{"repo": "missing", "source": "head", "path": "app.py"}]}],
             "/pending_confirmations/0/related_changes/0", "captured selected changes"),
            ("repeated_change", [{**item, "related_changes": item["related_changes"] * 2}],
             "/pending_confirmations/0/related_changes/1", "duplicate pending related change"),
            ("repeated_question", [item, {**item, "id": "Q-002"}], "/pending_confirmations/1", "duplicate pending confirmation"),
            ("manual_edit_fields", [{**item, "confirmed": True}], "/pending_confirmations/0", "unknown pending confirmation field"),
        ]
        with mock.patch.object(api, "run_git", side_effect=AssertionError("diagnostics ran Git")):
            for name, pending, pointer, message in cases:
                with self.subTest(case=name):
                    diagnosed = review_diagnostics.inspect({**draft, "pending_confirmations": pending}, manifest)
                    error = next(row for row in diagnosed["errors"] if row["pointer"] == pointer)
                    self.assertIn(message, error["message"])
                    self.assertEqual("repair-draft", diagnosed["publication_readiness"])
                    self.assertEqual(0, diagnosed["git_commands"])

    def test_valid_pending_allows_publication_while_coverage_gaps_keep_review_incomplete(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        draft["report_layout"] = "engineering-review/v1"
        draft["pending_confirmations"] = [self.pending(draft)]
        pending = review_diagnostics.inspect(draft, manifest)
        self.assertEqual([], pending["errors"])
        self.assertEqual("ready", pending["publication_readiness"])
        self.assertFalse(pending["ready_for_finish"])
        self.assertEqual("待人工確認", pending["expected_outcome"]["review_status"])
        self.assertFalse(pending["expected_outcome"]["review_complete"])
        draft["coverage"][0].update(status="not-reviewed", reason="必要程式路徑仍待核對。", evidence=[])
        gap = review_diagnostics.inspect(draft, manifest)
        self.assertEqual("incomplete", gap["publication_readiness"])
        self.assertEqual("審查未完成", gap["expected_outcome"]["review_status"])

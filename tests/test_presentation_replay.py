"""Presentation comparison rejects omitted facts without enforcing old whitespace."""
import copy
import unittest

from tests.test_readable_report import readable_fixture
from tests.fixtures import workspace_report
from tests.replay_log_workflow import presentation_signature, expanded_source_bytes, worker


class PresentationReplayTests(unittest.TestCase):
    def test_collapsed_syntax_highlighted_and_legacy_reports_have_equal_factual_signatures(self):
        result, manifest = readable_fixture()
        report = workspace_report.render(result, manifest)
        old = copy.deepcopy(result)
        old.pop("report_layout")
        legacy = workspace_report.render(old, manifest)
        kwargs = {"repo_names": {"repo-000": "Orders"},
                  "excerpts": {"F-001": ["total = price * quantity", "total = price + quantity", "return total"]}}
        self.assertEqual(presentation_signature(result["findings"], legacy, **kwargs),
                         presentation_signature(result["findings"], report, **kwargs))
        self.assertLess(expanded_source_bytes(report), len(report.encode("utf-8")))

    def test_presentation_mode_rejects_changed_priority_missing_facts_and_collapsed_evidence(self):
        result, manifest = readable_fixture()
        report = workspace_report.render(result, manifest)
        changes = [
            ("priority", report.replace("### F-001｜[P1]", "### F-001｜[P2]")),
            ("expected", report.replace("總額應為 30。", "總額應為 13。")),
            ("scope", report.replace("影響非零數量的訂單。", "")),
            ("reproduction", report.replace("依程式推導，未實際重現", "已實際重現")),
            ("evidence", report.replace("Orders/caller.py:3–3", "Orders/another.py:3–3")),
            ("excerpt", report.replace("return total", "return other")),
        ]
        for name, changed in changes:
            with self.subTest(case=name), self.assertRaises(AssertionError):
                presentation_signature(result["findings"], changed, repo_names={"repo-000": "Orders"},
                                       excerpts={"F-001": ["return total"]})

    def test_unknown_comparison_mode_is_rejected_before_creating_any_output(self):
        with self.assertRaisesRegex(ValueError, "unsupported comparison mode"):
            worker(None, None, None, comparison_mode="ignore-content")

    def test_presentation_mode_rejects_changed_decision_counts_completeness_and_required_gaps(self):
        result, manifest = readable_fixture()
        result["review_complete"] = False
        result["limitations"] = ["固定消費端缺失。"]
        report = workspace_report.render(result, manifest)
        outcome = {**{key: result[key] for key in ("recommendation", "review_status", "review_complete")},
                   "gaps": result["limitations"]}
        presentation_signature(result["findings"], report, outcome=outcome)
        for before, after in [(result["recommendation"], "另一項建議"), ("P1 1", "P1 0"),
                              ("審查完整性：未完整", "審查完整性：完整"), ("固定消費端缺失。", "")]:
            with self.subTest(before=before), self.assertRaises(AssertionError):
                presentation_signature(result["findings"], report.replace(before, after), outcome=outcome)

    def test_visible_source_metric_keeps_literal_details_tags_inside_code_fences(self):
        text = '```html\n<details>\nsecret-free example\n</details>\n```'
        self.assertEqual(len(text.encode()), expanded_source_bytes(text))

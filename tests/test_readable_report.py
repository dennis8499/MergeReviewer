"""Human-facing report layers retain every validated engineering fact."""
from __future__ import annotations

import copy
import re
import unittest

from tests.fixtures import workspace_report
import report_receipt


def readable_fixture():
    base, head = "a" * 40, "b" * 40
    repo = {"id": "repo-000", "name": "Orders", "path": "/workspace/Orders", "role": "seed",
            "sources": {"base": {"ref": base}, "head": {"ref": head}},
            "native": {"base_input": "origin/main", "head_input": "origin/fix",
                       "base_resolved_ref": "refs/frozen/base", "head_resolved_ref": "refs/frozen/head",
                       "diff_base": base, "review_right": head}}
    manifest = {"selected_repo": repo["id"], "repositories": [repo], "extension_reason": "單一 Repo。"}
    evidence = [{"repo": repo["id"], "source": source, "ref": reference, "path": path,
                 "line_start": 3, "line_end": 3, "excerpt": excerpt, "content_sha256": "c" * 64}
                for source, reference, path, excerpt in (
                    ("base", base, "pricing.py", "total = price * quantity"),
                    ("head", head, "pricing.py", "total = price + quantity"),
                    ("head", head, "caller.py", "return total"))]
    finding = {"id": "F-001", "priority": "P1", "title": "總額計算改成加法",
               "impact": "訂單總額不正確。", "impact_scope": "影響非零數量的訂單。",
               "operation_scenario": "建立訂單並取得總額。", "trigger": "單價 10、數量 3。",
               "technical_trigger": "固定 head 執行加法運算。", "expected": "總額應為 30。",
               "actual": "實際總額為 13。", "technical_explanation": "修改行以加號取代乘號。",
               "evidence_summary": "比較前後計算式，並核對呼叫端回傳。",
               "recommendation": "恢復乘法。", "engineering_fix": "補上非零數量的回歸案例。",
               "owner": "開發", "scenario_source": "code-derived", "evidence": evidence}
    result = {"report_layout": "engineering-review/v1", "summary": "訂單總額錯誤，需修正計算式。",
              "recommendation": "修正後再合併", "review_status": "發現具體問題", "review_complete": True,
              "findings": [finding], "priority_counts": {"P0": 0, "P1": 1, "P2": 0, "P3": 0},
              "limitations": [], "pending_confirmation_count": 0, "pending_confirmations": [],
              "extension_status": "complete", "tests_executed": [], "tests_not_executed": ["未執行專案測試。"],
              "scope_notes": [], "next_steps": [{"action": "修正計算式後重審。", "owner": "開發"}],
              "versions": [{"repo": repo["id"], "sources": {"base": base, "head": head}}],
              "coverage": [], "repository_coverage": [], "impact_coverage": [], "changed_contracts": [],
              "boundary_reviews": [], "merge_parent_review": [], "searches": []}
    return result, manifest


class ReadableReportTests(unittest.TestCase):
    def test_overview_states_observable_impact_certainty_and_owner_without_engineering_excerpt(self):
        result, manifest = readable_fixture()
        result["next_steps"] = []
        body = workspace_report.render(result, manifest)
        overview = body.split("## 問題詳情", 1)[0]
        for fact in ("下一步：開發先處理", "建立訂單並取得總額", "訂單總額不正確", "依程式推導"):
            self.assertIn(fact, overview)
        self.assertNotIn("total = price + quantity", overview)

    def test_summary_exposes_decision_completeness_versions_and_authored_summary(self):
        result, manifest = readable_fixture()
        text = workspace_report.render(result, manifest)
        summary = text.split("## 問題總攬", 1)[0]
        for fact in ("合併建議", "修正後再合併", "審查完整性", "完整", result["summary"],
                     "origin/fix", "bbbbbbbb", "origin/main", "aaaaaaaa", "修正計算式後重審。", "未執行專案測試。"):
            self.assertIn(fact, summary)
        self.assertIn("[F-001](#finding-F-001)", text)
        self.assertEqual(["審查結論", "問題總攬", "問題詳情（工程師參考）", "範圍與限制", "技術審查紀錄（工程師參考）"],
                         re.findall(r"^## (.+)$", text, re.M))
        self.assertEqual(1, text.count("- 本機路徑："))
        self.assertIn("| Repo | 角色 | 來源 | 完整 SHA |\n| --- | --- | --- | --- |", text)

    def test_primary_excerpts_are_open_secondary_evidence_collapses_and_all_facts_survive(self):
        result, manifest = readable_fixture()
        original = copy.deepcopy(result)
        text = workspace_report.render(result, manifest)
        section = text.split('### F-001｜', 1)[1].split("## 範圍與限制", 1)[0]
        primary, remaining = section.split("<details>", 1)
        self.assertIn("total = price * quantity", primary)
        self.assertIn("total = price + quantity", primary)
        self.assertNotIn("return total", primary)
        self.assertIn("<summary>其他證據（1 筆）</summary>", remaining)
        self.assertIn("return total", remaining.split("</details>", 1)[0])
        self.assertIn("**建議改善方式**", remaining.split("</details>", 1)[1])
        self.assertIn("```python\ntotal = price + quantity\n```", primary)
        labels = ["問題描述", "重現原因", "預期結果", "實際結果", "證據", "建議改善方式"]
        self.assertEqual(labels, re.findall(r"^\*\*([^*]+)\*\*$", section, re.M))
        finding = result["findings"][0]
        for field in ("impact", "impact_scope", "operation_scenario", "trigger", "technical_trigger", "expected",
                      "actual", "technical_explanation", "evidence_summary", "recommendation", "engineering_fix", "owner"):
            self.assertIn(finding[field], section)
        for entry in finding["evidence"]:
            for field in ("path", "ref", "excerpt"):
                self.assertIn(entry[field], section)
        self.assertEqual(original, result)

        # Each finding has its own navigation target and collapsed evidence;
        # closing one must not swallow the next finding or either fix block.
        another = copy.deepcopy(result["findings"][0])
        another.update(id="F-002", priority="P2", title="另一項工程問題", expected="另一項預期結果。")
        result["findings"].append(another)
        result["priority_counts"]["P2"] = 1
        multiple = workspace_report.render(result, manifest)
        for identifier in ("F-001", "F-002"):
            self.assertIn(f"[{identifier}](#finding-{identifier})", multiple)
        first, second = multiple.split("### F-002｜", 1)
        self.assertEqual(1, first.count("<summary>其他證據（1 筆）</summary>"))
        self.assertEqual(first.count("<details>"), first.count("</details>"))
        self.assertIn("另一項預期結果。", second)
        self.assertIn("**建議改善方式**", second.split("</details>", 1)[1])

    def test_pending_confirmations_and_manual_record_are_outside_collapsed_content(self):
        result, manifest = readable_fixture()
        result.update(review_complete=False, recommendation="需人工確認", review_status="待人工確認",
                      pending_confirmation_count=1, limitations=["缺少必要部署證據。"],
                      pending_confirmations=[{"id": "Q-001", "title": "確認已註冊的消費端",
                          "reason": "固定註冊指向必要消費端。", "missing_evidence": "缺少該消費端版本。",
                          "verification": "核對固定部署清單。", "owner": "QA",
                          "related_changes": [{"repo": "repo-000", "path": "pricing.py", "source": "head"}]}])
        text = workspace_report.render(result, manifest)
        scope = text.split("## 範圍與限制", 1)[1].split("## 技術審查紀錄", 1)[0]
        self.assertNotIn("<details>", scope)
        self.assertIn("缺少必要部署證據。", scope)
        self.assertIn("<!-- merge-confirmations:start -->", scope)
        self.assertIn("Q-001 已完成核對", scope)
        edited = text.replace("- [ ] Q-001 已完成核對", "- [x] Q-001 已完成核對")
        self.assertEqual(report_receipt.create(text)["static_body_sha256"],
                         report_receipt.create(edited)["static_body_sha256"])
        changed = text.replace("總額應為 30。", "總額應為 13。")
        self.assertNotEqual(report_receipt.create(text)["static_body_sha256"],
                            report_receipt.create(changed)["static_body_sha256"])

    def test_empty_single_and_legacy_reports_keep_their_distinct_contracts(self):
        for label, amount, engineering in (("empty", 0, True), ("singleton", 1, True), ("legacy", 3, False)):
            with self.subTest(case=label):
                result, manifest = readable_fixture()
                result["findings"][0]["evidence"] = result["findings"][0]["evidence"][:amount]
                if amount == 0:
                    result.update(findings=[], priority_counts={"P0": 0, "P1": 0, "P2": 0, "P3": 0})
                if not engineering:
                    result.pop("report_layout")
                text = workspace_report.render(result, manifest)
                self.assertNotIn("其他證據", text)
                self.assertIn("無已確認問題。" if not amount else "總額應為 30。", text)
                if not engineering:
                    self.assertIn("## 問題\n", text)
                    self.assertNotIn("## 問題總攬", text)
                    self.assertIn("\n```\ntotal = price + quantity\n```", text)

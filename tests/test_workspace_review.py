from __future__ import annotations
import copy
import json

from tests.fixtures import ReviewFixture, api, workspace, workspace_report
from tests.support import run_git as git, write_text


class WorkspaceReviewTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.consumer = self.make_repo("consumer")
        self.commit()

    def test_related_consumer_and_changed_contract_are_connected(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        self.attach_consumer(draft, manifest, self.consumer)
        result = workspace_report.validate(draft, manifest, context)
        self.assertTrue(result["review_complete"])
        self.assertEqual("complete", result["extension_status"])

    def test_unreviewed_required_consumer_keeps_supported_findings_and_is_incomplete(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        self.attach_consumer(draft, manifest, self.consumer, reviewed=False)
        draft["findings"] = [self.finding(draft)]
        result = workspace_report.validate(draft, manifest, context)
        self.assertEqual("審查未完成", result["review_status"])
        self.assertEqual("incomplete", result["extension_status"])
        self.assertEqual("F-001", result["findings"][0]["id"])
        self.assertEqual(1, result["priority_counts"]["P1"])

    def test_disconnected_consumer_and_unknown_graph_edge_are_rejected(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        node = self.attach_consumer(draft, manifest, self.consumer, connected=False)
        with self.assertRaisesRegex(ValueError, "disconnected"):
            workspace_report.validate(draft, manifest, context)
        node["related_to"] = ["I-missing"]
        with self.assertRaisesRegex(ValueError, "unknown"):
            workspace_report.validate(draft, manifest, context)

    def test_every_candidate_and_every_changed_path_need_dispositions(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        draft["repository_coverage"].pop()
        with self.assertRaisesRegex(ValueError, "every candidate"):
            workspace_report.validate(draft, manifest, context)

    def test_coverage_cannot_replace_seed_with_same_path_in_another_repo(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        self.attach_consumer(draft, manifest, self.consumer)
        record = next(r for r in manifest["repositories"] if r["path"] == str(self.consumer))
        draft["coverage"][0]["evidence"][0].update(repo=record["id"], source="head", ref=record["sources"]["head"]["ref"])
        with self.assertRaises(ValueError):
            workspace_report.validate(draft, manifest, context)

    def test_search_requires_changed_contract_and_relevant_term(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        with self.assertRaisesRegex(ValueError, "registered"):
            workspace.search(context, manifest, "contract", None, api)
        contract = manifest["changed_contracts"][0]
        with self.assertRaisesRegex(ValueError, "unrelated"):
            workspace.search(context, manifest, "unrelated word", None, api, contract_id=contract["id"])

    def test_one_negative_search_cannot_exclude_a_candidate(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        candidate = next(r for r in draft["repository_coverage"] if r["status"] != "seed")
        candidate["search_ids"] = candidate["search_ids"][:1]
        with self.assertRaisesRegex(ValueError, "alternate"):
            workspace_report.validate(draft, manifest, context)

    def test_search_ledger_is_checked_against_actual_fixed_objects(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        manifest["searches"][0]["output"] = "invented match"
        with self.assertRaisesRegex(ValueError, "ledger"):
            workspace_report.validate(draft, manifest, context)

    def test_contract_anchor_must_overlap_changed_lines(self):
        self.commit("unchanged = 'header'\ncontract = 'new'\n")
        # Reuse the first line as baseline so only the second line changes.
        git(self.repo, "switch", "main")
        self.commit("unchanged = 'header'\ncontract = 'old'\n")
        git(self.repo, "switch", "feature")
        # Feature originally had a different first line; reset the baseline relationship
        # through a new branch based on the prepared main in this isolated fixture.
        git(self.repo, "switch", "-c", "second", "main")
        self.commit("unchanged = 'header'\ncontract = 'changed'\n")
        context, manifest = self.capture(head="second")
        row = next(r for r in manifest["required_coverage"] if r["source"] == "head")
        seed = workspace.repository(manifest, manifest["selected_repo"])
        item = {"id": "C-bad", "kind": "behavior", "symbol": "unchanged", "root": row,
                "evidence": {**row, "ref": seed["sources"]["head"]["ref"], "line_start": 1, "line_end": 1},
                "search_terms": [{"query": "unchanged", "reason": "同檔案但未修改的欄位。"}]}
        with self.assertRaisesRegex(ValueError, "changed lines"):
            workspace.register_contract(context, manifest, item, api)

    def test_propagated_alias_requires_its_own_fixed_anchor_and_connected_node(self):
        write_text(self.consumer / "app.py", "contract = 'old'\nalias = contract\n")
        git(self.consumer, "add", ".")
        git(self.consumer, "commit", "-m", "wrapper")
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        node = self.attach_consumer(draft, manifest, self.consumer)
        record = workspace.repository(manifest, node["repo"])
        parent = manifest["changed_contracts"][0]
        evidence = {**node["evidence"][0], "line_start": 2, "line_end": 2}
        contract = workspace.register_contract(context, manifest, {
            "id": "C-alias", "kind": "behavior", "symbol": "alias", "root": parent["root"],
            "parent_ids": [parent["id"]], "evidence": evidence,
            "search_terms": [{"query": "alias", "reason": "實際包裝名稱。"},
                             {"query": "contract", "reason": "包裝仍連至原契約。"}]
        }, api)
        node["contract_ids"].append(contract["id"])
        for row in draft["repository_coverage"]:
            for term in contract["search_terms"]:
                search = workspace.search(context, manifest, term["query"], [row["repo"]], api, contract_id=contract["id"])
                row["search_ids"] += [s["id"] for s in search["searches"]]
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])

    def test_consumer_head_or_workspace_file_drift_prevents_completion(self):
        workspace_file = self.root / "visible.code-workspace"
        workspace_file.write_text(json.dumps({"folders": [{"path": "api"}, {"path": "consumer"}]}), encoding="utf-8")
        context, manifest = self.capture("--workspace-file", str(workspace_file))
        draft = self.draft(context, manifest)
        self.attach_consumer(draft, manifest, self.consumer)
        self.commit("contract = 'changed consumer'\n", repository=self.consumer)
        self.assertFalse(workspace_report.validate(draft, manifest, context)["review_complete"])
        workspace_file.write_text(json.dumps({"folders": [{"path": "api"}]}), encoding="utf-8")
        self.assertFalse(workspace_report.validate(draft, manifest, context)["review_complete"])

    def test_registered_contracts_require_a_connected_review_anchor(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        draft["impact_coverage"][0]["contract_ids"] = []
        with self.assertRaises(ValueError):
            workspace_report.validate(draft, manifest, context)

    def test_cycles_are_valid_but_unrelated_repo_is_not_a_second_seed(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        node = self.attach_consumer(draft, manifest, self.consumer)
        draft["impact_coverage"][0]["related_to"] = [node["id"]]
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])
        self.assertEqual([manifest["selected_repo"]], manifest["seed_repos"])

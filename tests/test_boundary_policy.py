"""Containment is explicit; exported effects retain ordinary consumer obligations."""
from __future__ import annotations

import copy
from unittest import mock

from tests.fixtures import ReviewFixture, api, workspace, workspace_report
import contract_boundary
import review_diagnostics


class BoundaryTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.consumer = self.make_repo("consumer")
        self.commit()

    def local(self):
        context, manifest = self.capture()
        draft = self.draft(context, manifest)
        contract = manifest["changed_contracts"][0]
        contract["search_scope"] = "repo-local"
        manifest["searches"] = []
        for row in draft["repository_coverage"]:
            row["search_ids"] = []
        workspace.save(context, manifest)
        search = workspace.search(context, manifest, contract["symbol"], None, api,
                                  source_name=contract["evidence"]["source"], contract_id=contract["id"])
        draft["boundary_reviews"] = [{"contract_id": contract["id"], "outcome": "contained",
            "reason": "Reviewed local uses and the operation's output; this value remains inside the operation.",
            "evidence": [copy.deepcopy(contract["evidence"])],
            "search_ids": [row["id"] for row in search["searches"]], "boundary_contract_ids": []}]
        return context, manifest, draft

    def test_local_change_completes_without_peer_searches(self):
        context, manifest, draft = self.local()
        self.assertEqual(contract_boundary.POLICY, manifest["contract_scope_policy"])
        self.assertEqual([manifest["selected_repo"]], [s["repo"] for s in manifest["searches"]])
        result = workspace_report.validate(draft, manifest, context)
        self.assertTrue(result["review_complete"])
        self.assertEqual("contained", result["boundary_reviews"][0]["outcome"])

    def test_missing_review_search_or_evidence_cannot_claim_containment(self):
        context, manifest, draft = self.local()
        variants = [("boundary_reviews", []), ("search_ids", []), ("evidence", [])]
        for field, value in variants:
            with self.subTest(field=field):
                bad = copy.deepcopy(draft)
                if field == "boundary_reviews":
                    bad[field] = value
                else:
                    bad["boundary_reviews"][0][field] = value
                with self.assertRaises(workspace_report.DraftValidationError):
                    workspace_report.validate(bad, manifest, context)

    def test_unresolved_local_boundary_preserves_incomplete_result(self):
        context, manifest, draft = self.local()
        draft["boundary_reviews"][0].update(outcome="unresolved", evidence=[], search_ids=[],
                                            reason="The output mapping is still unavailable.")
        result = workspace_report.validate(draft, manifest, context)
        self.assertFalse(result["review_complete"])
        self.assertEqual("incomplete", result["extension_status"])

    def export(self, context, manifest, draft):
        parent = manifest["changed_contracts"][0]
        child = workspace.register_contract(context, manifest, {
            "id": "C-output", "root": parent["root"], "kind": "data", "symbol": "new",
            "parent_ids": [parent["id"]], "search_scope": "workspace",
            "evidence": copy.deepcopy(parent["evidence"]),
            "search_terms": [{"query": "new", "reason": "Serialized output value."},
                             {"query": "contract = 'new'", "reason": "Output mapping."}]}, api)
        draft["impact_coverage"][0]["contract_ids"].append(child["id"])
        for row in draft["repository_coverage"]:
            if row["repo"] == parent["evidence"]["repo"]:
                continue
            for term in child["search_terms"]:
                result = workspace.search(context, manifest, term["query"], [row["repo"]], api, contract_id=child["id"])
                row["search_ids"].extend(s["id"] for s in result["searches"])
        return child

    def test_internal_change_exported_as_payload_keeps_workspace_obligations(self):
        context, manifest, draft = self.local()
        child = self.export(context, manifest, draft)
        with self.assertRaisesRegex(ValueError, "hide workspace descendants"):
            workspace_report.validate(draft, manifest, context)
        draft["boundary_reviews"][0].update(outcome="propagates", boundary_contract_ids=[child["id"]],
                                            reason="The local value determines the serialized payload.")
        self.assertTrue(workspace_report.validate(draft, manifest, context)["review_complete"])
        draft["repository_coverage"][1]["search_ids"] = []
        with self.assertRaisesRegex(ValueError, "completed searches"):
            workspace_report.validate(draft, manifest, context)

    def test_propagation_requires_workspace_descendant(self):
        context, manifest, draft = self.local()
        draft["boundary_reviews"][0].update(outcome="propagates", boundary_contract_ids=["missing"])
        with self.assertRaisesRegex(ValueError, "workspace descendant"):
            workspace_report.validate(draft, manifest, context)

    def test_legacy_context_retains_matrix_and_unknown_policy_is_rejected(self):
        context, manifest, draft = self.local()
        manifest.pop("contract_scope_policy")
        draft.pop("boundary_reviews")
        with self.assertRaisesRegex(ValueError, "completed searches"):
            workspace_report.validate(draft, manifest, context)
        manifest["contract_scope_policy"] = "future-policy"
        workspace.save(context, manifest)
        with self.assertRaisesRegex(ValueError, "unsupported contract scope policy"):
            workspace.load(context)

    def test_local_search_cannot_be_spread_to_peers(self):
        context, manifest, _ = self.local()
        peer = next(r["id"] for r in manifest["repositories"] if r["id"] != manifest["selected_repo"])
        with self.assertRaisesRegex(ValueError, "only their evidence Repo"):
            workspace.search(context, manifest, "contract", [peer], api, contract_id="C-001")

    def test_diagnostics_collects_independent_errors_without_git(self):
        _, manifest, draft = self.local()
        draft["report_layout"] = "engineering-review/v1"
        draft["summary"] = ""
        draft["boundary_reviews"][0]["search_ids"] = []
        draft["repository_coverage"].pop()
        with mock.patch.object(api, "run_git", side_effect=AssertionError("check ran Git")), \
             mock.patch.object(api, "run_git_capture", side_effect=AssertionError("check ran Git")):
            result = review_diagnostics.inspect(draft, manifest)
        self.assertEqual(0, result["git_commands"])
        self.assertGreaterEqual(len(result["errors"]), 3)
        self.assertFalse(result["ready_for_finish"])

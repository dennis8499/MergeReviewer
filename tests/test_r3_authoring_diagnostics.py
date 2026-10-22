"""Own impact evidence is a JSON metadata check; cross-source support is allowed."""
from __future__ import annotations

import copy
from pathlib import Path
import unittest
from unittest import mock

from tests.fixtures import api, workspace
import review_diagnostics


OWN_EVIDENCE = "authoring.impact-evidence-must-cover-its-own-fixed-path-source"


class ImpactOwnEvidenceDiagnosticsTests(unittest.TestCase):
    def fixture(self):
        root = {"repo": "repo-000", "source": "head", "path": "app.py"}
        anchor = {**root, "ref": "a" * 40, "line_start": 1, "line_end": 1}
        manifest = {
            "extension_enabled": True, "seed_repos": ["repo-000"],
            "repositories": [{"id": "repo-000", "state": "complete", "review_source": "head",
                              "sources": {name: {"ref": ref, "files": [
                                  {"path": path, "kind": "blob"} for path in ("app.py", "other.py")]}
                                  for name, ref in (("head", "a" * 40), ("merge-base", "b" * 40))}}],
            "required_coverage": [root], "searches": [], "required_merge_views": [],
            "changed_contracts": [{"id": "C-001", "root": root, "evidence": anchor}],
        }
        draft = {"schema": workspace.RESULT, "report_layout": "engineering-review/v1", "summary": "Reviewed.",
                 "coverage": [{**root, "status": "reviewed", "evidence": [anchor], "impact_ids": ["I-001"]}],
                 "impact_coverage": [{"id": "I-001", **root, "status": "reviewed", "reason": "Fixed own path.",
                                      "evidence": [anchor], "contract_ids": ["C-001"], "related_to": []}],
                 "repository_coverage": [{"repo": "repo-000", "status": "seed", "reason": "Selected Repo.",
                                          "search_ids": []}],
                 **{name: [] for name in ("findings", "merge_parent_review", "next_steps", "tests_executed",
                                          "tests_not_executed", "limitations", "boundary_reviews")}}
        return copy.deepcopy(draft), copy.deepcopy(manifest)

    def inspect(self, draft, manifest):
        with mock.patch.object(api, "run_git", side_effect=AssertionError("check ran Git")), \
             mock.patch.object(api, "run_git_capture", side_effect=AssertionError("check ran Git")), \
             mock.patch.object(Path, "read_text", side_effect=AssertionError("check read a source file")), \
             mock.patch.object(Path, "read_bytes", side_effect=AssertionError("check read a source file")):
            result = review_diagnostics.inspect(draft, manifest)
        self.assertEqual(0, result["git_commands"])
        self.assertFalse(result["evidence_verified"])
        return result

    def own_errors(self, result):
        return [row for row in result["errors"] if row["code"] == OWN_EVIDENCE]

    def test_each_own_location_component_must_match(self):
        for field, wrong in (("repo", "repo-other"), ("source", "merge-base"), ("path", "other.py")):
            with self.subTest(field=field):
                draft, manifest = self.fixture()
                draft["impact_coverage"][0][field] = wrong
                result = self.inspect(draft, manifest)
                self.assertFalse(result["ready_for_finish"])
                self.assertEqual(["/impact_coverage/0/evidence"], [row["pointer"] for row in self.own_errors(result)])

    def test_cross_source_support_does_not_replace_or_invalidate_valid_own_evidence(self):
        draft, manifest = self.fixture()
        support = {**draft["impact_coverage"][0]["evidence"][0], "source": "merge-base", "ref": "b" * 40}
        draft["impact_coverage"][0]["evidence"].insert(0, support)
        result = self.inspect(draft, manifest)
        self.assertEqual([], result["errors"])
        self.assertTrue(result["ready_for_finish"])
        draft["impact_coverage"][0]["evidence"].pop()
        self.assertEqual(1, len(self.own_errors(self.inspect(draft, manifest))))

    def test_invalid_fixed_reference_blob_or_interval_cannot_count_as_own(self):
        for change in ({"ref": "c" * 40}, {"path": "missing.py"}, {"line_start": 0}, {"line_end": True}):
            with self.subTest(change=change):
                draft, manifest = self.fixture()
                # Independent copies keep the registered contract and coverage valid.
                draft["impact_coverage"][0]["evidence"] = [{**draft["impact_coverage"][0]["evidence"][0], **change}]
                result = self.inspect(draft, manifest)
                self.assertEqual(1, len(self.own_errors(result)))
                self.assertGreaterEqual(len(result["errors"]), 2)

    def test_unreviewed_node_keeps_gap_without_requiring_own_evidence(self):
        draft, manifest = self.fixture()
        draft["impact_coverage"][0].update(status="not-reviewed", evidence=[])
        result = self.inspect(draft, manifest)
        self.assertFalse(result["ready_for_finish"])
        self.assertEqual([], self.own_errors(result))
        self.assertTrue(any(gap["kind"] == "impact" and gap["id"] == "I-001" for gap in result["gaps"]))

    def test_malformed_location_or_evidence_does_not_raise(self):
        for field in ("repo", "source", "path", "evidence"):
            for value in ([], {}, None, True):
                with self.subTest(field=field, value=value):
                    draft, manifest = self.fixture()
                    draft["impact_coverage"][0][field] = value
                    result = self.inspect(draft, manifest)
                    self.assertFalse(result["ready_for_finish"])
                    self.assertEqual(1, len(self.own_errors(result)))


if __name__ == "__main__":
    unittest.main()

"""Malformed editable JSON remains a repairable, zero-Git diagnostic."""
from __future__ import annotations

import copy
import unittest

from tests.fixtures import workspace  # Initializes the scripts import path.
import review_diagnostics


class DiagnosticShapeTests(unittest.TestCase):
    def test_unhashable_and_scalar_fields_do_not_raise(self):
        manifest = {"repositories": [], "required_coverage": [], "changed_contracts": [], "searches": []}
        initial = {"schema": workspace.RESULT, "report_layout": "engineering-review/v1", "summary": "Reviewed.",
                   **{key: [] for key in ("coverage", "impact_coverage", "repository_coverage", "findings",
                       "merge_parent_review", "next_steps", "tests_executed", "tests_not_executed", "limitations")}}
        shapes = [None, True, 7, "text", [], {}, [1], {"unexpected": []}]
        for field in ("coverage", "impact_coverage", "merge_parent_review", "repository_coverage", "boundary_reviews"):
            for value in shapes:
                draft = copy.deepcopy(initial)
                draft[field] = [{"id": value, "repo": value, "source": value, "path": value,
                                 "evidence": [], "related_to": value, "search_ids": value}]
                with self.subTest(field=field, value=value):
                    result = review_diagnostics.inspect(draft, manifest)
                    self.assertFalse(result["evidence_verified"])
                    self.assertEqual(0, result["git_commands"])
                    self.assertTrue(result["errors"])
                    self.assertTrue(all("code" in row and "pointer" in row for row in result["errors"]))

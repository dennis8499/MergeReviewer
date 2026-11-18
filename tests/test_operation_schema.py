"""Runtime-free CLI discovery and preflight share the installed contracts."""
from __future__ import annotations

import json
import unittest

from tests.fixtures import SCRIPTS
from tests.support import run_python
import review_author_operation as author
import review_flow as flow
import review_operation_schema as schema


class OperationSchemaTests(unittest.TestCase):
    def test_discovery_pins_required_fields_types_and_checkpoint_revision_errors(self):
        operations = schema.describe()["operations"]
        self.assertTrue(operations["read"]["fields"]["repo"]["required"])
        self.assertTrue(operations["read"]["fields"]["path"]["required"])
        self.assertFalse(operations["read"]["fields"]["source"]["required"])
        self.assertFalse(operations["instructions"]["fields"]["path"]["required"])
        self.assertEqual("integer", operations["read"]["fields"]["line_end"]["type"])
        self.assertEqual("object", operations["register-contract"]["fields"]["contract"]["type"])
        self.assertTrue(operations["author"]["fields"]["updates"]["required"])
        for revision in (True, -1, "0"):
            with self.subTest(case=type(revision).__name__):
                result = flow.operation_preflight([{"id": "save", "op": "author", "updates": {},
                                                  "expected_checkpoint_revision": revision}])
                self.assertEqual("/operations/0/expected_checkpoint_revision", result["pointer"])
                self.assertEqual("authoring.invalid-operation-argument", result["code"])

    def test_schema_argument_sets_match_preflight_and_author_fields(self):
        contracts = schema.describe()["operations"]
        self.assertEqual(set(flow.OPERATION_ARGUMENTS), set(contracts))
        for name, contract in contracts.items():
            with self.subTest(operation=name):
                self.assertEqual(flow.OPERATION_ARGUMENTS[name], set(contract["fields"]))
                operation = contract["example"]
                self.assertIsNone(flow.operation_preflight(operation))
                unknown = [{**operation[0], "not_an_argument": True}]
                self.assertEqual("authoring.invalid-operation-argument", flow.operation_preflight(unknown)["code"])
        self.assertEqual(set(author.ROW_KEYS) | author.TEXT_LISTS | {"summary"}, set(contracts["author"]["update_fields"]))
        self.assertEqual("array[string]", contracts["search"]["fields"]["repo"]["type"])

    def test_schema_and_help_work_without_runtime_or_request(self):
        script = SCRIPTS / "review_session.py"
        result = run_python(script, "schema", "--operation", "author")
        self.assertEqual(0, result.returncode, result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(["author"], list(value["operations"]))
        self.assertIn("expected_checkpoint_revision", value["operations"]["author"]["fields"])
        for command in ([], ["view"], ["schema"], ["step"]):
            with self.subTest(command="top" if not command else command[0]):
                help_result = run_python(script, *command, "--help")
                self.assertEqual(0, help_result.returncode, help_result.stderr)
                expected = "schema" if not command else {"view": "--requests-file", "schema": "--operation", "step": "--requests-file"}[command[0]]
                self.assertIn(expected, help_result.stdout)
                if command == ["view"]:
                    self.assertNotIn("--draft-format", help_result.stdout)

    def test_unknown_schema_operation_is_rejected_without_runtime(self):
        result = run_python(SCRIPTS / "review_session.py", "schema", "--operation", "arbitrary")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("invalid choice", result.stderr)

    def test_author_nested_schema_describes_partial_merges_lists_citations_and_findings(self):
        value = schema.describe("author")
        properties = value["operations"]["author"]["update_schema"]["properties"]
        self.assertEqual(set(author.ROW_KEYS) | author.TEXT_LISTS | {"summary"}, set(properties))
        self.assertEqual("array", properties["tests_not_executed"]["type"])
        self.assertEqual("string", properties["tests_not_executed"]["items"]["type"])
        self.assertEqual(["action", "owner"], properties["next_steps"]["items"]["required"])
        coverage = properties["coverage"]
        self.assertEqual("change_id", coverage["merge_key"])
        self.assertEqual(["change_id"], coverage["items"]["required"])
        citation = coverage["items"]["properties"]["evidence"]["items"]
        compact, full = citation["oneOf"]
        self.assertEqual(["read_id", "line_start", "line_end"], compact["required"])
        self.assertFalse(compact["additionalProperties"])
        self.assertEqual({"repo", "source", "ref", "path"}, set(full["required"]))
        self.assertEqual({"line_start": ["line_end"], "line_end": ["line_start"]}, full["dependentRequired"])
        self.assertEqual(["integer", "null"], full["properties"]["line_start"]["type"])
        self.assertFalse(full["additionalProperties"])
        related = properties["pending_confirmations"]["items"]["properties"]["related_changes"]["items"]["oneOf"]
        self.assertEqual("string", related[0]["type"])
        self.assertEqual({"repo", "source", "path"}, set(related[1]["required"]))
        finding = properties["findings"]["items"]["properties"]
        self.assertEqual(["P0", "P1", "P2", "P3"], finding["priority"]["enum"])
        self.assertEqual(["code-derived", "reproduced"], finding["scenario_source"]["enum"])
        self.assertTrue({"scenario", "explanation", "fix", "operation_scenario", "technical_explanation",
                         "engineering_fix", "recommendation"}.issubset(finding))
        self.assertIn("-InputObject @($operations)", value["serialization"]["powershell"])


if __name__ == "__main__":
    unittest.main()

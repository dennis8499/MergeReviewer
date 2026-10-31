"""Fast protocol, paging, decode and redaction regression cases."""
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/merge-reviewer/scripts"
sys.path.insert(0, str(SCRIPTS))

import review_content
import review_flow
import review_authoring
import review_runtime
import review_workspace


class FlowOptimizationTests(unittest.TestCase):
    def test_capabilities_advertise_compact_and_read_only_scoped_views(self):
        value = review_flow.capabilities()
        self.assertEqual("MergeReviewCapabilities/v1", value["schema"])
        self.assertIn("compact", value["draft_formats"])
        self.assertEqual(["context", "runtime"], value["features"]["artifact_scopes"])
        self.assertTrue(value["features"]["read_only_artifact_view"])

    def test_compact_draft_template_expands_to_the_same_v3_validator_input(self):
        root = {"id": "CH-0001", "repo": "repo-000", "source": "head", "path": "app.py"}
        manifest = {"required_coverage": [root], "required_merge_views": [],
                    "repositories": [{"id": "repo-000", "sources": {
                        "head": {"ref": "a" * 40}}}], "selected_repo": "repo-000",
                    "boundary_changes": [], "extension_enabled": False}
        compact = review_flow.draft_template(manifest, "compact")
        self.assertEqual("merge-reviewer-authoring/v1", compact["schema"])
        self.assertEqual("CH-0001", compact["coverage"][0]["change_id"])
        expanded = review_authoring.expand(compact, manifest, Path("."))
        self.assertEqual("merge-reviewer-workspace-result/v3", expanded["schema"])
        self.assertEqual(root, {key: expanded["coverage"][0][key] for key in root})

    def test_request_preflight_reports_exact_operation_pointer_without_git(self):
        result = review_flow.operation_preflight([
            {"id": "page-1", "op": "view", "artifact": "a" * 64, "fields": ["/reads"]}])
        self.assertEqual("authoring.invalid-operation-argument", result["code"])
        self.assertEqual("/operations/0/fields", result["pointer"])
        self.assertEqual("page-1", result["operation_id"])
        self.assertIn("output-only", result["error"])

    def test_request_preflight_checks_fixed_sources_duplicate_ids_and_cached_line_counts(self):
        repo = {"id": "repo-000", "review_source": "head", "sources": {
            "head": {"ref": "a" * 40, "files": [{"path": "app.py", "kind": "blob", "blob_sha": "b" * 40}]}}}
        manifest = {"repositories": [repo]}
        operation = {"id": "read-1", "op": "read", "repo": "repo-000", "path": "app.py",
                     "line_start": 1, "line_end": 4}
        key = review_flow._line_count_key(manifest, operation)
        result = review_flow.operation_preflight([operation], manifest, {key: 3})
        self.assertEqual("evidence.line-range-out-of-bounds", result["code"])
        self.assertEqual(3, result["line_count"])
        unknown = {**operation, "id": "read-2", "source": "not-captured"}
        self.assertEqual("authoring.unknown-source",
                         review_flow.operation_preflight([unknown], manifest)["code"])
        duplicate = [dict(operation, line_end=1), dict(operation, line_end=1)]
        self.assertEqual("authoring.duplicate-operation-id",
                         review_flow.operation_preflight(duplicate)["code"])

    def test_artifact_view_returns_scope_and_executable_read_only_continuation(self):
        scratch = Path(__file__).resolve().parents[1] / ".test-tmp"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            context = Path(directory)
            value = {"lines": [{"line": 1, "text": "x" * 10000}]}
            artifact = review_runtime.store_view(context, value)
            before = sorted(path.name for path in (context / "views").iterdir())
            page = review_flow.artifact_view(context, artifact, "/lines", page_size=1, scope="context")
            after = sorted(path.name for path in (context / "views").iterdir())
            self.assertEqual(before, after)
            self.assertEqual("context", page["scope"])
            self.assertFalse(page["view_complete"])
            continuation = page["items"][0]["drilldowns"][0]
            self.assertEqual("review_session.py view", continuation["command"])
            self.assertEqual("context", continuation["artifact_scope"])
            self.assertEqual("/lines/0/text", continuation["pointer"])
            self.assertNotIn("string_offset", continuation)

    def test_legacy_text_decodes_losslessly_binary_returns_metadata_and_secrets_are_masked(self):
        source = "第一行\n第二行\n".encode("cp950")
        lines, encoding, lossy = review_content.decode_source(source, "legacy.cs")
        self.assertEqual(["第一行", "第二行"], lines)
        self.assertEqual("cp950", encoding)
        self.assertFalse(lossy)
        lines, encoding, lossy = review_content.decode_source(
            "first\u2028not-a-git-line\r\nsecond\n".encode("utf-16"), "legacy.txt")
        self.assertEqual(["first\u2028not-a-git-line", "second"], lines)
        self.assertEqual("utf-16", encoding)
        self.assertFalse(lossy)
        lines, encoding, lossy = review_content.decode_source(b"MZ\x00\x01blob", "plugin.dll")
        self.assertIsNone(lines)
        self.assertIsNone(encoding)
        self.assertFalse(lossy)
        lines, encoding, lossy = review_content.decode_source(b"\x81", "legacy.cs")
        self.assertEqual("unknown", encoding)
        self.assertTrue(lossy)
        visible, changed = review_content.redact_lines(
            ["token = 'secret-value'", "safe = True"], "settings.py")
        self.assertEqual(2, len(visible))
        self.assertIn("[已遮蔽]", visible[0])
        self.assertNotIn("secret-value", visible[0])
        self.assertTrue(changed)
        visible, changed = review_content.redact_lines(['{"password": "json-secret"}'], "settings.json")
        self.assertNotIn("json-secret", visible[0])
        self.assertTrue(changed)

    def test_read_returns_binary_identity_without_failing_and_marks_lossy_text(self):
        repo = {"id": "repo-000", "state": "captured", "review_source": "head", "snapshot": None,
                "sources": {"head": {"ref": "a" * 40, "files": [
                    {"path": "plugin.dll", "kind": "blob"}, {"path": "unknown.cs", "kind": "blob"}]}}}
        manifest = {"repositories": [repo], "snapshot_policy": None}

        class Cache:
            def read(self, _repo, _source, path):
                return b"MZ\x00\x01binary" if path == "plugin.dll" else b"\x81"

        binary = review_workspace.read(Path("."), manifest, "repo-000", "plugin.dll", None,
                                       "head", cache=Cache())
        self.assertTrue(binary["binary"])
        self.assertFalse(binary["content_available"])
        self.assertEqual([], binary["lines"])
        self.assertEqual(10, binary["byte_size"])
        text = review_workspace.read(Path("."), manifest, "repo-000", "unknown.cs", None,
                                     "head", cache=Cache())
        self.assertTrue(text["lossy"])
        self.assertEqual("unknown", text["encoding"])


if __name__ == "__main__":
    unittest.main()

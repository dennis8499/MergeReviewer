"""The added quality oracles describe reproducible source behavior."""
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest import mock

from tests import create_merge_quality_fixture as fixtures

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/merge-reviewer/scripts"))
import git_review_context as api
import review_session
import review_workspace as workspace


def namespace(text):
    result = {}
    exec(compile(text, "<fixed-quality-fixture>", "exec"), result)
    return result


class MergeQualityFixtureTests(unittest.TestCase):
    def test_cross_branch_defect_is_in_the_actual_frozen_merge_preview(self):
        with tempfile.TemporaryDirectory(prefix="merge-quality-preview-") as temporary:
            root = Path(temporary)
            source = fixtures.create(root / "case", "cross-branch")
            with mock.patch.dict(os.environ, {"GITLAB_WORKSPACE_RUNTIME_DIR": str(root / "network")}):
                manifest = api.build_manifest(api.parse_args(["--workspace", source["workspace"],
                    "--project", source["project"], "--base", "main", "--head", "feature/review", "--no-fetch"]))
            context = Path(manifest["context_dir"])
            try:
                seed = workspace.repository(manifest, manifest["selected_repo"])
                self.assertIn("merge-preview", seed["sources"])
                merged = namespace(workspace.fixed_bytes(context, seed, "merge-preview", "app.py", api).decode("utf-8"))
                with self.assertRaises(AttributeError):
                    merged["accept"]("  ")
            finally:
                review_session.cleanup_session(context)

    def test_added_cases_preserve_real_regressions_and_parent_only_oracles(self):
        with tempfile.TemporaryDirectory(prefix="merge-quality-fixture-") as temporary:
            root = Path(temporary)
            for case in fixtures.CASES:
                with self.subTest(case=case):
                    source = fixtures.create(root / case, case)
                    repo = Path(source["project"])
                    old = namespace(fixtures.common.git(repo, "show", "main:app.py"))
                    new = namespace(fixtures.common.git(repo, "show", "feature/review:app.py"))
                    self.assertTrue((root / case / "oracle.json").is_file())
                    self.assertFalse((Path(source["workspace"]) / "oracle.json").exists())
                    if case == "p2":
                        self.assertEqual(900, old["price"](1000, 10))
                        self.assertEqual(999, new["price"](1000, 10))
                    elif case == "p3":
                        self.assertEqual("3 orders", old["label"](3))
                        self.assertEqual("0 orders", new["label"](3))
                    elif case == "outflow":
                        consumer = namespace((Path(source["workspace"]) / "billing-consumer/consumer.py").read_text(encoding="utf-8"))
                        self.assertEqual("invoice.created", consumer["TOPIC"])
                        self.assertEqual(500, consumer["consume"](old["event"](500)))
                        self.assertEqual(5, consumer["consume"](new["event"](500)))
                    elif case == "security-safe":
                        self.assertTrue(old["verify"](b"match", b"match"))
                        self.assertTrue(new["verify"](b"match", b"match"))
                        self.assertFalse(new["verify"](b"wrong", b"match"))
                        self.assertIn("hmac.compare_digest", fixtures.SECURITY_HEAD)
                    else:
                        self.assertFalse(old["accept"]("  "))
                        self.assertFalse(new["accept"]("  "))
                        tree = fixtures.common.git(repo, "merge-tree", "--write-tree", "main", "feature/review").splitlines()[0]
                        preview = namespace(fixtures.common.git(repo, "show", f"{tree}:app.py"))
                        with self.assertRaises(AttributeError):
                            preview["accept"]("  ")


if __name__ == "__main__":
    unittest.main()

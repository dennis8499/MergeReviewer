"""The sanitized replay must reject missing findings or hidden coverage gaps."""
import copy
from pathlib import Path
import tempfile
import unittest

from tests.replay_log_workflow import TITLE, digest, oracle


class LogWorkflowReplayTests(unittest.TestCase):
    def test_oracle_checks_findings_and_completion_independently(self):
        signature = {"findings": [{"title": TITLE, "priority": "P2"}],
                     "outcome": {"review_complete": True, "gaps": []}}
        report = TITLE + " expected 30, actual 13"
        oracle(signature, report)
        for change in ({"findings": []}, {"outcome": {"review_complete": False, "gaps": []}},
                       {"outcome": {"review_complete": True, "gaps": ["missing evidence"]}}):
            with self.subTest(change=change), self.assertRaises(AssertionError):
                oracle({**copy.deepcopy(signature), **change}, report)
        with self.assertRaises(AssertionError):
            oracle(signature, "no finding rendered")

    def test_frozen_skill_digest_changes_with_content_and_ignores_bytecode(self):
        with tempfile.TemporaryDirectory(prefix="review-replay-digest-") as directory:
            root = Path(directory)
            source = root / "helper.py"
            source.write_text("version = 1\n", encoding="utf-8")
            before = digest(root)
            (root / "__pycache__").mkdir()
            (root / "__pycache__/helper.pyc").write_bytes(b"ignored")
            self.assertEqual(before, digest(root))
            source.write_text("version = 2\n", encoding="utf-8")
            self.assertNotEqual(before, digest(root))

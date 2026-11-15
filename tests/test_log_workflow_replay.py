"""The sanitized replay must reject missing findings or hidden coverage gaps."""
import copy
from pathlib import Path
import shutil
import tempfile
import unittest

from tests.replay_log_workflow import ROOT, TITLE, canonical_pages, digest, fixture, freeze, oracle, worker


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

    def test_evidence_comparison_ignores_duplicate_calls_but_detects_changed_content(self):
        one = {"artifact": "a" * 64, "pointer": "/lines", "items": [{"line": 2, "text": "return price + quantity"}]}
        two = {"artifact": "a" * 64, "pointer": "/source", "value": "head"}
        self.assertEqual(canonical_pages([one, two]), canonical_pages([two, one, copy.deepcopy(one)]))
        changed = copy.deepcopy(one)
        changed["items"][0]["text"] = "return price * quantity"
        self.assertNotEqual(canonical_pages([one, two]), canonical_pages([changed, two]))

    def test_explicit_frozen_baseline_preserves_payload_identity(self):
        with tempfile.TemporaryDirectory(prefix="replay-freeze-") as directory:
            root = Path(directory)
            baseline, candidate = root / "old", root / "new"
            baseline.mkdir(); candidate.mkdir()
            (baseline / "VERSION").write_text("old\n", encoding="utf-8")
            (candidate / "VERSION").write_text("new\n", encoding="utf-8")
            old, new, identity = freeze(root / "run", "HEAD", candidate, baseline)
            self.assertEqual("old\n", (old / "VERSION").read_text(encoding="utf-8"))
            self.assertEqual("new\n", (new / "VERSION").read_text(encoding="utf-8"))
            self.assertEqual("frozen-directory", identity["baseline_source"])
            self.assertEqual(digest(baseline), identity["baseline_skill_sha256"])
            self.assertEqual(digest(candidate), identity["candidate_skill_sha256"])
            self.assertNotEqual(identity["baseline_skill_sha256"], identity["candidate_skill_sha256"])

    def test_paging_replay_uses_atomic_author_and_recovers_equivalent_evidence(self):
        with tempfile.TemporaryDirectory(prefix="paging-replay-") as directory:
            root = Path(directory).resolve()
            skill = root / "skill"
            shutil.copytree(ROOT / "skills/merge-reviewer", skill,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            repo = fixture(root)
            legacy_metrics, legacy_signature = worker(skill, repo, root / "legacy", "legacy")
            metrics, signature = worker(skill, repo, root / "paging", "paging", duplicate_count=12)
            self.assertEqual(legacy_signature, signature)
            self.assertTrue(metrics["paging_protocol_used"])
            self.assertEqual(2, metrics["atomic_author_calls"])
            self.assertEqual(1, metrics["checkpoint_recoveries"])
            self.assertGreaterEqual(metrics["duplicates_avoided"], 12)
            self.assertEqual(1, metrics["finish_attempts"])
            self.assertEqual(legacy_metrics["full_validations"], metrics["full_validations"])
            self.assertIsNone(metrics["model_review_seconds"])
            self.assertEqual(TITLE, signature["findings"][0]["title"])
            self.assertTrue(signature["outcome"]["review_complete"])
            self.assertEqual([], signature["outcome"]["gaps"])

"""Independent fixture behavior and strict rejection of misleading review results."""
from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from tests.compare_review_models import mechanical_check, summarize, task
from tests.review_improvement_corpus import CASES, assert_behavior, create, git, sources


class ImprovementCorpusTests(unittest.TestCase):
    def test_economic_overlap_and_independence_and_public_units_have_distinct_executable_oracles(self):
        for case in CASES[:-1]:
            with self.subTest(case=case):
                before, after, oracle = sources(case)
                assert_behavior(case, before, after)
                path = next(iter(after))
                if oracle["findings"]:
                    self.assertNotEqual(before[path].splitlines()[oracle["line"] - 1],
                                        after[path].splitlines()[oracle["line"] - 1])
        self.assertEqual(0, sources("interest-alias-safe")[2]["findings"])
        self.assertEqual(1, sources("interest-independent")[2]["findings"])

    def test_required_gap_fixture_keeps_known_local_defect_and_missing_registered_consumer(self):
        with tempfile.TemporaryDirectory(prefix="mr-corpus-") as temporary:
            root = Path(temporary) / "case"
            fixture = create(root, "required-gap")
            project = Path(fixture["project"])
            before, after = {}, {}
            exec(git(project, "show", "main:app.py"), before)
            exec((project / "app.py").read_text(encoding="utf-8"), after)
            self.assertEqual(10, before["checkout"](10))
            with self.assertRaises(KeyError):
                after["checkout"](10)
            missing = Path(fixture["workspace"]) / "billing-consumer"
            self.assertTrue((missing / ".git").is_dir())
            self.assertFalse((missing / ".git/refs/heads/main").exists())
            self.assertIsNone(fixture["review_draft"])
            self.assertFalse((project / "oracle.json").exists())
            self.assertFalse(json.loads((root / "oracle.json").read_text(encoding="utf-8"))["complete"])

    def test_mechanical_checks_reject_false_positive_wrong_citation_and_false_completion(self):
        safe = sources("interest-alias-safe")[2]
        finding = {"priority": "P1", "evidence": [{"path": "interest.py", "line_start": 13, "line_end": 13}]}
        self.assertFalse(mechanical_check({"findings": [finding]}, {}, {"review_complete": True}, safe)["passed"])
        bad = sources("interest-independent")[2]
        self.assertTrue(mechanical_check({"findings": [finding]}, {}, {"review_complete": True}, bad)["passed"])
        finding["evidence"][0]["line_start"] = 14
        self.assertFalse(mechanical_check({"findings": [finding]}, {}, {"review_complete": True}, bad)["passed"])
        self.assertFalse(mechanical_check({"findings": []}, {}, {"review_complete": True}, {"findings": 0, "complete": False})["passed"])

        with tempfile.TemporaryDirectory(prefix="model-speed-filter-") as temporary:
            root = Path(temporary)
            jobs = [{"id": "run-01", "case": "fixture", "variant": "baseline"},
                    {"id": "run-02", "case": "fixture", "variant": "candidate"}]
            valid = {"returncode": 0, "timed_out": False, "wall_seconds": 10, "published": True,
                     "mechanical_check": {"passed": True}, **{key: True for key in (
                         "skill_unchanged", "tracked_sources_unchanged", "fixed_refs_match", "receipt_present", "report_hashes_match")}}
            for job in jobs:
                output = root / "runs" / job["id"] / "audit/result.json"
                output.parent.mkdir(parents=True)
                output.write_text(json.dumps(valid), encoding="utf-8")
            schedule = {"jobs": jobs, "model_requested": "fixture-model", "reasoning_requested": "max"}
            self.assertEqual(1, summarize(root, schedule)["valid_speed_pairs"])
            failed = root / "runs/run-02/audit/result.json"
            failed.write_text(json.dumps({**valid, "wall_seconds": 1, "published": False}), encoding="utf-8")
            result = summarize(root, schedule)
            self.assertEqual(0, result["valid_speed_pairs"])
            self.assertNotIn("paired_wall_ratio_median", result)
            self.assertEqual(["fixture"], result["excluded_speed_cases"])

    def test_public_task_exposes_sources_and_instrumentation_without_oracle_or_prepared_draft(self):
        text = task(Path("frozen"), Path("workspace"), Path("workspace/orders"),
                    {"base": "main", "head": "feature/review"}, Path("runtime"), Path("audit"), Path("tools/trace.py"))
        self.assertIn("review_session <command>", text)
        self.assertIn("Do not run application tests", text)
        self.assertNotIn("oracle.json", text)
        self.assertNotIn("expected_findings", text)
        self.assertNotIn("draft.json", text)


if __name__ == "__main__":
    unittest.main()

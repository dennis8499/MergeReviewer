"""The acceptance matrix must not manufacture passing model results."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests import merge_acceptance_matrix as matrix


class MergeAcceptanceMatrixTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="merge-matrix-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.jobs = matrix.jobs()
        for variant in ("baseline", "candidate"):
            skill = self.root / "skills" / variant
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(variant, encoding="utf-8")
        baseline_hash = matrix.skill_digest(self.root / "skills/baseline")
        patcher = mock.patch.object(matrix, "BASELINE_SKILL_SHA256", baseline_hash)
        patcher.start()
        self.addCleanup(patcher.stop)
        wrapper = self.root / "tools/agent_trace_cli.py"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text("# frozen test wrapper\n", encoding="utf-8")
        method = {"models": list(matrix.MODELS), "reasoning": matrix.REASONING,
                  "speed_model": matrix.SPEED_MODEL, "large_ratio_limit": .85, "scenario_ratio_limit": 1.05,
                  "fresh_agents": True, "sequential": True, "oracle_visible_to_reviewers": False,
                  "files_per_repo": 1000, "skill_hashes": {variant: matrix.skill_digest(self.root / "skills" / variant)
                                                            for variant in ("baseline", "candidate")},
                  "trace_wrapper_sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest()}
        matrix.save(self.root / "schedule.json", {"schema": "MergeReviewerAcceptanceMatrix/v1", "jobs": self.jobs,
                                                   "method": method})
        for case in matrix.QUALITY_CASES:
            matrix.save(self.root / "seeds" / case / "oracle.json",
                        {"secret_sentinels": ["SYNTHETIC-SECRET"] if case == "secrets" else []})

    def fill(self):
        for job in self.jobs:
            directory = self.root / "runs" / job["id"]
            report = directory / "reports" / "review.md"
            report.parent.mkdir(parents=True)
            report.write_text("Synthetic audit test report.\n", encoding="utf-8")
            elapsed = 100 if job["variant"] != "candidate" else (104 if job["scenario"] == "single" else 80)
            request = directory / "public-request.json"
            matrix.save(request, {"run_id": job["id"], "model": job["model"], "reasoning": job["reasoning"]})
            request_hash = hashlib.sha256(request.read_bytes()).hexdigest()
            matrix.save(directory / "started.json", {"schema": "MergeReviewerAcceptanceStart/v1",
                "run_id": job["id"], "request_sha256": request_hash, "started_at_unix": 1000,
                "started_monotonic": 100})
            completion = {
                "schema": "MergeReviewerAcceptanceCompletion/v1", "request_sha256": request_hash,
                "run_id": job["id"], "model": job["model"], "reasoning": job["reasoning"],
                "variant": job["variant"], "elapsed_seconds": elapsed, "report": "reports/review.md",
                "ended_at_unix": 1000 + elapsed, "ended_monotonic": 100 + elapsed,
                "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest()}
            if job["phase"] == "speed":
                trace = directory / "helper-trace.jsonl"
                events = [{"schema": "ReviewerHelperTrace/v5", "sequence": index,
                           "module": "review_session", "operation": operation, "elapsed_seconds": 1,
                           "git_calls": 1, "output_bytes": 10,
                           "full_result_validations": int(operation == "finish")}
                          for index, operation in enumerate(("start", "step", "finish"), 1)]
                trace.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
                completion["trace_sha256"] = hashlib.sha256(trace.read_bytes()).hexdigest()
                completion["helper_metrics"] = matrix.trace_metrics(trace, directory / "audit")
            matrix.save(directory / "completion.json", completion)
            matrix.save(directory / "audit.json", {
                "semantic": {"passed": True, "observations": ["Independent test assessment."],
                             "dimensions": {key: True for key in matrix.DIMENSIONS[job["case"]]}},
                "mechanical_checks": {key: True for key in matrix.CHECKS}})

    def row(self, **conditions):
        return next(job for job in self.jobs if all(job[key] == value for key, value in conditions.items()))

    def test_schedule_has_all_three_models_conditions_and_fixed_pairs(self):
        self.assertEqual(135, len(self.jobs))
        for model in matrix.MODELS:
            for variant in matrix.VARIANTS:
                self.assertEqual(set(matrix.QUALITY_CASES), {job["case"] for job in self.jobs
                    if job["phase"] == "quality" and job["model"] == model and job["variant"] == variant})
        self.assertTrue(all(job["reasoning"] == "high" for job in self.jobs))
        self.assertEqual({matrix.SPEED_MODEL}, {job["model"] for job in self.jobs if job["phase"] == "speed"})
        for scenario in ("local", "mixed", "single"):
            for repeat in (1, 2, 3):
                variants = [job["variant"] for job in self.jobs if job["phase"] == "speed"
                            and job["scenario"] == scenario and job["repeat"] == repeat]
                self.assertEqual(["candidate", "baseline"] if repeat == 2 else ["baseline", "candidate"], variants)

    def test_run_timer_and_parent_audit_preserve_original_receipts(self):
        job = self.row(phase="quality", model=matrix.MODELS[0], variant="no-skill", case="p0")
        directory = self.root / "runs" / job["id"]
        matrix.save(directory / "public-request.json", {"run_id": job["id"], "model": job["model"],
                                                          "reasoning": job["reasoning"]})
        matrix.start_run(self.root, job["id"])
        report = directory / "reports" / "review.md"
        report.parent.mkdir(parents=True)
        report.write_text("Manually reviewed fixture.\n", encoding="utf-8")
        matrix.complete_run(self.root, job["id"], report, actual_model=job["model"], actual_reasoning="high")
        parent_audit = self.root / "parent-audit.json"
        matrix.save(parent_audit, {"semantic": {"passed": True, "observations": ["Manual assessment."],
                                                "dimensions": {key: True for key in matrix.DIMENSIONS[job["case"]]}},
                                   "mechanical_checks": {key: True for key in matrix.CHECKS}})
        matrix.save_audit(self.root, job["id"], parent_audit)
        self.assertTrue(matrix.record(self.root, job)["passed"])
        with self.assertRaises(FileExistsError):
            matrix.start_run(self.root, job["id"])
        with self.assertRaises(FileExistsError):
            matrix.complete_run(self.root, job["id"], report, actual_model=job["model"], actual_reasoning="high")
        with self.assertRaises(FileExistsError):
            matrix.save_audit(self.root, job["id"], parent_audit)

    def test_blind_request_excludes_oracle_and_rejects_changed_frozen_skill(self):
        job = self.row(phase="quality", model=matrix.MODELS[0], variant="no-skill", case="p0")
        seed = self.root / "seeds/p0"
        project = seed / "workspace/orders-service"
        project.mkdir(parents=True)
        (project / "app.py").write_text("fixed source\n", encoding="utf-8")
        matrix.save(seed / "source-fixture.json", {"workspace": str(seed / "workspace"), "project": str(project),
                                                    "base": "main", "head": "feature/review"})
        request = matrix.prepare_run(self.root, job["id"])
        self.assertIsNone(request["skill_dir"])
        self.assertFalse((Path(request["workspace"]) / "oracle.json").exists())
        self.assertNotIn("oracle", str(request))
        (self.root / "skills/candidate/SKILL.md").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Frozen acceptance skills"):
            matrix.evaluate(self.root)
        (self.root / "skills/candidate/SKILL.md").write_text("candidate", encoding="utf-8")
        (self.root / "tools/agent_trace_cli.py").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "trace wrapper"):
            matrix.evaluate(self.root)

    def test_missing_results_and_failed_semantics_cannot_pass(self):
        self.assertFalse(matrix.evaluate(self.root)["model_gates_passed"])
        self.fill()
        result = matrix.evaluate(self.root)
        self.assertTrue(result["model_gates_passed"])
        self.assertEqual(.8, result["large_paired_ratio_median"])
        self.assertEqual(1.04, result["scenarios"]["single"]["paired_ratio_median"])
        no_skill = self.row(phase="quality", model=matrix.MODELS[0], variant="no-skill", case="safe")
        audit_path = self.root / "runs" / no_skill["id"] / "audit.json"
        audit = matrix.read(audit_path)
        audit["semantic"]["passed"] = False
        audit["semantic"]["dimensions"]["safety_false_positives"] = False
        matrix.save(audit_path, audit)
        self.assertTrue(matrix.evaluate(self.root)["quality"][matrix.MODELS[0]]["gate"],
                        "A recorded no-skill failure remains a comparison, not a candidate failure")
        candidate = self.row(phase="quality", model=matrix.MODELS[0], variant="candidate", case="p0")
        audit_path = self.root / "runs" / candidate["id"] / "audit.json"
        audit = matrix.read(audit_path)
        audit["semantic"]["passed"] = False
        audit["semantic"]["dimensions"]["required_findings"] = False
        matrix.save(audit_path, audit)
        self.assertFalse(matrix.evaluate(self.root)["quality"][matrix.MODELS[0]]["gate"])

    def test_invalid_pairs_secrets_and_unstable_reports_are_never_hidden(self):
        self.fill()
        speed = self.row(phase="speed", scenario="local", variant="baseline", repeat=1)
        path = self.root / "runs" / speed["id"] / "completion.json"
        completion = matrix.read(path)
        completion["model"] = "different-provider-model"
        matrix.save(path, completion)
        result = matrix.evaluate(self.root)
        self.assertEqual(5, result["large_valid_pairs"])
        self.assertFalse(result["speed_gate"])
        secret = self.row(phase="quality", model=matrix.MODELS[1], variant="candidate", case="secrets")
        directory = self.root / "runs" / secret["id"]
        report = directory / "reports" / "review.md"
        report.write_text("SYNTHETIC-SECRET", encoding="utf-8")
        completion_path = directory / "completion.json"
        completion = matrix.read(completion_path)
        completion["report_sha256"] = hashlib.sha256(report.read_bytes()).hexdigest()
        matrix.save(completion_path, completion)
        result = matrix.evaluate(self.root)
        row = next(item for item in result["rows"] if item["id"] == secret["id"])
        self.assertFalse(row["passed"])
        self.assertTrue(row["issues"])
        report.write_text("A later edit", encoding="utf-8")
        row = next(item for item in matrix.evaluate(self.root)["rows"] if item["id"] == secret["id"])
        self.assertFalse(row["audited"])

    def test_full_review_requires_unchanged_complete_helper_trace(self):
        self.fill()
        job = self.row(phase="speed", scenario="mixed", variant="candidate", repeat=2)
        trace = self.root / "runs" / job["id"] / "helper-trace.jsonl"
        trace.unlink()
        result = matrix.evaluate(self.root)
        self.assertEqual(5, result["large_valid_pairs"])
        self.assertFalse(result["speed_gate"])

    def test_trace_reports_distinct_helper_phases_and_unknown_network_wait(self):
        audit = self.root / "audit"
        response = audit / "cli/start.json"
        matrix.save(response, {"network_activity": {"fetch_attempt_count": 0}})
        events = [{"schema": "ReviewerHelperTrace/v5", "sequence": 1, "module": "review_session",
                   "operation": "start", "elapsed_seconds": 2, "git_calls": 2, "output_bytes": 10,
                   "full_result_validations": 0, "stdout_file": str(response)},
                  {"schema": "ReviewerHelperTrace/v5", "sequence": 2, "module": "review_session",
                   "operation": "step", "elapsed_seconds": 3, "git_calls": 1, "output_bytes": 20,
                   "stderr_bytes": 1, "full_result_validations": 0},
                  {"schema": "ReviewerHelperTrace/v5", "sequence": 3, "module": "review_session",
                   "operation": "finish", "elapsed_seconds": 4, "git_calls": 1, "output_bytes": 30,
                   "full_result_validations": 1}]
        trace = self.root / "helper-trace.jsonl"
        trace.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        result = matrix.trace_metrics(trace, audit)
        self.assertEqual((2, 3, 4), (result["start_seconds"], result["step_seconds"], result["finish_seconds"]))
        self.assertEqual(61, result["helper_output_bytes"])
        self.assertEqual(0, result["network_wait_seconds"])
        self.assertIsNone(result["model_analysis_seconds"])
        matrix.save(response, {"network_activity": {"fetch_attempt_count": 1}})
        self.assertIsNone(matrix.trace_metrics(trace, audit)["network_wait_seconds"])
        matrix.save(response, {"network_activity": {"fetch_attempt_count": 1},
                               "metrics": {"phase_seconds": {"waiting-network": 2.5}}})
        self.assertEqual(2.5, matrix.trace_metrics(trace, audit)["network_wait_seconds"])

    def test_actual_completion_captures_full_review_trace(self):
        job = self.row(phase="speed", scenario="mixed", variant="candidate", repeat=1)
        directory = self.root / "runs" / job["id"]
        matrix.save(directory / "public-request.json", {"run_id": job["id"], "model": job["model"],
                                                          "reasoning": job["reasoning"]})
        matrix.start_run(self.root, job["id"])
        report = directory / "reports/review.md"
        report.parent.mkdir(parents=True)
        report.write_text("Audited review.\n", encoding="utf-8")
        trace = directory / "helper-trace.jsonl"
        events = [{"schema": "ReviewerHelperTrace/v5", "sequence": index, "module": "review_session",
                   "operation": operation, "elapsed_seconds": 1, "git_calls": 1,
                   "output_bytes": 42 if operation == "finish" else 0,
                   "full_result_validations": int(operation == "finish")}
                  for index, operation in enumerate(("start", "step", "finish"), 1)]
        trace.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        completion = matrix.complete_run(self.root, job["id"], report,
                                         actual_model=job["model"], actual_reasoning=job["reasoning"])
        self.assertEqual(42, completion["helper_metrics"]["helper_output_bytes"])
        parent_audit = self.root / "speed-audit.json"
        matrix.save(parent_audit, {"semantic": {"passed": True, "observations": ["Manual assessment."],
                                                "dimensions": {key: True for key in matrix.DIMENSIONS[job["case"]]}},
                                   "mechanical_checks": {key: True for key in matrix.CHECKS}})
        matrix.save_audit(self.root, job["id"], parent_audit)
        self.assertTrue(matrix.record(self.root, job)["passed"])

    def test_scenario_regression_fails_even_if_large_median_passes(self):
        self.fill()
        for repeat in (1, 2):
            job = self.row(phase="speed", scenario="mixed", variant="candidate", repeat=repeat)
            path = self.root / "runs" / job["id"] / "completion.json"
            value = matrix.read(path)
            value["elapsed_seconds"] = 110
            value["ended_at_unix"] = 1110
            value["ended_monotonic"] = 210
            matrix.save(path, value)
        result = matrix.evaluate(self.root)
        self.assertLessEqual(result["large_paired_ratio_median"], .85)
        self.assertFalse(result["scenarios"]["mixed"]["gate"])
        self.assertFalse(result["speed_gate"])
        schedule = matrix.read(self.root / "schedule.json")
        schedule["jobs"].pop()
        matrix.save(self.root / "schedule.json", schedule)
        with self.assertRaisesRegex(ValueError, "required matrix"):
            matrix.evaluate(self.root)


if __name__ == "__main__":
    unittest.main()

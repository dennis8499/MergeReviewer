"""Keep source-only acceptance workloads honest and their oracle isolated."""
import tempfile
from pathlib import Path
import unittest

from tests import agent_acceptance as acceptance


class AgentAcceptanceSourceTests(unittest.TestCase):
    def test_source_chains_execute_and_the_comparison_exposes_a_real_regression(self):
        class Bus:
            def __init__(self):
                self.handlers = {}

            def subscribe(self, topic, handler):
                self.handlers.setdefault(topic, []).append(handler)

            def send(self, topic, payload):
                return [handler(payload) for handler in self.handlers.get(topic, [])]

        def namespace(source):
            result = {}
            exec(compile(source, "<acceptance-source>", "exec"), result)
            return result

        for number in range(1, 5):
            with self.subTest(chain=number):
                bus = Bus()
                relay = namespace(acceptance.initial_sources(f"relay-{number}")["src/relay.py"])
                consumer = namespace(acceptance.initial_sources(f"consumer-{number}")["src/consumer.py"])
                relay["bootstrap"](bus)
                consumer["bootstrap"](bus)
                source = acceptance.initial_sources("orders-api")[f"src/chain_{number}.py"]
                self.assertEqual([[700]], namespace(source)["publish"](bus, 7))
                changed = source.replace('{"amount": amount}', '{"total": amount}')
                with self.assertRaisesRegex(KeyError, "amount"):
                    namespace(changed)["publish"](bus, 7)
        local = acceptance.initial_sources("orders-api")["src/formatters.py"]
        self.assertEqual(namespace(local)["display_amount"](7.5),
                         namespace(local.replace("local_total", "display_total"))["display_amount"](7.5))

    def test_missing_runs_can_never_pass_the_acceptance_gate(self):
        with tempfile.TemporaryDirectory(prefix="review-agent-gate-") as temporary:
            root = Path(temporary)
            acceptance.save(root / "schedule.json", {"method": {}, "jobs": [
                {"id": "run-001", "reviewer": "merge", "variant": "baseline", "scenario": "p0", "repeat": 1},
                {"id": "run-002", "reviewer": "release", "variant": "candidate", "scenario": "p0", "repeat": 1}]})
            result = acceptance.summarize(root)
            self.assertFalse(result["gates"]["merge"]["accepted"])
            self.assertFalse(result["gates"]["release"]["speed_pass"])
            self.assertFalse(acceptance.read(root / "acceptance-results.json")["accepted"])

    def test_failed_or_missing_reports_never_supply_a_speed_sample(self):
        with tempfile.TemporaryDirectory(prefix="review-agent-pair-") as temporary:
            root = Path(temporary)
            jobs = []
            for scenario in (*acceptance.QUALITY, "local", "mixed"):
                for repeat in (range(1, 4) if scenario in ("local", "mixed") else (1,)):
                    for variant in ("baseline", "candidate"):
                        identifier = f"run-{len(jobs) + 1:03}"
                        job = {"id": identifier, "reviewer": "merge", "variant": variant,
                               "scenario": scenario, "repeat": repeat}
                        jobs.append(job)
                        directory = root / "runs" / identifier
                        acceptance.save(directory / "completion.json", {"elapsed_seconds": 10 if variant == "baseline" else 5,
                            "reports": ["audited.md"], "full_result_validations": 1})
                        acceptance.save(directory / "quality-audit.json", {"passed": True,
                            "semantic": {"passed": True}, "mechanical_checks": {key: True for key in (
                                "single_report", "fixed_source_shas", "no_secret_sentinel", "no_post_publish_edits",
                                "published_semantic_outcome")}})
            acceptance.save(root / "schedule.json", {"method": {}, "jobs": jobs})
            self.assertTrue(acceptance.summarize(root)["gates"]["merge"]["accepted"])
            baseline = next(job for job in jobs if job["scenario"] == "local" and job["variant"] == "baseline")
            audit_file = root / "runs" / baseline["id"] / "quality-audit.json"
            audit = acceptance.read(audit_file)
            audit["passed"] = False
            audit["mechanical_checks"]["single_full_result_validation"] = False
            acceptance.save(audit_file, audit)
            gates = acceptance.summarize(root)["gates"]["merge"]
            self.assertTrue(gates["accepted"], "A baseline workflow failure is retained, not imposed as a new candidate guarantee")
            self.assertEqual(5, gates["full_gate_pair_count"])
            failed = next(job for job in jobs if job["scenario"] == "local" and job["variant"] == "baseline")
            directory = root / "runs" / failed["id"]
            acceptance.save(directory / "completion.json", {"elapsed_seconds": 1, "reports": [], "full_result_validations": 0})
            acceptance.save(directory / "quality-audit.json", {"passed": False, "semantic": {"passed": False},
                "mechanical_checks": {}})
            gates = acceptance.summarize(root)["gates"]["merge"]
            self.assertEqual(6, len(gates["attempted_large_pairs"]))
            self.assertEqual(5, gates["large_pair_count"])
            self.assertEqual(23, gates["successful_reports"])
            self.assertFalse(gates["speed_pass"])
            self.assertFalse(gates["scenarios"]["local"]["non_regression_pass"])
            self.assertFalse(gates["accepted"])
            repaired = next(job for job in jobs if job["scenario"] == "mixed" and job["variant"] == "candidate")
            audit_file = root / "runs" / repaired["id"] / "quality-audit.json"
            audit = acceptance.read(audit_file)
            audit["passed"] = False
            audit["mechanical_checks"]["single_full_result_validation"] = False
            acceptance.save(audit_file, audit)
            gates = acceptance.summarize(root)["gates"]["merge"]
            self.assertEqual(5, gates["large_pair_count"], "Repairs remain in valid report timings")
            self.assertEqual(4, gates["full_gate_pair_count"])

    def test_passing_mechanics_cannot_override_a_failed_semantic_audit(self):
        with tempfile.TemporaryDirectory(prefix="review-agent-oracle-") as temporary:
            root = Path(temporary)
            directory = root / "runs" / "run-001"
            seed = root / "parent-seed"
            acceptance.save(root / "schedule.json", {"jobs": [{"id": "run-001", "seed": str(seed),
                "reviewer": "merge", "variant": "candidate", "scenario": "p1"}]})
            acceptance.save(seed / "oracle.json", {"secret_sentinels": []})
            acceptance.save(seed / "source-fixture.json", {"project": str(root / "workspace" / "service"),
                "base_sha": "a" * 40, "head_sha": "b" * 40})
            acceptance.save(directory / "audit" / "attempts" / "publish-001" / "manifest.json",
                {"repositories": [{"path": str(root / "workspace" / "service"),
                  "native": {"base_sha": "a" * 40, "head_sha": "b" * 40}}]})
            acceptance.save(directory / "runtime" / "jobs" / "unit.json",
                {"review_outcome": {"schema": "MergeReviewOutcome/v1", "review_complete": True, "gaps": []}})
            report = directory / "audit" / "reports" / "report.md"
            report.parent.mkdir(parents=True)
            report.write_text("Unit fixture report\n", encoding="utf-8")
            acceptance.save(directory / "completion.json", {"reports": [str(report)], "context_removed": True,
                "post_publish_edits": [], "zero_git_checks": True, "full_result_validations": 1,
                "git_operations": {}})
            semantic = root / "manual.json"
            acceptance.save(semantic, {"passed": False, "observations": ["Expected consumer defect was omitted."]})
            result = acceptance.audit_run(root, "run-001", semantic)
            self.assertTrue(all(result["mechanical_checks"].values()))
            self.assertFalse(result["passed"])
            acceptance.save(semantic, {"passed": True, "observations": ["Finding matches the source oracle."]})
            acceptance.save(directory / "audit" / "attempts" / "publish-001" / "manifest.json",
                {"repositories": [{"path": str(root / "workspace" / "service"),
                  "native": {"base_sha": "a" * 40, "head_sha": "c" * 40}}]})
            mismatched = acceptance.audit_run(root, "run-001", semantic)
            self.assertFalse(mismatched["mechanical_checks"]["fixed_source_shas"])
            self.assertFalse(mismatched["passed"])
            acceptance.save(directory / "audit" / "attempts" / "publish-001" / "manifest.json",
                {"repositories": [{"path": str(root / "workspace" / "service"),
                  "native": {"base_sha": "a" * 40, "head_sha": "b" * 40}}]})
            acceptance.save(directory / "runtime" / "jobs" / "unit.json",
                {"review_outcome": {"schema": "MergeReviewOutcome/v1", "review_complete": None,
                   "gaps": ["Semantic state unavailable."]}})
            unknown = acceptance.audit_run(root, "run-001", semantic)
            self.assertFalse(unknown["mechanical_checks"]["published_semantic_outcome"])
            self.assertFalse(unknown["passed"])

    def test_eighteen_repo_roles_and_source_chains_are_real(self):
        self.assertEqual(18, len(set(acceptance.NAMES)))
        for number in range(1, 5):
            relay = acceptance.initial_sources(f"relay-{number}")["src/relay.py"]
            consumer = acceptance.initial_sources(f"consumer-{number}")["src/consumer.py"]
            self.assertIn(f'orders.chain{number}', relay)
            self.assertIn(f'settlement.chain{number}', relay)
            self.assertIn(f'settlement.chain{number}', consumer)
            self.assertIn('payload["amount"]', consumer)

    def test_large_source_builder_preserves_shapes_and_external_oracle(self):
        with tempfile.TemporaryDirectory(prefix="review-agent-source-") as temporary:
            root = Path(temporary)
            for scenario, expected_changes in (("local", 3), ("mixed", 8)):
                fixture = acceptance.create_large(root / scenario, scenario, files_per_repo=12)
                self.assertEqual(18, len(fixture["repositories"]))
                self.assertEqual(expected_changes, len(fixture["changed_files"]))
                self.assertIsNone(fixture["review_draft"])
                self.assertFalse(list(Path(fixture["workspace"]).rglob("oracle.json")))
                oracle = acceptance.read(root / scenario / "oracle.json")
                self.assertEqual(0 if scenario == "local" else 4, len(oracle["expected"]["introduced_defects"]))
                if scenario == "mixed":
                    project = Path(fixture["project"])
                    self.assertIn("INVOICE_API_URL", (project / "src/deployment.py").read_text())
                    self.assertFalse((project / "docs/legacy.md").exists())


if __name__ == "__main__":
    unittest.main()

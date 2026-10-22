"""Guard the benchmark oracle against hiding source or report regressions."""
from pathlib import Path
import unittest

from tests import benchmark_review as benchmark
from tests import create_benchmark_fixture as fixture
from tests import benchmark_flow as flow_benchmark


class BenchmarkOracleTests(unittest.TestCase):
    def test_flow_normalization_removes_only_empty_additive_boundary_fields(self):
        original = {"changed_contracts": [{"id": "C-001"}], "searches": [{"query": "amount", "matches": ["fixed"]}]}
        added = {**original, "contract_scope_policy": "boundary-contracts/v1", "boundary_reviews": []}
        root = Path("unused")
        expected = flow_benchmark.comparable_artifacts(original, root)
        self.assertEqual(expected, flow_benchmark.comparable_artifacts(added, root))
        for changed in ({**added, "boundary_reviews": [{"outcome": "contained"}]},
                        {**added, "contract_scope_policy": "unknown"},
                        {**added, "searches": []},
                        {**added, "changed_contracts": [{"id": "C-001", "search_scope": "repo-local"}]}):
            self.assertNotEqual(expected, flow_benchmark.comparable_artifacts(changed, root))

    def test_flow_signature_keeps_frozen_objects_inventories_contracts_and_gates(self):
        manifest = {"repositories": [{"id": "repo-000", "sources": {"head": {
            "ref": "commit", "tree_sha": "tree", "files": [{"path": "source.py", "blob_sha": "blob"}]}}}],
            "required_coverage": [{"path": "source.py"}], "changed_contracts": [{"id": "C-001"}]}
        first = flow_benchmark.source_signature(manifest)
        for field in ("ref", "tree_sha", "files"):
            original = manifest["repositories"][0]["sources"]["head"][field]
            manifest["repositories"][0]["sources"]["head"][field] = "different"
            self.assertNotEqual(first, flow_benchmark.source_signature(manifest))
            manifest["repositories"][0]["sources"]["head"][field] = original
        manifest["required_coverage"] = []
        self.assertNotEqual(first, flow_benchmark.source_signature(manifest))

    def test_capture_instance_fields_are_ignored_but_source_identities_are_not(self):
        first = {"generated_at": "one", "captured_at": "one", "capture_completed_at": "one",
                 "run_id": "one", "context_sha256": "one", "objects": "C:/first/objects",
                 "blob_sha": "blob", "tree_sha": "tree", "sha256": "content", "ref": "commit"}
        second = {**first, "generated_at": "two", "captured_at": "two", "capture_completed_at": "two",
                  "run_id": "two", "context_sha256": "two", "objects": "C:/second/objects"}
        expected = benchmark.normalize(first, Path("C:/first"))
        self.assertEqual(expected, benchmark.normalize(second, Path("C:/second")))
        for field in ("blob_sha", "tree_sha", "sha256", "ref"):
            with self.subTest(field=field):
                changed = {**second, field: "different"}
                self.assertNotEqual(expected, benchmark.normalize(changed, Path("C:/second")))

    def test_only_capture_timestamp_line_is_normalized_in_markdown(self):
        one = "- 快照擷取：one → two（UTC）。\ncode content changed at one\n"
        two = "- 快照擷取：three → four（UTC）。\ncode content changed at one\n"
        self.assertEqual(benchmark.normalize(one, Path("unused")), benchmark.normalize(two, Path("unused")))
        self.assertNotEqual(benchmark.normalize(one, Path("unused")),
                            benchmark.normalize(two.replace("content", "behavior"), Path("unused")))

    def test_large_fixture_has_twelve_sources_and_four_distinct_chains(self):
        self.assertEqual(12, len(set(fixture.NAMES)))
        old = fixture.contract_files("orders-api")
        new = fixture.contract_files("orders-api", head=True)
        self.assertEqual(4, len(old))
        for number in range(1, 5):
            key = f"src/chain_{number}.py"
            self.assertIn('"amount"', old[key])
            self.assertIn('"total"', new[key])
            relay = fixture.contract_files(f"relay-{number}")["src/relay.py"]
            consumer = fixture.contract_files(f"consumer-{number}")["src/consumer.py"]
            self.assertIn(f'orders.chain{number}', relay)
            self.assertIn(f'settlement.chain{number}', relay)
            self.assertIn("bus.send(NEXT_TOPIC, payload)", relay)
            self.assertIn("bus.subscribe(SOURCE_TOPIC, lambda payload: forward(bus, payload))", relay)
            self.assertIn(f'settlement.chain{number}', consumer)
            self.assertIn("bus.subscribe(TOPIC, consume)", consumer)
            self.assertIn('payload["amount"]', consumer)
            self.assertIn("return register(bus)", relay)
            self.assertIn("return register(bus)", consumer)

    def test_generated_chains_execute_baseline_and_expose_changed_field_failure(self):
        class Bus:
            def __init__(self):
                self.handlers = {}

            def subscribe(self, topic, handler):
                self.handlers.setdefault(topic, []).append(handler)

            def send(self, topic, payload):
                return [handler(payload) for handler in self.handlers.get(topic, [])]

        def load(source):
            namespace = {}
            exec(compile(source, "<generated-fixture>", "exec"), namespace)
            return namespace

        for number in range(1, 5):
            with self.subTest(chain=number):
                bus = Bus()
                relay = load(fixture.contract_files(f"relay-{number}")["src/relay.py"])
                consumer = load(fixture.contract_files(f"consumer-{number}")["src/consumer.py"])
                consumer["bootstrap"](bus)
                relay["bootstrap"](bus)
                path = f"src/chain_{number}.py"
                old = load(fixture.contract_files("orders-api")[path])
                new = load(fixture.contract_files("orders-api", head=True)[path])
                self.assertEqual([[700]], old["bootstrap"](bus, 7))
                with self.assertRaisesRegex(KeyError, "amount"):
                    new["bootstrap"](bus, 7)


if __name__ == "__main__":
    unittest.main()

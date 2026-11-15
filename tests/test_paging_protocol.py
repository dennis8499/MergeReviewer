"""Behavior contracts for caller-confirmed continuation batches."""
from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.fixtures import SCRIPTS
import review_paging as paging
import review_runtime
import review_views


class PagingProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="paging-")
        self.addCleanup(self.temporary.cleanup)
        self.context = Path(self.temporary.name)
        self.root = self.context / "runtime"
        self.identifier = "a" * 32

    def link(self, artifact, pointer="", **options):
        return {"command": "review_session.py view", "runtime_root": str(self.root),
                "request_id": self.identifier, "read_only": True, "artifact": artifact,
                "artifact_scope": "context", "pointer": pointer, "offset": 0, "page_size": 100,
                **options}

    def reader(self, root, identifier, artifact, *, scope, pointer, offset, page_size, string_offset, cache):
        self.assertEqual(self.root, root)
        self.assertEqual(self.identifier, identifier)
        return review_views.artifact_view(self.context, artifact, pointer, offset, page_size,
                                          string_offset, scope, cache=cache), scope

    def state(self, links, **options):
        return paging.new_state(self.root, self.identifier, links, **options)

    def test_duplicate_pages_share_load_and_acknowledged_pages_do_not_read_again(self):
        artifact = review_runtime.store_view(self.context, {"first": 7, "second": 11})
        first = self.link(artifact, "/first")
        state = self.state([first, first, self.link(artifact, "/second")])
        before = {path.name: path.read_bytes() for path in (self.context / "views").iterdir()}
        with mock.patch.object(review_runtime, "load_view", wraps=review_runtime.load_view) as load, \
             mock.patch.object(Path, "write_text", side_effect=AssertionError("view wrote text")), \
             mock.patch.object(Path, "write_bytes", side_effect=AssertionError("view wrote bytes")):
            result = paging.execute(self.root, self.identifier, state, self.reader)
        self.assertEqual(1, load.call_count)
        self.assertEqual(["returned", "duplicate", "returned"], [row["status"] for row in result["results"]])
        self.assertEqual(state["pending"][0]["id"], result["results"][1]["duplicate_of"])
        self.assertEqual([7, 11], [result["results"][i]["result"]["value"] for i in (0, 2)])
        self.assertEqual(1, result["artifact_load_count"])
        self.assertEqual(0, result["git_commands"])
        self.assertEqual(before, {path.name: path.read_bytes() for path in (self.context / "views").iterdir()})
        confirmed = paging.apply_delta(state, result["paging_delta"])
        self.assertEqual([], confirmed["pending"])
        self.assertEqual(2, len(confirmed["acknowledged"]))
        confirmed["pending"] = [state["pending"][0]]
        with mock.patch.object(review_runtime, "load_view", side_effect=AssertionError("reread confirmed page")):
            repeated = paging.execute(self.root, self.identifier, confirmed, self.reader)
        self.assertEqual("already-returned", repeated["results"][0]["status"])

    def test_lost_output_repeats_same_batch_until_caller_applies_delta(self):
        artifact = review_runtime.store_view(self.context, ["original"])
        state = self.state([self.link(artifact)])
        original = copy.deepcopy(state)
        one = paging.execute(self.root, self.identifier, state, self.reader)
        two = paging.execute(self.root, self.identifier, state, self.reader)
        self.assertEqual(one, two)
        self.assertEqual(original, state)
        confirmed = paging.apply_delta(state, one["paging_delta"])
        self.assertEqual(confirmed, paging.apply_delta(confirmed, one["paging_delta"]))

    def test_nested_and_long_string_continuations_recover_all_required_content(self):
        text = "abc" * 4000
        artifact = review_runtime.store_view(self.context, [[text]])
        state = self.state([self.link(artifact)])
        recovered = ""
        offsets = []
        for _ in range(30):
            result = paging.execute(self.root, self.identifier, state, self.reader)
            self.assertLess(len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1,
                            paging.LIMIT)
            for row in result["results"]:
                page = row.get("result", {})
                if "text" in page:
                    recovered += page["text"]
                for item in page.get("items", []):
                    if isinstance(item, dict) and "preview" in item:
                        recovered += item["preview"]
            state = paging.apply_delta(state, result["paging_delta"])
            offsets.extend(row.get("string_offset", 0) for row in state["pending"])
            if not state["pending"]:
                break
        self.assertEqual(text, recovered)
        self.assertIn(2048, offsets)
        self.assertEqual([], state["blocked"])
        self.assertTrue(result["view_complete"])

    def test_source_content_named_continue_is_not_followed(self):
        fake = self.link("f" * 64, "/outside")
        artifact = review_runtime.store_view(self.context, {"continue": fake, "drilldowns": [fake]})
        state = self.state([self.link(artifact)])
        result = paging.execute(self.root, self.identifier, state, self.reader)
        self.assertEqual([], result["paging_delta"]["new_pending"])
        self.assertEqual(fake, result["results"][0]["result"]["value"]["continue"])
        self.assertTrue(result["view_complete"])

    def test_large_dictionary_field_pages_remain_reachable_after_each_acknowledgement(self):
        original = {"field-" + str(index): str(index) + "x" * 120 for index in range(80)}
        artifact = review_runtime.store_view(self.context, original)
        state = self.state([self.link(artifact)])
        recovered, batches = {}, 0
        while state["pending"] and batches < 10:
            result = paging.execute(self.root, self.identifier, state, self.reader)
            for row in result["results"]:
                for field in row.get("result", {}).get("value", {}).get("fields", []):
                    recovered.update(field)
            state = paging.apply_delta(state, result["paging_delta"])
            batches += 1
        self.assertEqual(original, recovered)
        self.assertEqual(4, batches)
        self.assertEqual([], state["pending"])
        self.assertEqual([], state["blocked"])
        self.assertTrue(result["view_complete"])

    def test_byte_budget_preserves_unreturned_tail_and_does_not_print_large_history(self):
        self.root = self.context / ("\u6e2c\u8a66\u8def\u5f91" * 6)
        artifact = review_runtime.store_view(self.context, {str(i): "\u6587" * 180 for i in range(8)})
        state = self.state([self.link(artifact, "/" + str(i)) for i in range(8)])
        state["acknowledged"] = [f"{i:064x}" for i in range(3000)]
        original_ids = [row["id"] for row in state["pending"]]
        returned = []
        for _ in range(12):
            result = paging.execute(self.root, self.identifier, state, self.reader, limit=2400)
            self.assertLess(len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1, 2400)
            self.assertGreater(len(result["results"]), 0)
            self.assertNotIn("acknowledged", result)
            before = state["pending"]
            confirmed_count = len(result["paging_delta"]["processed_ids"])
            state = paging.apply_delta(state, result["paging_delta"])
            self.assertEqual(before[confirmed_count:], state["pending"])
            returned.extend(row["id"] for row in result["results"])
            if not state["pending"]:
                break
        self.assertEqual(original_ids, returned)
        self.assertEqual(3008, len(state["acknowledged"]))

    def test_oversized_single_page_remains_pending_without_acknowledgement(self):
        state = self.state([self.link("b" * 64)])
        reader = mock.Mock(return_value=({"text": "z" * 10000, "view_complete": True}, "context"))
        result = paging.execute(self.root, self.identifier, state, reader, limit=1600)
        self.assertEqual([], result["results"])
        self.assertEqual([], result["paging_delta"]["processed_ids"])
        self.assertEqual("evidence.paging-page-too-large", result["code"])
        self.assertTrue(result["recoverable"])
        self.assertEqual(state["pending"], paging.apply_delta(state, result["paging_delta"])["pending"])

    def test_final_cache_count_digit_growth_preserves_wire_budget_and_pending_prefix(self):
        requests = [self.link(f"{index:064x}") for index in range(1, 11)]
        state = self.state(requests)
        calls = []
        def reader(root, identifier, artifact, *, cache, **options):
            calls.append(artifact)
            cache[artifact] = True
            return {"text": "x" * (40000 if len(cache) == 10 else 40), "view_complete": True}, "context"
        probe = paging.execute(self.root, self.identifier, state, reader)
        self.assertEqual(9, len(probe["results"]))
        # A 9-load prefix plus CRLF exactly fits; the actual tenth tentative
        # artifact load adds one metadata digit and must trigger a final rollback.
        limit = len(json.dumps(probe, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
        calls.clear()
        result = paging.execute(self.root, self.identifier, state, reader, limit=limit)
        wire = io.BytesIO()
        stream = io.TextIOWrapper(wire, encoding="utf-8", newline="\r\n")
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), file=stream)
        stream.flush()
        raw = wire.getvalue()
        self.assertTrue(raw.endswith(b"\r\n"))
        self.assertLessEqual(len(raw), limit)
        self.assertEqual(10, len(calls))
        self.assertEqual(len(calls), result["artifact_load_count"])
        self.assertEqual(8, len(result["results"]))
        self.assertEqual([row["id"] for row in state["pending"][:8]], result["paging_delta"]["processed_ids"])
        confirmed = paging.apply_delta(state, result["paging_delta"])
        self.assertEqual(state["pending"][8:], confirmed["pending"])
        self.assertEqual(2, result["pending_count"])

    def test_bad_inputs_are_blocked_once_and_do_not_hide_valid_rows(self):
        artifact = review_runtime.store_view(self.context, ["valid"])
        bad = [self.link(artifact, request_id="b" * 32),
               self.link(artifact, runtime_root=str(self.context / "other")),
               self.link("a" * 65), self.link(artifact, offset=9000),
               self.link(artifact, command="python arbitrary.py"),
               self.link(artifact, read_only=False), self.link(artifact, pointer="/bad~2token")]
        state = self.state(bad + [self.link(artifact)])
        result = paging.execute(self.root, self.identifier, state, self.reader)
        self.assertEqual(["evidence.invalid-view-request"] * len(bad),
                         [row["code"] for row in result["results"][:-1]])
        self.assertEqual(["valid"], result["results"][-1]["result"]["items"])
        confirmed = paging.apply_delta(state, result["paging_delta"])
        self.assertEqual([], confirmed["pending"])
        self.assertEqual(len(bad), len(confirmed["blocked"]))
        with mock.patch.object(review_runtime, "load_view", side_effect=AssertionError("retried blocked request")):
            repeated = paging.execute(self.root, self.identifier, confirmed, self.reader)
        self.assertEqual([], repeated["results"])
        self.assertFalse(repeated["view_complete"])

    def test_identity_and_stale_or_reordered_deltas_are_rejected(self):
        artifact = review_runtime.store_view(self.context, [1, 2])
        state = self.state([self.link(artifact), self.link(artifact, offset=1)], source_identity="fixed-one")
        with self.assertRaisesRegex(ValueError, "request_id differs"):
            paging.execute(self.root, "b" * 32, state, self.reader)
        with self.assertRaisesRegex(ValueError, "fixed snapshot"):
            paging.validate_state(state, source_identity="fixed-two")
        result = paging.execute(self.root, self.identifier, state, self.reader)
        bad = copy.deepcopy(result["paging_delta"])
        bad["processed_ids"].reverse()
        with self.assertRaisesRegex(ValueError, "ordered prefix"):
            paging.apply_delta(state, bad)
        changed = copy.deepcopy(state)
        changed["pending"][0]["page_size"] = 1
        with self.assertRaisesRegex(ValueError, "current state"):
            paging.apply_delta(changed, result["paging_delta"])

    def test_existing_pending_continuation_is_not_enqueued_twice(self):
        artifact = review_runtime.store_view(self.context, ["a", "b", "c"])
        state = self.state([self.link(artifact, page_size=1), self.link(artifact, offset=1, page_size=1)])
        result = paging.execute(self.root, self.identifier, state, self.reader)
        confirmed = paging.apply_delta(state, result["paging_delta"])
        self.assertEqual([2], [row["offset"] for row in confirmed["pending"]])
        self.assertEqual(2, len(confirmed["acknowledged"]))

    def test_empty_state_needs_no_reader_and_has_no_semantic_coverage_claim(self):
        state = self.state([])
        reader = mock.Mock(side_effect=AssertionError("empty queue read an artifact"))
        result = paging.execute(self.root, self.identifier, state, reader)
        self.assertTrue(result["complete"])
        self.assertTrue(result["view_complete"])
        self.assertEqual([], result["results"])
        self.assertNotIn("review_complete", result)
        self.assertNotIn("coverage", result)
        self.assertEqual([], paging.apply_delta(state, result["paging_delta"])["pending"])

    def test_nonadvancing_continuation_and_changed_scope_block_instead_of_looping(self):
        link = self.link("d" * 64)
        state = self.state([link])
        for label, returned in (("same-page", ({"continue": copy.deepcopy(link), "view_complete": False}, "context")),
                                ("changed-scope", ({"value": "wrong scope", "view_complete": True}, "runtime"))):
            with self.subTest(case=label):
                result = paging.execute(self.root, self.identifier, state, mock.Mock(return_value=returned))
                self.assertEqual("evidence.invalid-view-request", result["results"][0]["code"])
                self.assertNotIn("result", result["results"][0])
                confirmed = paging.apply_delta(state, result["paging_delta"])
                self.assertEqual([], confirmed["pending"])
                self.assertEqual([], confirmed["acknowledged"])
                self.assertEqual(1, len(confirmed["blocked"]))

    def test_canonical_keys_include_identity_scope_and_string_offset_and_reject_boolean_cursors(self):
        original = self.link("e" * 64)
        key = paging.page_key(original)
        for field, value in (("request_id", "b" * 32), ("runtime_root", str(self.context / "other")),
                             ("artifact_scope", "runtime"), ("string_offset", 2048), ("page_size", 20)):
            with self.subTest(field=field):
                self.assertNotEqual(key, paging.page_key({**original, field: value}))
        for field, value in (("offset", False), ("string_offset", True), ("page_size", 0), ("page_size", 1001)):
            with self.subTest(invalid_field=field, value=value):
                with self.assertRaises(ValueError):
                    paging.normalize({**original, field: value}, self.root, self.identifier)
        with self.assertRaises(ValueError):
            paging.page_key("not an object", self.root, self.identifier)

    def test_delta_cannot_silently_drop_processed_pages_without_acknowledgement(self):
        state = self.state([self.link("b" * 64, "/lines")])
        delta = {"schema": paging.DELTA, "base_state_sha256": paging.digest(state),
                 "processed_ids": [state["pending"][0]["id"]], "acknowledged": [],
                 "new_pending": [], "blocked": []}
        with self.assertRaisesRegex(ValueError, "needs acknowledgement"):
            paging.apply_delta(state, delta)
        self.assertEqual(1, len(state["pending"]))
        self.assertEqual([], state["acknowledged"])

    def test_delta_cannot_acknowledge_an_unprocessed_page_or_inject_foreign_pending(self):
        artifact = review_runtime.store_view(self.context, [9])
        state = self.state([self.link(artifact)])
        result = paging.execute(self.root, self.identifier, state, self.reader)
        delta = copy.deepcopy(result["paging_delta"])
        delta["acknowledged"].append("f" * 64)
        with self.assertRaisesRegex(ValueError, "unprocessed page"):
            paging.apply_delta(state, delta)
        delta = copy.deepcopy(result["paging_delta"])
        delta["new_pending"].append(self.link(artifact, request_id="b" * 32))
        with self.assertRaisesRegex(ValueError, "request_id differs"):
            paging.apply_delta(state, delta)


if __name__ == "__main__":
    unittest.main()

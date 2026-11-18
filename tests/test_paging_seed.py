"""Prepared projections carry unique bodies and original, independently checkable anchors."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tests.fixtures import api
import review_flow as flow
import review_paging as paging
import review_runtime as runtime
from review_views import artifact_view


class PagingSeedTests(unittest.TestCase):
    def test_large_shared_reads_and_patch_files_return_every_line_once_with_original_sources(self):
        with tempfile.TemporaryDirectory(prefix="paging-seed-") as temporary:
            root = Path(temporary)
            identifier = "1" * 32
            context = root / "sessions" / identifier
            context.mkdir(parents=True)
            lines = [{"line": i, "text": "source-" + str(i) + ":" + "x" * 180} for i in range(1, 301)]
            sha = hashlib.sha256(runtime.canonical(lines)).hexdigest()
            operations = []
            for number, source in enumerate(("head", "merge-preview")):
                operations.append({"id": "read-" + str(number), "artifact": "a" * 64,
                    "result": {"repo": "repo-000", "source": source, "ref": str(number + 1) * 40,
                               "path": "service.py", "sha256": sha, "lines": lines}})
            patch = [{"line": 1, "text": "diff --git a/one.py b/one.py"},
                     {"line": 2, "text": "-old"}, {"line": 3, "text": "+new"},
                     {"line": 4, "text": 'diff --git "a/two space.py" "b/two space.py"'},
                     {"line": 5, "text": "Binary files differ"}]
            operations.append({"id": "patch", "artifact": "b" * 64,
                               "result": {"path": "native/diff.patch", "lines": patch}})
            value = {"operations": operations, "complete": True, "next_index": 3}
            original = copy.deepcopy(value)
            response = flow.compact(value, context)
            self.assertLess(len(runtime.canonical(response)), flow.LIMIT)
            self.assertEqual(original, value)
            self.assertEqual(value, runtime.load_view(context, response["artifact"]))
            projection = runtime.load_view(context, response["paging_projection"]["artifact"])
            first, second = projection["operations"][:2]
            self.assertEqual(first["result"]["content_id"], second["result"]["content_id"])
            self.assertNotEqual(first["result"]["ref"], second["result"]["ref"])
            self.assertEqual(["head", "merge-preview"], [first["result"]["source"], second["result"]["source"]])
            files = projection["operations"][2]["result"]["file_index"]
            self.assertEqual([(1, 3), (4, 5)], [(item["line_start"], item["line_end"]) for item in files])
            self.assertEqual(patch, [line for item in files for line in projection["contents"][item["content_id"]]["lines"]])
            self.assertEqual(3, len(projection["contents"]))
            seed_path = Path(response["paging_requests_file"])
            saved = seed_path.read_bytes()
            state = json.loads(saved)
            self.assertEqual([], state["acknowledged"])
            self.assertEqual(4, len(state["pending"]))
            returned = {}

            def read(_root, _id, artifact, *, scope, pointer, offset, page_size, string_offset, cache):
                page = artifact_view(context, artifact, pointer, offset, page_size, string_offset, scope, cache=cache)
                if pointer.startswith("/contents/"):
                    returned.setdefault(pointer, []).extend(row["line"] for row in page.get("items", []))
                return page, scope

            for _ in range(100):
                before = copy.deepcopy(state)
                batch = paging.execute(root, identifier, state, read)
                self.assertEqual(before, state)
                self.assertLessEqual(len(runtime.canonical(batch)) + 2, flow.LIMIT)
                self.assertEqual(0, batch["git_commands"])
                state = paging.apply_delta(state, batch["paging_delta"])
                if batch["view_complete"]:
                    break
            else:
                self.fail("projection paging did not finish")
            self.assertEqual(saved, seed_path.read_bytes())
            self.assertEqual([], state["pending"])
            self.assertEqual([], state["blocked"])
            content_pointer = "/contents/" + first["result"]["content_id"] + "/lines"
            self.assertEqual(list(range(1, 301)), returned[content_pointer])
            self.assertEqual(305, sum(len(rows) for rows in returned.values()))

    def test_replaying_unacknowledged_seed_returns_same_page_and_keeps_legacy_views(self):
        with tempfile.TemporaryDirectory(prefix="paging-replay-") as temporary:
            root = Path(temporary)
            identifier = "2" * 32
            context = root / "sessions" / identifier
            context.mkdir(parents=True)
            value = {"operations": [{"id": "raw", "result": {"path": "diff.patch", "lines": [
                {"line": n, "text": "diff --git a/a.py b/a.py" if n == 1 else "x" * 220}
                for n in range(1, 301)]}}]}
            response = flow.compact(value, context)
            state = json.loads(Path(response["paging_requests_file"]).read_text(encoding="utf-8"))

            def read(_root, _id, artifact, *, scope, pointer, offset, page_size, string_offset, cache):
                return artifact_view(context, artifact, pointer, offset, page_size, string_offset, scope, cache=cache), scope

            first = paging.execute(root, identifier, state, read)
            second = paging.execute(root, identifier, state, read)
            self.assertEqual(first, second)
            self.assertEqual([], state["acknowledged"])
            legacy = artifact_view(context, response["artifact"], "/operations/0/result/lines", 0, 10)
            self.assertEqual(value["operations"][0]["result"]["lines"][:10], legacy["items"])

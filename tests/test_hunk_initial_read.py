"""Frozen first-read windows and complete incremental view transport parity."""
from __future__ import annotations

from collections import defaultdict
from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.fixtures import ReviewFixture, api, workspace
from tests.support import run_git as git, write_text
import review_flow as flow
import review_hunks as hunks
import review_paging as paging
import review_runtime as runtime
import review_session
from review_views import artifact_view


class HunkRangeTests(unittest.TestCase):
    def test_seed_offsets_match_raw_patch_and_invalid_counts_fall_back(self):
        path = b"sample.txt"
        patch = (b"diff --git a/sample.txt b/sample.txt\n"
                 b"index abcdef0..fedcba0 100644\n--- a/sample.txt\n+++ b/sample.txt\n"
                 b"@@ -10,2 +10,2 @@\n same\n-old\n+new\n"
                 b"@@ -100 +100 @@\n-old\n+new\n")
        change = {"old_blob": "abcdef0" + "a" * 33, "new_blob": "fedcba0" + "b" * 33,
                  "additions": 2, "deletions": 2}
        header = b"diff --git a/sample.txt b/sample.txt"
        section = hunks.patch_sections(io.BytesIO(patch))[header][0]
        self.assertEqual(([(10, 11), (100, 100)], [(10, 11), (100, 100)]),
                         hunks.intervals(section, path, change))
        self.assertEqual([(10, 70), (150, 151)], hunks.merge_ranges([(10, 11), (50, 70), (150, 151)]))
        for name, damaged in (("incomplete-body", section[:-1]),
                              ("zero-old-span", section[:3] + [b"@@ -10,0 +10 @@", b"+new"]),
                              ("different-blob", [b"index aaaaaaa..fedcba0 100644", *section[1:]])):
            with self.subTest(case=name):
                self.assertIsNone(hunks.intervals(damaged, path, change))

    def test_source_numbering_and_inaccurate_hunk_fall_back_but_exact_read_stays_strict(self):
        manifest = {"repositories": [{"id": "repo-001", "review_source": "head",
                                      "sources": {"head": {"ref": "b" * 40, "files": [
                                          {"path": "sample.txt", "kind": "blob", "blob_sha": "a" * 40}]}}}]}
        cache = mock.Mock()
        for name, content, end, reason in (("bare-cr", b"a\rb\rc", 2, "source-line-numbering"),
                                           ("utf16", "a\r\nb".encode("utf-16"), 2, "source-line-numbering"),
                                           ("range-overflow", b"a\nb\n", 99, "hunk-outside-source")):
            with self.subTest(case=name):
                cache.read.return_value = content
                def read(**bounds):
                    return workspace.read(Path.cwd(), manifest, "repo-001", "sample.txt", api, cache=cache, **bounds)

                full = read()
                bounded = read(hunk={"line_start": 1, "line_end": end})
                self.assertEqual({**full, "hunk_fallback": reason}, bounded)
        with self.assertRaisesRegex(ValueError, "exceeds fixed file line count"):
            read(line_start=1, line_end=99)
        with self.assertRaisesRegex(ValueError, "cannot accompany"):
            workspace.validate_read_request({"hunk": {"line_start": 1, "line_end": 2}, "line_end": 2})
        operation = {"id": "read-outside", "op": "read", "repo": "repo-001", "path": "sample.txt"}
        line_counts = {flow._line_count_key(manifest, operation): 2}
        self.assertIsNone(flow.operation_preflight([{**operation, "hunk": {"line_start": 1, "line_end": 99}}],
                                                   manifest, line_counts))
        invalid = flow.operation_preflight([{**operation, "line_start": 1, "line_end": 99}], manifest, line_counts)
        self.assertEqual("evidence.line-range-out-of-bounds", invalid["code"])

    def test_hunk_redaction_uses_context_before_the_window(self):
        manifest = {"repositories": [{"id": "repo-001", "review_source": "head",
                                      "sources": {"head": {"ref": "b" * 40}}}]}
        rows = ["public"] * 10 + ["-----BEGIN PRIVATE KEY-----"] + ["private payload"] * 65 + ["-----END PRIVATE KEY-----"]
        content = ("\n".join(rows) + "\n").encode()
        cache = mock.Mock()
        cache.read.return_value = content
        result = workspace.read(Path.cwd(), manifest, "repo-001", "sample.txt", api, cache=cache,
                                hunk={"line_start": 45, "line_end": 45})
        self.assertEqual((25, 65), (result["line_start"], result["line_end"]))
        self.assertTrue(result["sensitive_values_redacted"])
        self.assertEqual({"[私鑰內容已省略]"}, {row["text"] for row in result["lines"]})
        self.assertEqual(hashlib.sha256(content).hexdigest(), result["sha256"])

    def test_optional_seed_preserves_full_reads_for_unmapped_changes_and_io_failure(self):
        modes = {"old_mode": "100644", "new_mode": "100644"}
        old, new, different = "a" * 40, "b" * 40, "c" * 40
        paths = ("safe.txt", "different-base.txt", "parent.txt", "preview.txt", "image.bin", "rename.txt", "space name.txt")
        records = [{**modes, "path": path, "source": "head", "status": "M", "old_path": None,
                    "old_blob": old, "new_blob": new, "binary": False, "additions": 1, "deletions": 1}
                   for path in paths]
        records[4]["binary"] = True
        records[5].update(status="R095", old_path="old-name.txt")
        records.append({**records[3], "source": "merge-preview"})
        def source(ref, sha):
            return {"ref": ref, "files": [{"path": path, "kind": "blob", "mode": "100644", "blob_sha": sha}
                                         for path in paths]}

        sources = {"base": source("1" * 40, old), "merge-base": source("2" * 40, old),
                   "head": source("3" * 40, new), "merge-preview": source("4" * 40, new)}
        sources["base"]["files"][1]["blob_sha"] = different
        parent = "5" * 40
        sources["parent-" + parent] = source(parent, old)
        patch = "repos/repo-001/native/diff.patch"
        manifest = {"selected_repo": "repo-001", "repositories": [{"id": "repo-001", "sources": sources,
            "diff_base": sources["merge-base"]["ref"], "changed_files": records, "patches": [patch]}],
            "required_coverage": [{"repo": "repo-001", "source": "head", "path": path} for path in paths],
            "required_merge_views": [{"parent": parent, "parent_content_available": True, "paths": ["parent.txt"]}]}
        with tempfile.TemporaryDirectory(prefix="hunk-fallback-") as temporary:
            context = Path(temporary)
            patch_path = context / patch
            patch_path.parent.mkdir(parents=True)
            patch_path.write_bytes("".join(f"diff --git a/{name} b/{name}\nindex aaaaaaa..bbbbbbb 100644\n"
                f"--- a/{name}\n+++ b/{name}\n@@ -4 +4 @@\n-old\n+new\n" for name in paths).encode())
            full = flow.initial_read_requests(manifest)
            focused = hunks.initial_read_requests(context, manifest, full)
            self.assertIsNotNone(focused)
            self.assertEqual({"safe.txt"}, {request["path"] for request in focused if "hunk" in request})
            self.assertEqual([row for row in full if row.get("path") != "safe.txt"],
                             [row for row in focused if "hunk" not in row])
            with mock.patch.object(Path, "open", side_effect=PermissionError("captured patch unavailable")):
                self.assertIsNone(hunks.initial_read_requests(context, manifest, full))


def run_cli(argv):
    output, errors = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        try:
            code = review_session.main([str(value) for value in argv])
        except SystemExit as exc:
            code = exc.code
    raw = output.getvalue().encode("utf-8")
    return code, json.loads(raw) if raw else None, len(raw), errors.getvalue()


class PagingApplyCliTests(unittest.TestCase):
    def test_native_apply_checks_batch_topology_and_preserves_the_original_inputs(self):
        with tempfile.TemporaryDirectory(prefix="paging-apply-") as temporary:
            scratch = Path(temporary)
            root, identifier = scratch / "r", "a" * 32
            context = root / "sessions" / identifier
            context.mkdir(parents=True)
            content = runtime.store_view(context, {"lines": [10, 20]})
            requests = [{"id": "first", "artifact": content, "pointer": "/lines", "page_size": 1},
                        {"id": "same", "artifact": content, "pointer": "/lines", "page_size": 1},
                        {"id": "bad", "artifact": "invalid"}]
            state = paging.new_state(root, identifier, requests)
            source, captured, output = scratch / "state.json", scratch / "batch.json", scratch / "next.json"
            write_text(source, json.dumps(state))

            def view(_root, _id, key, *, scope, pointer, offset, page_size, string_offset, cache):
                return artifact_view(context, key, pointer, offset, page_size, string_offset, scope, cache=cache), scope

            batch = paging.execute(root, identifier, state, view)
            self.assertTrue(batch["complete"])
            self.assertFalse(batch["view_complete"])
            self.assertEqual(1, batch["pending_count"])
            self.assertEqual(1, batch["blocked_count"])
            original = json.dumps(batch)
            write_text(captured, original)
            args = ["paging-apply", "--state-file", source, "--batch-file", captured]
            code, result, _size, errors = run_cli([*args, "--output-file", output])
            self.assertEqual(0, code, errors)
            self.assertIsNone(result)
            next_state = json.loads(output.read_bytes())
            self.assertEqual(paging.apply_delta(state, batch["paging_delta"]), next_state)
            self.assertEqual(1, next_state["pending"][0]["offset"])
            self.assertEqual("bad", next_state["blocked"][0]["id"])
            self.assertEqual(2, run_cli(args)[0])
            self.assertEqual((0, None), run_cli([*args, "--output-file", output])[:2])
            self.assertEqual(next_state, json.loads(output.read_bytes()))
            self.assertEqual(state, json.loads(source.read_bytes()))
            self.assertEqual(batch, json.loads(captured.read_bytes()))
            for name, target in (("same-state", source), ("same-batch", captured),
                                 ("runtime-artifact", context / "next.json")):
                with self.subTest(case=name):
                    code, _, _, errors = run_cli([*args, "--output-file", target])
                    self.assertEqual(2, code)
                    self.assertIn("outside the review runtime", errors)
            malformed = copy.deepcopy(batch)
            malformed["paging_delta"]["new_pending"] = []
            write_text(captured, json.dumps(malformed))
            code, _, _, errors = run_cli([*args, "--output-file", output])
            self.assertEqual(2, code)
            self.assertIn("page topology", errors)
            self.assertEqual(next_state, json.loads(output.read_bytes()))


class PagingApplyGeometryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="paging-geometry-")
        self.addCleanup(temporary.cleanup)
        self.scratch = Path(temporary.name)
        self.root, self.identifier = self.scratch / "r", "a" * 32
        self.context = self.root / "sessions" / self.identifier
        self.context.mkdir(parents=True)

    def view(self, _root, _id, key, *, scope, pointer, offset, page_size, string_offset, cache):
        return artifact_view(self.context, key, pointer, offset, page_size, string_offset, scope, cache=cache), scope

    def batch(self, value, **bounds):
        artifact = runtime.store_view(self.context, value)
        state = paging.new_state(self.root, self.identifier, [{"artifact": artifact, **bounds}])
        return state, paging.execute(self.root, self.identifier, state, self.view)

    def test_cli_rejects_forged_array_and_unicode_offsets_with_matching_delta(self):
        for name, value, coordinates, expected, length in (
                ("array", [10, 20, 30], ("offset", "next_offset"), 1, 1),
                ("unicode", "\U0001f600" * 2050, ("string_offset", "next_string_offset"), 2048, 2048)):
            with self.subTest(case=name):
                coordinate, next_coordinate = coordinates
                state, batch = self.batch(value, page_size=1)
                lead = batch["results"][0]["result"]
                self.assertEqual(length, len(lead.get("items", lead.get("text", ""))))
                self.assertEqual(expected, lead[next_coordinate])
                saved_state = self.scratch / "state.json"
                saved_batch = self.scratch / "batch.json"
                output = self.scratch / "next.json"
                args = ["paging-apply", "--state-file", saved_state, "--batch-file", saved_batch,
                        "--output-file", output]
                write_text(saved_state, json.dumps(state))
                write_text(saved_batch, json.dumps(batch))
                code, _, _, errors = run_cli(args)
                self.assertEqual(0, code, errors)
                committed = output.read_bytes()
                current = json.loads(committed)
                self.assertEqual(expected, current["pending"][0][coordinate])
                payload = "items" if name == "array" else "text"
                delivered = list(lead[payload])
                for _ in range(2):
                    later = paging.execute(self.root, self.identifier, current, self.view)
                    delivered.extend(later["results"][0]["result"][payload])
                    write_text(saved_state, json.dumps(current))
                    write_text(saved_batch, json.dumps(later))
                    code, _, _, errors = run_cli(args)
                    self.assertEqual(0, code, errors)
                    current = json.loads(output.read_bytes())
                    if not current["pending"]:
                        break
                self.assertEqual([], current["pending"])
                self.assertEqual(list(value), delivered)

                lead[next_coordinate] = expected + 1
                lead["continue"][coordinate] = expected + 1
                following = paging.normalize(lead["continue"], self.root, self.identifier)
                following["id"] = "page-" + paging.page_key(following)
                batch["paging_delta"]["new_pending"] = [following]
                write_text(saved_state, json.dumps(state))
                write_text(saved_batch, json.dumps(batch))
                output.write_bytes(committed)
                before = saved_state.read_bytes()
                code, _, _, errors = run_cli(args)
                self.assertEqual(2, code)
                self.assertIn("continuation differs from returned", errors)
                self.assertEqual(before, saved_state.read_bytes())
                self.assertEqual(committed, output.read_bytes())

    def test_apply_accepts_empty_eof_budget_shortened_arrays_and_real_drilldowns(self):
        for name, value, bounds, predicate in (
                ("empty-array-at-end", [10], {"offset": 1}, lambda page: page["items"] == []),
                ("string-past-end", "abcd", {"string_offset": 5}, lambda page: page["text"] == ""),
                ("array-shortened-to-fit", ["s" * 1200] * 40, {"page_size": 40},
                 lambda page: 0 < len(page["items"]) < page["total"]),
                ("array-drilldown-without-successor", ["x" * 8000], {},
                 lambda page: bool(page["drilldowns"]) and page["next_offset"] is None),
                ("projected-object-successor", {str(index): "x" * 700 for index in range(40)}, {},
                 lambda page: any(link["pointer"] == page["pointer"] for link in page["drilldowns"])),
                ("raw-object-contents", {"continue": "literal", "fields": [], "omitted": True}, {},
                 lambda page: page["value"]["continue"] == "literal" and page["view_complete"])):
            with self.subTest(case=name):
                state, batch = self.batch(value, **bounds)
                page = batch["results"][0]["result"]
                self.assertTrue(predicate(page))
                self.assertLessEqual(len(runtime.canonical(batch)) + 2, paging.LIMIT)
                self.assertEqual(paging.apply_delta(state, batch["paging_delta"]), paging.apply_batch(state, batch))

    def test_array_apply_rejects_a_second_same_pointer_continuation(self):
        state, batch = self.batch([10, 20, 30], page_size=1)
        page = batch["results"][0]["result"]
        contradictory = {**page["continue"], "offset": 2}
        page["drilldowns"] = [contradictory]
        queued = paging.normalize(contradictory, self.root, self.identifier)
        queued["id"] = "page-" + paging.page_key(queued)
        batch["paging_delta"]["new_pending"].append(queued)
        batch["pending_count"] = 2
        with self.assertRaisesRegex(ValueError, "does not match the end"):
            paging.apply_batch(state, batch)


class HunkInitialFlowTests(ReviewFixture):
    def fixture(self, files, lines, center):
        originals, revised = {}, {}
        for index in range(files):
            path = f"service-{index:02}.txt"
            rows = [f"service {index:02} unchanged source line {number:04} " + "x" * 74
                    for number in range(1, lines + 1)]
            originals[path] = "\n".join(rows) + "\n"
            rows[center - 1] += " revised contract"
            revised[path] = "\n".join(rows) + "\n"
            (self.repo / path).write_bytes(originals[path].encode("utf-8"))
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "initial multi-line source")
        git(self.repo, "branch", "-f", "main", "HEAD")
        for path, value in revised.items():
            (self.repo / path).write_bytes(value.encode("utf-8"))
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "revise one line per file")
        return originals, revised

    def start_request(self, name):
        arguments = ["start", "--workspace", self.root, "--project", self.repo, "--base", "main",
                     "--head", "feature", "--runtime-root", self.root / name, "--draft-format", "compact"]
        with redirect_stderr(io.StringIO()):
            started = flow.start(flow.parser().parse_args([str(arg) for arg in arguments]))
        self.addCleanup(review_session.cleanup_if_owned, Path(started["context_dir"]))
        return started

    def read_seed(self, started, seed_field):
        context = Path(started["context_dir"])
        common = ["--runtime-root", started["runtime_root"], "--request-id", started["request_id"]]
        code, first, total_bytes, errors = run_cli(["step", *common, "--requests-file", started[seed_field]])
        self.assertEqual(0, code, errors)
        self.assertLessEqual(total_bytes + 1, flow.LIMIT)
        bodies = defaultdict(list)
        view_calls = 0
        if "paging_requests_file" not in first:
            reads = first["operations"]
            for row in reads:
                result = row["result"]
                if "content_id" in result:
                    result["lines"] = first["contents"][result["content_id"]]["lines"]
            return reads, view_calls, total_bytes, None

        seed = Path(first["paging_requests_file"])
        saved = seed.read_bytes()
        projected_artifact = first["paging_projection"]["artifact"]
        projection = runtime.load_view(context, projected_artifact)
        batch_file = self.root / "received.json"
        for turn in range(400):
            code, batch, view_size, errors = run_cli(["view", *common, "--read-only", "--requests-file", seed])
            self.assertIn(code, (0, 3), errors)
            self.assertNotIn("error", batch)
            self.assertTrue(all("error" not in row for row in batch["results"]))
            self.assertLessEqual(view_size + 1, flow.LIMIT)
            self.assertEqual(0, batch["git_commands"])
            total_bytes += view_size
            view_calls += 1
            for row in batch["results"]:
                if row.get("status") == "returned":
                    page = row["result"]
                    bodies[page["pointer"]].extend(page.get("items", []))
            write_text(batch_file, json.dumps(batch))
            next_seed = self.root / f"next-{turn % 2}.json"
            code, _empty, apply_size, errors = run_cli(["paging-apply", "--state-file", seed,
                                                       "--batch-file", batch_file, "--output-file", next_seed])
            self.assertEqual(0, code, errors)
            total_bytes += apply_size
            seed = next_seed
            if batch["view_complete"]:
                break
        else:
            self.fail("paging did not complete")
        state = json.loads(seed.read_bytes())
        self.assertEqual(([], []), (state["pending"], state["blocked"]))
        self.assertEqual(saved, Path(first["paging_requests_file"]).read_bytes())
        self.assertEqual([row["id"] for row in projection["operations"]],
                         [row["id"] for row in bodies["/operations"]])
        for key, content in projection["contents"].items():
            self.assertEqual(content["lines"], bodies[f"/contents/{key}/lines"])
        for row in projection["operations"]:
            result = row["result"]
            if "content_id" in result:
                result["lines"] = bodies[f"/contents/{result['content_id']}/lines"]
        return projection["operations"], view_calls, total_bytes, state

    def author_and_check(self, started, rows, center, state=None):
        context = Path(started["context_dir"])
        common = ["--runtime-root", started["runtime_root"], "--request-id", started["request_id"]]
        draft_path = Path(started["draft_path"])
        original_draft = draft_path.read_bytes()
        expected_hash = hashlib.sha256(original_draft).hexdigest()
        operations_file = self.root / "author.json"
        if state is not None:
            write_text(operations_file, json.dumps([{"id": "checkpoint-only", "op": "author",
                "expected_draft_sha256": expected_hash, "expected_checkpoint_revision": 0, "updates": {},
                "checkpoint": {"paging": state, "next_action": "Read and judge the relevant code."}}]))
            code, result, _, errors = run_cli(["step", *common, "--requests-file", operations_file])
            self.assertEqual(0, code, (errors, result))
            self.assertEqual(original_draft, draft_path.read_bytes())
            code, check, _, errors = run_cli(["check", *common])
            self.assertEqual(3, code, (errors, check))
            self.assertFalse(check["ready_for_finish"])
        manifest = workspace.load(context)
        evidence = {(row["result"]["source"], row["result"]["path"]): row["id"] for row in rows
                    if row["result"].get("sha256") and row["result"].get("source")}
        coverage = []
        for change in manifest["boundary_changes"]:
            root = change["root"]
            source = root["source"]
            coverage.append({"change_id": change["id"], "status": "reviewed",
                "reason": "Compared the changed line with the fixed original and final source.",
                "evidence": [{"read_id": evidence[(source, root["path"])], "line_start": center, "line_end": center}]})
        write_text(operations_file, json.dumps([{"id": "review-meaning", "op": "author",
            "expected_draft_sha256": expected_hash, "updates": {"coverage": coverage,
                "summary": "Reviewed the fixed source and its changed value."}}]))
        code, result, _, errors = run_cli(["step", *common, "--requests-file", operations_file])
        self.assertEqual(0, code, (errors, result))
        code, check, _, errors = run_cli(["check", *common])
        self.assertEqual(0, code, (errors, check))
        self.assertTrue(check["ready_for_finish"])
        code, finish, _, errors = run_cli(["finish", *common])
        self.assertEqual(0, code, (errors, finish))
        self.assertTrue(finish["review_outcome"]["review_complete"])
        return manifest["required_coverage"], check["expected_outcome"]

    def test_single_line_seed_reduces_paging_bytes_and_preserves_the_verified_outcome(self):
        self.fixture(1, 2000, 1001)
        baseline, candidate = self.start_request("full"), self.start_request("hunk")
        full_rows, full_views, full_bytes, _ = self.read_seed(baseline, "initial_reads_file")
        seed_rows, seed_views, seed_bytes, _ = self.read_seed(candidate, "initial_hunk_reads_file")
        self.assertLess(seed_views, full_views)
        self.assertLess(seed_bytes, full_bytes)
        self.assertLess(1 + 2 * seed_views, 1 + 2 * full_views)
        full = {row["result"]["source"]: row["result"] for row in full_rows if row["result"].get("source")}
        bounded = {row["result"]["source"]: row["result"] for row in seed_rows if row["result"].get("source")}
        self.assertEqual(full.keys(), bounded.keys())
        for source, result in bounded.items():
            self.assertEqual((978, 1024), (result["line_start"], result["line_end"]))
            self.assertEqual(full[source]["lines"][977:1024], result["lines"])
            self.assertEqual(full[source]["sha256"], result["sha256"])
            self.assertEqual(full[source]["ref"], result["ref"])
        self.assertEqual(self.author_and_check(baseline, full_rows, 1001),
                         self.author_and_check(candidate, seed_rows, 1001))

    def test_optional_hunk_write_failure_retains_the_ready_full_handoff(self):
        self.fixture(1, 70, 35)
        atomic = flow.coordination.atomic
        def fail_optional(path, value):
            atomic(path, value)
            if path.name == "initial-hunk-reads.json":
                raise OSError("optional seed persistence failed")

        with mock.patch.object(flow.coordination, "atomic", side_effect=fail_optional):
            started = self.start_request("fallback")
        self.assertEqual("ready", started["state"])
        self.assertTrue((Path(started["context_dir"]) / "initial-hunk-reads.json").is_file())
        self.assertNotIn("initial_hunk_reads_file", started)
        self.assertNotIn("initial_hunk_read_count", started)
        summary = flow.status(Path(started["runtime_root"]), started["request_id"], summary=True)
        self.assertEqual("initial-evidence", summary["resume_from"])
        handoff = summary["initial_evidence"]
        self.assertEqual(started["initial_reads_file"], handoff["initial_reads_file"])
        self.assertNotIn("initial_hunk_reads_file", handoff)
        self.assertNotIn("initial_hunk_read_count", handoff)
        rows, _, _, _ = self.read_seed({**started, **handoff}, "initial_reads_file")
        head = next(row["result"] for row in rows if row["result"].get("source") == "head")
        self.assertEqual(started["repositories"][0]["sources"]["head"]["ref"], head["ref"])
        self.assertEqual(70, len(head["lines"]))

    def test_status_omits_an_invalid_optional_seed_and_keeps_the_full_seed_and_guide(self):
        self.fixture(1, 70, 35)
        started = self.start_request("optional")
        root, identifier = Path(started["runtime_root"]), started["request_id"]
        path = Path(started["initial_hunk_reads_file"])
        original = path.read_bytes()
        full = Path(started["initial_reads_file"])
        before = full.read_bytes()
        initial = flow.status(root, identifier, summary=True)["initial_evidence"]
        self.assertEqual(started["initial_hunk_read_count"], initial["initial_hunk_read_count"])
        invalid = {"malformed": b"{", "incorrect-count": b"[]"}
        for name, kind in (("invalid-operation", "unknown"), ("array-operation", []), ("object-operation", {})):
            invalid_operations = json.loads(original)
            invalid_operations[0]["op"] = kind
            invalid[name] = json.dumps(invalid_operations).encode("utf-8")
            diagnostic = flow.operation_preflight(invalid_operations)
            self.assertEqual("authoring.unsupported-operation", diagnostic["code"])
            self.assertEqual("/operations/0/op", diagnostic["pointer"])
        is_link = workspace.is_link
        for name in ("missing", *invalid, "unsafe-link"):
            with self.subTest(case=name):
                if name == "missing":
                    path.unlink()
                else:
                    path.write_bytes(invalid.get(name, original))
                with mock.patch.object(workspace, "is_link", side_effect=(
                        lambda candidate: (name == "unsafe-link" and candidate == path) or is_link(candidate))), \
                     mock.patch.object(api, "run_git", side_effect=AssertionError("status ran Git")), \
                     mock.patch.object(flow.coordination, "atomic", side_effect=AssertionError("status wrote state")):
                    summary = flow.status(root, identifier, summary=True)
                self.assertEqual("initial-evidence", summary["resume_from"])
                self.assertEqual({key: value for key, value in initial.items() if not key.startswith("initial_hunk_")},
                                 summary["initial_evidence"])
                self.assertEqual(before, full.read_bytes())

    def test_thirty_two_files_page_every_hunk_before_the_author_sets_coverage(self):
        before, after = self.fixture(32, 95, 48)
        started = self.start_request("wide")
        rows, view_calls, total_bytes, state = self.read_seed(started, "initial_hunk_reads_file")
        self.assertGreater(view_calls, 1)
        self.assertGreater(total_bytes, flow.LIMIT)
        evidence = [row["result"] for row in rows if row["result"].get("source")]
        self.assertEqual(32, len({row["path"] for row in evidence}))
        for row in evidence:
            text = (before if row["source"] == "base" else after)[row["path"]]
            expected = [{"line": index, "text": line} for index, line in enumerate(text.splitlines(), 1)
                        if 25 <= index <= 71]
            self.assertEqual(expected, row["lines"])
            self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(), row["sha256"])
        assert state is not None
        required, outcome = self.author_and_check(started, rows, 48, state)
        self.assertEqual(32, len(required))
        self.assertTrue(outcome["review_complete"])

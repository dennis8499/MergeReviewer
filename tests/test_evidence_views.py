"""Bounded frozen reads and filtered artifact views retain full evidence identity."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from unittest import mock

from tests.fixtures import ReviewFixture, SCRIPTS, api, workspace
from tests.support import run_python
import review_runtime


class EvidenceViewTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.text = "contract = 'new'\nsecond = '值'\ncontract = 'third'\nlast = True\n"
        self.commit(self.text)
        self.commit("", relative="empty.txt")
        self.context, self.manifest = self.capture()
        self.repo_id = self.manifest["selected_repo"]

    def cli(self, *arguments):
        return run_python(SCRIPTS / "review_workspace.py", *arguments,
                          "--context-dir", str(self.context))

    def artifact(self):
        rows = []
        for query in ("contract", "second"):
            rows.extend(workspace.search(self.context, self.manifest, query, [self.repo_id], api)["searches"])
        value = {"searches": rows}
        page = review_runtime.page(value, self.context, size=1)
        return value, page

    def test_range_preserves_original_lines_ref_and_full_file_hash(self):
        full = workspace.read(self.context, self.manifest, self.repo_id, "app.py", api)
        bounded = workspace.read(self.context, self.manifest, self.repo_id, "app.py", api,
                                 line_start=2, line_end=3)
        self.assertEqual(full["lines"][1:3], bounded["lines"])
        self.assertEqual([2, 3], [line["line"] for line in bounded["lines"]])
        for key in ("repo", "source", "ref", "path", "sha256"):
            self.assertEqual(full[key], bounded[key])
        self.assertEqual(hashlib.sha256(self.text.encode("utf-8")).hexdigest(), bounded["sha256"])
        self.assertEqual((2, 3, 4), (bounded["line_start"], bounded["line_end"], bounded["total_lines"]))
        self.assertNotIn("line_start", full)
        self.assertEqual(self.manifest, workspace.load(self.context))

    def test_batch_ranges_share_verified_blob_but_not_distinct_excerpts(self):
        request = {"repo": self.repo_id, "path": "app.py"}
        requests = [request, {**request, "line_start": 1, "line_end": 2},
                    {**request, "line_start": 3, "line_end": 4},
                    {**request, "line_start": 1, "line_end": 2}]
        with mock.patch.object(api, "run_git", wraps=api.run_git) as calls:
            value = workspace.batch_read(self.context, self.manifest, requests, api)
        self.assertEqual(1, calls.call_count)
        shared = review_runtime.shared_reads(value)
        self.assertEqual(3, len(shared["contents"]))
        ids = [row["content_id"] for row in shared["reads"]]
        self.assertEqual(ids[1], ids[3])
        self.assertNotEqual(ids[1], ids[2])
        self.assertEqual(value["reads"][2]["lines"], shared["contents"][ids[2]]["lines"])
        self.assertEqual(value["reads"][0]["sha256"], ids[0])
        self.assertEqual(self.manifest, workspace.load(self.context))

    def test_invalid_bounds_and_empty_files_fail_without_changing_evidence(self):
        for start, end in ((1, None), (None, 1), (0, 1), (-1, 1), (3, 2),
                           (True, 2), (1, False), (1.0, 2), ("1", 2), (1, 5)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                workspace.read(self.context, self.manifest, self.repo_id, "app.py", api,
                               line_start=start, line_end=end)
        empty = workspace.read(self.context, self.manifest, self.repo_id, "empty.txt", api)
        self.assertEqual([], empty["lines"])
        with self.assertRaisesRegex(ValueError, "line count \\(0\\)"):
            workspace.read(self.context, self.manifest, self.repo_id, "empty.txt", api,
                           line_start=1, line_end=1)
        for fields in ({"line_start": 1}, {"line_end": 1}, {"line_start": None, "line_end": None},
                       {"line_start": True, "line_end": 2}):
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, "integers"):
                workspace.batch_read(self.context, self.manifest,
                    [{"repo": self.repo_id, "path": "app.py", **fields}], api)
        self.assertEqual(self.manifest, workspace.load(self.context))

    def test_cli_read_and_batch_ranges_are_consistent(self):
        single = self.cli("read", "--repo", self.repo_id, "--path", "app.py",
                          "--line-start", "2", "--line-end", "3")
        self.assertEqual(0, single.returncode, single.stderr)
        requests = self.context / "range-requests.json"
        requests.write_text(json.dumps([{"repo": self.repo_id, "path": "app.py",
                                         "line_start": 2, "line_end": 3}]), encoding="utf-8")
        batch = self.cli("batch-read", "--requests-file", str(requests), "--shared-content")
        self.assertEqual(0, batch.returncode, batch.stderr)
        shared = json.loads(batch.stdout)
        expected = json.loads(single.stdout)
        row = shared["reads"][0]
        self.assertEqual(expected, {**{key: item for key, item in row.items() if key != "content_id"},
                                   "lines": shared["contents"][row["content_id"]]["lines"]})
        bad = self.cli("read", "--repo", self.repo_id, "--path", "app.py", "--line-start", "1")
        self.assertEqual(2, bad.returncode)
        self.assertEqual("", bad.stdout)
        self.assertIn("supplied together", bad.stderr)
        bad = self.cli("inventory", "--line-start", "1", "--line-end", "2")
        self.assertEqual(2, bad.returncode)
        self.assertIn("line arguments require read", bad.stderr)

    def test_filtered_view_preserves_artifact_and_ledger_and_pages_selected_matches(self):
        value, page = self.artifact()
        artifact = self.context / "views" / (page["artifact"] + ".json")
        before_artifact = artifact.read_bytes()
        before_manifest = (self.context / "manifest.json").read_bytes()
        first, second = [row["id"] for row in value["searches"]]
        result = self.cli("view", "--artifact", page["artifact"], "--search-id", first,
                          "--page-size", "1", "--offset", "1")
        self.assertEqual(0, result.returncode, result.stderr)
        bounded = json.loads(result.stdout)
        self.assertEqual(page["artifact"], bounded["artifact"])
        rows = bounded["result"]["searches"]
        self.assertEqual([first], [row["id"] for row in rows])
        self.assertEqual(value["searches"][0]["matches"][1:2], rows[0]["matches"])
        self.assertEqual(2, rows[0]["view"]["total_matches"])
        result = self.cli("view", "--artifact", page["artifact"], "--search-id", second,
                          "--search-id", first, "--search-id", second, "--page-size", "100")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([first, second], [row["id"] for row in json.loads(result.stdout)["result"]["searches"]])
        self.assertEqual(before_artifact, artifact.read_bytes())
        self.assertEqual(before_manifest, (self.context / "manifest.json").read_bytes())
        self.assertEqual(value, review_runtime.load_view(self.context, page["artifact"]))

    def test_view_errors_are_explicit_and_never_rerun_search_or_transport(self):
        value, page = self.artifact()
        before = (self.context / "manifest.json").read_bytes()
        arguments = ["view", "--context-dir", str(self.context), "--artifact", page["artifact"],
                     "--search-id", value["searches"][0]["id"], "--page-size", "1"]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(workspace, "search", side_effect=AssertionError("must use saved artifact")), \
                mock.patch.object(api.git_network_lease, "run", side_effect=AssertionError("must remain offline")), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(0, workspace.main(arguments))
        self.assertEqual("", stderr.getvalue())
        unknown = self.cli("view", "--artifact", page["artifact"], "--search-id", "S-missing", "--page-size", "1")
        self.assertEqual(2, unknown.returncode)
        self.assertIn("Unknown search ID in artifact: S-missing", unknown.stderr)
        wrong = review_runtime.store_view(self.context, {"reads": []})
        wrong = self.cli("view", "--artifact", wrong, "--search-id", "S-0001", "--page-size", "1")
        self.assertEqual(2, wrong.returncode)
        self.assertIn("search artifact", wrong.stderr)
        invalid = self.cli("search", "--query", "contract", "--search-id", "S-0001")
        self.assertEqual(2, invalid.returncode)
        self.assertIn("--search-id requires view", invalid.stderr)
        self.assertEqual(before, (self.context / "manifest.json").read_bytes())

    def test_unfiltered_view_is_unchanged_and_checksum_is_always_checked(self):
        value, page = self.artifact()
        unfiltered = self.cli("view", "--artifact", page["artifact"], "--page-size", "100")
        self.assertEqual(0, unfiltered.returncode, unfiltered.stderr)
        expected = review_runtime.page(value, self.context, size=100, artifact=page["artifact"])
        self.assertEqual(expected, json.loads(unfiltered.stdout))
        artifact = self.context / "views" / (page["artifact"] + ".json")
        artifact.write_text("{}", encoding="utf-8")
        invalid = self.cli("view", "--artifact", page["artifact"], "--search-id", "S-0001", "--page-size", "1")
        self.assertEqual(2, invalid.returncode)
        self.assertIn("checksum mismatch", invalid.stderr)

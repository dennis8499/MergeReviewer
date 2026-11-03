"""Sanitized reproductions of publication, authoring and paging failures in real logs.

These fixtures verify helper behavior; authored findings are test inputs, not a
claim about model review quality or the business code in the original logs.
"""
from __future__ import annotations

import copy
import ctypes
import errno
import json
import os
from pathlib import Path
import subprocess
import stat
import tempfile
import unittest
from unittest import mock

from tests.fixtures import ReviewFixture, SCRIPTS, api, workspace, review_session, workspace_report
from tests.support import run_python
import report_publication
import review_authoring
import review_coordinator as coordination
import review_diagnostics
import review_flow as flow
import review_runtime
from review_validation import fixed_line_count_key


class LogPagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="review-pages-")
        self.addCleanup(self.temporary.cleanup)
        self.context = Path(self.temporary.name)

    def page(self, artifact, **options):
        with mock.patch.object(api, "run_git", side_effect=AssertionError("view ran Git")):
            return flow.artifact_view(self.context, artifact, **options)

    def follow(self, link):
        return self.page(link["artifact"], pointer=link["pointer"], offset=link["offset"],
                         page_size=link["page_size"], string_offset=link.get("string_offset", 0))

    def test_long_string_in_array_stays_incomplete_until_all_characters_are_returned(self):
        original = "x" * 12000
        artifact = review_runtime.store_view(self.context, [original])
        page = self.page(artifact)
        self.assertFalse(page["view_complete"])
        item = page["items"][0]
        text = item["preview"]
        self.assertEqual(item["continue"], page["drilldowns"][0])
        link = item["continue"]
        while link:
            page = self.follow(link)
            text += page["text"]
            self.assertEqual(page["next_string_offset"] is None, page["view_complete"])
            link = page.get("continue")
        self.assertEqual(original, text)

    def test_large_dictionary_has_continuations_for_every_omitted_field(self):
        original = {f"field-{i}": "content" * 80 for i in range(100)}
        artifact = review_runtime.store_view(self.context, original)
        page = self.page(artifact)
        recovered = {}
        for _ in range(100):
            for row in page["value"]["fields"]:
                recovered.update(row)
            link = page["value"].get("continue")
            self.assertEqual(not bool(link), page["view_complete"])
            if not link:
                break
            page = self.follow(link)
        self.assertEqual(original, recovered)

    def test_nested_lists_and_many_search_matches_have_bounded_reachable_pages(self):
        matches = [f"file-{i}:" + "text" * 80 for i in range(1000)]
        artifact = review_runtime.store_view(self.context, [matches])
        page = self.page(artifact)
        self.assertFalse(page["view_complete"])
        link = page["items"][0]["continue"]
        found = []
        while link:
            page = self.follow(link)
            self.assertLess(len(review_runtime.canonical(page)), flow.LIMIT)
            found.extend(page["items"])
            link = page.get("continue")
        self.assertEqual(matches, found)

    def test_batch_reuses_verified_artifact_and_preserves_individual_results_without_writes(self):
        artifact = review_runtime.store_view(self.context, {"state": "reviewing", "hash": "a" * 64})
        requests = [{"id": name, "artifact": artifact, "pointer": "/" + name} for name in ("state", "hash")]
        before = sorted(path.name for path in (self.context / "views").iterdir())
        with mock.patch.object(flow, "artifact_location", return_value=(self.context, "context")), \
             mock.patch.object(review_runtime, "load_view", wraps=review_runtime.load_view) as load, \
             mock.patch.object(api, "run_git", side_effect=AssertionError("batch ran Git")):
            result = flow.batch_artifact_views(Path("."), "a" * 32, requests)
        self.assertEqual(1, load.call_count)
        self.assertEqual(1, result["artifact_load_count"])
        self.assertTrue(result["view_complete"])
        for row, request in zip(result["results"], requests):
            self.assertEqual(self.page(artifact, pointer=request["pointer"]), row["result"])
        self.assertEqual(before, sorted(path.name for path in (self.context / "views").iterdir()))

    def test_batch_backpressure_and_bad_request_preserve_other_results(self):
        artifact = review_runtime.store_view(self.context, {"text": "x" * 2048})
        requests = [{"artifact": artifact, "pointer": "/text"} for _ in range(30)]
        requests.insert(1, {"artifact": artifact, "pointer": "/does-not-exist"})
        rows, index = [], 0
        with mock.patch.object(flow, "artifact_location", return_value=(self.context, "context")):
            while index is not None:
                result = flow.batch_artifact_views(Path("."), "a" * 32, requests, offset=index)
                self.assertLess(len(review_runtime.canonical(result)), flow.LIMIT)
                rows.extend(result["results"])
                index = result["next_index"]
        self.assertEqual(len(requests), len(rows))
        self.assertEqual("evidence.invalid-view-request", rows[1]["code"])
        self.assertTrue(all(row["result"]["text"] == "x" * 2048 for i, row in enumerate(rows) if i != 1))


class LogAuthoringTests(unittest.TestCase):
    def fixture(self):
        source = {"ref": "a" * 40, "files": [{"path": "app.py", "kind": "blob", "blob_sha": "b" * 40}]}
        manifest = {"selected_repo": "repo-000", "seed_repos": ["repo-000"], "extension_enabled": True,
                    "required_coverage": [], "required_merge_views": [], "changed_contracts": [], "searches": [],
                    "repositories": [{"id": identifier, "state": "captured", "review_source": "head",
                                      "sources": {"head": copy.deepcopy(source)}}
                                     for identifier in ("repo-000", "repo-001")]}
        draft = {"schema": workspace.RESULT, "report_layout": "engineering-review/v1", "summary": "Reviewed fixture.",
                 **{name: [] for name in ("coverage", "impact_coverage", "findings", "merge_parent_review", "next_steps",
                                          "tests_executed", "tests_not_executed", "limitations", "boundary_reviews")},
                 "repository_coverage": [{"repo": "repo-000", "status": "seed", "reason": "Selected.", "search_ids": []},
                                         {"repo": "repo-001", "status": "not-related", "reason": "Excluded.", "search_ids": []}]}
        return manifest, draft

    def test_unanchored_related_repo_is_rejected_without_git_or_source_reads(self):
        manifest, draft = self.fixture()
        draft["repository_coverage"][1]["status"] = "related"
        with mock.patch.object(api, "run_git", side_effect=AssertionError("check ran Git")), \
             mock.patch.object(Path, "read_bytes", side_effect=AssertionError("check read a source")):
            result = review_diagnostics.inspect(draft, manifest)
        self.assertFalse(result["ready_for_finish"])
        self.assertEqual("/repository_coverage/1/status", result["errors"][0]["pointer"])
        self.assertEqual("unrelated Repo cannot be expanded without evidence", result["errors"][0]["message"])

    def test_cached_line_upper_bound_is_bound_to_blob_identity(self):
        manifest, draft = self.fixture()
        evidence = {"repo": "repo-000", "source": "head", "path": "app.py", "ref": "a" * 40,
                    "line_start": 1, "line_end": 99}
        root = {key: evidence[key] for key in ("repo", "source", "path")}
        manifest["required_coverage"] = [root]
        draft["coverage"] = [{**root, "status": "reviewed", "evidence": [evidence]}]
        counts = {fixed_line_count_key(manifest, evidence): 3}
        result = review_diagnostics.inspect(draft, manifest, line_counts=counts)
        self.assertTrue(any(row["message"] == "evidence line interval is outside the frozen file" for row in result["errors"]))
        manifest["repositories"][0]["sources"]["head"]["files"][0]["blob_sha"] = "c" * 40
        result = review_diagnostics.inspect(draft, manifest, line_counts=counts)
        self.assertFalse(any(row["message"] == "evidence line interval is outside the frozen file" for row in result["errors"]))

    def test_finding_and_pending_evidence_cannot_silently_expand_excluded_repos(self):
        manifest, draft = self.fixture()
        seed = {"repo": "repo-000", "source": "head", "path": "app.py", "ref": "a" * 40,
                "line_start": 1, "line_end": 1}
        root = {key: seed[key] for key in ("repo", "source", "path")}
        manifest["required_coverage"] = [root]
        draft["coverage"] = [{**root, "status": "reviewed", "evidence": [seed]}]
        peer = {**seed, "repo": "repo-001"}
        draft["findings"] = [{"id": "F-001", "priority": "P2", "evidence": [seed, peer]}]
        draft["pending_confirmations"] = [{"id": "Q-001", "evidence": [peer]}]
        with mock.patch.object(api, "run_git", side_effect=AssertionError("check ran Git")):
            result = review_diagnostics.inspect(draft, manifest)
        errors = {(row["pointer"], row["message"]) for row in result["errors"]}
        self.assertIn(("/findings/0/evidence", "finding evidence cannot silently expand excluded Repos"), errors)
        self.assertIn(("/pending_confirmations/0/evidence", "pending evidence cannot expand excluded Repos"), errors)

    def test_compact_errors_identify_each_finding_citation_and_available_lines(self):
        manifest, _ = self.fixture()
        manifest["operations"] = {"read-1": {"op": "read", "artifact": "a" * 64}}
        draft = {"schema": review_authoring.SCHEMA, "findings": [
            {"trigger": "compact trigger", "technical_trigger": "different trigger", "fix": "compact fix",
             "recommendation": "different fix", "evidence": [{"read_id": "read-1", "line_start": 1, "line_end": 2}]},
            {"evidence": [{"read_id": "a" * 64, "line_start": 1, "line_end": 1}]}]}
        read = {"repo": "repo-000", "source": "head", "path": "app.py", "ref": "a" * 40, "sha256": "b" * 64,
                "lines": [{"line": i, "text": "source"} for i in (10, 11, 15)]}
        with mock.patch.object(review_runtime, "load_view", return_value=read):
            result = flow.diagnostics(draft, manifest, Path("."))
        errors = {row["pointer"]: row for row in result["errors"]}
        self.assertEqual({"/findings/0/technical_trigger", "/findings/0/recommendation", "/findings/0/evidence/0",
                          "/findings/1/evidence/0/read_id"}, set(errors))
        self.assertEqual([[10, 11], [15, 15]], errors["/findings/0/evidence/0"]["available_line_ranges"])
        self.assertEqual(["read-1"], errors["/findings/1/evidence/0/read_id"]["available_read_ids"])
        self.assertFalse(result["ready_for_finish"])

    def test_summary_exposes_state_errors_gaps_and_next_action_in_one_bounded_response(self):
        value = {"request_id": "a" * 32, "runtime_root": "runtime", "state": "reviewing", "phase": "publication-repair",
                 "errors": ["output denied"], "evidence_receipt": "x" * 100000,
                 "resume": {"remaining": {"changes": [{"path": f"file-{i}", "reason": "x" * 10000} for i in range(100)]},
                            "diagnostics": {"errors": [], "gaps": ["x" * 10000] * 100}}}
        result = flow.status_summary(value)
        self.assertEqual("repair-output-and-finish", result["next_action"])
        self.assertEqual(100, result["remaining_counts"]["changes"])
        self.assertEqual(100, result["gap_count"])
        self.assertEqual(1, result["error_count"])
        self.assertNotIn("evidence_receipt", result)
        self.assertLess(len(review_runtime.canonical(result)), flow.LIMIT)


class PublicationPrimitiveTests(unittest.TestCase):
    def test_staging_collision_never_removes_an_existing_file(self):
        with tempfile.TemporaryDirectory(prefix="report-collision-") as directory:
            path = Path(directory) / ".merge-review-existing.tmp"
            path.write_bytes(b"keep")
            identifier = mock.Mock(hex="existing")
            with mock.patch.object(report_publication.uuid, "uuid4", return_value=identifier):
                with self.assertRaises(report_publication.PublicationError):
                    with report_publication.staged_report(directory, b"replacement"):
                        self.fail("stage replaced an existing file")
            self.assertEqual(b"keep", path.read_bytes())

    def test_permission_capacity_and_busy_output_errors_are_recoverable(self):
        with tempfile.TemporaryDirectory(prefix="report-errors-") as directory:
            for number in (errno.EACCES, errno.ENOSPC, errno.EBUSY):
                with self.subTest(errno=number), report_publication.staged_report(directory, b"report") as stage:
                    failure = OSError(number, "fixture output failure")
                    with mock.patch.object(report_publication.os, "link", side_effect=failure), \
                         mock.patch.object(report_publication.os, "rename", side_effect=failure):
                        with self.assertRaises(report_publication.PublicationError) as raised:
                            report_publication.commit_report(stage, Path(directory) / "output.md")
                    self.assertTrue(raised.exception.recoverable)
                    self.assertTrue(stage.is_file())
            self.assertEqual([], list(Path(directory).iterdir()))

    @unittest.skipIf(os.name == "nt", "POSIX file mode comparison")
    def test_staging_retains_normal_output_file_permissions(self):
        with tempfile.TemporaryDirectory(prefix="report-mode-") as directory:
            ordinary = Path(directory) / "normal.md"
            ordinary.write_bytes(b"normal")
            with report_publication.staged_report(directory, b"report") as stage:
                self.assertEqual(stat.S_IMODE(ordinary.stat().st_mode), stat.S_IMODE(stage.stat().st_mode))


class LogWorkflowTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.commit()
        environment = mock.patch.dict(os.environ, {"GITLAB_WORKSPACE_RUNTIME_DIR": str(self.root / "network")})
        environment.start()
        self.addCleanup(environment.stop)

    def start(self):
        result = flow.start(flow.parser().parse_args(["start", "--workspace", str(self.root),
            "--project", str(self.repo), "--base", "main", "--head", "feature"]))
        context = Path(result["context_dir"])
        self.addCleanup(review_session.cleanup_session, context)
        return result, Path(result["runtime_root"]), result["request_id"], context

    def author(self, context):
        manifest = workspace.load(context)
        draft = self.draft(context, manifest)
        draft["report_layout"] = "engineering-review/v1"
        coordination.atomic(context / "draft.json", draft)
        return manifest, draft

    def deny_output(self, root, identifier):
        failure = report_publication.PublicationError("commit", self.repo / "review-reports",
                                                       PermissionError(errno.EACCES, "fixture output denied"))
        with mock.patch.object(report_publication, "commit_report", side_effect=failure):
            result, code = flow.finish(root, identifier)
        self.assertEqual(3, code)
        self.assertEqual("publication-repair", result["phase"])
        self.assertTrue(result["context_retained"])
        return result

    def test_publication_retry_keeps_original_sha_draft_and_evidence_then_publishes_once(self):
        result, root, identifier, context = self.start()
        manifest, draft = self.author(context)
        before = {name: (context / name).read_bytes() for name in ("manifest.json", "draft.json")}
        self.deny_output(root, identifier)
        self.assertEqual(before, {name: (context / name).read_bytes() for name in before})
        self.assertEqual("repair-output-and-finish", flow.status(root, identifier, summary=True)["next_action"])
        self.commit("contract = 'later source, outside this review'\n")
        with mock.patch.object(api, "fetch_inputs", side_effect=AssertionError("retry fetched")), \
             mock.patch.object(workspace_report, "validate", wraps=workspace_report.validate) as validate:
            published, code = flow.finish(root, identifier)
        self.assertEqual(0, code)
        self.assertEqual(1, validate.call_count)
        self.assertTrue(published["review_complete"])
        head = workspace.repository(manifest, result["selected_repo"])["sources"]["head"]["ref"]
        self.assertIn(head[:8], Path(published["report_path"]).name)
        self.assertIn(head, Path(published["report_path"]).read_text(encoding="utf-8"))
        self.assertFalse(context.exists())
        repeated, code = flow.finish(root, identifier)
        self.assertEqual(0, code)
        self.assertEqual(published["report_path"], repeated["report_path"])
        self.assertEqual(1, len(list((self.repo / "review-reports").glob("*.md"))))

    def test_corrupted_retained_evidence_is_terminal_and_cancel_still_removes_retained_context(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                _, root, identifier, context = self.start()
                self.author(context)
                self.deny_output(root, identifier)
                if corrupt:
                    pack = next(context.glob("repos/*/snapshot.git/objects/pack/*.pack"))
                    pack.chmod(stat.S_IREAD | stat.S_IWRITE)
                    pack.write_bytes(pack.read_bytes() + b"tampered")
                    with self.assertRaises((ValueError, api.ReviewContextError, OSError)):
                        flow.finish(root, identifier)
                    self.assertEqual("failed", flow.status(root, identifier)["state"])
                else:
                    self.assertEqual("cancelled", flow.cancel(root, identifier)["state"])
                self.assertFalse(context.exists())

    def test_preflight_uses_the_same_output_commit_before_capture(self):
        with mock.patch.object(report_publication, "commit_report", side_effect=PermissionError(errno.EACCES, "denied")), \
             mock.patch.object(api, "build_manifest", side_effect=AssertionError("capture before output preflight")):
            with self.assertRaises(PermissionError):
                self.start()
        self.assertEqual([], list((self.repo / "review-reports").iterdir()))

    def test_check_and_finish_reject_unanchored_peer_before_expensive_validation(self):
        self.make_repo("peer", {"app.py": "unrelated = True\n"})
        _, root, identifier, context = self.start()
        _, draft = self.author(context)
        peer = next(row for row in draft["repository_coverage"] if row["status"] != "seed")
        peer["status"] = "related"
        coordination.atomic(context / "draft.json", draft)
        checked, code = flow.check(root, identifier)
        self.assertEqual(3, code)
        self.assertTrue(any(row["message"] == "unrelated Repo cannot be expanded without evidence" for row in checked["errors"]))
        with mock.patch.object(workspace_report, "validate", side_effect=AssertionError("late structural validation")):
            rejected, code = flow.finish(root, identifier)
        self.assertEqual(3, code)
        self.assertTrue(rejected["context_retained"])
        peer["status"] = "not-related"
        coordination.atomic(context / "draft.json", draft)
        published, code = flow.finish(root, identifier)
        self.assertEqual(0, code)
        self.assertTrue(published["review_complete"])

    def test_cli_batch_and_summary_preserve_manifest_operations_and_identity(self):
        result, root, identifier, context = self.start()
        capabilities = flow.capabilities()
        self.assertEqual(capabilities["skill_version"], result["tool_identity"]["skill_version"])
        self.assertEqual(capabilities["helper_sha256"], result["tool_identity"]["helper_sha256"])
        self.assertEqual(flow.tool_identity(), flow.status(root, identifier)["tool_identity"])
        artifact = review_runtime.store_view(context, {"state": "reviewing", "sha256": "a" * 64})
        requests = context / "views-input.json"
        coordination.atomic(requests, [{"artifact": artifact, "pointer": "/state"},
                                       {"artifact": artifact, "pointer": "/sha256"}])
        manifest = (context / "manifest.json").read_bytes()
        output = run_python(SCRIPTS / "review_session.py", "view", "--runtime-root", str(root),
                            "--request-id", identifier, "--requests-file", str(requests))
        self.assertEqual(0, output.returncode, output.stderr)
        batch = json.loads(output.stdout)
        self.assertTrue(batch["view_complete"])
        self.assertEqual(1, batch["artifact_load_count"])
        output = run_python(SCRIPTS / "review_session.py", "status", "--runtime-root", str(root),
                            "--request-id", identifier, "--summary")
        self.assertEqual(0, output.returncode, output.stderr)
        summary = json.loads(output.stdout)
        self.assertEqual("MergeReviewStatusSummary/v1", summary["schema"])
        self.assertEqual("continue-review", summary["next_action"])
        self.assertLess(len(output.stdout.encode("utf-8")), flow.LIMIT)
        self.assertEqual(manifest, (context / "manifest.json").read_bytes())

    @unittest.skipUnless(os.name == "nt", "Windows inherited ACL check")
    def test_report_stage_inherits_parent_acl_and_child_process_can_read_published_file(self):
        directory = self.repo / "review-reports"
        directory.mkdir()
        output = directory / "fixture.md"
        with report_publication.staged_report(directory, b"review\n") as stage:
            security = ctypes.WinDLL("advapi32", use_last_error=True)
            get_security = security.GetFileSecurityW
            get_security.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p,
                                     ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
            get_security.restype = ctypes.c_int
            needed = ctypes.c_uint32()
            get_security(str(stage), 4, None, 0, ctypes.byref(needed))
            descriptor = ctypes.create_string_buffer(needed.value)
            self.assertTrue(get_security(str(stage), 4, descriptor, len(descriptor), ctypes.byref(needed)))
            control, revision = ctypes.c_uint16(), ctypes.c_uint32()
            get_control = security.GetSecurityDescriptorControl
            get_control.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint16), ctypes.POINTER(ctypes.c_uint32)]
            get_control.restype = ctypes.c_int
            self.assertTrue(get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)))
            self.assertFalse(control.value & 0x1000, "report staging installed a protected DACL")
            report_publication.commit_report(stage, output)
        reader = subprocess.run([os.sys.executable, "-c", "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())",
                                 str(output)], capture_output=True, text=True, check=True)
        self.assertEqual("review", reader.stdout.strip())
        with report_publication.staged_report(directory, b"replacement\n") as stage:
            with self.assertRaises(FileExistsError):
                report_publication.commit_report(stage, output)
        self.assertEqual(b"review\n", output.read_bytes())

"""End-to-end local Git fixtures for the session facade and shared caches."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from unittest import mock
import uuid

from tests.fixtures import ReviewFixture, api, workspace, review_session, workspace_report, SCRIPTS
from tests.support import run_git as git, run_python
import review_coordinator as coordination
import review_flow as flow
import review_runtime


class FlowTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.commit()
        self.environment = mock.patch.dict(os.environ, {
            "GITLAB_WORKSPACE_RUNTIME_DIR": str(self.root / "network")})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def start(self, **options):
        values = ["start", "--workspace", str(self.root), "--project", str(self.repo),
                  "--base", options.pop("base", "main"), "--head", options.pop("head", "feature")]
        for key, value in options.items():
            values.append("--" + key.replace("_", "-"))
            if value is not True:
                values.append(str(value))
        result = flow.start(flow.parser().parse_args(values))
        if result.get("context_dir"):
            self.addCleanup(review_session.cleanup_session, Path(result["context_dir"]))
        return result

    def remote(self):
        remote = self.root / "origin.git"
        git(self.root, "init", "--bare", str(remote))
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "push", "origin", "main", "feature")
        return remote

    def authored_draft(self, result):
        context = Path(result["context_dir"])
        manifest = workspace.load(context)
        draft = self.draft(context, manifest)
        draft["report_layout"] = "engineering-review/v1"
        coordination.atomic(context / "draft.json", draft)
        return context, manifest, draft

    def test_local_start_prepares_owned_draft_instructions_and_no_semantic_pass(self):
        result = self.start()
        context = Path(result["context_dir"])
        self.assertTrue(context.is_relative_to(self.root / ".review-runtime"))
        self.assertEqual(context, review_session.validate_session(context))
        self.assertEqual(api.empty_network_activity(), result["network_activity"])
        draft = coordination.read_json(context / "draft.json")
        self.assertTrue(all(row["status"] == "not-reviewed" for row in draft["coverage"]))
        self.assertEqual("", draft["summary"])
        self.assertEqual("ready", flow.status(Path(result["runtime_root"]), result["request_id"])["state"])
        snapshot = workspace.evidence_repository(context, workspace.repository(workspace.load(context), result["selected_repo"]))
        self.assertTrue(list((snapshot / "refs").rglob("*")))

    def test_cancel_after_successful_capture_keeps_the_executors_network_count(self):
        self.remote()
        identifier = uuid.uuid4().hex
        original = api.build_manifest
        def cancel_after_capture(args):
            manifest = original(args)
            active = coordination.ACTIVE.get()
            coordination.atomic(active.cancel_file, {"cancelled": True})
            return manifest
        with mock.patch.object(api, "build_manifest", side_effect=cancel_after_capture):
            with self.assertRaises(coordination.Cancelled):
                self.start(request_id=identifier, base="origin/main", head="origin/feature")
        job = coordination.read_json(self.root / ".review-runtime/jobs" / (identifier + ".json"))
        self.assertEqual("cancelled", job["state"])
        self.assertEqual(1, job["network_activity"]["fetch_attempt_count"])
        self.assertEqual(1, job["network_activity"]["fetch_success_count"])
        self.assertEqual("owner", job["sync_activity"][0]["mode"])
        self.assertFalse(Path(job["context_dir"]).exists())

    def test_expired_summary_cleanup_never_reads_a_live_owners_job(self):
        root = coordination.runtime(self.root / ".review-runtime", create=True)
        jobs = root / "jobs"
        jobs.mkdir()
        busy, expired, malformed = [uuid.uuid4().hex for _ in range(3)]
        coordination.atomic(jobs / (busy + ".json"), {
            "schema": flow.SCHEMA, "request_id": busy, "state": "running"})
        coordination.atomic(jobs / (expired + ".json"), {
            "schema": flow.SCHEMA, "request_id": expired, "state": "published", "finished_at_epoch": 0})
        coordination.atomic(jobs / (expired + ".cancel"), {})
        coordination.atomic(jobs / (malformed + ".json"), [])
        original = coordination.read_json
        def read_unowned_only(path):
            self.assertNotEqual(jobs / (busy + ".json"), Path(path), "cleanup read a live owner's job")
            return original(path)
        with coordination.FileLock(jobs / (busy + ".owner.lock")), \
             mock.patch.object(coordination, "read_json", side_effect=read_unowned_only):
            flow.prune_jobs(root)
        self.assertTrue((jobs / (busy + ".json")).is_file())
        self.assertFalse((jobs / (expired + ".json")).exists())
        self.assertFalse((jobs / (expired + ".cancel")).exists())
        self.assertTrue((jobs / (malformed + ".json")).is_file())

    def test_step_commits_prefix_once_and_reuses_operation_ids(self):
        result = self.start()
        root, identifier = Path(result["runtime_root"]), result["request_id"]
        context = Path(result["context_dir"])
        before = workspace.load(context)["generation"]
        operations = [{"id": "find", "op": "search", "query": "contract"},
                      {"id": "read", "op": "read", "repo": result["selected_repo"], "path": "missing.py"}]
        with mock.patch.object(review_runtime.os, "replace", wraps=review_runtime.os.replace) as writes:
            outcome = flow.step(root, identifier, operations)
        self.assertFalse(outcome["complete"])
        self.assertEqual(1, outcome["next_index"])
        manifest = workspace.load(context)
        self.assertEqual(before + 1, manifest["generation"])
        count = len(manifest["searches"])
        operations[1]["path"] = "app.py"
        outcome = flow.step(root, identifier, operations)
        self.assertTrue(outcome["complete"])
        self.assertTrue(outcome["operations"][0]["reused"])
        self.assertEqual(count, len(workspace.load(context)["searches"]))
        reordered = [dict(reversed(list(item.items()))) for item in operations]
        replay = flow.step(root, identifier, reordered)
        self.assertTrue(replay["complete"])
        self.assertTrue(all(row["reused"] for row in replay["operations"]))
        operations[0]["query"] = "different"
        self.assertIn("different input", flow.step(root, identifier, operations)["error"])

    def test_step_contract_registration_reuses_prefetched_content_and_changed_patch(self):
        self.make_repo("consumer")
        result = self.start()
        context = Path(result["context_dir"])
        manifest = workspace.load(context)
        seed = workspace.repository(manifest, result["selected_repo"])
        root = manifest["required_coverage"][0]
        contract = {"id": "C-001", "root": root, "kind": "behavior", "symbol": "contract",
                    "evidence": {**root, "ref": seed["sources"]["head"]["ref"], "line_start": 1, "line_end": 1},
                    "search_terms": [{"query": "contract", "reason": "Changed symbol"},
                                     {"query": "new", "reason": "New fixed contract value"}]}
        operations = [{"id": "read", "op": "read", "repo": seed["id"], "path": "app.py"},
                      {"id": "first", "op": "register-contract", "contract": contract},
                      {"id": "second", "op": "register-contract", "contract": {**contract, "id": "C-002"}}]
        with mock.patch.object(api, "run_git", wraps=api.run_git) as calls:
            outcome = flow.step(Path(result["runtime_root"]), result["request_id"], operations)
        self.assertTrue(outcome["complete"], outcome)
        self.assertEqual(1, sum(call.args[1][0] == "cat-file" for call in calls.call_args_list))
        self.assertEqual(1, sum(call.args[1][0] == "diff" for call in calls.call_args_list))

    def test_peer_capture_uses_pinned_commit_without_scanning_a_mutable_working_tree(self):
        consumer = self.make_repo("consumer")
        original_head = api.verified_commit(consumer, "HEAD")
        original_peer = coordination.Coordination.peer
        advanced = []
        def advance_after_pin(active, context, record, git_api, build):
            if record["path"] == str(consumer) and not advanced:
                self.assertEqual(original_head, record["head_sha"])
                self.commit("contract = 'later'\n", repository=consumer)
                (consumer / "app.py").write_text("uncommitted = True\n", encoding="utf-8")
                advanced.append(True)
            return original_peer(active, context, record, git_api, build)
        with mock.patch.object(coordination.Coordination, "peer", advance_after_pin), \
             mock.patch.object(api, "snapshot", wraps=api.snapshot) as snapshots:
            first = self.start()
        self.assertTrue(snapshots.call_count)
        self.assertTrue(all(Path(call.args[0]) == self.repo for call in snapshots.call_args_list))
        first_context = Path(first["context_dir"])
        first_peer = next(repo for repo in workspace.load(first_context)["repositories"]
                          if repo["path"] == str(consumer))
        self.assertEqual(original_head, first_peer["sources"]["head"]["ref"])
        self.assertEqual(b"contract = 'old'\n",
                         workspace.fixed_bytes(first_context, first_peer, "head", "app.py", api))
        second = self.start()
        second_context = Path(second["context_dir"])
        second_peer = next(repo for repo in workspace.load(second_context)["repositories"]
                           if repo["path"] == str(consumer))
        self.assertNotEqual(original_head, second_peer["sources"]["head"]["ref"])
        self.assertEqual(b"contract = 'later'\n",
                         workspace.fixed_bytes(second_context, second_peer, "head", "app.py", api))
        self.assertEqual("uncommitted = True\n", (consumer / "app.py").read_text(encoding="utf-8"))

    def test_around_line_clamps_but_exact_bounds_remain_strict(self):
        result = self.start()
        root, identifier = Path(result["runtime_root"]), result["request_id"]
        outcome = flow.step(root, identifier, [{"id": "slice", "op": "read",
            "repo": result["selected_repo"], "path": "app.py", "around_line": 999, "context_lines": 10}])
        value = outcome["operations"][0]["result"]
        self.assertEqual([1], [line["line"] for line in value["lines"]])
        self.assertEqual(999, value["requested_line"])
        outcome = flow.step(root, identifier, [{"id": "strict", "op": "read",
            "repo": result["selected_repo"], "path": "app.py", "line_start": 1, "line_end": 999}])
        self.assertFalse(outcome["complete"])

    def test_finish_repairs_in_place_publishes_five_sections_once_and_recovers_status(self):
        result = self.start()
        root, identifier = Path(result["runtime_root"]), result["request_id"]
        invalid, code = flow.finish(root, identifier)
        self.assertEqual(3, code)
        self.assertIn("summary is required", invalid["errors"])
        context, manifest, draft = self.authored_draft(result)
        with mock.patch.object(workspace_report, "validate", wraps=workspace_report.validate) as validate:
            published, code = flow.finish(root, identifier)
        self.assertEqual(0, code)
        self.assertEqual(1, validate.call_count)
        self.assertFalse(context.exists())
        report = Path(published["report_path"])
        self.assertEqual(["審查結論", "問題總攬", "問題詳情（工程師參考）", "範圍與限制", "技術審查紀錄（工程師參考）"],
                         re.findall(r"^## (.+)$", report.read_text(encoding="utf-8"), re.M))
        again, code = flow.finish(root, identifier)
        self.assertEqual(published["report_path"], again["report_path"])
        self.assertEqual(1, len(list(report.parent.glob("*.md"))))

    def test_recover_after_report_link_but_before_receipt_callback(self):
        result = self.start()
        root, identifier = Path(result["runtime_root"]), result["request_id"]
        self.authored_draft(result)
        original = flow.update
        def lost_receipt(*args, **kwargs):
            if kwargs.get("state") == "published":
                raise OSError("synthetic lost completion")
            return original(*args, **kwargs)
        with mock.patch.object(flow, "update", side_effect=lost_receipt):
            with self.assertRaisesRegex(OSError, "lost completion"):
                flow.finish(root, identifier)
        recovered, code = flow.finish(root, identifier)
        self.assertEqual(0, code)
        self.assertEqual("published", recovered["state"])
        self.assertEqual(1, len(list((self.repo / "review-reports").glob("*.md"))))

    def test_cancel_during_validation_stops_before_report_publication(self):
        result = self.start()
        root, identifier = Path(result["runtime_root"]), result["request_id"]
        context, _, _ = self.authored_draft(result)
        original = workspace_report.validate
        def cancel_after_validation(*args, **kwargs):
            value = original(*args, **kwargs)
            coordination.atomic(root / "jobs" / (identifier + ".cancel"), {"requested": True})
            return value
        with mock.patch.object(workspace_report, "validate", side_effect=cancel_after_validation):
            with self.assertRaises(coordination.Cancelled):
                flow.finish(root, identifier)
        self.assertEqual("cancelled", flow.status(root, identifier)["state"])
        self.assertFalse(context.exists())
        self.assertFalse(list((self.repo / "review-reports").glob("*.md")))

    def test_artifact_write_failure_rolls_back_only_the_uncheckpointed_operation(self):
        result = self.start()
        root, identifier, context = Path(result["runtime_root"]), result["request_id"], Path(result["context_dir"])
        operations = [{"id": "search", "op": "search", "query": "contract"}]
        with mock.patch.object(review_runtime, "store_view", side_effect=OSError("synthetic storage error")):
            failed = flow.step(root, identifier, operations)
        self.assertFalse(failed["complete"])
        self.assertEqual([], workspace.load(context)["searches"])
        self.assertTrue(flow.step(root, identifier, operations)["complete"])
        self.assertEqual(["S-0001"], [row["id"] for row in workspace.load(context)["searches"]])

    def test_crashed_v2_manifest_writer_releases_os_lock_and_can_resume_by_id(self):
        result = self.start()
        context = Path(result["context_dir"])
        program = ("import os,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
                   "import review_runtime; "
                   "lease=review_runtime.writer(Path(sys.argv[2])); lease.__enter__(); os._exit(7)")
        child = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", program, str(SCRIPTS), str(context)],
                               capture_output=True, timeout=15)
        self.assertEqual(7, child.returncode, child.stderr)
        value = flow.step(Path(result["runtime_root"]), result["request_id"],
                          [{"id": "resume", "op": "read", "repo": result["selected_repo"], "path": "app.py"}])
        self.assertTrue(value["complete"])

    def test_same_request_start_is_idempotent_and_inputs_cannot_change(self):
        identifier = uuid.uuid4().hex
        first = self.start(request_id=identifier)
        with mock.patch.object(api, "build_manifest", side_effect=AssertionError("recapture")):
            second = self.start(request_id=identifier)
        self.assertEqual(first["context_dir"], second["context_dir"])
        with self.assertRaisesRegex(ValueError, "different inputs"):
            self.start(request_id=identifier, head="main")

    def test_shared_remote_receipt_ttl_no_sliding_expiry_and_force_push(self):
        self.remote()
        first = self.start(base="origin/main", head="origin/feature")
        self.assertEqual(1, first["network_activity"]["fetch_success_count"])
        receipt = next((Path(first["runtime_root"]) / "sync").glob("*/*.receipt.json"))
        completed = coordination.read_json(receipt)["completed_at"]
        with mock.patch.object(coordination.time, "time", return_value=completed + 290):
            second = self.start(base="origin/main", head="origin/feature")
        self.assertEqual(0, second["network_activity"]["fetch_attempt_count"])
        self.assertEqual("cached", second["sync_activity"][0]["mode"])
        self.assertEqual(completed, coordination.read_json(receipt)["completed_at"])
        git(self.repo, "switch", "main")
        git(self.repo, "branch", "-f", "feature", "main")
        git(self.repo, "push", "--force", "origin", "feature")
        with mock.patch.object(coordination.time, "time", return_value=completed + 301):
            third = self.start(base="origin/main", head="origin/feature")
        self.assertEqual(1, third["network_activity"]["fetch_success_count"])
        manifest = workspace.load(Path(third["context_dir"]))
        seed = workspace.repository(manifest, third["selected_repo"])
        self.assertEqual(git(self.repo, "rev-parse", "main"), seed["sources"]["head"]["ref"])
        self.assertTrue(seed["native"]["head_resolved_ref"].startswith("refs/merge-reviewer-sync/"))

    def test_new_ref_repo_and_remote_url_require_their_own_sync_but_subsets_reuse(self):
        self.remote()
        first = self.start(base="origin/main", head="origin/feature")
        first_context = Path(first["context_dir"])
        original = workspace.repository(workspace.load(first_context), first["selected_repo"])["sources"]["head"]["ref"]
        git(self.repo, "branch", "topic")
        git(self.repo, "push", "origin", "topic")
        new_ref = self.start(base="origin/main", head="origin/topic")
        self.assertEqual(1, new_ref["network_activity"]["fetch_attempt_count"])
        subset = self.start(base="origin/main", head="origin/main")
        self.assertEqual(0, subset["network_activity"]["fetch_attempt_count"])
        other = self.make_repo("other")
        git(other, "switch", "-c", "feature")
        self.commit("contract = 'other'\n", repository=other)
        other_remote = self.root / "other-origin.git"
        git(self.root, "init", "--bare", str(other_remote))
        git(other, "remote", "add", "origin", str(other_remote))
        git(other, "push", "origin", "main", "feature")
        independent = self.start(project=str(other), base="origin/main", head="origin/feature")
        self.assertEqual(1, independent["network_activity"]["fetch_attempt_count"])
        git(self.repo, "remote", "set-url", "origin", str(other_remote))
        switched = self.start(base="origin/main", head="origin/feature")
        self.assertEqual(1, switched["network_activity"]["fetch_attempt_count"])
        switched_peer = workspace.repository(workspace.load(Path(switched["context_dir"])), switched["selected_repo"])
        self.assertEqual(api.verified_commit(other, "HEAD"), switched_peer["sources"]["head"]["ref"])
        self.assertEqual(original, workspace.repository(workspace.load(first_context), first["selected_repo"])["sources"]["head"]["ref"])

    def test_peer_lru_evicts_oldest_entry_without_affecting_delivered_evidence(self):
        self.make_repo("consumer-a")
        self.make_repo("consumer-b")
        first = self.start()
        root, context = Path(first["runtime_root"]), Path(first["context_dir"])
        entries = sorted(path for path in (root / "packs").iterdir() if path.is_dir())
        self.assertEqual(2, len(entries))
        for index, entry in enumerate(entries, 1):
            os.utime(entry, (index, index))
        keep_size = sum(path.stat().st_size for path in entries[1].iterdir() if path.is_file())
        with mock.patch.object(coordination, "PACK_LIMIT", keep_size):
            coordination.Coordination(root, uuid.uuid4().hex)._prune_packs()
        self.assertFalse(entries[0].exists())
        self.assertTrue(entries[1].is_dir())
        for repo in workspace.load(context)["repositories"]:
            if repo["role"] == "candidate":
                self.assertEqual(b"contract = 'old'\n",
                                 workspace.fixed_bytes(context, repo, "head", "app.py", api))

    def test_refresh_replaces_older_success_even_when_its_filename_sorts_first(self):
        self.remote()
        first = self.start(base="origin/main", head="origin/feature")
        root = Path(first["runtime_root"])
        receipt = next((root / "sync").glob("*/*.receipt.json"))
        oldest = receipt.with_name("f" * 32 + ".receipt.json")
        receipt.rename(oldest)
        self.commit("contract = 'refreshed'\n")
        git(self.repo, "push", "origin", "feature")
        refreshed = self.start(base="origin/main", head="origin/feature", refresh=True)
        reused = self.start(base="origin/main", head="origin/feature")
        self.assertEqual(0, reused["network_activity"]["fetch_attempt_count"])
        current = workspace.repository(workspace.load(Path(reused["context_dir"])), reused["selected_repo"])
        self.assertEqual(api.verified_commit(self.repo, "HEAD"), current["sources"]["head"]["ref"])
        self.assertTrue(coordination.read_json(oldest)["superseded_by"])
        old = workspace.repository(workspace.load(Path(first["context_dir"])), first["selected_repo"])
        self.assertNotEqual(old["sources"]["head"]["ref"], current["sources"]["head"]["ref"])
        self.assertEqual(1, refreshed["network_activity"]["fetch_attempt_count"])

    def test_failed_refresh_does_not_revive_older_success_after_cooldown(self):
        self.remote()
        first = self.start(base="origin/main", head="origin/feature")
        root = Path(first["runtime_root"])
        git(self.repo, "push", "origin", "--delete", "feature")
        with self.assertRaises(api.ReviewContextError):
            self.start(base="origin/main", head="origin/feature", refresh=True)
        failed = next((root / "sync").glob("*/*.failure.json"))
        retry_at = coordination.read_json(failed)["failed_at"] + 31
        with mock.patch.object(coordination.time, "time", return_value=retry_at), \
             mock.patch.object(api, "run_git", wraps=api.run_git) as calls:
            with self.assertRaises(api.ReviewContextError):
                self.start(base="origin/main", head="origin/feature")
        self.assertEqual(1, sum(call.args[1][0] == "fetch" for call in calls.call_args_list))
        self.assertEqual("ready", flow.status(root, first["request_id"])["state"])

    def test_failure_cooldown_and_refresh_do_not_fall_back_to_old_refs(self):
        self.remote()
        with self.assertRaises(api.ReviewContextError):
            self.start(base="origin/main", head="origin/missing")
        with mock.patch.object(api, "run_git", wraps=api.run_git) as calls:
            with self.assertRaisesRegex(api.ReviewContextError, "cooldown"):
                self.start(base="origin/main", head="origin/missing")
        self.assertFalse(any(call.args[1][0] == "fetch" for call in calls.call_args_list))
        with mock.patch.object(api, "run_git", wraps=api.run_git) as calls:
            with self.assertRaises(api.ReviewContextError):
                self.start(base="origin/main", head="origin/missing", refresh=True)
        self.assertEqual(1, sum(call.args[1][0] == "fetch" for call in calls.call_args_list))

    def test_peer_cache_is_copied_and_original_or_other_session_cleanup_cannot_break_it(self):
        peer = self.make_repo("consumer")
        first = self.start()
        second = self.start()
        self.assertEqual(1, second["pack_cache_hits"])
        context = Path(second["context_dir"])
        manifest = workspace.load(context)
        record = next(item for item in manifest["repositories"] if item["path"] == str(peer))
        review_session.cleanup_session(Path(first["context_dir"]))
        git(peer, "gc", "--prune=now")
        expected = workspace.read(context, manifest, record["id"], "app.py", api)
        self.assertEqual("contract = 'old'", expected["lines"][0]["text"])
        self.assertFalse((workspace.evidence_repository(context, record) / "objects/info/alternates").exists())

    def test_corrupt_cached_pack_rebuilds_at_capture_but_owned_damage_is_terminal(self):
        self.make_repo("consumer")
        first = self.start()
        pack = next((Path(first["runtime_root"]) / "packs").glob("*/snapshot-*.pack"))
        pack.write_bytes(b"corrupt")
        second = self.start()
        self.assertEqual(0, second["pack_cache_hits"])
        entry = next((Path(first["runtime_root"]) / "packs").glob("*/entry.json"))
        malformed = coordination.read_json(entry)
        malformed["files"] = []
        coordination.atomic(entry, malformed)
        rebuilt = self.start()
        self.assertEqual(0, rebuilt["pack_cache_hits"])
        context = Path(second["context_dir"])
        manifest = workspace.load(context)
        seed = workspace.repository(manifest, second["selected_repo"])
        store = workspace.evidence_repository(context, seed)
        for path in (store / "refs").rglob("*"):
            if path.is_file():
                path.unlink()
        for path in sorted((store / "refs").rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir():
                path.rmdir()
        (store / "refs").rmdir()
        with self.assertRaisesRegex(ValueError, "refs directory"):
            workspace.read(context, manifest, seed["id"], "app.py", api)

    def test_cancel_one_ready_session_preserves_another_and_no_recapture(self):
        first, second = self.start(), self.start()
        result = flow.cancel(Path(first["runtime_root"]), first["request_id"])
        self.assertEqual("cancelled", result["state"])
        self.assertFalse(Path(first["context_dir"]).exists())
        self.assertEqual("ready", flow.status(Path(second["runtime_root"]), second["request_id"])["state"])

    def test_bounded_output_preserves_full_artifact_and_paging(self):
        result = self.start()
        context = Path(result["context_dir"])
        raw = {"operations": [{"lines": [{"line": i, "text": "x" * 5000} for i in range(100)]}]}
        small = flow.compact(raw, context)
        self.assertLessEqual(len(review_runtime.canonical(small)), flow.LIMIT)
        self.assertEqual(raw, review_runtime.load_view(context, small["artifact"]))
        page = flow.artifact_view(context, small["artifact"], pointer="/operations/0/lines", page_size=100)
        self.assertFalse(page["view_complete"])
        self.assertGreater(page["next_offset"], 0)

    def test_compact_step_keeps_every_line_match_and_fixed_identity_without_paging(self):
        started = self.start()
        context = Path(started["context_dir"])
        read = {"repo": started["selected_repo"], "source": "head", "ref": "a"*40, "path": "long.py",
                "sha256": "b"*64, "lines": [{"line": i, "text": "shared content " * 20} for i in range(1, 31)]}
        rows = [{"id": "S-0001", "repo": "repo-000", "source": "head", "ref": "a"*40,
                 "query": "contract", "contract_id": None, "status": "complete",
                 "output": "exact match\n", "matches": ["exact match"]},
                {"id": "S-0002", "repo": "repo-001", "source": "head", "ref": "c"*40,
                 "query": "contract", "contract_id": None, "status": "complete",
                 "output": "", "matches": []}]
        raw = {"complete": True, "next_index": 5, "operations": [
            *[{"id": str(i), "result": copy.deepcopy(read)} for i in range(4)],
            {"id": "search", "result": {"searches": rows}}]}
        before = copy.deepcopy(raw)
        value = flow.compact(raw, context)
        self.assertEqual("MergeReviewEvidenceStep/v1", value["schema"])
        self.assertTrue(value["projection_complete"])
        self.assertLessEqual(len(review_runtime.canonical(value)), flow.LIMIT)
        self.assertEqual(before, raw)
        self.assertEqual(before, review_runtime.load_view(context, value["artifact"]))
        for operation in value["operations"][:4]:
            restored = dict(operation["result"])
            restored["lines"] = value["contents"][restored.pop("content_id")]["lines"]
            self.assertEqual(read, restored)
        search = value["operations"][4]["result"]
        for original, row in zip(rows, search["searches"]):
            restored = {**{key: search[key] for key in ("query", "contract_id", "source")}, **row}
            self.assertEqual({key: item for key, item in original.items() if key != "output"}, restored)

    def test_ten_processes_share_one_fetch_and_keep_ten_contexts(self):
        self.remote()
        command = [sys.executable, "-X", "utf8", "-B", str(SCRIPTS / "review_session.py"), "start",
                   "--workspace", str(self.root), "--project", str(self.repo),
                   "--base", "origin/main", "--head", "origin/feature"]
        children = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}) for _ in range(10)]
        results = []
        failures = []
        for child in children:
            try:
                output, error = child.communicate(timeout=120)
            except subprocess.TimeoutExpired:
                child.kill()
                output, error = child.communicate()
            if child.returncode:
                failures.append(error.decode("utf-8", errors="replace"))
                continue
            result = json.loads(output)
            results.append(result)
            self.addCleanup(review_session.cleanup_session, Path(result["context_dir"]))
        self.assertEqual([], failures)
        self.assertEqual(10, len({row["context_dir"] for row in results}))
        self.assertEqual(1, sum(row["network_activity"]["fetch_attempt_count"] for row in results))
        self.assertEqual(9, sum(row["sync_activity"][0]["mode"] in ("joined", "cached") for row in results))

    def test_malformed_draft_is_repairable_but_owned_evidence_damage_terminates(self):
        result = self.start()
        root, identifier, context = Path(result["runtime_root"]), result["request_id"], Path(result["context_dir"])
        (context / "draft.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(3, flow.finish(root, identifier)[1])
        self.assertTrue(context.exists())
        seed = workspace.repository(workspace.load(context), result["selected_repo"])
        pack = next((workspace.evidence_repository(context, seed) / "objects/pack").glob("*.pack"))
        pack.chmod(0o600)
        pack.write_bytes(b"damaged delivered evidence")
        with self.assertRaises((ValueError, api.ReviewContextError)):
            flow.finish(root, identifier)
        self.assertEqual("failed", flow.status(root, identifier)["state"])
        self.assertFalse(context.exists())

    def test_auth_invalidation_clock_rollback_and_branch_deletion_do_not_use_old_refs(self):
        self.remote()
        first = self.start(base="origin/main", head="origin/feature", auth_scope="account-a")
        root = Path(first["runtime_root"])
        second = self.start(base="origin/main", head="origin/feature", auth_scope="account-b")
        self.assertEqual(1, second["network_activity"]["fetch_success_count"])
        common = str(api.run_git(self.repo, ["rev-parse", "--path-format=absolute", "--git-common-dir"])).strip()
        (root / "invalidations").mkdir()
        coordination.atomic(root / "invalidations" / (coordination.digest(str(Path(common).resolve())) + ".json"), {"epoch": "push"})
        third = self.start(base="origin/main", head="origin/feature", auth_scope="account-a")
        self.assertEqual(1, third["network_activity"]["fetch_success_count"])
        with mock.patch.object(coordination.time, "time", return_value=1):
            rollback = self.start(base="origin/main", head="origin/feature", auth_scope="account-a")
        self.assertEqual(1, rollback["network_activity"]["fetch_success_count"])
        git(self.repo, "push", "origin", "--delete", "feature")
        with self.assertRaises(api.ReviewContextError):
            self.start(base="origin/main", head="origin/feature", auth_scope="account-a", refresh=True)
        self.assertEqual("ready", flow.status(root, first["request_id"])["state"])

    def test_cancelled_sync_owner_finishes_only_for_live_waiter(self):
        self.remote()
        root = coordination.runtime(self.root / ".review-runtime", create=True)
        (root / "jobs").mkdir()
        owner = coordination.Coordination(root, uuid.uuid4().hex)
        waiter = coordination.Coordination(root, uuid.uuid4().hex)
        targets = [api.RemoteTarget("origin", "main", "origin/main")]
        entered, proceed = threading.Event(), threading.Event()
        calls = []
        def fetch(specs):
            calls.append(specs)
            entered.set()
            self.assertTrue(proceed.wait(15))
            self.assertFalse(owner.network_cancelled())
            api.run_git(self.repo, ["fetch", "--atomic", "--no-tags", "origin", *specs])
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(owner.sync, self.repo, "origin", targets, api, fetch)
            self.assertTrue(entered.wait(15))
            second = pool.submit(waiter.sync, self.repo, "origin", targets, api,
                                 lambda _: self.fail("duplicate fetch"))
            deadline = time.monotonic() + 15
            while len(list((root / "sync").glob("*/waiters/*.json"))) < 2:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.02)
            coordination.atomic(owner.cancel_file, {"cancelled": True})
            proceed.set()
            first.result(timeout=20)
            second.result(timeout=20)
        self.assertEqual(1, len(calls))
        self.assertEqual(owner.pins, waiter.pins)
        with self.assertRaises(coordination.Cancelled):
            owner.check()
        owner.release_syncs()
        waiter.release_syncs()

    def test_foreign_context_cannot_be_cancelled_by_a_different_request_id(self):
        first, second = self.start(), self.start()
        root, identifier = Path(first["runtime_root"]), first["request_id"]
        flow.update(root, identifier, context_dir=second["context_dir"])
        with self.assertRaisesRegex(ValueError, "ownership"):
            flow.cancel(root, identifier)
        self.assertTrue(Path(second["context_dir"]).is_dir())

    def test_malformed_draft_fields_are_reported_together_and_keep_the_context(self):
        result = self.start()
        context = Path(result["context_dir"])
        coordination.atomic(context / "draft.json", {"coverage": None, "findings": []})
        value, code = flow.finish(Path(result["runtime_root"]), result["request_id"])
        self.assertEqual(3, code)
        self.assertGreater(len(value["errors"]), 5)
        self.assertTrue(context.is_dir())

    def test_preflight_stops_before_fetch_and_pack_quota_is_independent(self):
        self.remote()
        with mock.patch.object(flow, "preflight", side_effect=PermissionError("no write")), \
             mock.patch.object(api, "fetch_inputs", side_effect=AssertionError("transport before preflight")):
            with self.assertRaises(PermissionError):
                self.start(base="origin/main", head="origin/feature")
        root = coordination.runtime(self.root / ".review-runtime")
        mutex, current, maximum = threading.Lock(), [0], [0]
        def pack(_):
            active = coordination.Coordination(root, uuid.uuid4().hex)
            with active.packing():
                with mutex:
                    current[0] += 1
                    maximum[0] = max(maximum[0], current[0])
                with active.packing():
                    time.sleep(.1)
                with mutex:
                    current[0] -= 1
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(pack, range(4)))
        self.assertEqual(2, maximum[0])

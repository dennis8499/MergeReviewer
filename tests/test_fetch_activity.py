from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
from unittest import mock

from tests.fixtures import ReviewFixture, api, workspace, review_session
from tests.support import run_git as git


class FetchActivityTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"GITLAB_WORKSPACE_RUNTIME_DIR": str(self.root / "runtime")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.commit()

    def remote(self, name="origin"):
        remote = self.root / f"{name}.git"
        git(self.root, "init", "--bare", str(remote))
        git(self.repo, "remote", "add", name, str(remote))
        git(self.repo, "push", name, "main", "feature")
        return remote

    def test_local_capture_and_both_summaries_have_zero_activity_and_policy(self):
        _, manifest = self.capture()
        self.assertEqual(api.empty_network_activity(), manifest["network_activity"])
        self.assertEqual("ancestor-noop/v1", manifest["coverage_policy"])
        summary = api.context_summary(manifest)
        self.assertEqual(manifest["network_activity"], summary["network_activity"])
        self.assertEqual(manifest["coverage_policy"], summary["coverage_policy"])
        for flag, schema in (("--summary", "v1"), ("--summary-v2", "v2")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                result = api.main(["--workspace", str(self.root), "--project", str(self.repo),
                                   "--base", "main", "--head", "feature", flag])
            self.assertEqual(0, result)
            compact = json.loads(out.getvalue())
            self.addCleanup(review_session.cleanup_session, Path(compact["context_dir"]))
            self.assertTrue(compact["schema"].endswith(schema))
            self.assertEqual(api.empty_network_activity(), compact["network_activity"])
            self.assertEqual("ancestor-noop/v1", compact["coverage_policy"])

    def test_two_branches_one_remote_count_one_attempt_and_two_refs(self):
        self.remote()
        _, manifest = self.capture(base="origin/main", head="origin/feature")
        expected = {"fetch_attempt_count": 1, "fetch_success_count": 1,
                    "requested_ref_count": 2, "synced_ref_count": 2}
        self.assertEqual(expected, manifest["network_activity"])
        native = workspace.repository(manifest, manifest["selected_repo"])["native"]
        self.assertEqual(2, len(native["fetches"]))
        self.assertEqual(expected, native["network_activity"])
        _, again = self.capture(base="origin/main", head="origin/feature")
        self.assertEqual(expected, again["network_activity"])
        _, local = self.capture()
        self.assertEqual(api.empty_network_activity(), local["network_activity"])

    def test_duplicate_ref_is_counted_once(self):
        self.remote()
        _, manifest = self.capture(base="origin/main", head="origin/main")
        self.assertEqual({"fetch_attempt_count": 1, "fetch_success_count": 1,
                          "requested_ref_count": 1, "synced_ref_count": 1}, manifest["network_activity"])

    def test_disabled_remote_fetch_records_requested_refs_without_attempts(self):
        self.remote()
        with self.assertRaisesRegex(api.ReviewContextError, "--no-fetch") as raised:
            self.capture("--no-fetch", base="origin/main", head="origin/feature")
        self.assertEqual("branch-preflight", raised.exception.phase)
        self.assertEqual({"fetch_attempt_count": 0, "fetch_success_count": 0,
                          "requested_ref_count": 2, "synced_ref_count": 0}, raised.exception.network_activity)

    def test_lease_rejection_counts_command_attempt_without_claiming_a_connection(self):
        self.remote()
        with mock.patch.object(api.git_network_lease, "run", side_effect=PermissionError("lease denied")):
            with self.assertRaisesRegex(PermissionError, "lease denied") as raised:
                self.capture(base="origin/main", head="origin/feature")
        self.assertEqual("fetch", raised.exception.phase)
        self.assertEqual({"fetch_attempt_count": 1, "fetch_success_count": 0,
                          "requested_ref_count": 2, "synced_ref_count": 0}, raised.exception.network_activity)

    def test_partial_multi_remote_failure_preserves_attempts_and_successes(self):
        self.remote()
        self.remote("upstream")
        original = api.run_git

        def fail_second(repo, args, **kwargs):
            if args[0] == "fetch" and "upstream" in args:
                raise api.GitCommandError(args, 128, "connection refused")
            return original(repo, args, **kwargs)

        with mock.patch.object(api, "run_git", side_effect=fail_second):
            with self.assertRaisesRegex(api.ReviewContextError, "fetch upstream") as raised:
                self.capture(base="origin/main", head="upstream/feature")
        self.assertEqual("fetch", raised.exception.phase)
        self.assertEqual({"fetch_attempt_count": 2, "fetch_success_count": 1,
                          "requested_ref_count": 2, "synced_ref_count": 1}, raised.exception.network_activity)
        self.assertIsNotNone(raised.exception.hint)

    def test_timeout_counts_attempt_without_success_and_cli_keeps_exit_two(self):
        self.remote()
        err = io.StringIO()
        with mock.patch.object(api.git_network_lease, "run", side_effect=subprocess.TimeoutExpired("git", 1)), \
                contextlib.redirect_stderr(err):
            result = api.main(["--workspace", str(self.root), "--project", str(self.repo),
                               "--base", "origin/main", "--head", "origin/feature"])
        self.assertEqual(2, result)
        diagnostic = json.loads(next(line.split("diagnostic: ", 1)[1] for line in err.getvalue().splitlines()
                                     if "diagnostic: " in line))
        self.assertEqual("fetch", diagnostic["phase"])
        self.assertEqual({"fetch_attempt_count": 1, "fetch_success_count": 0,
                          "requested_ref_count": 2, "synced_ref_count": 0}, diagnostic["network_activity"])
        self.assertIn("逾時", err.getvalue())
        self.assertIn("建議", err.getvalue())

    def test_post_fetch_failure_reports_success_and_session_failure_reports_zero(self):
        self.remote()
        with mock.patch.object(workspace, "branch_snapshot", side_effect=PermissionError("pack denied")):
            with self.assertRaisesRegex(PermissionError, "pack denied") as raised:
                self.capture(base="origin/main", head="origin/feature")
        self.assertEqual("snapshot-capture", raised.exception.phase)
        self.assertEqual(1, raised.exception.network_activity["fetch_success_count"])
        with mock.patch.object(review_session, "create_session", side_effect=PermissionError("temp denied")):
            with self.assertRaisesRegex(PermissionError, "temp denied") as raised:
                self.capture()
        self.assertEqual("session-preflight", raised.exception.phase)
        self.assertEqual(api.empty_network_activity(), raised.exception.network_activity)
        self.assertEqual(api.empty_network_activity(), api.network_activity())

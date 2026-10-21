from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

from tests.fixtures import ReviewFixture, api, workspace
from tests.support import run_git as git, write_text


class CapturePreflightTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"GITLAB_WORKSPACE_RUNTIME_DIR": str(self.root / "runtime")})
        patcher.start()
        self.addCleanup(patcher.stop)
        remote = self.root / "origin.git"
        git(self.root, "init", "--bare", str(remote))
        git(self.repo, "remote", "add", "origin", str(remote))
        self.commit()
        git(self.repo, "push", "origin", "main", "feature")

    def capture_remote(self):
        return self.capture(base="origin/main", head="origin/feature")

    def test_unborn_selected_head_keeps_existing_remote_branch_review_available(self):
        expected = git(self.repo, "rev-parse", "refs/heads/feature")
        git(self.repo, "symbolic-ref", "HEAD", "refs/heads/unborn")
        with mock.patch.object(api, "run_git", wraps=api.run_git) as calls:
            context, manifest = self.capture_remote()
        selected = workspace.repository(manifest, manifest["selected_repo"])
        self.assertEqual(expected, selected["sources"]["head"]["ref"])
        self.assertEqual("refs/heads/unborn", git(self.repo, "symbolic-ref", "HEAD"))
        self.assertEqual(1, len([call for call in calls.call_args_list if call.args[1][0] == "fetch"]))
        self.assertEqual({"fetch_attempt_count": 1, "fetch_success_count": 1,
                          "requested_ref_count": 2, "synced_ref_count": 2}, manifest["network_activity"])

    def test_all_local_roots_heads_and_formats_and_output_checks_precede_fetch(self):
        peer = self.make_repo("z-peer")
        write_text(self.root / "AGENTS.md", "Workspace instructions.\n")
        calls, fetched = [], []
        original_git, original_snapshot = api.run_git, api.snapshot

        def trace_git(repo, args, **kwargs):
            if args[0] == "fetch":
                fetched.append(True)
                for candidate in (self.repo, peer):
                    self.assertIn((candidate, ("rev-parse", "--show-toplevel")), calls)
                    self.assertIn((candidate, ("rev-parse", "--show-object-format")), calls)
                    self.assertTrue(any(path == candidate and arg == ("rev-parse", "--verify", "--end-of-options", "HEAD^{commit}")
                                        for path, arg in calls))
                self.assertTrue(snapshot_calls)
                session = Path(active_args.context_dir)
                self.assertTrue((session / "instructions/workspace-000.md").is_file())
                self.assertTrue((session / "repos/repo-000/native").is_dir())
                self.assertTrue((session / "repos/repo-001").is_dir())
            calls.append((Path(repo), tuple(args)))
            return original_git(repo, args, **kwargs)

        snapshot_calls = []

        def trace_snapshot(repo):
            snapshot_calls.append(Path(repo))
            return original_snapshot(repo)

        active_args = api.parse_args(["--workspace", str(self.root), "--project", str(self.repo),
                                      "--base", "origin/main", "--head", "origin/feature"])
        with mock.patch.object(api, "run_git", side_effect=trace_git), \
                mock.patch.object(api, "snapshot", side_effect=trace_snapshot):
            manifest = api.build_manifest(active_args)
        from tests.fixtures import review_session
        self.addCleanup(review_session.cleanup_session, Path(manifest["context_dir"]))
        self.assertEqual(1, len(fetched))

    def test_instruction_failure_has_zero_fetch_attempts(self):
        with mock.patch.object(workspace, "capture_workspace_instructions", side_effect=PermissionError("instruction denied")), \
                mock.patch.object(api, "fetch_inputs", wraps=api.fetch_inputs) as fetch:
            with self.assertRaisesRegex(PermissionError, "instruction denied") as raised:
                self.capture_remote()
        fetch.assert_not_called()
        self.assertEqual("output-preflight", raised.exception.phase)
        self.assertEqual(api.empty_network_activity(), raised.exception.network_activity)

    def test_selected_snapshot_failure_has_zero_fetch_attempts(self):
        with mock.patch.object(api, "snapshot", side_effect=PermissionError("index denied")), \
                mock.patch.object(api, "fetch_inputs", wraps=api.fetch_inputs) as fetch:
            with self.assertRaisesRegex(PermissionError, "index denied") as raised:
                self.capture_remote()
        fetch.assert_not_called()
        self.assertEqual("output-preflight", raised.exception.phase)
        self.assertEqual(api.empty_network_activity(), raised.exception.network_activity)

    def test_session_inside_repo_is_rejected_before_fetch(self):
        target = self.repo / "owned-evidence"
        with mock.patch.object(api, "fetch_inputs", wraps=api.fetch_inputs) as fetch:
            with self.assertRaisesRegex(ValueError, "outside every repository") as raised:
                self.capture("--context-dir", str(target), base="origin/main", head="origin/feature")
        fetch.assert_not_called()
        self.assertFalse(target.exists())
        self.assertEqual("output-preflight", raised.exception.phase)
        self.assertEqual(api.empty_network_activity(), raised.exception.network_activity)

    def test_output_directory_failure_is_rejected_before_fetch(self):
        original = workspace.safe_path

        def inaccessible(context, relative):
            if relative.endswith("/native"):
                raise PermissionError("output denied")
            return original(context, relative)

        with mock.patch.object(workspace, "safe_path", side_effect=inaccessible), \
                mock.patch.object(api, "fetch_inputs", wraps=api.fetch_inputs) as fetch:
            with self.assertRaisesRegex(PermissionError, "output denied") as raised:
                self.capture_remote()
        fetch.assert_not_called()
        self.assertEqual("output-preflight", raised.exception.phase)
        self.assertEqual(api.empty_network_activity(), raised.exception.network_activity)

    def test_selected_object_format_failure_has_zero_fetch_attempts(self):
        original = api.run_git

        def unsupported(repo, args, **kwargs):
            return "unsupported\n" if args == ["rev-parse", "--show-object-format"] else original(repo, args, **kwargs)

        with mock.patch.object(api, "run_git", side_effect=unsupported), \
                mock.patch.object(api, "fetch_inputs", wraps=api.fetch_inputs) as fetch:
            with self.assertRaisesRegex(ValueError, "unsupported Git object format") as raised:
                self.capture_remote()
        fetch.assert_not_called()
        self.assertEqual("local-preflight", raised.exception.phase)
        self.assertEqual(api.empty_network_activity(), raised.exception.network_activity)

    def test_peer_ownership_failure_is_checked_before_fetch_and_remains_unresolved(self):
        peer = self.make_repo("z-peer")
        original = workspace.validate_root
        checked = []

        def fail_peer(path, api_module):
            checked.append(path)
            if path == peer:
                raise api.GitCommandError(["rev-parse", "--show-toplevel"], 128, "dubious ownership")
            return original(path, api_module)

        original_fetch = api.fetch_inputs

        def check_fetch(*args, **kwargs):
            self.assertIn(peer, checked)
            return original_fetch(*args, **kwargs)

        with mock.patch.object(workspace, "validate_root", side_effect=fail_peer), \
                mock.patch.object(api, "fetch_inputs", side_effect=check_fetch):
            context, manifest = self.capture_remote()
        record = next(row for row in manifest["repositories"] if row["path"] == str(peer))
        self.assertEqual("error", record["state"])
        self.assertEqual({}, record["sources"])
        self.assertIn("dubious ownership", record["limitations"][0])
        self.assertEqual(1, checked.count(peer))
        self.assertEqual(1, manifest["network_activity"]["fetch_attempt_count"])

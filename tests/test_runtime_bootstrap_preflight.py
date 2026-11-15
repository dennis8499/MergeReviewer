"""Pure-read ownership inspection tolerates only a completed valid bootstrap."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.fixtures import SCRIPTS, api
import review_coordinator as coordination
import review_flow as flow


class RuntimeBootstrapPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bootstrap-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.marker = self.root / ".merge-reviewer-runtime.json"
        self.lock = self.root / ".ownership.lock"

    def write_valid_marker(self):
        self.marker.write_text(json.dumps({"schema": coordination.RUNTIME_SCHEMA,
                                           "directory": str(self.root)}), encoding="utf-8")

    @contextmanager
    def read_only(self):
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        original_open = Path.open
        def open_read(path, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                raise AssertionError("preflight opened a writable file")
            return original_open(path, mode, *args, **kwargs)
        with mock.patch.object(Path, "open", open_read), \
             mock.patch.object(Path, "mkdir", side_effect=AssertionError("preflight created a directory")), \
             mock.patch.object(coordination, "atomic", side_effect=AssertionError("preflight wrote state")), \
             mock.patch.object(coordination.FileLock, "acquire", side_effect=AssertionError("preflight acquired a lock")), \
             mock.patch.object(api, "run_git", side_effect=AssertionError("ownership inspection ran Git")), \
             mock.patch.object(api, "run_git_capture", side_effect=AssertionError("ownership inspection ran Git")):
            yield
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.root.iterdir()})

    def test_marker_completed_between_initial_check_and_enumeration_is_strictly_verified(self):
        self.lock.write_bytes(b"\0")
        self.write_valid_marker()
        original_exists = Path.exists
        initial = [True]
        def stale_marker_check(path):
            if path == self.marker and initial[0]:
                initial[0] = False
                return False
            return original_exists(path)
        with self.read_only(), mock.patch.object(Path, "exists", stale_marker_check), \
             mock.patch.object(coordination, "runtime", wraps=coordination.runtime) as verified, \
             mock.patch.object(flow.time, "sleep", side_effect=AssertionError("completed marker caused waiting")):
            flow.inspect_runtime_ownership(self.root)
        verified.assert_called_once_with(self.root)
        self.assertFalse(initial[0])

    def test_bootstrap_in_progress_waits_without_writes_then_verifies_valid_marker(self):
        self.lock.write_bytes(b"\0")
        self.write_valid_marker()
        clock, waits = [0.0], []
        original_exists = Path.exists
        def marker_visibility(path):
            if path == self.marker and clock[0] < .03:
                return False
            return original_exists(path)
        def advance(delay):
            waits.append(delay)
            clock[0] += delay
        with self.read_only(), mock.patch.object(Path, "exists", marker_visibility), \
             mock.patch.object(flow.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(flow.time, "sleep", side_effect=advance), \
             mock.patch.object(coordination, "runtime", wraps=coordination.runtime) as verified:
            flow.inspect_runtime_ownership(self.root)
        self.assertEqual([.01, .01, .01], waits)
        verified.assert_called_once_with(self.root)

    def test_launch_preflight_uses_race_safe_check_without_writes_or_git(self):
        self.lock.write_bytes(b"\0")
        self.write_valid_marker()
        original_exists = Path.exists
        first = [True]
        def stale_marker_check(path):
            if path == self.marker and first[0]:
                first[0] = False
                return False
            return original_exists(path)
        declared = self.root.parent
        repository = declared / "selected-fixture-repository"
        parameters = flow.parser().parse_args(["preflight", "--workspace", str(declared),
            "--project", str(repository), "--runtime-root", str(self.root), "--base", "main", "--head", "feature"])
        with self.read_only(), mock.patch.object(Path, "exists", stale_marker_check), \
             mock.patch.object(api, "workspace_roots", return_value=[declared]), \
             mock.patch.object(api, "discover_repositories", return_value=[repository]), \
             mock.patch.object(api, "choose_repository", return_value=(repository, [repository])), \
             mock.patch.object(api, "remote_target_for_ref", return_value=None), \
             mock.patch.object(api, "resolve_commit_ref", return_value=("a" * 40, "branch")):
            result = flow.launch_preflight(parameters)
        self.assertTrue(result["valid"], result)
        self.assertEqual([], result["errors"])
        self.assertEqual(0, result["writes"])
        self.assertEqual(0, result["network_calls"])
        self.assertEqual(str(self.root), result["canonical_inputs"]["runtime_root"])
        self.assertFalse(first[0])

    def test_forged_lock_never_accepts_foreign_directory_and_wait_is_bounded(self):
        self.lock.write_bytes(b"\0")
        (self.root / "foreign.txt").write_text("unowned data", encoding="utf-8")
        clock = [0.0]
        def advance(delay):
            clock[0] += delay
        with self.read_only(), mock.patch.object(flow.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(flow.time, "sleep", side_effect=advance), \
             mock.patch.object(coordination, "runtime", wraps=coordination.runtime) as verified, \
             self.assertRaisesRegex(ValueError, "runtime is not owned"):
            flow.inspect_runtime_ownership(self.root)
        self.assertAlmostEqual(.5, clock[0])
        verified.assert_not_called()
        self.assertFalse(self.marker.exists())

    def test_foreign_without_lock_and_invalid_marker_remain_rejected(self):
        (self.root / "foreign.txt").write_text("unowned data", encoding="utf-8")
        with self.read_only(), mock.patch.object(flow.time, "sleep", side_effect=AssertionError("unlocked foreign directory waited")), \
             self.assertRaisesRegex(ValueError, "runtime is not owned"):
            flow.inspect_runtime_ownership(self.root)
        self.marker.write_text(json.dumps({"schema": coordination.RUNTIME_SCHEMA,
                                           "directory": str(self.root / "wrong")}), encoding="utf-8")
        with self.read_only(), self.assertRaisesRegex(ValueError, "Invalid review runtime ownership"):
            flow.inspect_runtime_ownership(self.root)

    def test_fresh_empty_directory_remains_valid_without_creating_ownership(self):
        with self.read_only(), mock.patch.object(flow.time, "sleep", side_effect=AssertionError("empty runtime waited")):
            flow.inspect_runtime_ownership(self.root)
        self.assertEqual([], list(self.root.iterdir()))


if __name__ == "__main__":
    unittest.main()

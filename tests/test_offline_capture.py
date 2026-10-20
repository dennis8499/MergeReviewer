from __future__ import annotations

import json
import os
import stat
import subprocess
from unittest import mock

from tests.fixtures import ReviewFixture, api
from tests.support import run_git as git


class OfflineCaptureTests(ReviewFixture):
    def test_all_git_wrappers_force_offline_object_reads_and_disable_prompts(self):
        inherited = {"GIT_NO_LAZY_FETCH": "0", "GIT_TERMINAL_PROMPT": "1"}
        completed = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        with mock.patch.dict(os.environ, inherited), mock.patch.object(api.subprocess, "run", return_value=completed) as run:
            api.run_git(self.repo, ["cat-file", "-e", "HEAD"], env=inherited)
            api.run_git_capture(self.repo, ["cat-file", "-e", "HEAD"], env=inherited)
            self.assertTrue(api.git_ok(self.repo, ["cat-file", "-e", "HEAD"]))
        self.assertEqual(3, run.call_count)
        for call in run.call_args_list:
            self.assertEqual("1", call.kwargs["env"]["GIT_NO_LAZY_FETCH"])
            self.assertEqual("0", call.kwargs["env"]["GIT_TERMINAL_PROMPT"])

    def test_local_missing_promisor_blob_fails_capture_without_launching_fetch(self):
        self.commit()
        remote = self.root / "promisor.git"
        git(self.root, "clone", "--bare", str(self.repo), str(remote))
        # Configure an actual local promisor remote, then remove one loose blob.
        # The bare clone retains its copy; the control read below proves that
        # this fixture would launch Git's implicit fetch without our guard.
        git(self.repo, "remote", "add", "promisor", remote.as_uri())
        git(self.repo, "config", "remote.promisor.promisor", "true")
        git(self.repo, "config", "remote.promisor.partialclonefilter", "blob:none")
        missing = git(self.repo, "rev-parse", "feature:app.py")
        blob = self.repo / ".git" / "objects" / missing[:2] / missing[2:]
        self.assertTrue(blob.is_file(), "fixture must start with a removable loose blob")
        blob.chmod(stat.S_IREAD | stat.S_IWRITE)
        blob.unlink()
        trace = self.root / "guarded-trace.jsonl"
        with mock.patch.dict(os.environ, {"GIT_TRACE2_EVENT": str(trace), "GIT_NO_LAZY_FETCH": "0"}):
            with self.assertRaises(api.ReviewContextError):
                self.capture(workspace_root=self.repo)
        events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
        self.assertFalse(any(event.get("event") == "child_start" and "fetch" in event.get("argv", [])
                             for event in events), "local preparation must not launch an implicit fetch")
        self.assertFalse(blob.exists(), "failed capture must not hydrate the missing object")
        control_trace = self.root / "control-trace.jsonl"
        control_env = dict(os.environ)
        control_env.pop("GIT_NO_LAZY_FETCH", None)
        control_env.update(GIT_TRACE2_EVENT=str(control_trace), GIT_TERMINAL_PROMPT="0")
        control = subprocess.run(["git", "-C", str(self.repo), "cat-file", "-p", missing],
                                 capture_output=True, env=control_env, timeout=30)
        self.assertEqual(0, control.returncode, control.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(b"contract = 'new'\n", control.stdout)
        control_events = [json.loads(line) for line in control_trace.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(event.get("event") == "child_start" and "fetch" in event.get("argv", [])
                            for event in control_events), "control must demonstrate real implicit hydration")

"""Validate observation boundaries, not reviewer judgments."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from tests import agent_acceptance as acceptance
from tests import agent_trace_cli as trace_cli


class AgentTraceTests(unittest.TestCase):
    def wrapper_command(self, root, *arguments):
        return [sys.executable, "-X", "utf8", "-B", str(Path(__file__).with_name("agent_trace_cli.py")),
                "--skill-dir", str(root / "skill"), "--trace", str(root / "trace.jsonl"),
                "--audit-dir", str(root / "audit"), "review_session", *arguments]

    def invoke(self, root, body, workspace_module=None, flow_module=None, arguments=None):
        skill = root / "skill"
        scripts = skill / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "review_session.py").write_text(body, encoding="utf-8")
        if workspace_module:
            (scripts / "review_workspace.py").write_text(workspace_module, encoding="utf-8")
        if flow_module:
            (scripts / "review_flow.py").write_text(flow_module, encoding="utf-8")
        trace = root / "trace.jsonl"
        result = subprocess.run([sys.executable, "-X", "utf8", "-B",
            str(Path(__file__).with_name("agent_trace_cli.py")), "--skill-dir", str(skill),
            "--trace", str(trace), "--audit-dir", str(root / "audit"), "review_session", *(arguments or ["step"])],
            capture_output=True, text=True, encoding="utf-8", timeout=30)
        return result, json.loads(trace.read_text(encoding="utf-8"))

    def test_unhandled_helper_failure_records_actual_exit_and_stderr(self):
        with tempfile.TemporaryDirectory(prefix="review-trace-exit-") as temporary:
            root = Path(temporary)
            result, event = self.invoke(root, "def main(argv):\n    raise RuntimeError('fixture failure')\n")
            self.assertEqual(1, result.returncode)
            self.assertEqual(result.returncode, event["exit_code"])
            self.assertIn("fixture failure", (root / "audit" / "cli" / "001-stderr.txt").read_text())
            self.assertGreater(event["stderr_bytes"], 0)

    def test_unavailable_rows_are_not_counted_as_search_reuse(self):
        with tempfile.TemporaryDirectory(prefix="review-trace-search-") as temporary:
            root = Path(temporary)
            repo = root / "repository"
            acceptance.git(root, "init", "-b", "main", repo)
            (repo / "sample.txt").write_text("needle\n", encoding="utf-8")
            acceptance.git(repo, "add", ".")
            acceptance.git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                           "-c", "commit.gpgsign=false", "commit", "-m", "source")
            module = ("import subprocess\n"
                      "def search():\n"
                      f"    subprocess.run(['git', '-C', {str(repo)!r}, 'grep', '-n', '-e', 'needle', 'HEAD', '--'], capture_output=True)\n"
                      "    return {'searches': [{'status': 'complete'}, {'status': 'unresolved'}]}\n")
            result, event = self.invoke(root, "import review_workspace\ndef main(argv):\n    review_workspace.search()\n    return 0\n", module)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(2, event["logical_search_requests"])
            self.assertEqual(1, event["eligible_search_requests"])
            self.assertEqual(1, event["unresolved_search_requests"])
            self.assertEqual(1, event["executed_search_queries"])
            self.assertEqual(1, event["git_calls"])
            acceptance.save(root / "started.json", {"started_at_unix": time.time()})
            (root / "helper-trace.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
            completion = acceptance.complete_run(root)
            self.assertEqual(0, completion["search_deduplication_ratio"])
            self.assertEqual(1, completion["unresolved_search_requests"])

    def test_artifact_only_session_views_include_cli_load_cost(self):
        body = ("import subprocess, review_flow\ndef main(argv):\n"
                "    subprocess.run(['git', '--version'], capture_output=True)\n"
                "    review_flow.artifact_view()\n    return 0\n")
        flow = ("import subprocess\ndef artifact_view():\n"
                "    subprocess.run(['git', '--version'], capture_output=True)\n")
        for mixed in (False, True):
            with self.subTest(mixed=mixed), tempfile.TemporaryDirectory(prefix="review-trace-view-") as temporary:
                root = Path(temporary)
                requests = [{"id": "page", "op": "view"}]
                if mixed:
                    requests.append({"id": "read", "op": "read"})
                filename = root / "operations.json"
                acceptance.save(filename, requests)
                result, event = self.invoke(root, body, flow_module=flow,
                    arguments=["step", "--requests-file", str(filename)])
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(2, event["git_calls"])
                self.assertEqual(not mixed, event["artifact_only_cli"])
                self.assertEqual(1 if mixed else 2, event["artifact_view_git_calls"])

    def test_slow_start_and_fast_statuses_keep_unique_outputs(self):
        with tempfile.TemporaryDirectory(prefix="review-trace-overlap-") as temporary:
            root = Path(temporary)
            scripts = root / "skill" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "review_session.py").write_text(
                "from pathlib import Path\nimport sys, time\n"
                "def main(argv):\n"
                "    root = Path(argv[1])\n"
                "    if argv[0] == 'start':\n"
                "        (root / 'entered').touch()\n"
                "        deadline = time.monotonic() + 15\n"
                "        while not (root / 'release').exists():\n"
                "            if time.monotonic() > deadline: raise TimeoutError('barrier')\n"
                "            time.sleep(0.01)\n"
                "        print('slow start completed')\n"
                "        return 0\n"
                "    if argv[2] == 'first':\n"
                "        sys.stderr.write('x' * 97)\n"
                "        return 2\n"
                "    print('second status completed')\n"
                "    return 0\n", encoding="utf-8")
            slow = subprocess.Popen(self.wrapper_command(root, "start", str(root)),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while not (root / "entered").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue((root / "entered").exists(), "start did not reach its barrier")
                first = subprocess.run(self.wrapper_command(root, "status", str(root), "first"),
                                       capture_output=True, timeout=10)
                second = subprocess.run(self.wrapper_command(root, "status", str(root), "second"),
                                        capture_output=True, timeout=10)
                self.assertIsNone(slow.poll(), "status should not wait for the running start")
                self.assertEqual(2, first.returncode)
                self.assertEqual(b"x" * 97, first.stderr)
                self.assertEqual(0, second.returncode)
            finally:
                (root / "release").touch()
                stdout, stderr = slow.communicate(timeout=10)
            self.assertEqual(0, slow.returncode, stderr)
            events = [json.loads(line) for line in (root / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([2, 3, 1], [event["sequence"] for event in events])
            self.assertEqual(3, len({event["sequence"] for event in events}))
            self.assertEqual([2, 0, 0], [event["exit_code"] for event in events])
            self.assertEqual(97, events[0]["stderr_bytes"])
            self.assertEqual(b"x" * 97, Path(events[0]["stderr_file"]).read_bytes())
            for event in events:
                self.assertEqual("ReviewerHelperTrace/v5", event["schema"])
                self.assertEqual(event["output_bytes"], len(Path(event["stdout_file"]).read_bytes()))
                self.assertEqual(event["stderr_bytes"], len(Path(event["stderr_file"]).read_bytes()))
                self.assertEqual(0, event["git_calls"])
            self.assertEqual(b"slow start completed\n", (root / "audit" / "cli" / "001-stdout.txt").read_bytes())
            self.assertEqual(["slow start completed"], stdout.decode().splitlines())
            self.assertEqual(3, len(list((root / "audit" / "cli").glob("*-reservation.json"))))
            self.assertEqual(6, len(list((root / "audit" / "cli").glob("*.txt"))))

    def test_reservation_preserves_existing_history_and_abandoned_ids(self):
        with tempfile.TemporaryDirectory(prefix="review-trace-history-") as temporary:
            root = Path(temporary)
            trace, audit = root / "trace.jsonl", root / "audit"
            files = audit / "cli"
            files.mkdir(parents=True)
            history = ''.join(json.dumps({"schema": "ReviewerHelperTrace/v4", "sequence": number}) + "\n"
                              for number in (1, 2, 1))
            trace.write_text(history, encoding="utf-8")
            (files / "001-stderr.txt").write_text("preserved historical stderr", encoding="utf-8")
            (files / "005-reservation.json").touch()  # A killed wrapper must not surrender its identity.
            sequence = trace_cli.reserve_sequence(trace, audit, "review_session", ["status"])
            self.assertEqual(6, sequence)
            self.assertEqual(7, trace_cli.reserve_sequence(trace, audit, "review_session", ["start"]))
            trace_cli.append_trace(trace, {"schema": "ReviewerHelperTrace/v5", "sequence": sequence})
            self.assertTrue(trace.read_text(encoding="utf-8").startswith(history))
            self.assertEqual("preserved historical stderr", (files / "001-stderr.txt").read_text())
            self.assertEqual([1, 2, 1, 6], [json.loads(line)["sequence"] for line in trace.read_text().splitlines()])

    def test_simultaneous_wrappers_append_distinct_complete_events(self):
        with tempfile.TemporaryDirectory(prefix="review-trace-race-") as temporary:
            root = Path(temporary)
            scripts = root / "skill" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "review_session.py").write_text(
                "from pathlib import Path\nimport sys, time\n"
                "def main(argv):\n"
                "    root, label = Path(argv[1]), argv[2]\n"
                "    (root / ('entered-' + label)).touch()\n"
                "    deadline = time.monotonic() + 15\n"
                "    while not (root / 'release').exists():\n"
                "        if time.monotonic() > deadline: raise TimeoutError('barrier')\n"
                "        time.sleep(0.01)\n"
                "    print(label * 1000)\n"
                "    sys.stderr.write(label * 97)\n"
                "    return 2 if label == 'a' else 0\n", encoding="utf-8")
            children = [subprocess.Popen(self.wrapper_command(root, "status", str(root), label),
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE) for label in ("a", "b")]
            try:
                deadline = time.monotonic() + 10
                while not all((root / ("entered-" + label)).exists() for label in ("a", "b")):
                    if time.monotonic() > deadline:
                        self.fail("parallel wrappers did not reach their barrier")
                    time.sleep(0.01)
            finally:
                (root / "release").touch()
                results = [child.communicate(timeout=10) for child in children]
            self.assertEqual([2, 0], [child.returncode for child in children], results)
            events = [json.loads(line) for line in (root / "trace.jsonl").read_text().splitlines()]
            self.assertEqual({1, 2}, {event["sequence"] for event in events})
            self.assertEqual(2, len(events))
            self.assertEqual({b"a" * 97, b"b" * 97}, {Path(event["stderr_file"]).read_bytes() for event in events})
            self.assertEqual({b"a" * 1000 + b"\n", b"b" * 1000 + b"\n"},
                             {Path(event["stdout_file"]).read_bytes() for event in events})


if __name__ == "__main__":
    unittest.main()

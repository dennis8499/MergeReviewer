"""Trace actual reviewer CLI commands for a real agent exercise.

Example: python -X utf8 -B trace_review_cli.py --skill-dir PATH --trace trace.jsonl
git_review_context --workspace ... --project ... --base main --head feature

The parent timer starts BEFORE spawning the agent and stops at its completion
marker AFTER report copy and context cleanup. Final chat text is excluded.
This wrapper records helper costs, not that complete elapsed timer.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
import time


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("module", choices=("git_review_context", "review_workspace", "review_report",
                                           "review_session", "evidence", "release_report", "review_worklist"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(args.skill_dir.resolve() / "scripts"))
    stdout, stderr = io.StringIO(), io.StringIO()
    original = subprocess.run
    git_calls, git_seconds = 0, 0.0
    commands = []

    def observed(command, *positional, **kwargs):
        nonlocal git_calls, git_seconds
        is_git = isinstance(command, (list, tuple)) and command and Path(str(command[0])).stem.lower() == "git"
        start = time.perf_counter()
        if is_git:
            git_calls += 1
            # Record operation names, not command payloads or arbitrary data.
            tokens = list(map(str, command[1:]))
            if len(tokens) >= 2 and tokens[0] == "-C":
                tokens = tokens[2:]
            commands.append(next((token for token in tokens if not token.startswith("-")), "unknown"))
        try:
            return original(command, *positional, **kwargs)
        finally:
            if is_git:
                git_seconds += time.perf_counter() - start

    started, wall_started = time.perf_counter(), time.time()
    code = 2
    subprocess.run = observed
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = importlib.import_module(args.module).main(args.arguments)
        return code
    finally:
        subprocess.run = original
        raw = stdout.getvalue()
        event = {"schema": "ReviewerHelperTrace/v1", "module": args.module,
                 "started_at_unix": wall_started, "elapsed_seconds": time.perf_counter() - started,
                 "git_calls": git_calls, "git_seconds": git_seconds, "git_operations": commands,
                 "output_bytes": len(raw.encode("utf-8")), "exit_code": code}
        args.trace.parent.mkdir(parents=True, exist_ok=True)
        with args.trace.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.write(raw)
        sys.stderr.write(stderr.getvalue())


if __name__ == "__main__":
    raise SystemExit(main())

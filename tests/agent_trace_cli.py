"""Trace a frozen reviewer CLI and archive agent-authored evidence before cleanup.

The wrapper supplies no findings, semantic judgments or expected answers. Use it
for every reviewer-helper invocation in an independent agent acceptance run.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import functools
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import traceback


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


@contextmanager
def trace_lock(trace):
    """Serialize short observer writes, never the helper being observed."""
    trace.parent.mkdir(parents=True, exist_ok=True)
    with trace.with_name(trace.name + ".lock").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def reserve_sequence(trace, audit, module, arguments):
    """Reserve an invocation before it starts; completed trace rows are history."""
    files = audit / "cli"
    files.mkdir(parents=True, exist_ok=True)
    with trace_lock(trace):
        used = {int(match.group(1)) for path in files.iterdir()
                if (match := re.fullmatch(r"(\d+)-(?:stdout\.txt|stderr\.txt|reservation\.json)", path.name))}
        if trace.is_file():
            for line in trace.read_text(encoding="utf-8").splitlines():
                sequence = json.loads(line).get("sequence")
                if isinstance(sequence, int) and not isinstance(sequence, bool):
                    used.add(sequence)
        sequence = max(used, default=0) + 1
        while True:
            try:
                with (files / f"{sequence:03}-reservation.json").open("x", encoding="utf-8") as stream:
                    json.dump({"schema": "ReviewerHelperReservation/v1", "sequence": sequence,
                               "module": module, "operation": arguments[0] if arguments else None,
                               "reserved_at_unix": time.time()}, stream)
                    stream.write("\n")
                return sequence
            except FileExistsError:
                sequence += 1


def append_trace(trace, event):
    with trace_lock(trace):
        with trace.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")


def argument(values, flag):
    try:
        return values[values.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def find_context(arguments, audit):
    explicit = argument(arguments, "--context-dir")
    if explicit:
        return Path(explicit)
    root, identifier = argument(arguments, "--runtime-root"), argument(arguments, "--request-id")
    if root and identifier:
        file = Path(root) / "jobs" / (identifier + ".json")
        if file.is_file():
            context = load(file).get("context_dir")
            if context:
                return Path(context)
    file = audit / "last-context.json"
    if file.is_file():
        return Path(load(file)["context_dir"])
    return None


def response_context(raw):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if isinstance(item.get("context_dir"), str):
                return Path(item["context_dir"])
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return None


def report_directory(context):
    if context is None or not (context / "manifest.json").is_file():
        return None
    manifest = load(context / "manifest.json")
    if manifest.get("report_root"):
        return Path(manifest["report_root"])
    selected = manifest.get("selected_repo")
    for repo in manifest.get("repositories", []):
        if repo.get("id") == selected:
            return Path(repo["path"]) / "review-reports"
    return None


def known_reports(directory):
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.glob("*.md")} if directory and directory.is_dir() else {}


def git_operation(command):
    values = list(map(str, command[1:]))
    index = 0
    while index < len(values):
        value = values[index]
        if value in ("-C", "-c", "--git-dir", "--work-tree"):
            index += 2
        elif value.startswith("-"):
            index += 1
        else:
            return value
    return "unknown"


def artifact_only_cli(module, arguments):
    """Identify saved-artifact requests without trusting or altering their input."""
    if arguments and arguments[0] == "view" and module in ("evidence", "review_workspace"):
        return True
    if module != "review_session" or not arguments or arguments[0] != "step":
        return False
    filename = argument(arguments, "--requests-file")
    if not filename:
        return False
    try:
        requests = load(Path(filename))
    except (OSError, ValueError, UnicodeError):
        return False
    return isinstance(requests, list) and bool(requests) and all(
        isinstance(item, dict) and item.get("op") == "view" for item in requests)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("module", choices=("git_review_context", "review_workspace", "review_report",
                                           "review_session", "evidence", "release_report"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    artifact_only = artifact_only_cli(args.module, args.arguments)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(args.skill_dir.resolve() / "scripts"))
    args.audit_dir.mkdir(parents=True, exist_ok=True)
    sequence = reserve_sequence(args.trace, args.audit_dir, args.module, args.arguments)
    context = find_context(args.arguments, args.audit_dir)
    reports = report_directory(context)
    previous = known_reports(reports)
    archive_started = time.perf_counter()
    publishing = args.module in ("review_report", "release_report") or (
        args.module == "review_session" and args.arguments and args.arguments[0] == "finish")
    if publishing and context and context.is_dir():
        shutil.copytree(context, args.audit_dir / "attempts" / f"publish-{sequence:03}")
    archive_seconds = time.perf_counter() - archive_started
    counters, operations, operation_seconds = Counter(), Counter(), Counter()
    original_run = subprocess.run

    def observed(command, *positional, **keywords):
        is_git = isinstance(command, (list, tuple)) and command and Path(str(command[0])).stem.lower() == "git"
        tick = time.perf_counter()
        if is_git:
            counters["git_calls"] += 1
            operations[git_operation(command)] += 1
        try:
            completed = original_run(command, *positional, **keywords)
            if is_git and git_operation(command) == "grep" and completed.returncode in (0, 1):
                counters["successful_search_git_calls"] += 1
            return completed
        finally:
            if is_git:
                elapsed = time.perf_counter() - tick
                counters["git_seconds"] += elapsed
                operation_seconds[git_operation(command)] += elapsed

    hooks = []
    for name, function, counter in (("workspace_report", "validate", "full_result_validations"),
                                    ("release_report", "validate_result", "full_result_validations"),
                                    ("frozen_evidence", "validate", "snapshot_repo_validations")):
        if not (args.skill_dir / "scripts" / (name + ".py")).is_file():
            continue
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        original = getattr(module, function, None)
        if original is None:
            continue

        def make_hook(target, count):
            @functools.wraps(target)
            def observed_validation(*positional, **keywords):
                counters[count] += 1
                return target(*positional, **keywords)
            return observed_validation

        setattr(module, function, make_hook(original, counter))
        hooks.append((module, function, original))
    # Observe logical search requests separately from actual Git computations.
    # No expected result, cache state or reviewer decision is supplied here.
    for name in ("review_workspace", "evidence"):
        if not (args.skill_dir / "scripts" / (name + ".py")).is_file():
            continue
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        original = getattr(module, "search", None)
        if original is None:
            continue

        def make_search_hook(target, workspace_search):
            @functools.wraps(target)
            def observed_search(*positional, **keywords):
                before = operations["grep"]
                successful_before = counters["successful_search_git_calls"]
                result = target(*positional, **keywords)
                rows = result.get("searches", []) if workspace_search else [result]
                count = len(rows)
                eligible = sum(row.get("status", "complete") == "complete" for row in rows)
                counters["logical_search_requests"] += count
                counters["eligible_search_requests"] += eligible
                counters["unresolved_search_requests"] += count - eligible
                counters["attempted_search_queries"] += operations["grep"] - before
                counters["executed_search_queries"] += counters["successful_search_git_calls"] - successful_before
                return result
            return observed_search

        setattr(module, "search", make_search_hook(original, name == "review_workspace"))
        hooks.append((module, "search", original))
    if (args.skill_dir / "scripts" / "review_flow.py").is_file():
        flow = importlib.import_module("review_flow")
        original_view = getattr(flow, "artifact_view", None)
        if original_view is not None:
            @functools.wraps(original_view)
            def observed_view(*positional, **keywords):
                before = counters["git_calls"]
                try:
                    return original_view(*positional, **keywords)
                finally:
                    counters["artifact_operation_git_calls"] += counters["git_calls"] - before
            flow.artifact_view = observed_view
            hooks.append((flow, "artifact_view", original_view))
    output, errors = io.StringIO(), io.StringIO()
    started, wall_started = time.perf_counter(), time.time()
    code = 2
    subprocess.run = observed
    try:
        try:
            with redirect_stdout(output), redirect_stderr(errors):
                code = importlib.import_module(args.module).main(args.arguments)
        except SystemExit as error:
            code = error.code if isinstance(error.code, int) else (0 if error.code is None else 2)
        except Exception:
            traceback.print_exc(file=errors)
            code = 1
        code = 0 if code is None else code
        return code
    finally:
        helper_seconds = time.perf_counter() - started
        subprocess.run = original_run
        for module, function, original in hooks:
            setattr(module, function, original)
        raw, stderr = output.getvalue(), errors.getvalue()
        discovered = response_context(raw)
        if discovered:
            context = discovered
            save(args.audit_dir / "last-context.json", {"context_dir": str(discovered)})
        elif context:
            save(args.audit_dir / "last-context.json", {"context_dir": str(context)})
        if reports is None:
            reports = report_directory(context)
        if reports:
            published_file = args.audit_dir / "published.json"
            published = load(published_file) if published_file.is_file() else []
            for path, digest in known_reports(reports).items():
                if previous.get(path) != digest:
                    target = args.audit_dir / "reports" / Path(path).name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target)
                    published.append({"source_report": path, "archived_report": str(target), "sha256": digest,
                                      "helper_sequence": sequence})
            if published:
                save(published_file, published)
        files = args.audit_dir / "cli"
        files.mkdir(exist_ok=True)
        stdout_file, stderr_file = files / f"{sequence:03}-stdout.txt", files / f"{sequence:03}-stderr.txt"
        with stdout_file.open("x", encoding="utf-8", newline="") as stream:
            stream.write(raw)
        with stderr_file.open("x", encoding="utf-8", newline="") as stream:
            stream.write(stderr)
        event = {"schema": "ReviewerHelperTrace/v5", "sequence": sequence, "module": args.module,
                 "stdout_file": str(stdout_file), "stderr_file": str(stderr_file),
                 "operation": args.arguments[0] if args.arguments else None,
                 "started_at_unix": wall_started, "elapsed_seconds": helper_seconds,
                 "observer_seconds": archive_seconds + time.perf_counter() - started - helper_seconds,
                 "git_calls": counters["git_calls"], "git_seconds": counters["git_seconds"],
                 "git_operations": dict(operations), "git_operation_seconds": dict(operation_seconds),
                 "full_result_validations": counters["full_result_validations"],
                 "snapshot_repo_validations": counters["snapshot_repo_validations"],
                 "artifact_only_cli": artifact_only,
                 "artifact_view_git_calls": counters["git_calls"] if artifact_only else counters["artifact_operation_git_calls"],
                 "logical_search_requests": counters["logical_search_requests"],
                 "eligible_search_requests": counters["eligible_search_requests"],
                 "unresolved_search_requests": counters["unresolved_search_requests"],
                 "attempted_search_queries": counters["attempted_search_queries"],
                 "executed_search_queries": counters["executed_search_queries"],
                 "output_bytes": len(raw.encode("utf-8")), "stderr_bytes": len(stderr.encode("utf-8")),
                 "exit_code": code, "model_analysis_seconds": None, "tokens": None, "cost": None}
        append_trace(args.trace, event)
        sys.stdout.write(raw)
        sys.stderr.write(stderr)


if __name__ == "__main__":
    raise SystemExit(main())

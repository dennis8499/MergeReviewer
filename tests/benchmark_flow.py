"""Alternate a frozen helper baseline and candidate on one authored scenario.

Measures helpers only. Never synthesizes findings/coverage or times model analysis.
Use an already reviewed --scenario and the exact corresponding --fixture-root.
"""
from __future__ import annotations
from collections import Counter
import argparse
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import runpy
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from benchmark_review import normalize, write_json, restore_transport


def invoke():
    metrics, script = Path(sys.argv[2]), Path(sys.argv[3])
    sys.argv = sys.argv[3:]
    sys.path.insert(0, str(script.parent))
    counts, original = Counter(), subprocess.Popen
    class Measured(original):
        def __init__(self, command, *args, **kwargs):
            if isinstance(command, (list, tuple)) and command and Path(str(command[0])).stem == "git":
                counts["git_processes"] += 1
            super().__init__(command, *args, **kwargs)
    subprocess.Popen = Measured
    report = importlib.import_module("workspace_report")
    validate = report.validate
    def checked(*args, **kwargs):
        result = validate(*args, **kwargs)
        write_json(metrics.with_suffix(".validated.json"), result)
        return result
    report.validate = checked
    workspace = importlib.import_module("review_workspace")
    build, capture_phases = workspace.build, {}
    def timed_build(args, api):
        original_phase = api.capture_phase
        previous, tick = [None], [time.perf_counter()]
        def phase(name, **fields):
            current = time.perf_counter()
            if previous[0]:
                capture_phases[previous[0]] = capture_phases.get(previous[0], 0) + current - tick[0]
            previous[0], tick[0] = name, current
            return original_phase(name, **fields)
        api.capture_phase = phase
        try:
            return build(args, api)
        finally:
            if previous[0]:
                capture_phases[previous[0]] = capture_phases.get(previous[0], 0) + time.perf_counter() - tick[0]
            api.capture_phase = original_phase
    workspace.build = timed_build
    try:
        runpy.run_path(str(script), run_name="__main__")
    finally:
        write_json(metrics, {**counts, "_capture_phases": capture_phases})


def source_signature(manifest):
    """Compare frozen identities, every inventory entry, instructions and gates."""
    keys = ("selected_repo", "seed_repos", "extension_enabled", "review_basis", "coverage_policy",
            "merge_parent_policy", "contract_scope_policy", "required_coverage", "required_merge_views", "context_gaps",
            "workspace_instructions", "changed_contracts", "searches")
    signature = {key: manifest.get(key) for key in keys}
    signature["repositories"] = []
    for row in manifest["repositories"]:
        item = {key: row.get(key) for key in ("id", "name", "path", "role", "state", "head_sha",
                "review_source", "diff_base", "changed_files", "limitations", "ancestor_path", "instructions")}
        item["sources"] = {name: {key: value[key] for key in ("ref", "tree_sha", "files")}
                           for name, value in row["sources"].items()}
        signature["repositories"].append(item)
    return signature


def comparable_artifacts(value, context):
    """Normalize the additive policy marker and an empty boundary ledger.

    Nonempty judgments, scopes, searches, graphs and frozen source identities
    remain part of the equality oracle. V2 is additive only for a single Repo;
    unknown policies and v2 Group judgments remain visible.
    """
    single_repo = isinstance(value, dict) and isinstance(value.get("sources"), dict) and value["sources"].get("extension_enabled") is False
    def additive(item):
        if isinstance(item, dict):
            return {key: additive(entry) for key, entry in item.items()
                    if not (key == "contract_scope_policy" and (entry in (None, "boundary-contracts/v1")
                            or single_repo and entry == "boundary-contracts/v2"))
                    and not (key == "boundary_reviews" and entry == [])}
        if isinstance(item, list):
            return [additive(entry) for entry in item]
        return item
    return normalize(additive(value), context)


def operation_batches(operations, limit):
    """Keep the same ordered authored operations in each implementation's steps."""
    if type(limit) is not int or limit < 1:
        raise ValueError("operations-per-step must be a positive integer")
    return [operations[index:index + limit] for index in range(0, len(operations), limit)]


def wait_for_validation(lock, *, timeout=900):
    deadline = time.monotonic() + timeout
    while lock.exists():
        if time.monotonic() > deadline:
            raise TimeoutError("Local validation is still active; benchmark was not measured.")
        time.sleep(.5)


class ValidationWatch:
    """Reject a sample if another local validation overlaps its measurement."""

    def __init__(self, lock):
        self.lock = lock
        self.seen = threading.Event()
        self.stop = threading.Event()
        self.thread = None

    def check(self):
        if self.lock.exists():
            self.seen.set()

    def __enter__(self):
        self.check()
        def monitor():
            while not self.stop.wait(.05):
                self.check()
        self.thread = threading.Thread(target=monitor, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.check()
        self.stop.set()
        self.thread.join()


def measure(source, fixture, scenario, destination, candidate, runtime, *, mechanical_v1=False,
            operations_per_step=None):
    destination.mkdir()
    metrics_file = destination / "commands.json"
    totals, phases, capture_parts = Counter(), {}, {}
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    context, identifier = None, uuid.uuid4().hex
    # 0.18.0 already has the facade. Compare it through the same public entry
    # points; retain legacy batch replay for older frozen source archives.
    use_flow = (source / "skills/merge-reviewer/scripts/review_flow.py").is_file()
    def cli(script, *args):
        nonlocal capture_parts
        began = time.perf_counter()
        command = [sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()), "_invoke",
                   str(metrics_file), str(source / "skills/merge-reviewer/scripts" / script), *map(str, args)]
        process = subprocess.run(command, capture_output=True, encoding="utf-8", env=environment)
        elapsed = time.perf_counter() - began
        totals["python_cli"] += 1
        totals["stdout_bytes"] += len(process.stdout.encode("utf-8"))
        totals["stderr_bytes"] += len(process.stderr.encode("utf-8"))
        command_metrics = json.loads(metrics_file.read_text(encoding="utf-8"))
        parts = command_metrics.pop("_capture_phases", {})
        if parts:
            capture_parts = parts
        totals.update(command_metrics)
        if process.returncode:
            raise RuntimeError(f"{script} ({process.returncode}): {process.stderr}\n{process.stdout}")
        return process.stdout.strip(), elapsed
    def expand(value):
        if value.get("schema") == "MergeReviewEvidenceStep/v2" and not value.get("projection_complete"):
            raise ValueError("Benchmark requires unmeasured artifact paging; use a complete projection.")
        if value.get("schema") in ("MergeReviewEvidenceSummary/v1", "MergeReviewEvidenceStep/v1"):
            # This fixture must return every required line/match in the first
            # step response; hidden pagination cannot improve the measurements.
            if value["schema"] == "MergeReviewEvidenceSummary/v1":
                raise ValueError("Benchmark requires unmeasured artifact paging; use a complete projection.")
            raw = (context / "views" / (value["artifact"] + ".json")).read_bytes()
            if hashlib.sha256(raw).hexdigest() != value["artifact"]:
                raise ValueError("Benchmark artifact checksum differs")
            return json.loads(raw)
        return restore_transport(value)
    capture = scenario.get("capture", {})
    args = ["--workspace", fixture, "--project", capture.get("project", "orders-api"),
            "--base", capture.get("base", "main"), "--head", capture.get("head", "feature/amount")]
    try:
        if use_flow:
            context = runtime / "sessions" / identifier
            output, phases["capture_seconds"] = cli("review_session.py", "start", *args,
                "--runtime-root", runtime, "--request-id", identifier)
            start = expand(json.loads(output))
            if mechanical_v1 and candidate:
                # This disposable benchmark context models recovery of an old
                # v1 context, so identical authored v3 decisions can exercise
                # mechanical changes without inventing v2 semantic decisions.
                manifest_path = context / "manifest.json"
                legacy = json.loads(manifest_path.read_text(encoding="utf-8"))
                legacy["contract_scope_policy"] = "boundary-contracts/v1"
                legacy.pop("boundary_changes", None)
                write_json(manifest_path, legacy)
        else:
            output, phases["capture_seconds"] = cli("git_review_context.py", *args, "--summary-v2")
            start = json.loads(output)
            context = Path(start["context_dir"])
        reads, searches = [], []
        began = time.perf_counter()
        if use_flow:
            operations = []
            for index, contract in enumerate(scenario["contracts"]):
                operations.append({"id": f"contract-{index}", "op": "register-contract", "contract": contract})
            operations.extend({"id": f"read-{i}", "op": "read", **row} for i, row in enumerate(scenario["reads"]))
            operations.extend({"id": f"search-{i}", "op": "search", **row} for i, row in enumerate(scenario["searches"]))
            requests = context / "round.json"
            width = operations_per_step if operations_per_step is not None else max(1, len(operations))
            for batch_operations in operation_batches(operations, width):
                write_json(requests, batch_operations)
                output, _ = cli("review_session.py", "step", "--runtime-root", runtime, "--request-id",
                                identifier, "--requests-file", requests)
                batch = expand(json.loads(output))
                if [row.get("id") for row in batch["operations"]] != [row["id"] for row in batch_operations]:
                    raise ValueError("Benchmark step did not return every requested operation in order.")
                for row in batch["operations"]:
                    if row["id"].startswith("read-"):
                        reads.append(row["result"])
                    if row["id"].startswith("search-"):
                        searches.extend(row["result"]["searches"])
        else:
            for command, entries in (("register-contract", scenario["contracts"]),
                                     ("read", scenario["reads"]), ("search", scenario["searches"])):
                if not entries:
                    continue
                requests = context / (command + ".json")
                write_json(requests, entries)
                output, _ = cli("review_workspace.py", "batch-" + command, "--context-dir", context,
                    "--requests-file", requests, *(["--auto-content"] if command == "read" else
                                                 ["--page-size", "100"] if command == "search" else []))
                if command in ("read", "search"):
                    key = "reads" if command == "read" else "searches"
                    (reads if command == "read" else searches).extend(expand(json.loads(output))[key])
        phases["read_search_seconds"] = time.perf_counter() - began
        manifest = json.loads((context / "manifest.json").read_text(encoding="utf-8"))
        patches = {p.relative_to(context).as_posix(): p.read_bytes().hex() for p in context.rglob("*.patch")}
        write_json(context / "draft.json", scenario["draft"])
        if use_flow:
            output, phases["validation_publish_seconds"] = cli("review_session.py", "finish",
                "--runtime-root", runtime, "--request-id", identifier)
            job = json.loads(output)
            report = Path(job["report_path"])
            phase_seconds = job.get("metrics", {}).get("phase_seconds", {})
        else:
            output, phases["validation_publish_seconds"] = cli("review_report.py", "--context-dir", context,
                                                             "--result", context / "draft.json")
            report, phase_seconds = Path(output), capture_parts
        validated = json.loads(metrics_file.with_suffix(".validated.json").read_text(encoding="utf-8"))
        markdown = report.read_text(encoding="utf-8")
        write_json(destination / "report.json", {"path": str(report), "sha256": hashlib.sha256(report.read_bytes()).hexdigest()})
        (destination / "report.md").write_text(markdown, encoding="utf-8")
        # Body and evidence of each finding must remain byte-equivalent, despite
        # the intended new report title, overview and section placement.
        blocks = {}
        for finding in validated["findings"]:
            anchor = '<a id="finding-' + finding["id"] + '"></a>'
            block = markdown.split(anchor, 1)[1].split('<a id="finding-', 1)[0].split("## 範圍與限制", 1)[0].strip()
            blocks[finding["id"]] = block
        artifacts = comparable_artifacts({"sources": source_signature(manifest), "reads": reads, "searches": searches,
                               "patches": patches, "validated": validated, "finding_blocks": blocks}, context)
        write_json(destination / "artifacts.json", artifacts)
        row = {**phases, "helper_total_seconds": sum(phases.values()), "counts": dict(totals),
               "network_activity": manifest["network_activity"], "phase_seconds": phase_seconds,
               "network_wait_seconds": 0 if not manifest["network_activity"]["fetch_attempt_count"]
                    else phase_seconds.get("waiting-network"),
               "packing_seconds": phase_seconds.get("snapshot-capture", 0),
               "model_analysis_seconds": None}
        write_json(destination / "metrics.json", row)
        report.unlink()  # Only this concrete benchmark output, never historical reports.
        return row, artifacts
    finally:
        if context and context.exists():
            cli("review_session.py", "cleanup", "--context-dir", context)


def paired_gates(rows, *, large_multi_repo=False):
    for mode, pair in rows.items():
        if (not pair["baseline"] or len(pair["baseline"]) != len(pair["candidate"])
                or any(row["helper_total_seconds"] <= 0 for row in pair["baseline"] + pair["candidate"])):
            raise ValueError(f"Invalid or unpaired helper timings: {mode}")
    ratios = {mode: statistics.median(candidate["helper_total_seconds"] / baseline["helper_total_seconds"]
              for baseline, candidate in zip(pair["baseline"], pair["candidate"])) for mode, pair in rows.items()}
    limits = {mode: (0.75 if mode == "warm" and large_multi_repo else 1.05) for mode in rows}
    return ratios, limits, {mode: ratios[mode] <= limits[mode] for mode in rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--fixture-root", required=True, type=Path)
    parser.add_argument("--scenario", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--mechanical-v1", action="store_true",
                        help="Replay the identical legacy v3 draft against v1-policy benchmark contexts.")
    parser.add_argument("--large-multi-repo", action="store_true",
                        help="Apply the 25%% warm-cache target to the 12-Repo/12,000-file fixture.")
    parser.add_argument("--operations-per-step", type=int,
                        help="Use the same bounded operation batches in both variants; every step is timed.")
    parser.add_argument("--warm-first", action="store_true",
                        help="Measure all required modes, starting with warm-cache pairs.")
    parser.add_argument("--validation-lock", type=Path,
        default=ROOT.parent / "GitlabWorkSpace/.gitlab-workspace-validation/runner.lock",
        help="Wait for this local build/test lock before timing, to avoid self-induced I/O contention.")
    args = parser.parse_args()
    if args.repeats < 1 or args.large_multi_repo and args.repeats < 5:
        parser.error("large multi-Repo acceptance needs at least five paired repeats")
    if args.operations_per_step is not None and args.operations_per_step < 1:
        parser.error("--operations-per-step must be positive")
    wait_for_validation(args.validation_lock)
    time.sleep(2)  # Let the completed validation runner close its log/receipt files.
    scenario = json.loads(args.scenario.read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=False)
    rows, expected = {}, None
    # Keep source evidence paths below Windows MAX_PATH even when the benchmark
    # report directory itself has a long descriptive name.
    runtime_base = Path(tempfile.mkdtemp(prefix="flowbench-"))
    def measured(*parameters, **options):
        wait_for_validation(args.validation_lock)
        with ValidationWatch(args.validation_lock) as watch:
            result = measure(*parameters, **options)
        if watch.seen.is_set():
            raise RuntimeError("Local validation overlapped this sample; its raw data is invalid for acceptance.")
        return result

    modes = ("warm", "cold", "staggered") if args.warm_first else ("cold", "warm", "staggered")
    for mode in modes:
        rows[mode] = {"baseline": [], "candidate": []}
        if mode != "cold":
            for candidate in (False, True):
                label = "candidate" if candidate else "baseline"
                measured(ROOT if candidate else args.baseline, args.fixture_root, scenario,
                         args.output_dir / (mode + "-prime-" + label), candidate,
                         runtime_base / (mode + "-" + label), mechanical_v1=args.mechanical_v1,
                         operations_per_step=args.operations_per_step)
        for repetition in range(args.repeats):
            order = (False, True) if repetition % 2 == 0 else (True, False)
            for candidate in order:
                label = "candidate" if candidate else "baseline"
                destination = args.output_dir / f"{mode}-{label}-{repetition+1}"
                runtime = (runtime_base / (f"cold-{label}-{repetition}") if mode == "cold" else
                           runtime_base / (mode + "-" + label))
                result, artifacts = measured(ROOT if candidate else args.baseline, args.fixture_root,
                                              scenario, destination, candidate, runtime, mechanical_v1=args.mechanical_v1,
                                              operations_per_step=args.operations_per_step)
                if expected is None:
                    expected = artifacts
                if expected != artifacts:
                    raise AssertionError(f"Full frozen evidence/result/finding body differs: {destination}")
                rows[mode][label].append(result)
                print(json.dumps({"mode": mode, "label": label, "run": repetition+1, **result}), flush=True)
    medians = {mode: {label: statistics.median(row["helper_total_seconds"] for row in entries)
                      for label, entries in pair.items()} for mode, pair in rows.items()}
    if args.large_multi_repo:
        repos = expected["sources"]["repositories"]
        files = sum(len(row.get("sources", {}).get("head", {}).get("files", [])) for row in repos)
        if len(repos) < 12 or files < 12000:
            raise ValueError("Large acceptance requires at least 12 Repos and 12,000 fixed head files.")
    ratios, limits, gates = paired_gates(rows, large_multi_repo=args.large_multi_repo)
    write_json(args.output_dir / "summary.json", {"schema": "MergeReviewFlowBenchmark/v1",
        "repeats": args.repeats, "normalized_evidence_result_finding_bodies_equal": True,
        "medians": medians, "paired_ratio_medians": ratios, "ratio_limits": limits, "gates": gates,
        "mechanical_v1": args.mechanical_v1, "large_multi_repo": args.large_multi_repo,
        "operations_per_step": args.operations_per_step,
        "mode_order": list(modes),
        "runs": rows, "model_analysis_seconds": None,
        "staggered_definition": "Sequential independent starts while prior review receipts stay warm; overlapping stages are tested separately.",
        "scope": "Helper replay of an authored scenario; model quality and latency are separate acceptance gates."})
    print(json.dumps({"medians": medians, "gates": gates}), flush=True)
    return 0 if all(gates.values()) else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["_invoke"]:
        invoke()
    else:
        raise SystemExit(main())

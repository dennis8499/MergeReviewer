"""Compare helper cost using an authored fixed-source review, never model decisions.

Run with --baseline pointing to an unchanged source checkout, --fixture-root to
the isolated Git workspace, and --scenario to a JSON object containing ordered
contracts, reads, searches and the actual review draft. All output goes to
--output-dir. The same fixed commits/draft are used for both implementations.
"""
from __future__ import annotations

import argparse
from collections import Counter
import importlib
import hashlib
import json
from pathlib import Path
import re
import runpy
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def invoke():
    """Instrument actual CLI Git subprocesses, including their startup cost."""
    metrics_path, script = Path(sys.argv[2]), Path(sys.argv[3])
    sys.argv = sys.argv[3:]
    sys.path.insert(0, str(script.parent))
    commands = Counter()
    original = subprocess.run

    def measured(command, *args, **kwargs):
        if isinstance(command, (list, tuple)) and command and Path(str(command[0])).stem == "git":
            commands["git"] += 1
        return original(command, *args, **kwargs)

    subprocess.run = measured
    if script.name == "review_report.py":
        report = importlib.import_module("workspace_report")
        original_validate = report.validate

        def validated(*args, **kwargs):
            result = original_validate(*args, **kwargs)
            write_json(metrics_path.with_suffix(".validated.json"), result)
            return result

        report.validate = validated
    try:
        runpy.run_path(str(script), run_name="__main__")
    finally:
        write_json(metrics_path, dict(commands))


def normalize(value, context):
    # Capture-instance fields only; blob/tree/content hashes remain compared.
    volatile = {"generated_at", "captured_at", "capture_completed_at", "refs_pinned_at", "run_id", "context_sha256", "generation"}
    if isinstance(value, dict):
        return {k: normalize(v, context) for k, v in value.items() if k not in volatile
                and not (k in {"required_merge_views", "merge_parent_review"} and v == [])
                and not (k == "merge_parent_policy" and not value.get("required_merge_views"))}
    if isinstance(value, list):
        return [normalize(v, context) for v in value]
    if isinstance(value, str):
        value = value.replace(str(context), "<context>").replace(context.as_posix(), "<context>")
        return re.sub(r"(?m)^(- (?:快照擷取|全部 Repo SHA 固定完成)：).*（UTC）。$",
                      r"\1<capture-times>（UTC）。", value)
    return value


def restore_transport(value):
    """Compare complete evidence, never the shortened view printed to an agent."""
    if value.get("schema") == "ReviewEvidenceView/v1":
        raw = Path(value["artifact_path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != value["artifact"]:
            raise ValueError("Evidence view checksum mismatch")
        value = json.loads(raw)
    if value.get("schema") == "MergeReviewEvidenceBatch/v1":
        return {"reads": [{**{key: item for key, item in row.items() if key != "content_id"},
                           "lines": value["contents"][row["content_id"]]["lines"]} for row in value["reads"]]}
    return value


def measure(source, fixture, scenario, destination, optimized):
    scripts = source / "skills/merge-reviewer/scripts"
    metrics_path = destination / "command-metrics.json"
    totals = Counter()

    def cli(script, *args):
        totals["python_cli"] += 1
        started = time.perf_counter()
        result = subprocess.run([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
                                 "_invoke", str(metrics_path), str(scripts / script), *map(str, args)],
                                capture_output=True, text=True, encoding="utf-8")
        elapsed = time.perf_counter() - started
        totals.update(json.loads(metrics_path.read_text(encoding="utf-8")))
        totals["output_bytes"] += len(result.stdout.encode("utf-8"))
        if result.returncode:
            raise RuntimeError(f"{script} failed: {result.stderr}")
        return result.stdout, elapsed

    capture = scenario.get("capture", {})
    # Both frozen versions already support these batching/summary interfaces.
    output, capture_time = cli("git_review_context.py", "--workspace", fixture,
                               "--project", capture.get("project", "orders-api"),
                               "--base", capture.get("base", "main"),
                               "--head", capture.get("head", "feature/amount"), "--no-fetch", "--summary-v2" if optimized else "--summary")
    context = Path(json.loads(output)["context_dir"])
    counts = {"capture": dict(totals)}
    captured_bytes = len(output.encode("utf-8"))
    reads, searches = [], []
    read_search_time = 0.0
    oracle_time = 0.0
    try:
        if optimized and scenario["contracts"]:
            request_file = context / "benchmark-contracts.json"
            write_json(request_file, scenario["contracts"])
            _, elapsed = cli("review_workspace.py", "batch-register-contract", "--context-dir", context,
                             "--requests-file", request_file)
            read_search_time += elapsed
        else:
            for index, contract in enumerate(scenario["contracts"]):
                request_file = context / f"benchmark-contract-{index}.json"
                write_json(request_file, contract)
                _, elapsed = cli("review_workspace.py", "register-contract", "--context-dir", context,
                                 "--contract-file", request_file)
                read_search_time += elapsed
        for name in ("read", "search"):
            plural = "reads" if name == "read" else "searches"
            request_file = context / f"benchmark-{plural}.json"
            write_json(request_file, scenario[plural])
            output, elapsed = cli("review_workspace.py", "batch-" + name, "--context-dir", context,
                                   "--requests-file", request_file,
                                   *(["--auto-content"] if optimized and name == "read" else
                                     ["--page-size", "100"] if optimized else []))
            read_search_time += elapsed
            oracle_started = time.perf_counter()
            (reads if name == "read" else searches).extend(restore_transport(json.loads(output))[plural])
            oracle_time += time.perf_counter() - oracle_started
        counts["read_search"] = dict(Counter(totals) - Counter(counts["capture"]))
        manifest = json.loads((context / "manifest.json").read_text(encoding="utf-8"))
        patches = {path.relative_to(context).as_posix(): path.read_bytes().hex()
                   for path in sorted(context.rglob("*.patch"))}
        draft_file = context / "benchmark-draft.json"
        write_json(draft_file, scenario["draft"])
        before_publish = Counter(totals)
        output, publish_time = cli("review_report.py", "--context-dir", context, "--result", draft_file)
        validated_result = json.loads(metrics_path.with_suffix(".validated.json").read_text(encoding="utf-8"))
        counts["publish"] = dict(Counter(totals) - before_publish)
        report = Path(output.strip())
        try:
            markdown = report.read_text(encoding="utf-8")
        finally:
            # Remove only this invocation's concrete report, never a directory tree.
            report.unlink()
            if not any(report.parent.iterdir()):
                report.parent.rmdir()
        markdown = re.sub(r"(?m)^- Context 摘要：.*$", "- Context 摘要：<context-hash>", markdown)
        artifacts = normalize({"manifest": manifest, "reads": reads, "searches": searches,
                               "draft": scenario["draft"], "result": validated_result,
                               "patches": patches, "markdown": markdown}, context)
        write_json(destination / "artifacts.json", artifacts)
        row = {"capture_seconds": capture_time, "read_search_seconds": read_search_time,
               "publish_seconds": publish_time, "helper_total_seconds": capture_time + read_search_time + publish_time,
               "capture_stdout_bytes": captured_bytes, "counts": counts, "total_counts": dict(totals)}
        row["oracle_seconds"] = oracle_time
        write_json(destination / "metrics.json", row)
        return row, artifacts
    finally:
        if context.exists():
            cli("review_session.py", "cleanup", "--context-dir", context)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--scenario", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    scenario = json.loads(args.scenario.read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    runs = {"baseline": [], "optimized": []}
    expected = None
    for repetition in range(args.repeats):
        # Alternate execution order to reduce systematic warm-cache bias.
        order = (False, True) if repetition % 2 == 0 else (True, False)
        for optimized in order:
            label = "optimized" if optimized else "baseline"
            destination = args.output_dir / f"{label}-{repetition + 1}"
            destination.mkdir()
            row, artifacts = measure(ROOT if optimized else args.baseline.resolve(),
                                     args.fixture_root.resolve(), scenario, destination, optimized)
            if expected is None:
                expected = artifacts
            if artifacts != expected:
                raise AssertionError(f"normalized evidence/report differs: {destination}")
            runs[label].append(row)
            print(json.dumps({"run": str(destination), **row}, ensure_ascii=False), flush=True)
    medians = {label: {key: statistics.median(row[key] for row in rows)
                       for key in rows[0] if key not in ("counts", "total_counts")}
               for label, rows in runs.items()}
    result = {"repeats": args.repeats, "normalized_artifacts_equal": True,
              "model_review_seconds": None,
              "note": "Replays an authored draft; measures helpers, not repeated model reasoning.",
              "medians": medians, "runs": runs}
    write_json(args.output_dir / "summary.json", result)
    print(json.dumps({"summary": str(args.output_dir / "summary.json"), "medians": medians},
                     ensure_ascii=False), flush=True)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "_invoke":
        invoke()
    else:
        main()

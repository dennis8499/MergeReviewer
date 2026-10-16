"""Compare helper cost using an authored fixed-source review, never model decisions.

Run with --baseline pointing to an unchanged source checkout, --fixture-root to
the isolated Git workspace, and --scenario to a JSON object containing ordered
contracts, reads, searches and the actual review draft. All output goes to
--output-dir. The same fixed commits/draft are used for both implementations.
"""
from __future__ import annotations

import argparse
from collections import Counter
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
    try:
        runpy.run_path(str(script), run_name="__main__")
    finally:
        write_json(metrics_path, dict(commands))


def normalize(value, context):
    volatile = {"generated_at", "run_id", "context_sha256"}
    if isinstance(value, dict):
        return {k: normalize(v, context) for k, v in value.items() if k not in volatile}
    if isinstance(value, list):
        return [normalize(v, context) for v in value]
    if isinstance(value, str):
        return value.replace(str(context), "<context>").replace(context.as_posix(), "<context>")
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
        if result.returncode:
            raise RuntimeError(f"{script} failed: {result.stderr}")
        return result.stdout, elapsed

    output, capture_time = cli("git_review_context.py", "--workspace", fixture, "--project", "orders-api",
                               "--base", "main", "--head", "feature/amount", "--no-fetch",
                               *(["--summary"] if optimized else []))
    context = Path(json.loads(output)["context_dir"])
    counts = {"capture": dict(totals)}
    captured_bytes = len(output.encode("utf-8"))
    reads, searches = [], []
    read_search_time = 0.0
    try:
        for index, contract in enumerate(scenario["contracts"]):
            request_file = context / f"benchmark-contract-{index}.json"
            write_json(request_file, contract)
            _, elapsed = cli("review_workspace.py", "register-contract", "--context-dir", context,
                             "--contract-file", request_file)
            read_search_time += elapsed
        if optimized:
            for name in ("read", "search"):
                plural = "reads" if name == "read" else "searches"
                request_file = context / f"benchmark-{plural}.json"
                write_json(request_file, scenario[plural])
                output, elapsed = cli("review_workspace.py", "batch-" + name, "--context-dir", context,
                                       "--requests-file", request_file)
                read_search_time += elapsed
                (reads if name == "read" else searches).extend(json.loads(output)[plural])
        else:
            for request in scenario["reads"]:
                args = ["--repo", request["repo"], "--path", request["path"]]
                if request.get("source"):
                    args += ["--source", request["source"]]
                output, elapsed = cli("review_workspace.py", "read", "--context-dir", context, *args)
                reads.append(json.loads(output))
                read_search_time += elapsed
            for request in scenario["searches"]:
                args = ["--query", request["query"]]
                if request.get("contract"):
                    args += ["--contract", request["contract"]]
                if request.get("source"):
                    args += ["--source", request["source"]]
                for repo in request.get("repo") or []:
                    args += ["--repo", repo]
                output, elapsed = cli("review_workspace.py", "search", "--context-dir", context, *args)
                searches.extend(json.loads(output)["searches"])
                read_search_time += elapsed
        counts["read_search"] = dict(Counter(totals) - Counter(counts["capture"]))
        manifest = json.loads((context / "manifest.json").read_text(encoding="utf-8"))
        draft_file = context / "benchmark-draft.json"
        write_json(draft_file, scenario["draft"])
        before_publish = Counter(totals)
        output, publish_time = cli("review_report.py", "--context-dir", context, "--result", draft_file)
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
                               "draft": scenario["draft"], "markdown": markdown}, context)
        write_json(destination / "artifacts.json", artifacts)
        row = {"capture_seconds": capture_time, "read_search_seconds": read_search_time,
               "publish_seconds": publish_time, "helper_total_seconds": capture_time + read_search_time + publish_time,
               "capture_stdout_bytes": captured_bytes, "counts": counts, "total_counts": dict(totals)}
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

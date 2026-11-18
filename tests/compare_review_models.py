"""Run real, sequential Codex reviews against frozen 0.23/current skill payloads.

This opt-in harness consumes model quota. It never supplies a verdict or draft
to the reviewer. Mechanical checks are separate from semantic adjudication.
Run outside every Git repository; all failed attempts remain on disk.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import time

from tests.replay_log_workflow import ROOT, digest, freeze, save, load
from tests.review_improvement_corpus import CASES, create, git

BASELINE = "5a255fc9f35643eb4164fc893826cec42737ccbe"


def tracked_files(repo):
    raw = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True, check=True).stdout
    return {name.decode("utf-8"): hashlib.sha256((repo / name.decode("utf-8")).read_bytes()).hexdigest()
            for name in raw.split(b"\0") if name}


def prepare(output, candidate, model, reasoning, cases):
    output = Path(output).resolve()
    if not model.strip() or reasoning not in ("low", "medium", "high", "xhigh", "max"):
        raise ValueError("Specify a model and an explicit supported reasoning effort")
    if not cases or len(cases) != len(set(cases)) or any(case not in CASES for case in cases):
        raise ValueError("Choose distinct supported cases")
    ancestor = output.parent
    while not ancestor.exists():
        ancestor = ancestor.parent
    if subprocess.run(["git", "-C", str(ancestor), "rev-parse", "--show-toplevel"], capture_output=True).returncode == 0:
        raise ValueError("Model comparison root must be outside every Git repository")
    output.mkdir(parents=True, exist_ok=False)
    baseline, current, identity = freeze(output, BASELINE, Path(candidate).resolve())
    # The public task contains neutral paths, never an oracle or a variant label.
    baseline.rename(output / "skills/s0")
    current.rename(output / "skills/s1")
    tools = output / "tools"
    tools.mkdir()
    shutil.copy2(Path(__file__).with_name("agent_trace_cli.py"), tools / "trace.py")
    jobs = []
    for index, case in enumerate(cases):
        source = create(output / "seeds" / f"seed-{index + 1:02}", case)
        variants = ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
        for variant in variants:
            jobs.append({"id": f"run-{len(jobs) + 1:02}", "case": case, "variant": variant,
                         "seed": str(Path(source["workspace"]).parent), "state": "pending"})
    schedule = {"schema": "IncrementalModelComparison/v1", **identity, "model_requested": model,
                "reasoning_requested": reasoning, "served_model": None,
                "method": "Fresh sequential CLI turns, paired fixed commits, static review, parent-only oracle.",
                "jobs": jobs, "semantic_adjudication": "required separately; mechanical checks do not prove correctness"}
    save(output / "schedule.json", schedule)
    return schedule


def task(skill, workspace, project, source, runtime, audit, wrapper):
    trace = audit / "helper-trace.jsonl"
    return (
        f"Perform one complete static merge review using the frozen skill at {skill / 'SKILL.md'}. "
        f"Read that skill and necessary references. Workspace: {workspace}; project: {project}; "
        f"base: {source['base']}; head: {source['head']}. Use compact drafting. Runtime: {runtime}, outside Git. "
        "Publish the native Traditional Chinese report and verify its receipt. Preserve all tracked source files. "
        "Use one reviewer, with no child agents or external communication. Do not run application tests or scripts. "
        "Use fixed source evidence to make your own judgments; tests not run must be reported accurately. "
        "Every review_session helper command must use this instrumentation-only wrapper, followed by native arguments:\n"
        f'python -X utf8 -B "{wrapper}" --skill-dir "{skill}" --trace "{trace}" '
        f'--audit-dir "{audit / "helper-audit"}" review_session <command> <native arguments>\n'
        "Follow the frozen skill for reading, paging, citations, author/check/finish and cleanup. "
        "Do not read sibling review runs, fixtures, expected answers or previous reports. "
        "Stop after receipt-verified publication, and give its path, completeness, findings and checks actually run.\n"
    )


def mechanical_check(draft, manifest, outcome, oracle):
    """Check count, source anchors and completeness; do not invent semantic grades."""
    findings = draft.get("findings", [])
    errors = []
    if len(findings) != oracle["findings"]:
        errors.append("finding count differs from independent fixture oracle")
    if outcome.get("review_complete") is not oracle["complete"]:
        errors.append("review completeness differs from necessary-source oracle")
    if not oracle["complete"] and not outcome.get("gaps") and not outcome.get("pending_confirmation_count"):
        errors.append("required missing-source gap was not preserved")
    operations = manifest.get("operations", {})
    for finding in findings:
        if finding.get("priority") not in oracle.get("priorities", []):
            errors.append("finding severity outside predeclared band")
        locations = []
        for citation in finding.get("evidence", []):
            location = citation
            if "read_id" in citation:
                operation = operations.get(citation["read_id"], {})
                location = {**operation.get("request", {}), **citation}
                # Some historical manifests retain operation arguments at top level.
                location.setdefault("path", operation.get("path"))
            locations.append(location)
        if not any(item.get("path") == oracle.get("path") and
                   item.get("line_start", 0) <= oracle.get("line", 0) <= item.get("line_end", 0) for item in locations):
            errors.append("finding does not cite the independently seeded changed line")
    return {"passed": not errors, "errors": errors, "semantic_correctness": "not automatically adjudicated"}


def audit_run(directory, original, frozen_skill, expected_digest, oracle):
    audit = directory / "audit"
    trace_path = audit / "helper-trace.jsonl"
    rows = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()] if trace_path.exists() else []
    successful = [row for row in rows if row["operation"] == "finish" and row["exit_code"] == 0]
    result = {"helper_calls": len(rows), "helper_failures": sum(row["exit_code"] not in (0, 3) for row in rows),
              "nonzero_exit_calls": sum(row["exit_code"] != 0 for row in rows),
              "recoverable_exit3_calls": sum(row["exit_code"] == 3 for row in rows),
              "finish_attempts": sum(row["operation"] == "finish" for row in rows),
              "helper_seconds": sum(row["elapsed_seconds"] for row in rows),
              "git_calls": sum(row["git_calls"] for row in rows),
              "helper_stdout_bytes": sum(row["output_bytes"] for row in rows),
              "artifact_view_git_calls": sum(row["artifact_view_git_calls"] for row in rows),
              "published": bool(successful), "skill_unchanged": digest(frozen_skill) == expected_digest,
              "tracked_sources_unchanged": tracked_files(Path(original["project"])) == original["tracked"]}
    returned = []
    for row in rows:
        try:
            returned.append(load(Path(row["stdout_file"])))
        except (ValueError, OSError):
            continue  # Help and diagnostics may contain non-JSON output.
    result["paging_seeds_returned"] = sum(isinstance(value, dict) and "paging_requests_file" in value for value in returned)
    result["paging_batches_returned"] = sum(isinstance(value, dict) and value.get("schema") == "MergeReviewPagingBatch/v1" for value in returned)
    if not successful:
        result["mechanical_check"] = {"passed": False, "errors": ["no successful native finish observed"]}
        return result
    event = successful[-1]
    finished = load(Path(event["stdout_file"]))
    published = load(audit / "helper-audit/published.json")
    receipt = finished.get("report_receipt")
    result["receipt_present"] = isinstance(receipt, dict) and bool(receipt)
    result["report_paths"] = [item["archived_report"] for item in published]
    result["report_hashes_match"] = all(hashlib.sha256(Path(item["archived_report"]).read_bytes()).hexdigest() == item["sha256"]
                                        for item in published)
    snapshot = audit / "helper-audit/attempts" / f"publish-{event['sequence']:03}"
    draft, manifest = load(snapshot / "draft.json"), load(snapshot / "manifest.json")
    selected = next(repo for repo in manifest["repositories"] if repo["id"] == manifest["selected_repo"])
    result["fixed_refs_match"] = {key: selected["sources"][key]["ref"] for key in ("base", "head")} == original["fixed_refs"]
    # Resolve compact citation paths from the actual fixed read artifacts.
    for operation in manifest.get("operations", {}).values():
        artifact = operation.get("artifact")
        if artifact and (snapshot / "views" / (artifact + ".json")).is_file():
            value = load(snapshot / "views" / (artifact + ".json"))
            if isinstance(value, dict) and "path" in value:
                operation["path"] = value["path"]
    result["outcome"] = finished["review_outcome"]
    result["findings"] = draft.get("findings", [])
    result["tests_executed_as_authored"] = draft.get("tests_executed", [])
    result["tests_not_executed_as_authored"] = draft.get("tests_not_executed", [])
    result["mechanical_check"] = mechanical_check(draft, manifest, finished["review_outcome"], oracle)
    return result


def run_one(output, job, schedule, executable, timeout):
    directory = output / "runs" / job["id"]
    directory.mkdir(parents=True, exist_ok=False)
    seed = Path(job["seed"])
    source = load(seed / "source-fixture.json")
    workspace = directory / "workspace"
    shutil.copytree(source["workspace"], workspace)
    project = workspace / Path(source["project"]).name
    audit = directory / "audit"
    audit.mkdir()
    skill = output / ("skills/s0" if job["variant"] == "baseline" else "skills/s1")
    original = {"project": str(project), "tracked": tracked_files(project),
                "fixed_refs": {"base": source["base_sha"], "head": source["head_sha"]}}
    save(directory / "source-before.json", original)
    text = task(skill, workspace, project, source, directory / "runtime", audit, output / "tools/trace.py")
    (directory / "task.txt").write_text(text, encoding="utf-8")
    command = [executable, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "--approve-for-me",
               "-m", schedule["model_requested"], "-c", 'model_reasoning_effort="' + schedule["reasoning_requested"] + '"',
               "--json", "-C", str(workspace), "--output-last-message", str(audit / "answer.txt"), "-"]
    save(audit / "invocation.json", {"command": command, "started_unix": time.time(), "timeout_seconds": timeout})
    started = time.perf_counter()
    with (audit / "events.jsonl").open("w", encoding="utf-8") as stdout, (audit / "stderr.log").open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, encoding="utf-8", stdout=stdout, stderr=stderr,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            process.communicate(text, timeout=timeout)
            code, timed_out = process.returncode, False
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=30)
            else:
                process.kill()
            process.wait(timeout=30)
            code, timed_out = None, True
    elapsed = time.perf_counter() - started
    events, invalid = [], 0
    for line in (audit / "events.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            invalid += 1
    kinds = Counter(row.get("item", {}).get("type") for row in events if row.get("type") == "item.completed")
    usage = [row["usage"] for row in events if row.get("type") == "turn.completed" and "usage" in row]
    result = {"returncode": code, "timed_out": timed_out, "wall_seconds": elapsed,
              "completed_items": dict(kinds), "invalid_event_lines": invalid,
              "usage_as_reported": usage, "served_model": None}
    try:
        result.update(audit_run(directory, original, skill, schedule[job["variant"] + "_skill_sha256"], load(seed / "oracle.json")))
    except (ValueError, KeyError, OSError, TypeError) as error:
        result["audit_error"] = str(error)
    save(audit / "result.json", result)
    return result


def summarize(output, schedule):
    pairs = []
    for case in dict.fromkeys(job["case"] for job in schedule["jobs"]):
        pair = {"case": case}
        for job in [row for row in schedule["jobs"] if row["case"] == case]:
            path = output / "runs" / job["id"] / "audit/result.json"
            pair[job["variant"]] = load(path) if path.exists() else {"missing": True}
        pairs.append(pair)
    completed = [pair for pair in pairs if all("wall_seconds" in pair[name] for name in ("baseline", "candidate"))]
    def eligible(result):
        return (result.get("returncode") == 0 and result.get("timed_out") is False
                and result.get("published") is True and result.get("mechanical_check", {}).get("passed") is True
                and all(result.get(key) is True for key in
                        ("skill_unchanged", "tracked_sources_unchanged", "fixed_refs_match", "receipt_present", "report_hashes_match")))
    speed_pairs = [pair for pair in completed if all(eligible(pair[name]) for name in ("baseline", "candidate"))]
    summary = {"schema": "IncrementalModelComparisonResults/v1", "pairs": pairs,
               "model_requested": schedule["model_requested"], "reasoning_requested": schedule["reasoning_requested"],
               "served_model": None, "completed_pairs": len(completed), "valid_speed_pairs": len(speed_pairs),
               "excluded_speed_cases": [pair["case"] for pair in completed if pair not in speed_pairs],
               "semantic_adjudication": "required separately",
               "scope": "Small synthetic corpus; not a production accuracy estimate or human readability acceptance."}
    if speed_pairs:
        summary["paired_wall_ratio_median"] = statistics.median(row["candidate"]["wall_seconds"] / row["baseline"]["wall_seconds"] for row in speed_pairs)
    save(output / "summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--candidate-skill", type=Path, default=ROOT / "skills/merge-reviewer")
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning", required=True, choices=("low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--codex", default=shutil.which("codex.cmd") or shutil.which("codex"))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args(argv)
    if not args.codex or args.timeout < 1:
        parser.error("Codex CLI and a positive timeout are required")
    output = args.output_root.resolve()
    schedule = load(output / "schedule.json") if args.resume else prepare(output, args.candidate_skill, args.model, args.reasoning, args.cases)
    if (args.model, args.reasoning) != (schedule["model_requested"], schedule["reasoning_requested"]):
        parser.error("Resume must retain the requested model and reasoning")
    for variant, name in (("baseline", "s0"), ("candidate", "s1")):
        if digest(output / "skills" / name) != schedule[variant + "_skill_sha256"]:
            raise ValueError("Frozen skill changed; do not mix run identities")
    if args.prepare_only:
        return 0
    cli_version = subprocess.run([args.codex, "--version"], capture_output=True, encoding="utf-8", check=True).stdout.strip()
    if "codex_cli_version" in schedule and schedule["codex_cli_version"] != cli_version:
        raise ValueError("CLI version changed; retain original runs and start a separate comparison")
    schedule["codex_cli_version"] = cli_version
    save(output / "schedule.json", schedule)
    for job in schedule["jobs"]:
        if job["state"] != "pending":
            continue  # Preserve failures and interrupted attempts; never silently retry.
        job["state"] = "running"
        save(output / "schedule.json", schedule)
        print(json.dumps({"started": job["id"], "case": job["case"], "variant": job["variant"]}), flush=True)
        result = run_one(output, job, schedule, args.codex, args.timeout)
        job["state"] = "completed" if result.get("returncode") == 0 else "failed"
        save(output / "schedule.json", schedule)
        summarize(output, schedule)
        print(json.dumps({"finished": job["id"], "state": job["state"], "seconds": result["wall_seconds"],
                          "mechanical_check": result.get("mechanical_check"), "audit_error": result.get("audit_error")}), flush=True)
    summary = summarize(output, schedule)
    all_completed = summary["completed_pairs"] == len({job["case"] for job in schedule["jobs"]})
    mechanical_passed = all(pair[name].get("mechanical_check", {}).get("passed") is True
                            and pair[name].get("skill_unchanged") is True
                            and pair[name].get("tracked_sources_unchanged") is True
                            and pair[name].get("fixed_refs_match") is True
                            and pair[name].get("report_hashes_match") is True
                            and pair[name].get("receipt_present") is True
                            for pair in summary["pairs"] for name in ("baseline", "candidate"))
    return 0 if all_completed and mechanical_passed and all(job["state"] == "completed" for job in schedule["jobs"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Prepare and audit the MergeReviewer three-model acceptance matrix.

This parent-owned harness does not call a model or invent review decisions.
An external runner must use each blind request, retain the actual provider/model
metadata, and write completion.json and a separately authored audit.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import statistics
import subprocess
import time

if __package__:
    from . import agent_acceptance as legacy
    from . import create_merge_quality_fixture as added
else:
    import agent_acceptance as legacy
    import create_merge_quality_fixture as added

MODELS = ("gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-astra")
REASONING = "high"
SPEED_MODEL = "gpt-6.1-sol"
BASELINE_SKILL_SHA256 = "54e67c0945d3da2827479476f5619de49fd97b76e8cfabfc0f2b70aefe6d8ccf"
VARIANTS = ("no-skill", "baseline", "candidate")
QUALITY_CASES = (*legacy.QUALITY, *added.CASES, "local", "mixed")
DIMENSIONS = {
    "p0": ("required_findings", "actionability"),
    "p1": ("required_findings", "actionability"),
    "safe": ("safety_false_positives",),
    "net-zero": ("safety_false_positives",),
    "gap": ("required_findings", "actionability"),
    "secrets": ("safety_false_positives",),
    "p2": ("general_defects", "actionability"),
    "p3": ("general_defects", "actionability"),
    "outflow": ("required_findings", "general_defects", "actionability"),
    "cross-branch": ("required_findings", "general_defects", "actionability"),
    "security-safe": ("safety_false_positives",),
    "local": ("safety_false_positives",),
    "mixed": ("required_findings", "general_defects", "actionability"),
}
CHECKS = ("fixed_sources", "no_secret_sentinel", "no_post_publication_edits",
          "expected_review_outcome", "model_reasoning_verified", "correct_skill_condition",
          "no_gitlab_api", "no_unexpected_remote")


def jobs():
    """Return every required run in a fixed order, without judging outcomes."""
    result = []
    def add(**values):
        result.append({"id": f"run-{len(result) + 1:03}", **values})
    for model_index, model in enumerate(MODELS):
        for case_index, case in enumerate(QUALITY_CASES):
            shift = (model_index + case_index) % len(VARIANTS)
            for variant in VARIANTS[shift:] + VARIANTS[:shift]:
                add(phase="quality", model=model, reasoning=REASONING, variant=variant,
                    scenario=case, case=case, repeat=1)
    for scenario in ("local", "mixed", "single"):
        case = "p2" if scenario == "single" else scenario
        for repeat in range(1, 4):
            order = ("baseline", "candidate") if repeat % 2 else ("candidate", "baseline")
            for variant in order:
                add(phase="speed", model=SPEED_MODEL, reasoning=REASONING, variant=variant,
                    scenario=scenario, case=case, repeat=repeat)
    return result


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def save_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def skill_digest(root):
    root = Path(root)
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Frozen skills cannot contain symbolic links")
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def prepare(root, baseline_skill, candidate_skill, *, files_per_repo=1000):
    root, baseline, candidate = Path(root).resolve(), Path(baseline_skill).resolve(), Path(candidate_skill).resolve()
    if files_per_repo < 1000:
        raise ValueError("Acceptance workloads require at least 1,000 files per Repo")
    if ((baseline / "VERSION").read_text(encoding="utf-8").strip() != "0.19.0" or
            skill_digest(baseline) != BASELINE_SKILL_SHA256):
        raise ValueError("Baseline skill must match the frozen v0.19.0 release bytes")
    for skill in (baseline, candidate):
        if not (skill / "SKILL.md").is_file():
            raise ValueError("Both frozen skills must contain SKILL.md")
        skill_digest(skill)
    ancestor = root.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    git_root = subprocess.run(["git", "-C", str(ancestor), "rev-parse", "--show-toplevel"],
                              capture_output=True, timeout=10)
    if git_root.returncode == 0:
        raise ValueError("Acceptance root must be outside every Git Repo so isolated runtimes are valid")
    root.mkdir(parents=True, exist_ok=False)
    ignored = shutil.ignore_patterns("__pycache__", "*.pyc")
    for label, skill in (("baseline", baseline), ("candidate", candidate)):
        shutil.copytree(skill, root / "skills" / label, ignore=ignored)
    trace_wrapper = root / "tools" / "agent_trace_cli.py"
    trace_wrapper.parent.mkdir(parents=True)
    shutil.copy2(Path(__file__).with_name("agent_trace_cli.py"), trace_wrapper)
    quality = legacy.load_quality_builder()
    for case in legacy.QUALITY:
        quality.create(root / "seeds" / case, case)
    for case in added.CASES:
        added.create(root / "seeds" / case, case)
    for case in ("local", "mixed"):
        legacy.create_large(root / "seeds" / case, case, files_per_repo)
    data = {"schema": "MergeReviewerAcceptanceMatrix/v1", "jobs": jobs(),
            "method": {"models": list(MODELS), "reasoning": REASONING, "speed_model": SPEED_MODEL,
                       "fresh_agents": True, "sequential": True, "oracle_visible_to_reviewers": False,
                       "files_per_repo": files_per_repo, "large_ratio_limit": .85, "scenario_ratio_limit": 1.05,
                       "skill_hashes": {label: skill_digest(root / "skills" / label)
                                        for label in ("baseline", "candidate")},
                       "trace_wrapper_sha256": hashlib.sha256(trace_wrapper.read_bytes()).hexdigest(),
                       "model_analysis_seconds": None}}
    save(root / "schedule.json", data)
    return {"schedule": str(root / "schedule.json"), "runs": len(data["jobs"])}


def validated_schedule(root):
    schedule = read(root / "schedule.json")
    method = schedule.get("method", {})
    if not isinstance(method, dict):
        raise ValueError("Acceptance schedule has invalid method metadata")
    if (schedule.get("schema") != "MergeReviewerAcceptanceMatrix/v1" or schedule.get("jobs") != jobs() or
            method.get("models") != list(MODELS) or method.get("reasoning") != REASONING or
            method.get("speed_model") != SPEED_MODEL or method.get("large_ratio_limit") != .85 or
            method.get("scenario_ratio_limit") != 1.05 or method.get("fresh_agents") is not True or
            method.get("sequential") is not True or method.get("oracle_visible_to_reviewers") is not False or
            type(method.get("files_per_repo")) is not int or method["files_per_repo"] < 1000):
        raise ValueError("Acceptance schedule differs from the required matrix")
    hashes = method.get("skill_hashes", {})
    if not isinstance(hashes, dict):
        raise ValueError("Acceptance schedule has invalid frozen skill hashes")
    if (hashes.get("baseline") != BASELINE_SKILL_SHA256 or
            any(not (root / "skills" / variant / "SKILL.md").is_file() or
                skill_digest(root / "skills" / variant) != hashes.get(variant)
                for variant in ("baseline", "candidate"))):
        raise ValueError("Frozen acceptance skills differ from the schedule")
    trace_wrapper = root / "tools" / "agent_trace_cli.py"
    if (not trace_wrapper.is_file() or trace_wrapper.is_symlink() or
            hashlib.sha256(trace_wrapper.read_bytes()).hexdigest() != method.get("trace_wrapper_sha256")):
        raise ValueError("Frozen helper trace wrapper differs from the schedule")
    return schedule


def prepare_run(root, identifier):
    root = Path(root).resolve()
    schedule = validated_schedule(root)
    job = next((item for item in schedule["jobs"] if item["id"] == identifier), None)
    if job is None:
        raise ValueError("Unknown run")
    seed = root / "seeds" / job["case"]
    source = read(seed / "source-fixture.json")
    directory = root / "runs" / identifier
    directory.mkdir(parents=True, exist_ok=False)
    workspace = directory / "workspace"
    shutil.copytree(source["workspace"], workspace)
    skill = None if job["variant"] == "no-skill" else root / "skills" / job["variant"]
    if skill and skill_digest(skill) != schedule["method"]["skill_hashes"][job["variant"]]:
        raise ValueError("Frozen skill changed after scheduling")
    request = {"schema": "MergeReviewerBlindRequest/v1", "run_id": identifier,
               "model": job["model"], "reasoning": job["reasoning"],
               "workspace": str(workspace), "project": str(workspace / Path(source["project"]).name),
               "base": source["base"], "head": source["head"], "skill_dir": str(skill) if skill else None,
               "runtime_root": str(directory / "runtime"), "report_dir": str(directory / "reports"),
               "trace_wrapper": str(root / "tools" / "agent_trace_cli.py") if skill else None,
               "trace_file": str(directory / "helper-trace.jsonl") if skill else None,
               "audit_dir": str(directory / "audit") if skill else None,
               "instruction": ("Review the requested branch change without loading the MergeReviewer skill."
                               if skill is None else "Use the supplied frozen MergeReviewer skill; invoke each reviewer helper through the supplied trace wrapper.")}
    save(directory / "public-request.json", request)
    return request


def scheduled_job(root, identifier):
    schedule = validated_schedule(root)
    result = next((item for item in schedule["jobs"] if item["id"] == identifier), None)
    if result is None:
        raise ValueError("Unknown run")
    return schedule, result


def start_run(root, identifier):
    root = Path(root).resolve()
    _, job = scheduled_job(root, identifier)
    directory = root / "runs" / identifier
    request = directory / "public-request.json"
    if read(request).get("run_id") != job["id"]:
        raise ValueError("Prepared request does not identify this run")
    value = {"schema": "MergeReviewerAcceptanceStart/v1", "run_id": identifier,
             "request_sha256": hashlib.sha256(request.read_bytes()).hexdigest(),
             "started_at_unix": time.time(), "started_monotonic": time.monotonic()}
    save_new(directory / "started.json", value)
    return {"run_id": identifier, "started_at_unix": value["started_at_unix"]}


def trace_metrics(path, audit_dir):
    """Summarize actual wrapper receipts; unknown phase timings remain unknown."""
    path, audit_dir = Path(path), Path(audit_dir).resolve()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows or any(not isinstance(row, dict) or row.get("schema") != "ReviewerHelperTrace/v5" for row in rows):
        raise ValueError("Helper trace is empty or has an unsupported schema")
    sequences = [row.get("sequence") for row in rows]
    if any(type(value) is not int or value < 1 for value in sequences) or len(set(sequences)) != len(sequences):
        raise ValueError("Helper trace has duplicate or invalid sequence numbers")
    for row in rows:
        for key in ("elapsed_seconds", "git_calls", "output_bytes", "full_result_validations"):
            value = row.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"Helper trace has invalid {key}")
        for key in ("stderr_bytes", "observer_seconds"):
            value = row.get(key, 0)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"Helper trace has invalid {key}")
    starts = [row for row in rows if row.get("module") == "review_session" and row.get("operation") == "start"]
    waits = []
    for row in starts:
        filename = row.get("stdout_file")
        if not isinstance(filename, str):
            waits.append(None)
            continue
        output = Path(filename).resolve()
        if not output.is_relative_to(audit_dir) or not output.is_file():
            waits.append(None)
            continue
        try:
            response = read(output)
            network = response.get("network_activity", {})
            phase = response.get("metrics", {}).get("phase_seconds", {})
            wait = 0 if network.get("fetch_attempt_count") == 0 else phase.get("waiting-network")
            waits.append(wait if type(wait) in (int, float) and math.isfinite(wait) and wait >= 0 else None)
        except (OSError, ValueError, TypeError, AttributeError):
            waits.append(None)
    def phase(name):
        return sum(row["elapsed_seconds"] for row in rows
                   if row.get("module") == "review_session" and row.get("operation") == name)
    def count(name):
        return sum(row.get("module") == "review_session" and row.get("operation") == name for row in rows)
    return {"helper_calls": len(rows), "helper_seconds": sum(row["elapsed_seconds"] for row in rows),
            "observer_seconds": sum(row.get("observer_seconds", 0) for row in rows),
            "git_calls": sum(row["git_calls"] for row in rows),
            "helper_output_bytes": sum(row["output_bytes"] + row.get("stderr_bytes", 0) for row in rows),
            "start_seconds": phase("start"), "step_seconds": phase("step"), "finish_seconds": phase("finish"),
            "start_calls": count("start"), "step_calls": count("step"), "finish_calls": count("finish"),
            "step_scope": "Includes reads, searches and any other operations in the same steps.",
            "network_wait_seconds": sum(waits) if starts and all(value is not None for value in waits) else None,
            "full_result_validations": sum(row["full_result_validations"] for row in rows),
            "model_analysis_seconds": None}


def complete_run(root, identifier, report, *, actual_model, actual_reasoning):
    root = Path(root).resolve()
    schedule, job = scheduled_job(root, identifier)
    directory = root / "runs" / identifier
    started = read(directory / "started.json")
    request = directory / "public-request.json"
    if (started.get("run_id") != identifier or
            started.get("request_sha256") != hashlib.sha256(request.read_bytes()).hexdigest()):
        raise ValueError("Prepared request changed after the timer started")
    if job["variant"] != "no-skill" and skill_digest(root / "skills" / job["variant"]) != (
            schedule["method"]["skill_hashes"][job["variant"]]):
        raise ValueError("Frozen skill changed during the run")
    report = Path(report).resolve()
    if not report.is_relative_to((directory / "reports").resolve()) or not report.is_file():
        raise ValueError("The report must be a file inside this run's reports directory")
    ended_unix, ended_monotonic = time.time(), time.monotonic()
    elapsed = ended_monotonic - started["started_monotonic"]
    if elapsed <= 0 or abs((ended_unix - started["started_at_unix"]) - elapsed) > 5:
        raise ValueError("Run timer is invalid or the system clock changed")
    trace = directory / "helper-trace.jsonl"
    metrics = trace_metrics(trace, directory / "audit") if trace.is_file() else None
    value = {"schema": "MergeReviewerAcceptanceCompletion/v1", "run_id": identifier,
             "model": actual_model, "reasoning": actual_reasoning, "variant": job["variant"],
             "request_sha256": started["request_sha256"], "elapsed_seconds": elapsed,
             "ended_at_unix": ended_unix, "ended_monotonic": ended_monotonic,
             "report": report.relative_to(directory).as_posix(),
             "report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
             "trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest() if trace.is_file() else None,
             "helper_metrics": metrics,
             "model_analysis_seconds": None}
    save_new(directory / "completion.json", value)
    return value


def save_audit(root, identifier, audit_file):
    root = Path(root).resolve()
    scheduled_job(root, identifier)
    directory = root / "runs" / identifier
    if not (directory / "completion.json").is_file():
        raise ValueError("Complete the run before adding an independent audit")
    value = read(audit_file)
    if not isinstance(value, dict):
        raise ValueError("Audit must be a JSON object authored by the evaluator")
    save_new(directory / "audit.json", value)
    return {"run_id": identifier, "audit": str(directory / "audit.json")}


def record(root, job):
    """Check parent-authored receipts; semantic decisions are never inferred."""
    directory = root / "runs" / job["id"]
    issues = []
    try:
        completion, audit = read(directory / "completion.json"), read(directory / "audit.json")
        started, request = read(directory / "started.json"), directory / "public-request.json"
        oracle = read(root / "seeds" / job["case"] / "oracle.json")
        if not all(isinstance(value, dict) for value in (completion, audit, started, oracle)):
            raise ValueError("receipt is not an object")
        if (completion.get("run_id"), completion.get("model"), completion.get("reasoning"), completion.get("variant")) != (
                job["id"], job["model"], REASONING, job["variant"]):
            raise ValueError("run identity, provider model, reasoning or skill condition differs")
        if (started.get("schema") != "MergeReviewerAcceptanceStart/v1" or
                completion.get("schema") != "MergeReviewerAcceptanceCompletion/v1"):
            raise ValueError("unrecognized timing receipt")
        elapsed = completion.get("elapsed_seconds")
        if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed <= 0:
            raise ValueError("elapsed time is missing or invalid")
        times = (started.get("started_at_unix"), started.get("started_monotonic"),
                 completion.get("ended_at_unix"), completion.get("ended_monotonic"))
        if not all(type(value) in (int, float) and math.isfinite(value) for value in times):
            raise ValueError("timer timestamps are missing or invalid")
        request_value = read(request)
        if (started.get("run_id") != job["id"] or
                (request_value.get("run_id"), request_value.get("model"), request_value.get("reasoning")) !=
                (job["id"], job["model"], job["reasoning"]) or
                started.get("request_sha256") != hashlib.sha256(request.read_bytes()).hexdigest() or
                completion.get("request_sha256") != started["request_sha256"] or
                abs(completion["ended_monotonic"] - started["started_monotonic"] - elapsed) > .01 or
                abs(completion["ended_at_unix"] - started["started_at_unix"] - elapsed) > 5):
            raise ValueError("start, completion and fixed request do not form a valid timer")
        name = completion.get("report")
        if not isinstance(name, str) or not name:
            raise ValueError("report path is missing")
        report = (directory / name).resolve()
        if not report.is_relative_to((directory / "reports").resolve()) or not report.is_file():
            raise ValueError("report is outside the run's reports directory or missing")
        data = report.read_bytes()
        if hashlib.sha256(data).hexdigest() != completion.get("report_sha256"):
            raise ValueError("published report changed or its hash is missing")
        trace = directory / "helper-trace.jsonl"
        trace_hash = completion.get("trace_sha256")
        helper_metrics = completion.get("helper_metrics")
        if trace_hash is not None and (not trace.is_file() or hashlib.sha256(trace.read_bytes()).hexdigest() != trace_hash or
                                       trace_metrics(trace, directory / "audit") != helper_metrics):
            raise ValueError("helper trace changed after completion")
        if job["phase"] == "speed" and (trace_hash is None or not isinstance(helper_metrics, dict) or
                                        helper_metrics.get("full_result_validations", 0) < 1 or
                                        any(helper_metrics.get(key, 0) < 1
                                            for key in ("start_calls", "step_calls", "finish_calls"))):
            raise ValueError("full-review timing needs an unchanged complete helper trace")
        semantic, checks = audit.get("semantic"), audit.get("mechanical_checks")
        if not isinstance(semantic, dict) or not isinstance(checks, dict) or type(semantic.get("passed")) is not bool:
            raise ValueError("independent semantic audit is missing")
        observations, dimensions = semantic.get("observations"), semantic.get("dimensions")
        if (not isinstance(observations, list) or not observations or
                not all(isinstance(item, str) and item.strip() for item in observations) or
                not isinstance(dimensions, dict) or
                any(type(dimensions.get(key)) is not bool for key in DIMENSIONS[job["case"]]) or
                any(type(checks.get(key)) is not bool for key in CHECKS)):
            raise ValueError("audit must explicitly assess the required dimensions and checks")
        sentinels = oracle.get("secret_sentinels")
        if not isinstance(sentinels, list) or not all(isinstance(s, str) and s for s in sentinels):
            raise ValueError("parent-only oracle has invalid secret sentinels")
        leaked = any(s.encode("utf-8") in data for s in sentinels)
        passed = (semantic["passed"] and all(dimensions[key] for key in DIMENSIONS[job["case"]])
                  and all(checks[key] for key in CHECKS) and not leaked)
        if leaked:
            issues.append("Synthetic secret sentinel appeared in the report")
        return {"audited": True, "passed": bool(passed), "elapsed_seconds": elapsed,
                "dimensions": {key: dimensions[key] for key in DIMENSIONS[job["case"]]},
                "helper_metrics": helper_metrics, "model_analysis_seconds": None,
                "issues": issues}
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        return {"audited": False, "passed": False, "issues": [str(error)]}


def evaluate(root):
    root = Path(root).resolve()
    schedule = validated_schedule(root)
    rows = [{**job, **record(root, job)} for job in schedule["jobs"]]
    quality = {}
    for model in MODELS:
        model_rows = [row for row in rows if row["phase"] == "quality" and row["model"] == model]
        quality[model] = {variant: {"audited": sum(row["audited"] for row in model_rows if row["variant"] == variant),
                                   "passed": sum(row["passed"] for row in model_rows if row["variant"] == variant),
                                   "expected": len(QUALITY_CASES)} for variant in VARIANTS}
        quality[model]["gate"] = (all(quality[model][variant]["audited"] == len(QUALITY_CASES) for variant in VARIANTS)
                                  and quality[model]["candidate"]["passed"] == len(QUALITY_CASES))
    pairs, scenarios, large = [], {}, []
    for scenario in ("local", "mixed", "single"):
        ratios = []
        for repeat in range(1, 4):
            selected = {row["variant"]: row for row in rows if row["phase"] == "speed"
                        and row["scenario"] == scenario and row["repeat"] == repeat}
            valid = all(selected[variant]["passed"] for variant in ("baseline", "candidate"))
            ratio = (selected["candidate"]["elapsed_seconds"] / selected["baseline"]["elapsed_seconds"]
                     if valid else None)
            pairs.append({"scenario": scenario, "repeat": repeat, "valid": valid, "ratio": ratio})
            if valid:
                ratios.append(ratio)
                if scenario != "single":
                    large.append(ratio)
        median = statistics.median(ratios) if ratios else None
        scenarios[scenario] = {"valid_pairs": len(ratios), "paired_ratio_median": median,
                               "gate": len(ratios) == 3 and median <= 1.05}
    large_median = statistics.median(large) if large else None
    speed_gate = (len(large) == 6 and large_median <= .85 and all(value["gate"] for value in scenarios.values()))
    return {"schema": "MergeReviewerAcceptanceResult/v1", "rows": rows, "quality": quality,
            "pairs": pairs, "scenarios": scenarios, "large_valid_pairs": len(large),
            "large_paired_ratio_median": large_median, "speed_gate": speed_gate,
            "model_gates_passed": speed_gate and all(value["gate"] for value in quality.values()),
            "scope": "Model and full-review gates only; helper, platform and packaging gates remain separate."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("prepare")
    setup.add_argument("--root", type=Path, required=True)
    setup.add_argument("--baseline-skill", type=Path, required=True)
    setup.add_argument("--candidate-skill", type=Path, required=True)
    setup.add_argument("--files-per-repo", type=int, default=1000)
    run = commands.add_parser("prepare-run")
    run.add_argument("--root", type=Path, required=True)
    run.add_argument("--id", required=True)
    start = commands.add_parser("start")
    start.add_argument("--root", type=Path, required=True)
    start.add_argument("--id", required=True)
    complete = commands.add_parser("complete")
    complete.add_argument("--root", type=Path, required=True)
    complete.add_argument("--id", required=True)
    complete.add_argument("--report", type=Path, required=True)
    complete.add_argument("--actual-model", required=True)
    complete.add_argument("--actual-reasoning", required=True)
    audit = commands.add_parser("audit")
    audit.add_argument("--root", type=Path, required=True)
    audit.add_argument("--id", required=True)
    audit.add_argument("--audit-file", type=Path, required=True)
    summary = commands.add_parser("summarize")
    summary.add_argument("--root", type=Path, required=True)
    summary.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.root, args.baseline_skill, args.candidate_skill, files_per_repo=args.files_per_repo)
    elif args.command == "prepare-run":
        result = prepare_run(args.root, args.id)
    elif args.command == "start":
        result = start_run(args.root, args.id)
    elif args.command == "complete":
        result = complete_run(args.root, args.id, args.report, actual_model=args.actual_model,
                              actual_reasoning=args.actual_reasoning)
    elif args.command == "audit":
        result = save_audit(args.root, args.id, args.audit_file)
    else:
        result = evaluate(args.root)
        save_new(args.output, result)
        result = {"summary": str(args.output), "model_gates_passed": result["model_gates_passed"]}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

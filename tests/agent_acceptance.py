"""Prepare and audit fresh-agent exercises without manufacturing review verdicts.

This is a parent-owned harness, not a reviewer helper. Reviewer agents receive
only public-request.json, a frozen skill and their isolated source workspace.
Oracle data and the 48-run schedule remain outside every review workspace.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time


DATE = "2026-10-22T00:00:00+00:00"
QUALITY = ("p0", "p1", "safe", "net-zero", "gap", "secrets")
NAMES = ("orders-api", *(f"relay-{n}" for n in range(1, 5)),
         *(f"consumer-{n}" for n in range(1, 5)), "web", "specifications",
         *(f"unrelated-{n}" for n in range(1, 8)))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def git(repo, *arguments):
    environment = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                   "GIT_AUTHOR_DATE": DATE, "GIT_COMMITTER_DATE": DATE,
                   "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1"}
    result = subprocess.run(["git", "-C", str(repo), *map(str, arguments)], capture_output=True,
                            encoding="utf-8", env=environment, timeout=180)
    if result.returncode:
        raise ValueError(result.stderr)
    return result.stdout.strip()


def write(repo, path, content):
    target = Path(repo) / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8", newline="\n")


def initial_sources(name):
    if name == "orders-api":
        result = {f"src/chain_{n}.py":
                  f'TOPIC = "orders.chain{n}"\n\ndef publish(bus, amount):\n'
                  '    return bus.send(TOPIC, {"amount": amount})\n' for n in range(1, 5)}
        result.update({
            "src/formatters.py": 'def display_amount(amount):\n    local_total = amount\n    return f"{local_total:.2f}"\n',
            "src/counters.py": "def next_count(current):\n    local_count = current + 1\n    return local_count\n",
            "src/app.py": "from .formatters import display_amount\nfrom .counters import next_count\n\ndef local_diagnostics(amount, count):\n    return display_amount(amount), next_count(count)\n",
            "docs/operations.md": "Local diagnostics formats amounts for operator logs only.\n",
            "docs/legacy.md": "Retired operator notes; no runtime or deployment references.\n",
            "README.md": "src/app.py runs local operator diagnostics. chain_1 through chain_4 publish order events.\n"})
        return result
    if name.startswith("relay-"):
        number = name.rsplit("-", 1)[1]
        return {"src/relay.py": f'SOURCE_TOPIC = "orders.chain{number}"\nNEXT_TOPIC = "settlement.chain{number}"\n\ndef forward(bus, payload):\n    return bus.send(NEXT_TOPIC, payload)\n\ndef bootstrap(bus):\n    return bus.subscribe(SOURCE_TOPIC, lambda payload: forward(bus, payload))\n'}
    if name.startswith("consumer-"):
        number = name.rsplit("-", 1)[1]
        return {"src/consumer.py": f'TOPIC = "settlement.chain{number}"\n\ndef consume(payload):\n    return payload["amount"] * 100\n\ndef bootstrap(bus):\n    return bus.subscribe(TOPIC, consume)\n'}
    if name == "web":
        return {"src/resources.py": 'def invoice_tax(db):\n    return db.execute("SELECT rate FROM BillingDb.dbo.InvoiceTax")\n',
                "README.md": "This separately deployed web service has its own service account and database grants.\n"}
    if name == "specifications":
        return {"schema.sql": "CREATE TABLE BillingDb.dbo.InvoiceTax (rate DECIMAL(5,2));\n",
                "contracts.md": "Each settlement.chain1 through settlement.chain4 event carries numeric amount.\n"}
    return {"src/clock.py": 'def local_diagnostics(local_total, local_count):\n    return {"MarketValue": local_total, "FilePath": str(local_count)}\n',
            "README.md": "Independent clock utility. No order-event subscriptions or shared runtime dependencies.\n"}


def create_large(destination, scenario, files_per_repo=1000):
    """Build 18 committed repos; root oracle is never inside the workspace."""
    destination = Path(destination).resolve()
    if destination.exists() or scenario not in ("local", "mixed"):
        raise ValueError("Require a fresh destination and local/mixed scenario")
    workspace = destination / "workspace"
    workspace.mkdir(parents=True)
    repositories = []
    for name in NAMES:
        repo = workspace / name
        git(workspace, "init", "-b", "main", repo)
        for key, value in {"user.name": "Reviewer Acceptance", "user.email": "review@example.invalid",
                           "core.autocrlf": "false", "commit.gpgsign": "false",
                           "core.hooksPath": str(destination / "no-hooks")}.items():
            git(repo, "config", key, value)
        sources = initial_sources(name)
        if files_per_repo < len(sources):
            raise ValueError("files_per_repo is smaller than source inventory")
        for path, content in sources.items():
            write(repo, path, content)
        for number in range(files_per_repo - len(sources)):
            write(repo, f"notes/reference-{number:04}.txt", f"{name} reference {number:04}; stable inventory.\n")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "baseline sources")
        baseline = git(repo, "rev-parse", "HEAD")
        if name == "orders-api":
            git(repo, "switch", "-c", "feature/review")
            write(repo, "src/formatters.py", sources["src/formatters.py"].replace("local_total", "display_total"))
            if scenario == "local":
                write(repo, "src/counters.py", sources["src/counters.py"].replace("local_count", "next_value"))
                write(repo, "docs/operations.md", "Local diagnostics formats amounts for operator logs; fixed precision is unchanged.\n")
            else:
                for number in range(1, 5):
                    path = f"src/chain_{number}.py"
                    write(repo, path, sources[path].replace('{"amount": amount}', '{"total": amount}'))
                write(repo, "src/deployment.py", 'import os\n\ndef configure(db, client):\n    taxes = db.execute("SELECT rate FROM BillingDb.dbo.InvoiceTax")\n    response = client.get(os.getenv("INVOICE_API_URL", "https://invoice.example.invalid"))\n    return taxes, response\n')
                write(repo, "src/app.py", sources["src/app.py"] + '\nfrom .deployment import configure\n\ndef startup(db, client):\n    return configure(db, client)\n')
                (repo / "docs/legacy.md").unlink()
            git(repo, "add", ".")
            git(repo, "commit", "-m", "comparison sources")
        repositories.append({"name": name, "base_sha": baseline, "head_sha": git(repo, "rev-parse", "HEAD"),
                             "files": len(git(repo, "ls-files").splitlines())})
    project = workspace / "orders-api"
    changes = git(project, "diff", "--name-only", "main", "feature/review").splitlines()
    expected = 3 if scenario == "local" else 8
    if len(changes) != expected or len(repositories) != 18:
        raise AssertionError("Source workload differs from its declared shape")
    fixture = {"schema": "ReviewerAcceptanceSource/v1", "source_only": True,
               "workspace": str(workspace), "project": str(project), "base": "main", "head": "feature/review",
               "repositories": repositories, "changed_files": changes, "review_draft": None}
    oracle = {"schema": "ReviewerAcceptanceOracle/v1", "visibility": "parent-only", "scenario": scenario,
              "expected": {"introduced_defects": [] if scenario == "local" else [
                  {"priority": "P1", "topic": f"orders.chain{number}", "consumer_topic": f"settlement.chain{number}",
                   "cause": "producer removes amount; unchanged relay forwards total to amount-reading consumer"}
                  for number in range(1, 5)],
                  "release_dependencies": [] if scenario == "local" else ["INVOICE_API_URL", "BillingDb.dbo.InvoiceTax"],
                  "service_identity": "web's prior database use cannot establish orders-api's own access",
                  "review_complete": True}, "secret_sentinels": []}
    save(destination / "source-fixture.json", fixture)
    save(destination / "oracle.json", oracle)
    return fixture


def load_quality_builder():
    location = Path(__file__).with_name("create_quality_fixture.py")
    spec = importlib.util.spec_from_file_location("acceptance_quality_sources", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare(root, files_per_repo=1000):
    root = Path(root).resolve()
    if (root / "schedule.json").exists():
        raise ValueError("Acceptance schedule already exists; preserve its history")
    root.mkdir(parents=True, exist_ok=True)
    sources = {}
    quality = load_quality_builder()
    for index, case in enumerate(QUALITY):
        seed = root / "seeds" / f"canary-{index + 1:02}"
        sources[case] = quality.create(seed, case)
    for scenario in ("local", "mixed"):
        sources[scenario] = create_large(root / "seeds" / f"workload-{scenario}", scenario, files_per_repo)
    jobs = []
    for reviewer in ("merge", "release"):
        groups = [(case, 1) for case in QUALITY] + [(scenario, repeat)
                  for scenario in ("local", "mixed") for repeat in range(1, 4)]
        for index, (scenario, repeat) in enumerate(groups):
            variants = ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
            for variant in variants:
                number = len(jobs) + 1
                jobs.append({"id": f"run-{number:03}", "reviewer": reviewer, "variant": variant,
                             "scenario": scenario, "repeat": repeat, "seed": str(Path(sources[scenario]["workspace"]).parent),
                             "state": "pending"})
    data = {"schema": "ReviewerAcceptanceSchedule/v1", "jobs": jobs, "expected_runs": 48,
            "method": {"fresh_agents": True, "sequential": True, "same_parent_model": True,
                       "provider_model_id": None, "reasoning_setting": "inherited unchanged",
                       "oracle_visible_to_reviewers": False, "files_per_repo": files_per_repo,
                       "model_analysis_seconds": None, "tokens": None, "cost": None}}
    save(root / "schedule.json", data)
    return {"schedule": str(root / "schedule.json"), "runs": len(jobs)}


def prepare_run(root, identifier, skill):
    root = Path(root).resolve()
    schedule = read(root / "schedule.json")
    job = next(item for item in schedule["jobs"] if item["id"] == identifier)
    directory = root / "runs" / identifier
    if directory.exists():
        raise ValueError("Run already exists; do not erase failed or interrupted attempts")
    source = read(Path(job["seed"]) / "source-fixture.json")
    workspace = directory / "workspace"
    shutil.copytree(source["workspace"], workspace)
    project = workspace / Path(source["project"]).name
    public = {"schema": "ReviewerBlindRequest/v1", "reviewer": job["reviewer"],
              "skill_dir": str(Path(skill).resolve()), "workspace": str(workspace), "project": str(project),
              "base": source["base"], "head": source["head"],
              "runtime_root": str(directory / "runtime"), "audit_dir": str(directory / "audit"),
              "trace_file": str(directory / "helper-trace.jsonl"),
              "instruction": "Read the frozen skill; conduct and publish the requested static review. No application tests or deployment. Archive authored evidence through the trace wrapper, then mark completion."}
    save(directory / "public-request.json", public)
    job["state"] = "prepared"
    save(root / "schedule.json", schedule)
    return public


def start_run(root, identifier):
    directory = Path(root).resolve() / "runs" / identifier
    if (directory / "started.json").exists():
        raise ValueError("Run already started; keep its original timer")
    request = read(directory / "public-request.json")
    save(directory / "started.json", {"started_at_unix": time.time(), "request_sha256":
                                      hashlib.sha256((directory / "public-request.json").read_bytes()).hexdigest()})
    return {"run": identifier, "request": str(directory / "public-request.json"), "reviewer": request["reviewer"]}


def complete_run(directory):
    directory = Path(directory).resolve()
    if (directory / "completion.json").exists():
        raise ValueError("Completion already recorded; do not replace timing")
    started = read(directory / "started.json")
    reports = sorted((directory / "audit" / "reports").glob("*.md"))
    trace_file = directory / "helper-trace.jsonl"
    traces = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines()] if trace_file.exists() else []
    published_file = directory / "audit" / "published.json"
    published = read(published_file) if published_file.is_file() else []
    edits = [item["source_report"] for item in published
             if not Path(item["source_report"]).is_file() or
             hashlib.sha256(Path(item["source_report"]).read_bytes()).hexdigest() != item["sha256"]]
    context_file = directory / "audit" / "last-context.json"
    context_removed = not context_file.is_file() or not Path(read(context_file)["context_dir"]).exists()
    git_operations, operation_seconds = Counter(), Counter()
    for trace in traces:
        git_operations.update(trace.get("git_operations", {}))
        operation_seconds.update(trace.get("git_operation_seconds", {}))
    finished = time.time()
    review_searches = [t for t in traces if not (t["module"] in ("review_report", "release_report")
                       or t["module"] == "review_session" and t.get("operation") == "finish")]
    logical_searches = sum(t.get("logical_search_requests", 0) for t in review_searches)
    eligible_searches = sum(t.get("eligible_search_requests", 0) for t in review_searches)
    unresolved_searches = sum(t.get("unresolved_search_requests", 0) for t in review_searches)
    computed_searches = sum(t.get("executed_search_queries", 0) for t in review_searches)
    publication_retries = max(0, sum(1 for t in traces if t["module"] in ("review_report", "release_report")
                  or t["module"] == "review_session" and t.get("operation") == "finish") - 1)
    result = {"schema": "ReviewerAgentCompletion/v3", "finished_at_unix": finished,
              "elapsed_seconds": finished - started["started_at_unix"], "reports": [str(p) for p in reports],
              "helper_calls": len(traces), "helper_seconds": sum(t["elapsed_seconds"] for t in traces),
              "observer_seconds": sum(t.get("observer_seconds", 0) for t in traces),
              "git_calls": sum(t["git_calls"] for t in traces),
              "git_operations": dict(git_operations),
              "git_operation_seconds": dict(operation_seconds),
              "full_result_validations": sum(t.get("full_result_validations", 0) for t in traces),
              "snapshot_repo_validations": sum(t.get("snapshot_repo_validations", 0) for t in traces),
              "logical_search_requests": logical_searches,
              "eligible_search_requests": eligible_searches,
              "unresolved_search_requests": unresolved_searches,
              "attempted_search_queries": sum(t.get("attempted_search_queries", 0) for t in review_searches),
              "executed_search_queries": computed_searches,
              "search_deduplication_ratio": (1 - computed_searches / eligible_searches) if eligible_searches else None,
              "search_metric_basis": "successful returned searches; unresolved rows excluded",
              "draft_repair_attempts": publication_retries,
              "publication_retry_count": publication_retries,
              "draft_edit_count": None,
              "output_bytes": sum(t["output_bytes"] + t.get("stderr_bytes", 0) for t in traces),
              "helper_nonzero_exits": [t["exit_code"] for t in traces if t["exit_code"] != 0],
              "post_publish_edits": edits, "context_removed": context_removed,
              "zero_git_checks": all(t["git_calls"] == 0 for t in traces
                                     if t["module"] == "review_session" and t.get("operation") == "check"),
              "artifact_view_git_calls": sum(t.get("artifact_view_git_calls", t["git_calls"]
                  if t["module"] in ("evidence", "review_workspace") and t.get("operation") == "view" else 0)
                  for t in traces),
              "output_metric_scope": "helper stdout and stderr bytes only",
              "report_bytes": sum(path.stat().st_size for path in reports),
              "model_analysis_seconds": None, "tokens": None, "cost": None}
    save(directory / "completion.json", result)
    return result


def summarize(root, output_name="acceptance-results.json"):
    root = Path(root).resolve()
    schedule = read(root / "schedule.json")
    rows = []
    for job in schedule["jobs"]:
        directory = root / "runs" / job["id"]
        row = {k: job[k] for k in ("id", "reviewer", "variant", "scenario", "repeat")}
        for filename, field in (("completion.json", "completion"), ("quality-audit.json", "quality")):
            if (directory / filename).exists():
                row[field] = read(directory / filename)
        rows.append(row)
    def valid_report(row):
        completion, quality = row.get("completion", {}), row.get("quality", {})
        checks = quality.get("mechanical_checks", {})
        required = ("single_report", "fixed_source_shas", "no_secret_sentinel", "no_post_publish_edits",
                    "published_semantic_outcome")
        return (quality.get("semantic", {}).get("passed") is True and len(completion.get("reports", [])) == 1
                and completion.get("full_result_validations", 0) >= 1
                and all(checks.get(key) is True for key in required))

    gates = {}
    for reviewer in ("merge", "release"):
        selected = [row for row in rows if row["reviewer"] == reviewer]
        ratios, full_gate_ratios, attempted_pairs, scenarios = [], [], [], {}
        for scenario in ("local", "mixed"):
            baseline, candidate = [], []
            for repeat in range(1, 4):
                pair = {row["variant"]: row for row in selected if row["scenario"] == scenario and row["repeat"] == repeat}
                if all("completion" in pair.get(v, {}) for v in ("baseline", "candidate")):
                    old, new = (pair[v]["completion"]["elapsed_seconds"] for v in ("baseline", "candidate"))
                    report_pair = all(valid_report(pair[v]) for v in ("baseline", "candidate"))
                    full_gate_pair = all(pair[v].get("quality", {}).get("passed") is True for v in ("baseline", "candidate"))
                    attempted_pairs.append({"scenario": scenario, "repeat": repeat, "ratio": new / old,
                                            "valid_report_pair": report_pair, "full_gate_pair": full_gate_pair})
                    if not report_pair:
                        continue
                    ratios.append(new / old)
                    if full_gate_pair:
                        full_gate_ratios.append(new / old)
                    baseline.append(old)
                    candidate.append(new)
            scenarios[scenario] = {"pairs": len(baseline),
                                   "baseline_median": statistics.median(baseline) if baseline else None,
                                   "candidate_median": statistics.median(candidate) if candidate else None,
                                   "non_regression_pass": len(baseline) == 3 and statistics.median(candidate) <= statistics.median(baseline) * 1.05}
        complete = sum("completion" in row for row in selected)
        candidates = [row for row in selected if row["variant"] == "candidate"]
        baselines = [row for row in selected if row["variant"] == "baseline"]
        quality = len(candidates) == 12 and all(row.get("quality", {}).get("passed") is True for row in candidates)
        gates[reviewer] = {"completed": complete, "expected": 24, "quality_pass": quality,
                           "candidate_quality_pass": quality,
                           "baseline_semantic_pass": len(baselines) == 12 and all(valid_report(row) for row in baselines),
                           "quality_gate_scope": "All candidate semantic and feature gates; baseline reports require correct semantics for each speed pair. Baseline workflow failures remain recorded.",
                           "successful_reports": sum(valid_report(row) for row in selected),
                           "attempted_large_pairs": attempted_pairs,
                           "full_gate_pair_count": len(full_gate_ratios),
                           "large_pair_count": len(ratios), "paired_ratio_median": statistics.median(ratios) if ratios else None,
                           "speed_pass": len(ratios) == 6 and statistics.median(ratios) <= .70,
                           "scenarios": scenarios,
                           "accepted": complete == 24 and quality and len(ratios) == 6 and statistics.median(ratios) <= .70
                                       and all(s["non_regression_pass"] for s in scenarios.values())}
    result = {"schema": "ReviewerAgentAcceptance/v2", "method": schedule["method"], "rows": rows,
              "gates": gates, "accepted": all(value["accepted"] for value in gates.values())}
    save(root / output_name, result)
    return {"result": str(root / output_name), "gates": gates}


def audit_run(root, identifier, semantic_file):
    """Combine parent-authored semantic scoring with mechanical publication checks."""
    root = Path(root).resolve()
    job = next(item for item in read(root / "schedule.json")["jobs"] if item["id"] == identifier)
    directory = root / "runs" / identifier
    completion = read(directory / "completion.json")
    semantic = read(semantic_file)
    if not isinstance(semantic.get("passed"), bool) or not isinstance(semantic.get("observations"), list) or not semantic["observations"]:
        raise ValueError("Parent semantic audit requires a boolean verdict and concrete observations")
    oracle_file = Path(job["seed"]) / "oracle.json"
    oracle = read(oracle_file)
    fixture_file = Path(job["seed"]) / "source-fixture.json"
    fixture = read(fixture_file)
    saved_manifests = sorted((directory / "audit" / "attempts").glob("*/manifest.json"))
    source_checks = []
    if saved_manifests:
        manifest = read(saved_manifests[-1])
        expected = fixture.get("repositories") or [{"name": Path(fixture["project"]).name,
                    "base_sha": fixture["base_sha"], "head_sha": fixture["head_sha"]}]
        captured = {Path(repo["path"]).name: repo for repo in manifest["repositories"]}
        for repo in expected:
            actual = captured.get(repo["name"], {})
            entry = repo["name"] == Path(fixture["project"]).name
            if job["reviewer"] == "merge":
                native = actual.get("native", {})
                matched = ((native.get("base_sha"), native.get("head_sha")) ==
                           (repo["base_sha"], repo["head_sha"])) if entry else actual.get("head_sha") == repo["head_sha"]
            else:
                matched = ((manifest.get("base_sha"), manifest.get("head_sha")) ==
                           (repo["base_sha"], repo["head_sha"])) if entry else actual.get("ref") == repo["head_sha"]
            source_checks.append({"repo": repo["name"], "matched": bool(matched)})
    reports = [Path(path) for path in completion["reports"]]
    # Fixed-source evidence is authorized input; this oracle checks publication.
    outward_files = reports
    leak_locations = []
    for index, sentinel in enumerate(oracle.get("secret_sentinels", []), 1):
        exposed = [str(path.relative_to(directory)) for path in outward_files
                   if sentinel in path.read_text(encoding="utf-8")]
        if exposed:
            leak_locations.append({"sentinel": f"sentinel-{index}", "files": exposed})
    leaks = [item["sentinel"] for item in leak_locations]
    request_file = directory / "public-request.json"
    runtime = Path(read(request_file)["runtime_root"]) if request_file.exists() else directory / "runtime"
    outcomes = [read(path).get("review_outcome") for path in runtime.glob("**/jobs/*.json")]
    outcome = next((item for item in outcomes if isinstance(item, dict)), None)
    expected_complete = job["scenario"] != "gap"
    outcome_schema = {"merge": "MergeReviewOutcome/v1", "release": "ReleaseReviewOutcome/v1"}[job["reviewer"]]
    outcome_valid = (isinstance(outcome, dict) and outcome.get("schema") == outcome_schema
                     and type(outcome.get("review_complete")) is bool
                     and outcome["review_complete"] == expected_complete
                     and isinstance(outcome.get("gaps"), list)
                     and bool(outcome["gaps"]) == (not expected_complete))
    checks = {"single_report": len(reports) == 1, "context_removed": completion["context_removed"],
              "no_post_publish_edits": not completion["post_publish_edits"], "no_secret_sentinel": not leaks,
              "check_zero_git": completion["zero_git_checks"],
              "single_full_result_validation": completion["full_result_validations"] == 1,
              "fixed_source_shas": bool(source_checks) and all(item["matched"] for item in source_checks),
              "published_semantic_outcome": job["variant"] != "candidate" or outcome_valid,
              "artifact_view_zero_git": job["variant"] != "candidate" or completion.get("artifact_view_git_calls", 0) == 0,
              "no_network": not any(completion["git_operations"].get(operation, 0)
                                    for operation in ("fetch", "pull", "clone", "ls-remote", "push"))}
    result = {"schema": "ReviewerParentQualityAudit/v2", "reviewer": job["reviewer"],
              "variant": job["variant"], "scenario": job["scenario"], "semantic": semantic,
              "mechanical_checks": checks, "sentinel_leaks": leaks, "sentinel_leak_locations": leak_locations,
              "sentinel_check_scope": "published rendered reports; authorized fixed-source input excluded",
              "oracle_sha256": hashlib.sha256(oracle_file.read_bytes()).hexdigest(),
              "source_fixture_sha256": hashlib.sha256(fixture_file.read_bytes()).hexdigest(),
              "source_checks": source_checks,
              "published_review_outcome": outcome,
              "passed": semantic["passed"] and all(checks.values())}
    save(directory / "quality-audit.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--root", type=Path, required=True)
    prepare_parser.add_argument("--files-per-repo", type=int, default=1000)
    for command in ("prepare-run", "start", "summarize", "audit"):
        child = commands.add_parser(command)
        child.add_argument("--root", type=Path, required=True)
        if command != "summarize":
            child.add_argument("--id", required=True)
        if command == "prepare-run":
            child.add_argument("--skill-dir", type=Path, required=True)
        if command == "audit":
            child.add_argument("--semantic-file", type=Path, required=True)
    complete = commands.add_parser("complete")
    complete.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.root, args.files_per_repo)
    elif args.command == "prepare-run":
        result = prepare_run(args.root, args.id, args.skill_dir)
    elif args.command == "start":
        result = start_run(args.root, args.id)
    elif args.command == "complete":
        result = complete_run(args.run_dir)
    elif args.command == "audit":
        result = audit_run(args.root, args.id, args.semantic_file)
    else:
        result = summarize(args.root)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
